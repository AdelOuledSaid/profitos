"""v470-v471 — Encaissement : QR code de virement SEPA et lien de paiement sur les factures,
relances automatiques par étapes."""
import io
import re
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, qr, invoice_payment as IP
from profitos.config import DevelopmentConfig

CSRF = 'c' * 64
IBAN = 'FR7630006000011234567890189'


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    try:
        rt.limiter.enabled = False
    except Exception:
        pass
    return a


def post(cl, url, data=None, **kw):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data or {}, csrf_token=CSRF), **kw)


def flashes(cl):
    with cl.session_transaction() as s:
        return [m for _, m in s.pop('_flashes', [])]


def signup(app, iban=IBAN):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'Alice', 'last_name': 'Martin',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                             'company_name': 'Atelier Martin'})
    with cl.session_transaction() as s:
        org, uid = s['org_id'], s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='BUSINESS',status='ACTIVE_PAID' WHERE id=?", (org,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit(); ac.close()
    post(cl, '/company', {'name': 'Atelier Martin', 'legal_form': 'SAS', 'siret': '73282932000074', 'vat_number': 'FR44732829320',
                          'address': '1 rue Test', 'postal_code': '75001', 'city': 'Paris', 'share_capital': '1000',
                          'rcs_city': 'Paris', 'payment_terms': '30 jours', 'late_penalty_rate': '10', 'iban': iban, 'bic': 'AGRIFRPP'})
    flashes(cl)
    return cl, org


def issue_invoice(cl, client_email='client@example.com', price='1000'):
    r = post(cl, '/facturation/nouvelle', {'client_name': 'Client Durand', 'client_address': '2 rue X\n75002 Paris',
                                           'client_siren': '732829320', 'client_email': client_email, 'due_date': '2026-12-31',
                                           'service_date': '2026-10-01', 'operation_nature': 'services',
                                           'label_1': 'Mission', 'qty_1': '1', 'price_1': price, 'vat_1': '20'})
    inv_id = int(re.search(r'/facturation/(\d+)', r.headers['Location']).group(1))
    post(cl, f'/facturation/{inv_id}/marquer-emise')
    flashes(cl)
    return inv_id


def invoice_row(org, inv_id):
    c = rt.tenant_cx_direct(org)
    row = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()
    c.close()
    return row


# ---------------------------------------------------------------- QR code
def test_qr_structure_et_contenu_epc():
    m = qr.matrix('HELLO')
    assert len(m) == 21                                            # version 1
    for r, c in ((0, 0), (0, 14), (14, 0)):                        # motifs de position
        assert all(m[r][c + i] for i in range(7)) and all(m[r + i][c] for i in range(7))
    assert len(qr.matrix('x' * 120)) == 17 + 4 * 7                  # version 7 (informations de version)
    with pytest.raises(ValueError):
        qr.matrix('x' * 500)
    payload = qr.epc_payload('Atelier  Martin', 'FR76 3000 6000 0112 3456 7890 189', 1200, 'Facture F-2026-0001', 'agrifrpp')
    assert payload.split('\n') == ['BCD', '002', '1', 'SCT', 'AGRIFRPP', 'Atelier Martin', IBAN, 'EUR1200.00', '', '',
                                   'Facture F-2026-0001']
    for bad in ((0, 'x'), (1e10, 'x')):
        with pytest.raises(ValueError):
            qr.epc_payload('A', IBAN, *bad)
    svg = qr.svg(payload, label='QR')
    assert svg.startswith('<svg') and '<script' not in svg and 'Atelier' not in svg


def test_qr_lisible_par_un_decodeur():
    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    if not hasattr(cv2, 'QRCodeDetectorAruco'):
        pytest.skip('décodeur QR indisponible')
    det = cv2.QRCodeDetectorAruco()
    payloads = [qr.epc_payload('Bâti Sud SARL', IBAN, 1234.56, 'Facture F-2026-0042', 'AGRIFRPP'), 'A' * 300, 'ligne\nligne']
    for text in payloads:
        m = qr.matrix(text)
        n, b, s = len(m), 4, 6
        img = np.full(((n + 2 * b) * s,) * 2, 255, np.uint8)
        for r, row in enumerate(m):
            for c, v in enumerate(row):
                if v:
                    img[(r + b) * s:(r + b + 1) * s, (c + b) * s:(c + b + 1) * s] = 0
        assert det.detectAndDecode(img)[0] == text


