"""v454 — Dépôt par l'entreprise sur impots.gouv.fr : ProfitOS calcule et prépare
(récapitulatif PDF), l'entreprise dépose elle-même, puis archive l'accusé de réception ;
la déclaration n'est « déposée » qu'avec ce justificatif."""
import io
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, teledeclaration as T, automation as A
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry

IDENTITY = {'name': 'Exemple SARL', 'siren': '732829320', 'siret': '73282932000074'}
CSRF = 'c' * 64
PDF = b'%PDF-1.4\n% accuse de reception\n'
CA12 = {'type': 'TVA_CA12', 'du': '2025-01-01', 'au': '2025-12-31', 'regime': 'normal'}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    monkeypatch.delenv('EDI_PARTNER_ENABLED', raising=False)
    monkeypatch.delenv('EDI_SIMULATION_ENABLED', raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def book(c):
    create_entry(c, 'BQ', '2025-01-05', 'Capital', [{'account_code': '512000', 'debit': 20000},
                                                     {'account_code': '101300', 'credit': 20000}])
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 120000, 'auxiliary_name': 'C'},
                                                  {'account_code': '706000', 'credit': 100000},
                                                  {'account_code': '445710', 'credit': 20000, 'vat_rate': 20.0, 'vat_base': 100000}])


def tenant(app):
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(1,'Org','BUSINESS','ACTIVE_PAID',?,?)",
               (rt.now(), rt.now()))
    ac.commit(); ac.close()
    rt.migrate_tenant(1, force=True)
    return rt.tenant_cx_direct(1)


def signup(app, role='OWNER'):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    r = cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'full_name': 'Alice Martin',
                                 'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                                 'company_name': 'Exemple SARL'})
    assert r.status_code in (302, 303)
    with cl.session_transaction() as s:
        org_id, uid = s['org_id'], s['user_id']
    post(cl, '/company', {'name': 'Exemple SARL', 'legal_form': 'SARL', 'siret': '73282932000074', 'address': '1 rue Test',
                          'postal_code': '75001', 'city': 'Paris'})
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.execute('UPDATE memberships SET role=? WHERE organization_id=?', (role, org_id))
    ac.commit(); ac.close()
    c = rt.tenant_cx_direct(org_id)
    book(c)
    c.close()
    return cl, org_id


def post(cl, url, data, **kw):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data, csrf_token=CSRF), **kw)


def flashes(cl):
    with cl.session_transaction() as s:
        return [m for _, m in s.pop('_flashes', [])]


def test_filing_channels():
    for t in ('TVA_CA3', 'TVA_CA12', 'TVA_3514', 'IS_2571', 'IS_2572', 'DAS2'):
        assert T.filing_channel(t) == 'efi'
    assert T.filing_channel('LIASSE', 'simplifie') == 'efi'
    assert T.filing_channel('LIASSE', 'normal') == 'edi' and T.filing_channel('CVAE_1330') == 'edi'
    assert 'EDI obligatoire' in T.efi_guide('CVAE_1330') and '3517-S' in T.efi_guide('TVA_CA12')


def test_prepare_update_file_and_lock(app):
    c = tenant(app)
    book(c)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    d1 = T.prepare_manual(c, built, None, 'a@x.fr')
    d2 = T.prepare_manual(c, built, None, 'a@x.fr')
    assert d1 == d2
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (d1,)).fetchone()
    assert (row['status'], row['mode'], row['channel']) == ('prepared', 'manual', 'EFI (impots.gouv.fr)')
    assert not T.active_declaration(c, None, 'TVA_CA12', '2025-01-01', '2025-12-31')
    assert T.refresh_status(c, row) == 'prepared'
    with pytest.raises(T.TeledeclarationError):
        T.record_filing(c, row, 'x.pdf', 'ar.pdf', '2099-01-01')
    T.record_filing(c, row, 'x.pdf', 'ar.pdf', '2026-05-02', 'AR-1', 'a@x.fr')
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (d1,)).fetchone()
    assert (row['status'], row['filed_at'], row['ack_number'], row['receipt_name']) == ('filed', '2026-05-02', 'AR-1', 'ar.pdf')
    assert T.active_declaration(c, None, 'TVA_CA12', '2025-01-01', '2025-12-31')['id'] == d1
    with pytest.raises(T.TeledeclarationError):
        T.prepare_manual(c, built, None)
    d3 = T.prepare_manual(c, built, None, corrective=True)
    corr = c.execute('SELECT * FROM teledeclarations WHERE id=?', (d3,)).fetchone()
    assert d3 != d1 and corr['corrective'] == 1 and corr['replaces_id'] == d1
    c.close()


def test_receipt_checks():
    assert T.check_receipt('ar.PDF', PDF) == '.pdf'
    assert T.check_receipt('ar.png', b'\x89PNG\r\n\x1a\n') == '.png'
    for name, data in (('ar.exe', PDF), ('ar.pdf', b'MZ..'), ('ar.pdf', b''), ('ar.pdf', b'%PDF-' + b'0' * T.MAX_RECEIPT_BYTES)):
        with pytest.raises(T.TeledeclarationError):
            T.check_receipt(name, data)


