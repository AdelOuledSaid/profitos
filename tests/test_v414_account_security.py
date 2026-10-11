"""v414 — Sécurité des comptes : politique de mot de passe, verrouillage par compte,
temps constant, 2FA TOTP, sessions par appareil, inactivité, ré-authentification,
changement de mot de passe / d'e-mail, inscription sur une adresse existante."""
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from support_v4xx import PASSWORD, app, auth, flashes, login, post, signup  # noqa: F401


# ------------------------------------------------------------------ mot de passe
@pytest.mark.parametrize('pw,email,ok', [
    ('court1', None, False),
    ('password1234', None, False),          # liste des plus courants
    ('azertyuiop123', None, False),
    ('aaaaaaaaaaaa', None, False),           # trop répétitif
    ('alice.martin-2026!', 'alice.martin@example.com', False),  # contient l'e-mail
    ('Brume-Cactus-Velo-48', 'alice@example.com', True),
])
def test_password_policy(pw, email, ok):
    from profitos.runtime import password_error
    assert (password_error(pw, email) is None) is ok


def test_common_password_list_is_shipped():
    from profitos.account_security import common_passwords
    assert len(common_passwords()) >= 1000


# ------------------------------------------------------------------ verrouillage / temps constant
def test_account_lock_after_ten_failures_even_with_right_password(app, monkeypatch):
    from profitos import account_security as acsec
    notices = []
    monkeypatch.setattr(acsec, 'notify_user', lambda user, title, *a, **k: notices.append(title))
    _, email, _, uid = signup(app, 'Verrou SAS')
    for _ in range(10):
        login(app, email, 'mauvais-mot-de-passe')
    assert auth('SELECT locked_until FROM users WHERE id=?', (uid,), 'one')['locked_until']
    client, r = login(app, email, PASSWORD)
    with client.session_transaction() as s:
        assert 'user_id' not in s
    assert any('temporairement verrouillé' in t for t in notices)
    # Après expiration du verrou, la connexion fonctionne et remet le compteur à zéro.
    auth('UPDATE users SET locked_until=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), uid))
    client, r = login(app, email, PASSWORD)
    with client.session_transaction() as s:
        assert s.get('user_id') == uid


def test_unknown_email_still_computes_a_password_hash(app, monkeypatch):
    from profitos import account_security as acsec
    calls = []
    real = acsec.dummy_password_check
    monkeypatch.setattr(acsec, 'dummy_password_check', lambda pw: calls.append(pw) or real(pw))
    login(app, 'personne-inconnue@example.com', 'peu-importe-123')
    assert calls == ['peu-importe-123']


# ------------------------------------------------------------------ TOTP
def test_totp_matches_rfc6238_vector():
    from profitos import totp
    secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # "12345678901234567890"
    assert totp.totp(secret, at=59) == '287082'
    assert totp.totp(secret, at=1111111109) == '081804'
    assert totp.verify(secret, '287082', at=59) is not None
    assert totp.verify(secret, '287082', at=59, last_step=totp.current_step(59)) is None  # anti-rejeu
    assert totp.provisioning_uri(secret, 'a@b.fr').startswith('otpauth://totp/ProfitOS%3Aa%40b.fr?secret=')


def _enable_2fa(client):
    from profitos import totp
    page = client.get('/compte/2fa').get_data(as_text=True)
    secret = re.search(r'secret=([A-Z2-7]+)', page).group(1)
    r = post(client, '/compte/2fa', {'code': totp.totp(secret)})
    codes = re.findall(r'<li>([a-z0-9]{5}-[a-z0-9]{5})</li>', r.get_data(as_text=True))
    return secret, codes


def test_two_factor_login_flow_and_single_use_recovery_codes(app):
    from profitos import totp
    client, email, _, uid = signup(app, 'Deux Facteurs')
    secret, codes = _enable_2fa(client)
    assert len(codes) == 10
    stored = auth('SELECT totp_secret,totp_enabled,totp_recovery_codes FROM users WHERE id=?', (uid,), 'one')
    assert stored['totp_enabled'] == 1 and stored['totp_secret'].startswith('enc:v1:')
    assert codes[0] not in stored['totp_recovery_codes']

    c2, r = login(app, email)
    assert '/login/2fa' in r.headers['Location']
    with c2.session_transaction() as s:
        assert 'user_id' not in s
    post(c2, '/login/2fa', {'code': '000000'})
    with c2.session_transaction() as s:
        assert 'user_id' not in s
    post(c2, '/login/2fa', {'code': codes[0]})
    with c2.session_transaction() as s:
        assert s['user_id'] == uid
    c3, _ = login(app, email)
    post(c3, '/login/2fa', {'code': codes[0]})  # déjà utilisé
    with c3.session_transaction() as s:
        assert 'user_id' not in s


def test_owner_can_require_two_factor_for_members(app):
    owner, _, org_id, _ = signup(app, 'Org 2FA', plan='BUSINESS')
    _enable_2fa(owner)
    post(owner, '/organisation/2fa-obligatoire', {'require': '1'})
    assert auth('SELECT require_2fa FROM organizations WHERE id=?', (org_id,), 'one')['require_2fa'] == 1
    member, email, _, mid = signup(app, 'Autre')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'MEMBER','2026-01-01')", (mid, org_id))
    with member.session_transaction() as s:
        s['org_id'] = org_id
    r = member.get('/facturation')
    assert r.status_code == 302 and '/compte/2fa' in r.headers['Location']