def test_moyens_de_paiement_selon_la_facture():
    inv = {'status': 'sent', 'invoice_number': 'F-1'}
    seller = {'name': 'Atelier', 'iban': IBAN, 'bic': 'AGRIFRPP'}
    o = IP.options(inv, seller, 600.0, dict(IP.DEFAULTS))
    assert o['iban'] == 'FR76 3000 6000 0112 3456 7890 189' and o['qr_svg'] and 'EUR600.00' in o['epc']
    assert IP.options(dict(inv, status='paid'), seller, 600.0, IP.DEFAULTS) is None
    assert IP.options(inv, seller, 0, IP.DEFAULTS) is None
    assert IP.options(inv, dict(seller, iban='FR7630006000011234567890188'), 600.0, IP.DEFAULTS) is None   # IBAN invalide
    linked = IP.options(inv, dict(seller, iban=''), 600.0, dict(IP.DEFAULTS, link='https://pay.example/x'))
    assert linked['link'] == 'https://pay.example/x' and linked['qr_svg'] is None
    assert IP.options(inv, seller, 600.0, dict(IP.DEFAULTS, qr=False))['qr_svg'] is None
    for bad in ('http://pay.example', 'javascript:alert(1)', 'https://x y', 'https://' + 'a' * 300):
        with pytest.raises(IP.PaymentSettingsError):
            IP.clean_link(bad)
    for bad in ([3, 3, 30], [0, 15, 30], [3, 15, 200], ['a', 2, 3], [3, 15]):
        with pytest.raises(IP.PaymentSettingsError):
            IP.clean_steps(bad)
    assert [IP.reminder_step(d, n, [3, 15, 30]) for d, n in ((2, 0), (3, 0), (14, 1), (15, 1), (40, 2), (40, 3))] == \
        [None, 0, None, 1, 2, None]


