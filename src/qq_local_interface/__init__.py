"""Local QQ snapshots and stable interfaces. No application writes or sending."""

SCHEMA_VERSION = "qq.local.v1"
PROTOCOL_VERSION = "qq.local.protocol.v1"


def __getattr__(name):
    if name == "QQSource":
        from .source import QQSource
        return QQSource
    if name == "QQVault":
        from .vault import QQVault
        return QQVault
    raise AttributeError(name)


__all__ = ["QQSource", "QQVault", "SCHEMA_VERSION", "PROTOCOL_VERSION"]
