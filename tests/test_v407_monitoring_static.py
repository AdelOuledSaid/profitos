"""Sentry optionnel, journal d'accès sans jetons, cache des assets statiques."""
import gzip
import importlib.util
from pathlib import Path

from profitos import create_app, _sentry_before_send, _scrub_pii, _init_sentry
from profitos.config import DevelopmentConfig

ROOT = Path(__file__).resolve().parents[1]


def _client():
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app, app.test_client()


def test_sentry_scrubs_emails_cookies_and_query_strings():
    event = {
        'message': 'Resend email failed for alice@exemple.fr',
        'user': {'email': 'alice@exemple.fr'},
        'request': {'cookies': {'session': 'x'}, 'data': {'password': 'p'}, 'query_string': 'token=abc',
                    'headers': {'Authorization': 'Bearer k', 'User-Agent': 'ua'}},
        'extra': {'to': ['bob@cabinet.fr']},
    }
    out = _sentry_before_send(event)
    assert 'alice@exemple.fr' not in str(out) and 'a***@exemple.fr' in out['message']
    assert 'user' not in out and 'cookies' not in out['request'] and 'data' not in out['request']
    assert out['request']['query_string'] == '[filtré]'
    assert out['request']['headers']['Authorization'] == '[filtré]'
    assert out['extra']['to'] == ['b***@cabinet.fr']
    assert _scrub_pii(42) == 42


def test_sentry_is_optional(monkeypatch):
    app, _ = _client()
    monkeypatch.delenv('SENTRY_DSN', raising=False)
    assert _init_sentry(app) is False
    monkeypatch.setenv('SENTRY_DSN', 'https://k@o0.ingest.sentry.io/1')
    app.testing = False
    try:
        import sentry_sdk  # noqa: F401
    except ImportError:
        assert _init_sentry(app) is False  # non installé : avertissement, pas de crash


def test_gunicorn_access_log_scrubs_tokens_and_query_strings():
    spec = importlib.util.spec_from_file_location('gconf', ROOT / 'gunicorn.conf.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.scrub_path('/reset-password/Zx8fK2pQ9vLmN3rT7wYb1cDe?next=/x') == '/reset-password/[jeton]'
    assert mod.scrub_path('/facture/abcdefghijklmnopqrstuvwxyz012345/pdf') == '/facture/[jeton]/pdf'
    assert mod.scrub_path('/recover?token=secret') == '/recover'
    assert '%(q)s' not in mod.access_log_format and '%(r)s' not in mod.access_log_format
    assert '%(U)s' in mod.access_log_format and '%(f)s' not in mod.access_log_format


def test_versioned_static_assets_are_cached_for_a_year():
    app, client = _client()
    r = client.get('/static/style.css?v=123')
    assert r.status_code == 200
    assert r.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    r2 = client.get('/static/style.css')
    assert 'immutable' not in r2.headers.get('Cache-Control', '')
    r3 = client.get('/static/style.css?v=0')  # asset introuvable au calcul de version
    assert 'immutable' not in r3.headers.get('Cache-Control', '')


def test_static_text_assets_are_gzipped():
    app, client = _client()
    r = client.get('/static/style.css?v=1', headers={'Accept-Encoding': 'gzip, br'})
    assert r.headers.get('Content-Encoding') == 'gzip'
    assert 'Accept-Encoding' in r.headers.get('Vary', '')
    original = (ROOT / 'static' / 'style.css').read_bytes()
    assert gzip.decompress(r.data) == original
    assert int(r.headers['Content-Length']) < len(original)
    img = client.get('/static/hero-skyline.jpg?v=1', headers={'Accept-Encoding': 'gzip'})
    assert 'Content-Encoding' not in img.headers
    plain = client.get('/static/style.css?v=1')
    assert 'Content-Encoding' not in plain.headers and plain.data == original
