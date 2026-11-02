"""v492 — Webhook Resend : e-mails remis, rejetés ou signalés comme spam ; adresse bloquée,
fiche facture, Mission Control et correction de l'adresse."""
import base64
import json
import sys
import time
import types
from datetime import timedelta

import pytest

import profitos.runtime as rt
from profitos import email_delivery as ED
from profitos import mission_control as MC
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, issue_invoice, post, signup  # noqa: F401

SECRET = 'whsec_' + base64.b64encode(b'cle-de-test-du-webhook-resend-32o').decode()


@pytest.fixture
def resend_ok(monkeypatch):
    """Faux module resend : chaque envoi reçoit un identifiant unique."""
    sent = []

    class Emails:
        @staticmethod
        def send(params):
            sent.append(params)
            return {'id': f'em_{len(sent)}_{time.time_ns()}'}

    monkeypatch.setitem(sys.modules, 'resend', types.SimpleNamespace(Emails=Emails, api_key=None))
    monkeypatch.setenv('RESEND_API_KEY', 're_test')
    monkeypatch.setenv('RESEND_WEBHOOK_SECRET', SECRET)
    return sent


def webhook(cl, event, secret=SECRET, ts=None, msg_id=None):
    body = json.dumps(event)
    msg_id = msg_id or f'msg_{time.time_ns()}'
    ts = str(int(ts if ts is not None else time.time()))
    return cl.post('/webhooks/resend', data=body, content_type='application/json',
                   headers={'svix-id': msg_id, 'svix-timestamp': ts, 'svix-signature': ED.sign(secret, msg_id, ts, body)})


def event(kind, email_id, to='client@example.com', **data):
    return {'type': kind, 'created_at': '2026-10-27T10:00:00Z', 'data': dict(email_id=email_id, to=[to], **data)}


def last_delivery(org):
    c = rt.tenant_cx_direct(org)
    ED.ensure_table(c)
    row = c.execute('SELECT * FROM email_deliveries ORDER BY id DESC LIMIT 1').fetchone()
    c.close()
    return row


def send_invoice(cl, inv):
    return post(cl, f'/facturation/{inv}/envoyer')


# ---------------------------------------------------------------- signature
def test_signature_svix_verifiee():
    body, ts = '{"a":1}', str(int(time.time()))
    headers = {'svix-id': 'msg_1', 'svix-timestamp': ts, 'svix-signature': 'v1,faux ' + ED.sign(SECRET, 'msg_1', ts, body)}
    assert ED.verify_signature(SECRET, headers, body) == 'msg_1'
    for bad in ({**headers, 'svix-signature': 'v1,faux'}, {**headers, 'svix-timestamp': str(int(time.time()) - 600)},
                {k: v for k, v in headers.items() if k != 'svix-id'}):
        with pytest.raises(ED.SignatureError):
            ED.verify_signature(SECRET, bad, body)
    with pytest.raises(ED.SignatureError):
        ED.verify_signature(SECRET, headers, '{"a":2}')        # corps modifié


def test_route_refuse_sans_secret_ou_signature(app, monkeypatch):
    cl = app.test_client()
    monkeypatch.delenv('RESEND_WEBHOOK_SECRET', raising=False)
    assert webhook(cl, event('email.delivered', 'x')).status_code == 503
    monkeypatch.setenv('RESEND_WEBHOOK_SECRET', SECRET)
    other = 'whsec_' + base64.b64encode(b'une-autre-cle-de-signature-0000').decode()
    assert webhook(cl, event('email.delivered', 'x'), secret=other).status_code == 400
    assert webhook(cl, event('email.delivered', 'x'), ts=time.time() - 3600).status_code == 400
    assert webhook(cl, event('email.delivered', 'inconnu')).get_json()['result'] == 'unknown_message'


# --------------------------------------------------- envoi suivi puis rejeté
def test_facture_suivie_puis_rejetee_bloque_les_relances(app, resend_ok):
    cl, org = signup(app)
    inv = issue_invoice(cl, client_email='Client@Example.com')
    send_invoice(cl, inv)
    d = last_delivery(org)
    assert d and d['ref_type'] == 'outgoing_invoice' and d['ref_id'] == inv and d['recipient'] == 'client@example.com'
    assert d['status'] == 'sent'
    assert webhook(cl, event('email.delivered', d['provider_id'])).get_json()['result'] == 'delivered'
    r = webhook(cl, event('email.bounced', d['provider_id'], bounce={'type': 'Permanent', 'message': 'Boîte inexistante'}))
    assert r.status_code == 200 and r.get_json()['result'] == 'bounced'
    assert last_delivery(org)['status'] == 'bounced'
    # un « remis » arrivé en retard ne fait pas revenir en arrière
    assert webhook(cl, event('email.delivered', d['provider_id'])).get_json()['result'] == 'kept'
    ac = rt.auth_cx()
    assert ED.suppressed(ac, 'CLIENT@example.com')['reason'] == 'rejet'
    ac.close()
    # fiche facture : statut affiché et lien de correction
    html = cl.get(f'/facturation/{inv}').get_data(as_text=True)
    assert 'Non distribué (adresse rejetée)' in html and 'Boîte inexistante' in html and '/facturation/emails-non-distribues' in html
    # nouvel envoi refusé, sans appel à Resend
    n = len(resend_ok)
    send_invoice(cl, inv)
    assert len(resend_ok) == n
    assert any('ne reçoit plus' in m for m in flashes(cl))


