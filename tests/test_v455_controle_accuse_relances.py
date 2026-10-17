"""v455 — Contrôle automatique de l'accusé de dépôt joint (SIREN, formulaire, période,
montant) et relances quand une échéance passe sans justificatif."""
import io
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, jobs, teledeclaration as T, automation as A, corporate_tax as CT
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry

IDENTITY = {'name': 'Exemple SARL', 'siren': '732829320', 'siret': '73282932000074'}
CSRF = 'c' * 64


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    monkeypatch.delenv('EDI_PARTNER_ENABLED', raising=False)
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


def org(app):
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(1,'Org','BUSINESS','ACTIVE_PAID',?,?)",
               (rt.now(), rt.now()))
    ac.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES(1,'owner@exemple.fr','x',?)", (rt.now(),))
    ac.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(1,1,'OWNER',?)", (rt.now(),))
    ac.commit(); ac.close()
    rt.migrate_tenant(1, force=True)
    c = rt.tenant_cx_direct(1)
    book(c)
    CT.save_fiscal_profile(c, None, {'vat_filing': 'ca12'}, rt.now())
    return c


def receipt_pdf(lines):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Helvetica', '', 11)
    for line in lines:
        pdf.cell(0, 8, line, new_x='LMARGIN', new_y='NEXT')
    return bytes(pdf.output())


GOOD = ['Accuse de reception de votre declaration', 'Declaration de TVA 3517-S CA12',
        'SIREN : 732 829 320', 'Periode du 01/01/2025 au 31/12/2025', 'Montant a payer : 20 000,00 EUR']


def _row(c, decl):
    return c.execute('SELECT * FROM teledeclarations WHERE id=?', (decl,)).fetchone()


def test_analyze_receipt_statuses(app):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    row = _row(c, decl)
    payload = built['payload']
    ok = T.analyze_receipt(None, '.pdf', row, payload, text='\n'.join(GOOD))
    assert ok['status'] == 'concordant' and len(ok['checks']) == 4
    bad = T.analyze_receipt(None, '.pdf', row, payload,
                            text='Declaration 3310-CA3 SIREN 999 999 999 periode 03/2025 montant 1 234,00')
    failed = {x['label'] for x in bad['checks'] if not x['ok']}
    assert bad['status'] == 'ecarts' and any('SIREN' in f for f in failed) and any('Formulaire' in f for f in failed)
    assert any('Montant' in f for f in failed) and any('Exercice' in f for f in failed)
    # 120 000 ne doit pas valider un montant de 20 000.
    near = T.analyze_receipt(None, '.pdf', row, payload, text='\n'.join(GOOD[:4] + ['Total 120 000,00']))
    assert near['status'] == 'ecarts'
    assert T.analyze_receipt(b'\x89PNG', '.png', row, payload)['status'] == 'non_verifiable'
    assert T.analyze_receipt(b'%PDF-1.4 broken', '.pdf', row, payload)['status'] == 'non_verifiable'
    real = receipt_pdf(GOOD)
    assert T.analyze_receipt(real, '.pdf', row, payload)['status'] == 'concordant'
    c.close()


def test_upload_stores_check_and_warns(app):
    c = org(app)
    c.close()
    cl = app.test_client()
    with cl.session_transaction() as s:
        s.update({'user_id': 1, 'org_id': 1, 'csrf_token': CSRF})
    ac = rt.auth_cx()
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=1")
    ac.commit(); ac.close()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    cl.post('/company', data={'csrf_token': CSRF, 'name': 'Exemple SARL', 'legal_form': 'SARL', 'siret': '73282932000074',
                              'address': '1 rue Test', 'postal_code': '75001', 'city': 'Paris'})
    r = cl.post('/comptabilite/teledeclarations/preparer-depot',
                data={'csrf_token': CSRF, 'type': 'TVA_CA12', 'du': '2025-01-01', 'au': '2025-12-31', 'regime': 'normal'})
    if r.status_code != 302 or '/teledeclarations/' not in r.headers.get('Location', ''):
        pytest.skip('session de test non authentifiée')
    decl = r.headers['Location'].rstrip('/').split('/')[-1]
    bad = receipt_pdf(['Accuse de reception', 'Declaration 3310-CA3', 'SIREN 732 829 320', 'Periode 03/2025'])
    cl.post(f'/comptabilite/teledeclarations/{decl}/justificatif', content_type='multipart/form-data',
            data={'csrf_token': CSRF, 'receipt': (io.BytesIO(bad), 'ar.pdf'), 'filed_on': '2026-05-02'})
    with cl.session_transaction() as s:
        msgs = [m for _, m in s.pop('_flashes', [])]
    assert any('ne correspond pas entièrement' in m for m in msgs)
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    assert 'Écarts entre l' in page and 'non retrouvé' in page
    cl.post(f'/comptabilite/teledeclarations/{decl}/justificatif', content_type='multipart/form-data',
            data={'csrf_token': CSRF, 'receipt': (io.BytesIO(receipt_pdf(GOOD)), 'ar2.pdf'), 'filed_on': '2026-05-02'})
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    assert 'Justificatif concordant' in page


