import os


def test_rate_limit_config_uses_redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://example:6379")
    import importlib
    import profitos.config as cfg
    importlib.reload(cfg)
    assert cfg.BaseConfig.RATELIMIT_STORAGE_URI == "redis://example:6379"
    assert cfg.BaseConfig.RATELIMIT_HEADERS_ENABLED is True
    assert cfg.BaseConfig.RATELIMIT_IN_MEMORY_FALLBACK_ENABLED is True


def test_rate_limit_string():
    from profitos.runtime import _seconds_limit_string
    assert _seconds_limit_string(8, 300) == "8 per 300 seconds"