# ----------------------------------------------------------- facture client
def test_facture_en_ligne_et_pdf_avec_qr(app):
    cl, org = signup(app)
    inv_id = issue_invoice(cl)
    row = invoice_row(org, inv_id)
    page = app.test_client().get(f"/facture/{row['public_token']}").get_data(as_text=True)
    assert 'Régler cette facture' in page and '<svg' in page and 'FR76 3000 6000 0112 3456 7890 189' in page
    assert f"Facture {row['invoice_number']}" in page and 'Payer en ligne' not in page
    pdf = app.test_client().get(f"/facture/{row['public_token']}/pdf").data
    from pypdf import PdfReader
    text = ''.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert 'Régler cette facture' in text and 'FR76 3000' in text and 'application de votre banque' in text
    # Lien de paiement en ligne : https uniquement.
    post(cl, '/facturation/encaissement', {'qr': '1', 'link': 'http://pay.example', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    assert any('https://' in m for m in flashes(cl))
    post(cl, '/facturation/encaissement', {'link': 'https://pay.example/atelier', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    page = app.test_client().get(f"/facture/{row['public_token']}").get_data(as_text=True)
    assert 'Payer en ligne' in page and 'https://pay.example/atelier' in page and '<svg' not in page   # QR désactivé
    # Facture soldée : plus aucun moyen de paiement proposé.
    post(cl, f'/facturation/{inv_id}/marquer-payee')
    page = app.test_client().get(f"/facture/{row['public_token']}").get_data(as_text=True)
    assert 'Régler cette facture' not in page


def test_sans_iban_ni_lien_rien_n_est_propose(app):
    cl, org = signup(app, iban='')
    row = invoice_row(org, issue_invoice(cl))
    assert 'Régler cette facture' not in app.test_client().get(f"/facture/{row['public_token']}").get_data(as_text=True)
    assert 'Renseignez un IBAN valide' in cl.get('/facturation/encaissement').get_data(as_text=True)


# ------------------------------------------------------- relances automatiques
def test_relances_automatiques_par_etapes(app, monkeypatch):
    cl, org = signup(app)
    inv_a = issue_invoice(cl, 'a@example.com')
    inv_b = issue_invoice(cl, 'b@example.com')
    inv_c = issue_invoice(cl, '')                                    # sans e-mail : jamais relancée
    c = rt.tenant_cx_direct(org)
    for i in (inv_a, inv_b, inv_c):
        c.execute("UPDATE outgoing_invoices SET issue_date='2026-08-01', due_date='2026-09-01' WHERE id=?", (i,))
    c.commit(); c.close()
    sent = []
    monkeypatch.setattr(rt, 'send_email', lambda to, subject, html, **k: sent.append((to, subject, html)) or {'sent': True})
    import profitos.routes.invoicing as INV
    monkeypatch.setattr(INV, 'send_email', lambda to, subject, html, **k: sent.append((to, subject, html)) or {'sent': True})
    import profitos.accounting as acc
    from profitos import jobs

    def run(day):
        sent.clear()
        monkeypatch.setattr(acc, 'paris_today', lambda: day)
        monkeypatch.setattr(INV, 'now', lambda: f'{day.isoformat()}T09:00:00')
        jobs.run_job(app, 'invoice_reminders', force=True)
        return sorted((to, subj.split(' — ')[0]) for to, subj, _h in sent)

    # Relances désactivées par défaut.
    assert run(date(2026, 9, 10)) == []
    post(cl, '/facturation/encaissement', {'qr': '1', 'auto_reminders': '1', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    page = cl.get('/facturation/encaissement').get_data(as_text=True)
    assert 'Rappel courtois' in page and 'Aucun e-mail client' in page
    assert run(date(2026, 9, 3)) == []                               # J+2 : trop tôt
    assert run(date(2026, 9, 4)) == [('a@example.com', 'Rappel courtois'), ('b@example.com', 'Rappel courtois')]
    html = next(h for to, _s, h in sent if to == 'a@example.com')
    assert 'simple oubli' in html and '/facture/' in html and 'QR code et IBAN' in html
    assert run(date(2026, 9, 5)) == []                               # déjà envoyée
    # B est suspendue, A est réglée partiellement : la relance porte sur le reste.
    post(cl, f'/facturation/{inv_b}/relances-auto')
    post(cl, f'/facturation/{inv_a}/reglement', {'amount': '400', 'payment_date': '2026-09-10', 'payment_method': 'bank'})
    flashes(cl)
    assert run(date(2026, 9, 16)) == [('a@example.com', 'Rappel ferme')]
    assert '800,00 €' in sent[0][2].replace(' ', ' ').replace('\xa0', ' ') and 'L441-10' in sent[0][2]
    # B réactivée à J+30 : son rappel ferme part ; son dernier avis attendra 5 jours.
    post(cl, f'/facturation/{inv_b}/relances-auto')                 # réactivée
    assert run(date(2026, 10, 1)) == [('a@example.com', 'Dernier avis avant mise en demeure'), ('b@example.com', 'Rappel ferme')]
    assert 'mise en demeure' in next(h for to, _s, h in sent if to == 'a@example.com')
    assert run(date(2026, 10, 3)) == []                              # B : moins de 5 jours depuis la précédente
    assert run(date(2026, 10, 6)) == [('b@example.com', 'Dernier avis avant mise en demeure')]
    assert run(date(2026, 11, 30)) == []                             # trois étapes au plus
    c = rt.tenant_cx_direct(org)
    n = c.execute('SELECT COUNT(*) n FROM invoice_reminders').fetchone()['n']
    c.close()
    assert n == 6                                                    # trois étapes pour A et pour B
    # Facture réglée : plus aucune relance, même avec des étapes restantes.
    post(cl, '/facturation/encaissement', {'qr': '1', 'auto_reminders': '1', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    post(cl, f'/facturation/{inv_a}/marquer-payee')
    assert 'a@example.com' not in str(run(date(2026, 12, 1)))
