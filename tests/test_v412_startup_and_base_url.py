"""v412 — Clé secrète obligatoire, APP_BASE_URL https en production, liens jamais
construits depuis l'en-tête Host."""
import re

import pytest

from support_v4xx import app, auth, post, signup  # noqa: F401


@pytest.mark.parametrize('env', ['', 'production', 'prod', 'staging'])
def test_startup_refused_without_secret_outside_dev(monkeypatch, env):
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    if env:
        monkeypatch.setenv('PROFITOS_ENV', env)
    else:
        monkeypatch.delenv('PROFITOS_ENV', raising=False)
    monkeypatch.delenv('PROFITOS_SECRET_KEY', raising=False)
    monkeypatch.setattr(DevelopmentConfig, 'SECRET_KEY', None)
    with pytest.raises(RuntimeError):
        create_app(DevelopmentConfig)


@pytest.mark.parametrize('env', ['development', 'test'])
def test_dev_uses_random_process_key_not_known_default(monkeypatch, env):
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    monkeypatch.setenv('PROFITOS_ENV', env)
    monkeypatch.setattr(DevelopmentConfig, 'SECRET_KEY', None)
    a = create_app(DevelopmentConfig)
    assert a.config['SECRET_KEY'] and a.config['SECRET_KEY'] != 'dev-only-change-me'
    assert len(a.config['SECRET_KEY']) >= 32


def test_unknown_env_is_treated_as_production(monkeypatch):
    from profitos.config import is_production, runtime_env
    monkeypatch.setenv('PROFITOS_ENV', 'Prod')
    assert is_production()
    monkeypatch.delenv('PROFITOS_ENV')
    assert runtime_env() == 'production'
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    assert not is_production()


@pytest.mark.parametrize('base', ['', 'http://app.example.fr'])
def test_production_requires_https_app_base_url(monkeypatch, base):
    from profitos import create_app
    from profitos.config import ProductionConfig
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', 'Zq8w-' + 'k3Lm9Pq2Rt7Vx4Yz1Ab6Cd0Ef5Gh8Ij3')
    if base:
        monkeypatch.setenv('APP_BASE_URL', base)
    else:
        monkeypatch.delenv('APP_BASE_URL', raising=False)
    with pytest.raises(RuntimeError, match='APP_BASE_URL'):
        create_app(ProductionConfig)


def test_app_base_url_never_uses_host_in_production(app, monkeypatch):
    from profitos.runtime import app_base_url
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.delenv('APP_BASE_URL', raising=False)
    with app.test_request_context('/', headers={'Host': 'evil.example'}):
        with pytest.raises(RuntimeError):
            app_base_url()
    monkeypatch.setenv('APP_BASE_URL', 'https://app.profitos.fr')
    with app.test_request_context('/', headers={'Host': 'evil.example'}):
        assert app_base_url() == 'https://app.profitos.fr'


def test_reset_link_uses_configured_base_url_not_host(app, monkeypatch):
    from profitos import runtime as rt
    _, email, _, uid = signup(app, 'Reset SAS')
    monkeypatch.setenv('APP_BASE_URL', 'https://app.profitos.fr')
    captured = []
    monkeypatch.setattr(rt, 'send_email', lambda to, subject, html, **kw: captured.append(html) or {'sent': True})
    user = auth('SELECT * FROM users WHERE id=?', (uid,), 'one')
    with app.test_request_context('/forgot-password', method='POST', headers={'Host': 'evil.example'}):
        rt.send_reset_email(user)
    assert captured and 'https://app.profitos.fr/reset-password/' in captured[0]
    assert 'evil.example' not in captured[0]


def test_host_derived_call_sites_removed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ('profitos/routes/account.py', 'profitos/routes/accounting.py', 'profitos/routes/invoicing.py',
                'profitos/routes/cloud_storage.py', 'profitos/routes/gocardless_baas.py'):
        assert "request.host_url" not in (root / rel).read_text(encoding='utf-8'), rel
