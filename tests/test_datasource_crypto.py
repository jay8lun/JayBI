"""
DatasourceConfigUtil 加解密测试（P0-S5 修复的安全网）
- v2 格式：enc:v2: 前缀 + AES-CBC + 随机 IV
- 旧格式（AES-ECB + 遗留密钥）存量密文兼容解密
- 密钥长度 fail-fast / 缺 pycryptodome fail-fast / 禁止 base64 明文降级

在 aix-db 容器内运行：docker exec aix-db python -m pytest /app/tests/test_datasource_crypto.py -v
"""

import base64
import json
import os
import sys
import types

import pytest

pytest.importorskip("Crypto.Cipher.AES", reason="pycryptodome 未安装")

# ---- 给 datasource_util 的无关重量级依赖打桩，避免测试环境装全套驱动 ----
# 只隔离 import 链，被测的 DatasourceConfigUtil 逻辑是真实代码。
class _StubModule(types.ModuleType):
    """属性宽容的桩模块：任何缺失属性都返回占位类。"""

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        placeholder = type(item, (object,), {})
        setattr(self, item, placeholder)
        return placeholder


_STUBS = ["pymysql", "psycopg2", "requests", "elasticsearch", "sqlalchemy",
          "sqlalchemy.exc", "dmPython", "redshift_connector", "clickhouse_driver",
          "neo4j", "ray", "kafka"]
for _name in _STUBS:
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            sys.modules[_name] = _StubModule(_name)
_sa = sys.modules["sqlalchemy"]
if not hasattr(_sa, "create_engine"):
    _sa.create_engine = lambda *a, **k: None
if not hasattr(_sa, "text"):
    _sa.text = lambda *a, **k: None
if not hasattr(_sa, "inspect"):
    _sa.inspect = lambda *a, **k: None

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from common.datasource_util import DatasourceConfigUtil

LEGACY_KEY = b"AixDB12345678901"
GOOD_KEY_32 = "K" * 32  # 合法长度：AES-256


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATASOURCE_ENC_KEY", GOOD_KEY_32)


def test_v2_roundtrip():
    cfg = {"host": "db.example.com", "port": 5432, "password": "p@ss w0rd"}
    enc = DatasourceConfigUtil.encrypt_config(cfg)
    assert enc.startswith(DatasourceConfigUtil.V2_PREFIX)
    assert DatasourceConfigUtil.decrypt_config(enc) == cfg


def test_v2_random_iv_same_plaintext_diff_ciphertext():
    a = DatasourceConfigUtil.encrypt_config({"x": 1})
    b = DatasourceConfigUtil.encrypt_config({"x": 1})
    assert a != b, "CBC 随机 IV 下同明文密文必须不同"


def test_legacy_ecb_still_decryptable():
    """存量旧格式密文（无前缀 ECB）必须能解密，保证平滑迁移。"""
    raw = pad(json.dumps({"a": 1, "b": "中文"}).encode("utf-8"), 16)
    legacy = base64.b64encode(AES.new(LEGACY_KEY, AES.MODE_ECB).encrypt(raw)).decode()
    assert DatasourceConfigUtil.decrypt_config(legacy) == {"a": 1, "b": "中文"}


def test_bad_key_length_fails_fast(monkeypatch):
    monkeypatch.setenv("DATASOURCE_ENC_KEY", "short-key")
    with pytest.raises(RuntimeError):
        DatasourceConfigUtil.encrypt_config({"a": 1})


def test_missing_crypto_lib_fails_fast(monkeypatch):
    """pycryptodome 缺失时必须抛 ImportError，绝不降级为 base64 明文。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("Crypto"):
            raise ImportError("No module named 'Crypto'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError):
        DatasourceConfigUtil.encrypt_config({"password": "secret"})
    # 旧格式路径同样 fail-fast（修复前会静默 base64 解码成"明文配置"）
    legacy_looking = base64.b64encode(json.dumps({"password": "secret"}).encode()).decode()
    with pytest.raises(ImportError):
        DatasourceConfigUtil.decrypt_config(legacy_looking)