def test_full_flow_pages_pdf_and_receipt(app):
    cl, oid = signup(app)
    html = cl.get('/comptabilite/teledeclarations').get_data(as_text=True)
    assert 'Déclarations fiscales' in html and "n'effectue pas lui-même la transmission" in html
    page = cl.get('/comptabilite/teledeclarations/preparer?type=TVA_CA12&du=2025-01-01&au=2025-12-31').get_data(as_text=True)
    assert 'Préparer ma déclaration' in page and '3517-S CA12' in page
    r = post(cl, '/comptabilite/teledeclarations/preparer-depot', CA12)
    decl = r.headers['Location'].rstrip('/').split('/')[-1]
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    assert 'Accéder à impots.gouv.fr' in page and 'https://www.impots.gouv.fr/accueil' in page and 'Enregistrer le dépôt' in page
    pdf = cl.get(f'/comptabilite/teledeclarations/{decl}/recapitulatif.pdf')
    assert pdf.status_code == 200 and pdf.mimetype == 'application/pdf' and pdf.data.startswith(b'%PDF')
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(b'GIF89a'), 'ar.pdf'), 'filed_on': '2026-05-02'}, content_type='multipart/form-data')
    assert any('format' in m for m in flashes(cl))
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'accuse.pdf'), 'filed_on': '2026-05-02', 'reference': 'AR-42'},
         content_type='multipart/form-data')
    assert any('Dépôt enregistré' in m for m in flashes(cl))
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    assert 'justificatif archivé' in page and 'accuse.pdf' in page and 'AR-42' in page
    dl = cl.get(f'/comptabilite/teledeclarations/{decl}/justificatif')
    assert dl.status_code == 200 and dl.data == PDF and 'accuse.pdf' in dl.headers['Content-Disposition']
    # Une seule pièce archivée, et remplacement possible.
    from profitos import storage
    files = list((storage.UPLOAD_ROOT / 'declaration_receipts' / str(oid)).iterdir())
    assert len(files) == 1
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF + b'v2'), 'accuse2.pdf'), 'filed_on': '2026-05-03'}, content_type='multipart/form-data')
    files = list((storage.UPLOAD_ROOT / 'declaration_receipts' / str(oid)).iterdir())
    assert len(files) == 1 and files[0].read_bytes() == PDF + b'v2'


def test_regime_normal_liasse_marked_edi(app):
    cl, _oid = signup(app)
    page = cl.get('/comptabilite/teledeclarations/preparer?type=LIASSE&du=2025-01-01&au=2025-12-31&regime=normal').get_data(as_text=True)
    assert 'Dépôt EDI obligatoire' in page
    r = post(cl, '/comptabilite/teledeclarations/preparer-depot', dict(CA12, type='CVAE_1330'))
    decl = r.headers['Location'].rstrip('/').split('/')[-1]
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    assert 'Accéder à impots.gouv.fr' not in page and 'expert-comptable' in page


def test_roles_and_isolation(app):
    cl, oid = signup(app)
    r = post(cl, '/comptabilite/teledeclarations/preparer-depot', CA12)
    decl = r.headers['Location'].rstrip('/').split('/')[-1]
    ac = rt.auth_cx()
    ac.execute("UPDATE memberships SET role='LECTEUR' WHERE organization_id=?", (oid,))
    ac.commit(); ac.close()
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'a.pdf'), 'filed_on': '2026-05-02'}, content_type='multipart/form-data')
    c = rt.tenant_cx_direct(oid)
    assert c.execute('SELECT status FROM teledeclarations WHERE id=?', (decl,)).fetchone()['status'] == 'prepared'
    c.close()
    other, _ = signup(app)
    assert other.get(f'/comptabilite/teledeclarations/{decl}/recapitulatif.pdf').status_code == 404
    assert other.get(f'/comptabilite/teledeclarations/{decl}/justificatif').status_code == 404


def test_simulation_hidden_in_production(app, monkeypatch):
    cl, oid = signup(app)
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    flashes(cl)
    page = cl.get('/comptabilite/teledeclarations/preparer?type=TVA_CA12&du=2025-01-01&au=2025-12-31').get_data(as_text=True)
    assert 'Déposer en mode simulation' not in page and 'Préparer ma déclaration' in page
    post(cl, '/comptabilite/teledeclarations/deposer', dict(CA12, certify='1'))
    c = rt.tenant_cx_direct(oid)
    assert c.execute('SELECT COUNT(*) n FROM teledeclarations').fetchone()['n'] == 0
    c.close()


def test_deadlines_show_prepared_then_filed(app):
    c = tenant(app)
    book(c)
    from profitos import corporate_tax as CT
    CT.save_fiscal_profile(c, None, {'vat_filing': 'ca12'}, rt.now())
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    item = next(x for x in A.upcoming_deadlines(c, None, date(2026, 4, 20), 30) if x['type'] == 'TVA_CA12')
    assert item['prepared']['id'] == decl and not item['deposited']
    T.record_filing(c, c.execute('SELECT * FROM teledeclarations WHERE id=?', (decl,)).fetchone(), 'x.pdf', 'a.pdf', '2026-04-20')
    item = next(x for x in A.upcoming_deadlines(c, None, date(2026, 4, 20), 30) if x['type'] == 'TVA_CA12')
    c.close()
    assert item['deposited']['id'] == decl