# ------------------------------------------------------------------ sessions
def test_logout_closes_only_this_device(app):
    c1, email, _, uid = signup(app, 'Deux Appareils')
    c2, _ = login(app, email)
    post(c1, '/logout')
    assert c2.get('/compte/securite').status_code == 200
    # Le cookie de c1, rejoué après déconnexion, n'est plus valable.
    assert auth('SELECT COUNT(*) n FROM user_sessions WHERE user_id=? AND revoked_at IS NOT NULL', (uid,), 'one')['n'] == 1


def test_idle_timeout(app):
    client, *_ = signup(app, 'Inactif')
    with client.session_transaction() as s:
        s['last_seen'] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    r = client.get('/compte/securite')
    assert r.status_code == 302 and '/login' in r.headers['Location']
    with client.session_transaction() as s:
        assert 'user_id' not in s


def test_password_change_keeps_current_session_and_closes_others(app, monkeypatch):
    from profitos import account_security as acsec
    notices = []
    monkeypatch.setattr(acsec, 'notify_user', lambda user, title, *a, **k: notices.append(title))
    c1, email, _, uid = signup(app, 'Change MDP')
    c2, _ = login(app, email)
    new = 'Nouvelle-Phrase-Secrete-77'
    post(c1, '/compte/mot-de-passe', {'current_password': 'faux', 'new_password': new, 'new_password_confirm': new})
    assert auth('SELECT password_changed_at FROM users WHERE id=?', (uid,), 'one')['password_changed_at'] is None
    post(c1, '/compte/mot-de-passe', {'current_password': PASSWORD, 'new_password': new, 'new_password_confirm': new})
    assert c1.get('/compte/securite').status_code == 200
    assert c2.get('/compte/securite').status_code == 302
    assert 'Mot de passe modifié' in notices


def test_new_device_login_sends_notice(app, monkeypatch):
    from profitos import account_security as acsec
    notices = []
    monkeypatch.setattr(acsec, 'notify_user', lambda user, title, *a, **k: notices.append(title))
    _, email, _, _ = signup(app, 'Appareils')
    login(app, email)  # nouveau client = nouvel appareil
    assert 'Nouvelle connexion à votre compte' in notices


def test_email_change_requires_confirmation_from_new_address(app, monkeypatch):
    from profitos import runtime as rt
    from profitos.routes import security
    sent = []
    monkeypatch.setattr(security, 'send_email', lambda to, subject, html, **kw: sent.append((to, html)) or {'sent': True})
    monkeypatch.setattr(security.acsec, 'notify_user', lambda *a, **k: None)
    client, email, _, uid = signup(app, 'Change Mail')
    import uuid
    new_email = f'{uuid.uuid4().hex[:8]}@example.com'
    post(client, '/compte/email', {'new_email': new_email, 'current_password': PASSWORD})
    assert auth('SELECT email FROM users WHERE id=?', (uid,), 'one')['email'] == email  # pas encore changé
    to, html = sent[0]
    assert to == new_email
    path = re.search(r'(/compte/email/confirmer/[A-Za-z0-9_\-]+)', html).group(1)
    client.get(path)
    row = auth('SELECT email,email_verified FROM users WHERE id=?', (uid,), 'one')
    assert row['email'] == new_email and row['email_verified'] == 1


# ------------------------------------------------------------------ ré-authentification
def test_destructive_actions_require_current_password(app):
    owner, _, org_id, uid = signup(app, 'Réauth SAS', plan='BUSINESS')
    _, _, _, mid = signup(app, 'Membre')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'MEMBER','2026-01-01')", (mid, org_id))
    post(owner, f'/team/{mid}/remove')
    assert auth('SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?', (mid, org_id), 'one')
    post(owner, f'/team/{mid}/remove', {'current_password': PASSWORD})
    assert auth('SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?', (mid, org_id), 'one') is None

    post(owner, '/settings/api-keys', {'action': 'create', 'scopes': 'read'})
    assert auth('SELECT COUNT(*) n FROM api_keys WHERE organization_id=?', (org_id,), 'one')['n'] == 0
    post(owner, '/settings/api-keys', {'action': 'create', 'scopes': 'read', 'current_password': PASSWORD})
    assert auth('SELECT COUNT(*) n FROM api_keys WHERE organization_id=?', (org_id,), 'one')['n'] == 1

    post(owner, '/settings/delete-account', {'confirm_text': 'Réauth SAS', 'current_password': 'faux'})
    assert auth('SELECT deleted_at FROM organizations WHERE id=?', (org_id,), 'one')['deleted_at'] is None


# ------------------------------------------------------------------ inscription
def test_signup_with_existing_email_is_generic_and_notifies_owner(app, monkeypatch):
    from profitos import account_security as acsec
    notices = []
    monkeypatch.setattr(acsec, 'notify_user', lambda user, title, *a, **k: notices.append((user['email'], title)))
    _, email, _, _ = signup(app, 'Premier')
    client = app.test_client()
    post(client, '/signup', {'accept_terms': '1', 'full_name': 'X', 'email': email, 'password': PASSWORD, 'company_name': 'Second'})
    msgs = ' '.join(flashes(client))
    assert 'existe déjà' not in msgs
    assert (email, 'Tentative de création de compte') in notices
