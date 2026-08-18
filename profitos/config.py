import os
from pathlib import Path

APP_VERSION = "1.2"

class BaseConfig:
    SECRET_KEY = os.environ.get('PROFITOS_SECRET_KEY') or 'dev-only-change-me'
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_MB', '25')) * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    JSON_SORT_KEYS = False
    APP_VERSION = APP_VERSION
    DATABASE_URL = os.environ.get('DATABASE_URL')
    APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://127.0.0.1:5050')

class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False

class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = 'https'

def get_config():
    env = os.environ.get('PROFITOS_ENV', 'development').lower()
    return ProductionConfig if env == 'production' else DevelopmentConfig
