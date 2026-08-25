import os
from pathlib import Path
from datetime import timedelta

APP_VERSION = "1.7.5"


class BaseConfig:
    SECRET_KEY = os.environ.get('PROFITOS_SECRET_KEY') or 'dev-only-change-me'
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_MB', '15')) * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_REFRESH_EACH_REQUEST = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get('SESSION_HOURS', '12')))
    JSON_SORT_KEYS = False
    APP_VERSION = APP_VERSION
    DATABASE_URL = os.environ.get('DATABASE_URL')
    APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://127.0.0.1:5050')
    MAX_FORM_MEMORY_SIZE = 2 * 1024 * 1024
    # Flask-Limiter: Redis/Valkey en production, mémoire uniquement en local.
    RATELIMIT_STORAGE_URI = os.environ.get('REDIS_URL', 'memory://')
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_HEADER_RETRY_AFTER_VALUE = 'delta-seconds'
    # Si Key Value est temporairement indisponible, conserver une protection locale
    # plutôt que de faire tomber l'application.
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True
    RATELIMIT_SWALLOW_ERRORS = False
    RATELIMIT_KEY_PREFIX = 'profitos-v141'


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_NAME = 'profitos_session'


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_NAME = '__Host-profitos_session'
    PREFERRED_URL_SCHEME = 'https'


def get_config():
    env = os.environ.get('PROFITOS_ENV', 'development').lower()
    return ProductionConfig if env == 'production' else DevelopmentConfig