def test_rejet_temporaire_ne_bloque_pas(app, resend_ok):
    cl, org = signup(app)
    inv = issue_invoice(cl, client_email='pleine@example.com')
    send_invoice(cl, inv)
    d = last_delivery(org)
    webhook(cl, event('email.bounced', d['provider_id'], to='pleine@example.com', bounce={'type': 'Transient', 'message': 'Boîte pleine'}))
    assert last_delivery(org)['status'] == 'delayed'
    ac = rt.auth_cx()
    assert ED.suppressed(ac, 'pleine@example.com') is None
    ac.close()


def test_plainte_et_evenement_rejoue(app, resend_ok):
    cl, org = signup(app)
    inv = issue_invoice(cl, client_email='spam@example.com')
    send_invoice(cl, inv)
    d = last_delivery(org)
    ev = event('email.complained', d['provider_id'], to='spam@example.com')
    assert webhook(cl, ev, msg_id='msg_fixe').get_json()['result'] == 'complained'
    assert webhook(cl, ev, msg_id='msg_fixe').get_json()['result'] == 'duplicate'
    ac = rt.auth_cx()
    assert ED.suppressed(ac, 'spam@example.com')['reason'] == 'plainte'
    ac.close()


def test_emails_de_compte_toujours_tentes(app, resend_ok):
    ac = rt.auth_cx()
    ED.suppress(ac, 'bloque@example.com', 'rejet')
    ac.commit(); ac.close()
    with app.test_request_context('/'):
        assert rt.send_email('bloque@example.com', 'Sujet', '<p>x</p>', kind='system')['sent'] is True
        blocked = rt.send_email('bloque@example.com', 'Sujet', '<p>x</p>', on_behalf_of_org=1)
    assert blocked['sent'] is False and blocked['suppressed'] is True


# ------------------------------------------------- Mission Control et correction
def test_mission_control_puis_correction_de_l_adresse(app, resend_ok):
    cl, org = signup(app)
    inv = issue_invoice(cl, client_email='mort@example.com')
    send_invoice(cl, inv)
    d = last_delivery(org)
    webhook(cl, event('email.bounced', d['provider_id'], to='mort@example.com', bounce={'type': 'Permanent'}))
    c = rt.tenant_cx_direct(org)
    found = [f for f in MC.compute(c, None, paris_today(), accounting=False)[0] if f['kind'] == 'email_non_distribue']
    c.close()
    assert len(found) == 1 and found[0]['level'] == 'bloque' and found[0]['amount'] == 1200
    assert found[0]['action']['endpoint'] == 'email_failures'
    page = cl.get('/facturation/emails-non-distribues').get_data(as_text=True)
    assert 'mort@example.com' in page and 'Corriger l' in page
    assert 'mort@example.com' in cl.get('/mission-control').get_data(as_text=True)
    post(cl, '/facturation/emails-non-distribues', {'old': 'mort@example.com', 'new': 'pas-une-adresse'})
    assert any('invalide' in m for m in flashes(cl))
    post(cl, '/facturation/emails-non-distribues', {'old': 'mort@example.com', 'new': 'nouvelle@example.com'})
    assert any('Adresse corrigée sur 1 facture' in m for m in flashes(cl))
    c = rt.tenant_cx_direct(org)
    assert c.execute('SELECT client_email FROM outgoing_invoices WHERE id=?', (inv,)).fetchone()['client_email'] == 'nouvelle@example.com'
    assert not [f for f in MC.compute(c, None, paris_today(), accounting=False)[0] if f['kind'] == 'email_non_distribue']
    c.close()
    n = len(resend_ok)
    send_invoice(cl, inv)                                   # la nouvelle adresse reçoit la facture
    assert len(resend_ok) == n + 1 and resend_ok[-1]['to'] == ['nouvelle@example.com']


def test_correction_limitee_aux_adresses_en_echec(app, resend_ok):
    cl, org = signup(app)
    inv = issue_invoice(cl, client_email='ok@example.com')
    post(cl, '/facturation/emails-non-distribues', {'old': 'ok@example.com', 'new': 'pirate@example.com'})
    assert any('introuvable' in m for m in flashes(cl))
    c = rt.tenant_cx_direct(org)
    assert c.execute('SELECT client_email FROM outgoing_invoices WHERE id=?', (inv,)).fetchone()['client_email'] == 'ok@example.com'
    c.close()


def test_controle_securite_signale_le_webhook_absent():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('sc', Path(__file__).resolve().parent.parent / 'scripts' / 'security_check.py')
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    assert sc.check_email_webhook({}) == []
    assert sc.check_email_webhook({'RESEND_API_KEY': 're_x'})[0]['level'] == sc.WARN
    assert sc.check_email_webhook({'RESEND_API_KEY': 're_x', 'RESEND_WEBHOOK_SECRET': SECRET})[0]['level'] == sc.OK
