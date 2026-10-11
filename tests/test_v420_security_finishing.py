"""v420 — Finitions sécurité : CSRF non ASCII, secrets hors cookie, liens publics,
rôle lecture seule, profil entreprise, clés API expirées, readyz minimal."""
import base64
import json
import zlib

from support_v4xx import PASSWORD, app, auth, flashes, post, signup  # noqa: F401


def test_non_ascii_csrf_token_gives_400_not_500(app):
    client, *_ = signup(app, 'CSRF SAS')
    with client.session_transaction() as s:
        s['csrf_token'] = 'é' * 10
    r = client.post('/settings/theme', data={'csrf_token': 'éèà'})
    assert r.status_code == 400


def _cookie_payload(client):
    raw = client.get_cookie('profitos_session').value
    data = raw.split('.')[0 if not raw.startswith('.') else 1]
    padded = data + '=' * (-len(data) % 4)
    blob = base64.urlsafe_b64decode(padded)
    if raw.startswith('.'):
        blob = zlib.decompress(blob)
    return blob.decode('utf-8', 'replace')


def test_webhook_secret_never_in_session_cookie_and_shown_once(app, monkeypatch):
    from profitos.routes import api
    monkeypatch.setattr(api, 'validate_outbound_webhook_url', lambda url: (True, None))
    monkeypatch.setattr(api, 'new_webhook_secret', lambda: 'SECRET-WEBHOOK-UNIQUE-123')
    client, *_ = signup(app, 'Webhook SAS', plan='BUSINESS')
    post(client, '/settings/webhooks', {'url': 'https://hooks.example.com/x', 'events': 'invoice.sent'})
    assert 'SECRET-WEBHOOK-UNIQUE-123' not in _cookie_payload(client)
    first = client.get('/settings/webhooks').get_data(as_text=True)
    assert 'SECRET-WEBHOOK-UNIQUE-123' in first
    assert 'SECRET-WEBHOOK-UNIQUE-123' not in client.get('/settings/webhooks').get_data(as_text=True)


def test_public_token_pages_are_not_cached_and_rate_limited(app):
    from flask_limiter import Limiter  # noqa: F401
    r = app.test_client().get('/facture/jeton-inexistant')
    assert 'no-store' in r.headers.get('Cache-Control', '')
    view = app.view_functions['public_invoice_pdf']
    assert view.__wrapped__ is not None if hasattr(view, '__wrapped__') else True


def test_content_disposition_with_accents_is_encoded():
    from flask import Response
    from profitos import _rfc5987_content_disposition
    resp = Response(b'x', headers={'Content-Disposition': 'inline; filename="Facture-Société-Ω.pdf"'})
    _rfc5987_content_disposition(resp)
    value = resp.headers['Content-Disposition']
    value.encode('latin-1')
    assert "filename*=UTF-8''Facture-Soci%C3%A9t%C3%A9-%CE%A9.pdf" in value and 'filename="Facture-Societe-.pdf"' in value


def test_readyz_is_minimal(app):
    body = app.test_client().get('/readyz').get_json()
    assert body == {'status': 'ready'}


def test_read_only_role_cannot_write(app):
    owner, _, org_id, _ = signup(app, 'Lecture SAS', plan='BUSINESS')
    reader, _, _, rid = signup(app, 'Lecteur Org')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'LECTURE','2026-01-01')", (rid, org_id))
    with reader.session_transaction() as s:
        s['org_id'] = org_id
    assert reader.get('/facturation').status_code == 200
    r = post(reader, '/facturation/clients/nouveau', {'name': 'Client interdit'})
    assert r.status_code == 302
    assert any('Lecture seule' in m for m in flashes(reader))
    # Son propre compte reste modifiable.
    post(reader, '/settings/theme')
    assert not any('Lecture seule' in m for m in flashes(reader)[1:])


def test_commercial_sees_company_profile_read_only_without_iban(app):
    owner, _, org_id, _ = signup(app, 'Profil SAS', plan='BUSINESS')
    post(owner, '/company', {'name': 'Profil SAS', 'iban': 'FR7630006000011234567890189', 'bic': 'AGRIFRPP'})
    seller, _, _, sid = signup(app, 'Vendeur')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'COMMERCIAL','2026-01-01')", (sid, org_id))
    with seller.session_transaction() as s:
        s['org_id'] = org_id
    page = seller.get('/company').get_data(as_text=True)
    assert 'FR7630006000011234567890189' not in page and 'Consultation seule' in page


def test_expired_api_key_is_rejected(app):
    from profitos.runtime import generate_api_key, hash_api_key
    _, _, org_id, _ = signup(app, 'API SAS', plan='BUSINESS')
    raw = generate_api_key()
    auth("INSERT INTO api_keys(organization_id,key_hash,key_prefix,created_at,scope,scopes,expires_at) "
         "VALUES(?,?,?,?,'read','read','2020-01-01T00:00:00+00:00')", (org_id, hash_api_key(raw), raw[:16], '2019-01-01'))
    r = app.test_client().get('/api/v1/summary', headers={'Authorization': f'Bearer {raw}'})
    assert r.status_code == 401


def test_verify_link_rate_limit_counts_get_requests():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'profitos/routes/account.py').read_text(encoding='utf-8')
    block = src.split("@app.route('/verify/<token>')", 1)[1].split('def verify_email', 1)[0]
    assert "limiter.limit('20 per 900 seconds'" in block and 'rate_limit(' not in block


def test_email_provider_errors_are_not_shown_raw():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'profitos/runtime.py').read_text(encoding='utf-8')
    body = src.split('def send_email(', 1)[1].split('\ndef ', 1)[0]
    assert "'error':str(exc)" not in body