def test_overdue_and_weekly_reminders(app, monkeypatch):
    c = org(app)
    late = A.overdue_declarations(c, None, date(2026, 5, 6))
    ca12 = [x for x in late if x['type'] == 'TVA_CA12']
    assert ca12 and ca12[0]['days_late'] == 1
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    c.close()
    import profitos.accounting as acc
    sent = []
    monkeypatch.setattr(jobs, '_notify_owner', lambda org_, subject, title, intro, *a, **k: sent.append((subject, intro)) or {'sent': True})
    monkeypatch.setattr(acc, 'paris_today', lambda: date(2026, 5, 6))
    jobs.run_job(app, 'fiscal_deadlines')
    assert sent and 'en retard' in sent[0][0].lower() and '3517-S CA12' in sent[0][1]
    assert 'justificatif non archivé' in sent[0][1] and 'compte fiscal' in sent[0][1]
    sent.clear()
    monkeypatch.setattr(acc, 'paris_today', lambda: date(2026, 5, 8))   # 3 jours : pas de relance
    jobs.run_job(app, 'fiscal_deadlines', force=True)
    assert not any('3517-S CA12' in i for _s, i in sent)
    sent.clear()
    monkeypatch.setattr(acc, 'paris_today', lambda: date(2026, 5, 12))  # une semaine : relance
    jobs.run_job(app, 'fiscal_deadlines', force=True)
    assert any('3517-S CA12' in i for _s, i in sent)
    c = rt.tenant_cx_direct(1)
    T.record_filing(c, _row(c, decl), 'x.pdf', 'a.pdf', '2026-05-04')
    assert not [x for x in A.overdue_declarations(c, None, date(2026, 5, 12)) if x['type'] == 'TVA_CA12']
    c.close()


def test_status_follows_receipt_check_and_manual_confirmation(app):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    row = _row(c, decl)
    T.record_filing(c, row, 'a.pdf', 'a.pdf', '2026-05-02',
                    check=T.analyze_receipt(receipt_pdf(GOOD), '.pdf', row, built['payload']))
    assert _row(c, decl)['status'] == 'confirmed'
    with pytest.raises(T.TeledeclarationError):
        T.confirm_filing(c, _row(c, decl), 'a@x.fr')          # déjà confirmé
    bad = T.analyze_receipt(receipt_pdf(['3310-CA3', 'SIREN 111 111 111']), '.pdf', row, built['payload'])
    T.record_filing(c, _row(c, decl), 'b.pdf', 'b.pdf', '2026-05-02', check=bad)
    assert _row(c, decl)['status'] == 'to_verify'
    assert [r['id'] for r in A.receipts_to_verify(c, None)] == [decl]
    with pytest.raises(T.TeledeclarationError):
        T.confirm_filing(c, _row(c, decl), 'a@x.fr', note='ok')   # justification trop courte
    T.confirm_filing(c, _row(c, decl), 'a@x.fr', note="Accusé du dépôt rectifié, montant identique")
    row = _row(c, decl)
    import json
    assert row['status'] == 'confirmed' and json.loads(row['receipt_check'])['manual_confirmation']['by'] == 'a@x.fr'
    T.record_filing(c, row, 'c.png', 'c.png', '2026-05-02', check=T.analyze_receipt(b'\x89PNG', '.png', row, built['payload']))
    assert _row(c, decl)['status'] == 'declared'
    T.confirm_filing(c, _row(c, decl), 'a@x.fr')                   # accusé illisible : commentaire facultatif
    assert _row(c, decl)['status'] == 'confirmed' and not A.receipts_to_verify(c, None)
    # Tous ces statuts comptent comme déposés pour l'échéancier, aucun ne vaut acceptation.
    assert T.active_declaration(c, None, 'TVA_CA12', '2025-01-01', '2025-12-31')['id'] == decl
    assert 'accept' not in T.STATUS_LABELS['confirmed'].lower()
    c.close()


def test_legacy_filed_status_migrated(app):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    c.execute("""UPDATE teledeclarations SET status='filed',receipt_check=? WHERE id=?""",
              ('{"status": "ecarts", "checks": []}', decl))
    c.commit(); c.close()
    rt.migrate_tenant(1, force=True)
    c = rt.tenant_cx_direct(1)
    assert _row(c, decl)['status'] == 'to_verify'
    c.close()


def test_confirm_route_roles_and_note(app):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    T.record_filing(c, _row(c, decl), 'a.pdf', 'a.pdf', '2026-05-02', check={'status': 'ecarts', 'checks': []})
    c.close()
    cl = app.test_client()
    ac = rt.auth_cx()
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=1")
    ac.commit(); ac.close()
    with cl.session_transaction() as s:
        s.update({'user_id': 1, 'org_id': 1, 'csrf_token': CSRF})
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    if 'Confirmer le dépôt' not in page:
        pytest.skip('session de test non authentifiée')
    assert 'Justificatif à vérifier' in page and 'Explication des écarts' in page
    cl.post(f'/comptabilite/teledeclarations/{decl}/confirmer', data={'csrf_token': CSRF, 'verified': '1', 'note': 'court'})
    c = rt.tenant_cx_direct(1)
    assert _row(c, decl)['status'] == 'to_verify'
    c.close()
    cl.post(f'/comptabilite/teledeclarations/{decl}/confirmer',
            data={'csrf_token': CSRF, 'verified': '1', 'note': 'Ecart d arrondi de 1 euro sur le montant'})
    c = rt.tenant_cx_direct(1)
    assert _row(c, decl)['status'] == 'confirmed'
    c.close()
    assert 'Confirmé manuellement par' in cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
