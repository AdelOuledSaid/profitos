"""v415 — Anti-relais de spam : adresse vérifiée exigée, quota quotidien par
organisation, limite par destinataire (mot de passe oublié, démo)."""
from support_v4xx import app, auth, flashes, post, signup  # noqa: F401


def _fake_resend(monkeypatch):
    """Simule un fournisseur configuré (pas de dry-run) sans aucun appel réseau."""
    import sys, types
    sent = []
    fake = types.SimpleNamespace(api_key=None, Emails=types.SimpleNamespace(
        SendParams=dict, send=lambda params: sent.append(params) or {'id': 'x'}))
    monkeypatch.setitem(sys.modules, 'resend', fake)
    monkeypatch.setenv('RESEND_API_KEY', 're_test')
    return sent


def test_unverified_sender_is_blocked_for_external_email(app, monkeypatch):
    from profitos import runtime as rt
    sent = _fake_resend(monkeypatch)
    client, _, org_id, uid = signup(app, 'Spam SAS')
    with app.test_request_context('/'):
        from flask import session
        session['user_id'], session['org_id'] = uid, org_id
        r = rt.send_email('victime@example.com', 'Sujet libre', '<p>corps libre</p>')
        assert r['blocked'] and 'Confirmez' in r['error']
        # Les e-mails système (vérification, réinitialisation) ne sont jamais bloqués.
        assert rt.send_email('moi@example.com', 'Vérif', '<p>x</p>', kind='system')['sent']
    recipients = [p['to'][0] for p in sent]
    assert 'moi@example.com' in recipients and 'victime@example.com' not in recipients


def test_daily_quota_per_organization(app, monkeypatch):
    from profitos import runtime as rt
    _fake_resend(monkeypatch)
    monkeypatch.setitem(rt.OUTBOUND_EMAIL_DAILY_QUOTA, 'TRIAL', 3)
    client, _, org_id, uid = signup(app, 'Quota SAS', verified=True)
    with app.test_request_context('/'):
        from flask import session
        session['user_id'], session['org_id'] = uid, org_id
        results = [rt.send_email(f'c{i}@example.com', 'Facture', '<p>x</p>') for i in range(4)]
    assert [bool(r.get('sent')) for r in results] == [True, True, True, False]
    assert 'Limite' in results[3]['error']


def test_paid_plan_has_higher_quota():
    from profitos import runtime as rt
    assert rt.OUTBOUND_EMAIL_DAILY_QUOTA['PAID'] > rt.OUTBOUND_EMAIL_DAILY_QUOTA['TRIAL']


def test_recipient_rate_limit(app):
    from profitos import runtime as rt
    with app.test_request_context('/'):
        import uuid
        target = f'{uuid.uuid4().hex[:8]}@example.com'
        assert [rt.check_recipient_rate('password_reset', target) for _ in range(4)] == [True, True, True, False]
        assert rt.check_recipient_rate('password_reset', 'b' + target)


def test_forgot_password_sends_at_most_three_per_hour(app, monkeypatch):
    from profitos.routes import account
    calls = []
    monkeypatch.setattr(account, 'send_reset_email', lambda u: calls.append(u['email']))
    _, email, _, _ = signup(app, 'Oubli SAS')
    client = app.test_client()
    for _ in range(5):
        post(client, '/forgot-password', {'email': email})
    assert len(calls) == 3


def test_demo_acknowledgement_is_rate_limited_per_recipient():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'profitos/routes/main.py').read_text(encoding='utf-8')
    assert "check_recipient_rate('demo_ack',email)" in src


def test_action_email_requires_verified_sender():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'profitos/routes/actions.py').read_text(encoding='utf-8')
    assert "check_outbound_email_quota('action')" in src
