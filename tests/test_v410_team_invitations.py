"""v410 — Invitations d'équipe : plus de prise de contrôle d'un compte existant.

Avant : inviter l'adresse d'un compte non vérifié remplaçait son mot de passe,
incrémentait auth_version et créait une adhésion sans consentement.
"""
import re
import uuid

from support_v4xx import PASSWORD, app, auth, flashes, login, post, signup  # noqa: F401


def _capture_invites(monkeypatch):
    from profitos.routes import account
    sent = []

    def fake_send(to, subject, html, dry_run=None, reply_to=None, kind=None):
        sent.append({'to': to, 'subject': subject, 'html': html, 'kind': kind})
        return {'sent': True}
    monkeypatch.setattr(account, 'send_email', fake_send)
    return sent


def _token(mail):
    return re.search(r'/invitation/([A-Za-z0-9_\-]+)', mail['html']).group(1)


def test_inviting_existing_unverified_account_never_touches_it(app, monkeypatch):
    sent = _capture_invites(monkeypatch)
    owner, _, org_id, _ = signup(app, 'Inviteur SAS', verified=True, plan='BUSINESS')
    victim_client, victim_email, victim_org, victim_id = signup(app, 'Victime SARL')  # non vérifié
    before = auth('SELECT password_hash,auth_version FROM users WHERE id=?', (victim_id,), 'one')

    r = post(owner, '/team/invite', {'email': victim_email, 'role': 'ADMIN'})
    assert r.status_code == 302
    after = auth('SELECT password_hash,auth_version FROM users WHERE id=?', (victim_id,), 'one')
    assert after['password_hash'] == before['password_hash']
    assert after['auth_version'] == before['auth_version']
    assert auth('SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?', (victim_id, org_id), 'one') is None
    # La session de la victime reste valide.
    assert victim_client.get('/compte/securite').status_code == 200
    # Objet fixe (pas le nom d'organisation choisi par l'expéditeur), lien d'acceptation.
    assert sent and sent[0]['subject'] == 'Invitation à rejoindre une organisation sur ProfitOS'
    assert '/invitation/' in sent[0]['html'] and '/reset-password/' not in sent[0]['html']


def test_existing_user_must_be_logged_in_as_invited_email_and_verified(app, monkeypatch):
    sent = _capture_invites(monkeypatch)
    owner, _, org_id, _ = signup(app, 'Org Hôte', verified=True, plan='BUSINESS')
    guest, guest_email, _, guest_id = signup(app, 'Org Invitée')
    post(owner, '/team/invite', {'email': guest_email, 'role': 'COMPTABLE'})
    token = _token(sent[0])

    # Un tiers connecté ne peut pas accepter.
    intruder, *_ = signup(app, 'Intrus', verified=True)
    assert post(intruder, f'/invitation/{token}', {'action': 'accept'}).status_code == 403
    # L'invité non vérifié est refusé.
    post(guest, f'/invitation/{token}', {'action': 'accept'})
    assert auth('SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?', (guest_id, org_id), 'one') is None
    # Vérifié : adhésion créée à l'acceptation seulement, avec le rôle invité.
    auth('UPDATE users SET email_verified=1 WHERE id=?', (guest_id,))
    r = post(guest, f'/invitation/{token}', {'action': 'accept'})
    assert r.status_code == 302
    m = auth('SELECT role FROM memberships WHERE user_id=? AND organization_id=?', (guest_id, org_id), 'one')
    assert m['role'] == 'COMPTABLE'
    # Jeton à usage unique.
    assert guest.get(f'/invitation/{token}').status_code == 302


def test_new_user_creates_account_from_invitation(app, monkeypatch):
    sent = _capture_invites(monkeypatch)
    owner, _, org_id, _ = signup(app, 'Org Nouvelle', verified=True, plan='BUSINESS')
    email = f'{uuid.uuid4().hex[:8]}@example.com'
    post(owner, '/team/invite', {'email': email, 'role': 'MEMBER'})
    assert auth('SELECT 1 FROM users WHERE email=?', (email,), 'one') is None  # aucun compte pré-créé
    token = _token(sent[0])
    newcomer = app.test_client()
    assert newcomer.get(f'/invitation/{token}').status_code == 200
    r = post(newcomer, f'/invitation/{token}', {'action': 'create', 'full_name': 'Bob', 'password': 'password1234',
                                                 'password_confirm': 'password1234'})
    assert auth('SELECT 1 FROM users WHERE email=?', (email,), 'one') is None  # mot de passe courant refusé
    r = post(newcomer, f'/invitation/{token}', {'action': 'create', 'full_name': 'Bob', 'password': PASSWORD,
                                                 'password_confirm': PASSWORD})
    assert r.status_code == 302
    u = auth('SELECT id,email_verified FROM users WHERE email=?', (email,), 'one')
    assert u['email_verified'] == 1
    assert auth('SELECT role FROM memberships WHERE user_id=? AND organization_id=?', (u['id'], org_id), 'one')['role'] == 'MEMBER'


def test_pending_invitation_listed_and_revocable(app, monkeypatch):
    sent = _capture_invites(monkeypatch)
    owner, _, org_id, _ = signup(app, 'Org Révocation', verified=True, plan='BUSINESS')
    email = f'{uuid.uuid4().hex[:8]}@example.com'
    post(owner, '/team/invite', {'email': email, 'role': 'MEMBER'})
    page = owner.get('/team').get_data(as_text=True)
    assert email in page and 'Révoquer' in page
    inv = auth('SELECT id FROM team_invitations WHERE organization_id=? AND email=?', (org_id, email), 'one')
    post(owner, f"/team/invitations/{inv['id']}/revoquer")
    r = app.test_client().get(f"/invitation/{_token(sent[0])}")
    assert r.status_code == 302  # invitation révoquée : inutilisable
    stored = auth('SELECT token_hash FROM team_invitations WHERE id=?', (inv['id'],), 'one')['token_hash']
    assert _token(sent[0]) not in stored  # seul le haché est stocké


def test_unverified_owner_cannot_send_invitations(app, monkeypatch):
    sent = _capture_invites(monkeypatch)
    owner, *_ = signup(app, 'Org Non Vérifiée', plan='BUSINESS')
    post(owner, '/team/invite', {'email': 'quelquun@example.com', 'role': 'MEMBER'})
    assert not sent
    assert any('Confirmez' in m for m in flashes(owner))
