"""v459 — Sécurité du module fiscal : module fermé non contournable, routes et API,
isolation des entreprises et des entités, rôles habilités, validation des montants et des
dates, statuts (jamais d'acceptation sans accusé EDI)."""
import io
import json
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, jobs, teledeclaration as T, corporate_tax as CT
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry

CSRF = 'c' * 64
CA12 = {'type': 'TVA_CA12', 'du': '2025-01-01', 'au': '2025-12-31', 'regime': 'normal'}
PDF = b'%PDF-1.4\n% accuse\n'
IDENTITY = {'name': 'Exemple SARL', 'siren': '732829320', 'siret': '73282932000074'}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    for var in ('RESEND_API_KEY', 'SMTP_HOST', 'EDI_PARTNER_ENABLED', 'EDI_SIMULATION_ENABLED',
                'FISCAL_DECLARATIONS_ENABLED', 'FISCAL_DECLARATIONS_PILOT_ORGS'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def post(cl, url, data=None, **kw):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data or {}, csrf_token=CSRF), **kw)


def flashes(cl):
    with cl.session_transaction() as s:
        return [m for _, m in s.pop('_flashes', [])]


def signup(app, role='OWNER'):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'full_name': 'Alice Martin',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                             'company_name': 'Exemple SARL'})
    with cl.session_transaction() as s:
        org_id, uid = s['org_id'], s['user_id']
    post(cl, '/company', {'name': 'Exemple SARL', 'legal_form': 'SARL', 'siret': '73282932000074', 'address': '1 rue Test',
                          'postal_code': '75001', 'city': 'Paris'})
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit(); ac.close()
    c = rt.tenant_cx_direct(org_id)
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 120000, 'auxiliary_name': 'C'},
                                                  {'account_code': '706000', 'credit': 100000},
                                                  {'account_code': '445710', 'credit': 20000, 'vat_rate': 20.0, 'vat_base': 100000}])
    c.close()
    flashes(cl)
    set_role(org_id, role)
    return cl, org_id


def set_role(org_id, role):
    ac = rt.auth_cx()
    ac.execute('UPDATE memberships SET role=? WHERE organization_id=?', (role, org_id))
    ac.commit(); ac.close()


def prepared(cl, org_id):
    set_role(org_id, 'OWNER')
    r = post(cl, '/comptabilite/teledeclarations/preparer-depot', CA12)
    assert '/comptabilite/teledeclarations/' in r.headers['Location'], flashes(cl)
    return int(r.headers['Location'].rstrip('/').split('/')[-1])


def count(org_id, where='1=1'):
    c = rt.tenant_cx_direct(org_id)
    n = c.execute(f'SELECT COUNT(*) n FROM teledeclarations WHERE {where}').fetchone()['n']
    c.close()
    return n


def status(org_id, decl):
    c = rt.tenant_cx_direct(org_id)
    s = c.execute('SELECT status FROM teledeclarations WHERE id=?', (decl,)).fetchone()['status']
    c.close()
    return s


# ------------------------------------------------------------------ 1. module fermé
def test_closed_module_blocks_every_fiscal_operation(app, monkeypatch):
    cl, oid = signup(app)
    decl = prepared(cl, oid)
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    assert not T.declarations_open(oid)
    calls = [
        ('/comptabilite/teledeclarations/preparer-depot', CA12, {}),
        ('/comptabilite/teledeclarations/deposer', dict(CA12, certify='1'), {}),
        (f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'a.pdf'), 'filed_on': '2026-05-02'}, {'content_type': 'multipart/form-data'}),
        (f'/comptabilite/teledeclarations/{decl}/confirmer', {'verified': '1', 'note': 'vérifié manuellement'}, {}),
        (f'/comptabilite/teledeclarations/{decl}/actualiser', {}, {}),
    ]
    for url, data, kw in calls:
        r = post(cl, url, data, **kw)
        assert r.status_code == 302, url
        assert any('en cours de validation' in m for m in flashes(cl)), url
    assert count(oid) == 1 and status(oid, decl) == 'prepared'
    # Un pilote seulement : les autres organisations restent fermées.
    monkeypatch.setenv('FISCAL_DECLARATIONS_PILOT_ORGS', f'{oid + 1000}')
    post(cl, '/comptabilite/teledeclarations/preparer-depot', CA12)
    assert any('en cours de validation' in m for m in flashes(cl))


def test_closed_module_sends_no_fiscal_reminders(app, monkeypatch):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    CT.save_fiscal_profile(c, None, {'vat_filing': 'ca12'}, rt.now())
    c.close()
    import profitos.accounting as acc
    sent = []
    monkeypatch.setattr(jobs, '_notify_owner', lambda *a, **k: sent.append(a) or {'sent': True})
    monkeypatch.setattr(acc, 'paris_today', lambda: date(2026, 5, 6))
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.setattr(jobs, '_require_paid', lambda *a, **k: None)
    org = {'id': oid, 'name': 'Exemple SARL', 'plan': 'MULTI', 'status': 'ACTIVE_PAID'}
    assert jobs.fiscal_deadlines(org, {}) == 'module fiscal non ouvert' and not sent
    monkeypatch.setenv('FISCAL_DECLARATIONS_ENABLED', '1')
    jobs.fiscal_deadlines(org, {})
    assert sent


# ------------------------------------------------------------------ 2. routes et API
def test_fiscal_routes_require_login_and_no_fiscal_api(app):
    anon = app.test_client()
    for url in ('/comptabilite/teledeclarations', '/comptabilite/teledeclarations/1', '/comptabilite/teledeclarations/1/contenu.json',
                '/comptabilite/teledeclarations/1/recapitulatif.pdf', '/comptabilite/teledeclarations/1/justificatif',
                '/comptabilite/profil-fiscal', '/comptabilite/das2', '/comptabilite/ifu', '/comptabilite/cfe',
                '/comptabilite/dividendes-2777', '/comptabilite/tva/ca12', '/comptabilite/impot-societes'):
        r = anon.get(url)
        assert r.status_code in (302, 401) and '/login' in r.headers.get('Location', '/login'), url
    # Aucune route d'API (jeton) n'expose les déclarations ou le profil fiscal.
    api = [r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/api/')]
    assert not [u for u in api if any(k in u for k in ('declar', 'fiscal', 'liasse', 'das2', 'ifu', 'tva', 'impot'))]
    # Toute route fiscale est rattachée au contrôle des rôles (domaine comptabilité).
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith('/comptabilite'):
            fn = app.view_functions[rule.endpoint]
            assert rt.MODULE_AREAS.get(fn.__module__) or rt.ENDPOINT_AREAS.get(rule.endpoint), rule.rule


def test_csrf_required_on_fiscal_posts(app):
    cl, oid = signup(app)
    decl = prepared(cl, oid)
    for url in ('/comptabilite/teledeclarations/preparer-depot', f'/comptabilite/teledeclarations/{decl}/confirmer',
                '/comptabilite/profil-fiscal', '/comptabilite/das2'):
        r = cl.post(url, data={'type': 'TVA_CA12', 'du': '2025-01-01', 'au': '2025-12-31', 'verified': '1'})
        assert r.status_code in (400, 403), url
    assert count(oid) == 1


def test_json_export_masks_iban_for_non_admins(app):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    CT.save_fiscal_profile(c, None, {'mandate': {'iban': 'FR7630006000011234567890189', 'bic': 'AGRIFRPP', 'holder': 'X',
                                                 'rum': 'R1', 'signed_at': '2026-01-01', 'bank_confirmed': True}}, rt.now())
    c.close()
    decl = prepared(cl, oid)
    assert '30006000011234567890189' in cl.get(f'/comptabilite/teledeclarations/{decl}/contenu.json').get_data(as_text=True)
    set_role(oid, 'COMPTABLE')
    data = json.loads(cl.get(f'/comptabilite/teledeclarations/{decl}/contenu.json').get_data(as_text=True))
    assert data['payment']['iban'] == 'FR76 •••• •••• 0189'


# ------------------------------------------------------------------ 3. isolation
def test_other_company_cannot_reach_declarations(app):
    cl_a, org_a = signup(app)
    decl = prepared(cl_a, org_a)
    post(cl_a, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'a.pdf'), 'filed_on': '2026-05-02'}, content_type='multipart/form-data')
    cl_b, org_b = signup(app)
    for url in (f'/comptabilite/teledeclarations/{decl}', f'/comptabilite/teledeclarations/{decl}/contenu.json',
                f'/comptabilite/teledeclarations/{decl}/recapitulatif.pdf', f'/comptabilite/teledeclarations/{decl}/justificatif'):
        assert cl_b.get(url).status_code == 404, url
    for url, data, kw in ((f'/comptabilite/teledeclarations/{decl}/confirmer', {'verified': '1', 'note': 'tentative externe'}, {}),
                          (f'/comptabilite/teledeclarations/{decl}/justificatif',
                           {'receipt': (io.BytesIO(PDF), 'b.pdf'), 'filed_on': '2026-05-03'}, {'content_type': 'multipart/form-data'}),
                          (f'/comptabilite/teledeclarations/{decl}/actualiser', {}, {})):
        assert post(cl_b, url, data, **kw).status_code == 404, url
    assert status(org_a, decl) == 'declared'


def test_other_entity_cannot_reach_declarations(app):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), entity_id=None, identity=IDENTITY)
    other = T.prepare_manual(c, built, 999)          # déclaration d'une autre entité de l'organisation
    c.close()
    assert cl.get(f'/comptabilite/teledeclarations/{other}').status_code == 404
    assert cl.get(f'/comptabilite/teledeclarations/{other}/recapitulatif.pdf').status_code == 404
    assert post(cl, f'/comptabilite/teledeclarations/{other}/confirmer', {'verified': '1'}).status_code == 404


# ------------------------------------------------------------------ 4. autorisations
FISCAL_WRITES = [
    ('/comptabilite/profil-fiscal', {'tax_regime': 'simplifie', 'vat_filing': 'franchise', 'signataire': 'Intrus'}),
    ('/comptabilite/impot-societes', {'action': 'settings', 'credits_impot': '99999'}),
    ('/comptabilite/impot-societes', {'action': 'entry'}),
    ('/comptabilite/das2', {'annee': '2025', 'key_1': 'zz', 'amount_1': '5000', 'include_1': '1'}),
    ('/comptabilite/ifu', {'annee': '2025', 'dispensed': 'x'}),
    ('/comptabilite/cfe', {'annee': '2026', 'action': 'save', 'montant': '1234'}),
    ('/comptabilite/cfe', {'annee': '2026', 'action': 'entry'}),
    ('/comptabilite/dividendes-2777', {'mois': '2026-06', 'part_pp': '10'}),
    ('/comptabilite/teledeclarations/preparer-depot', CA12),
    ('/comptabilite/teledeclarations/deposer', dict(CA12, certify='1')),
]


@pytest.mark.parametrize('role', ['MEMBER', 'COMMERCIAL', 'LECTURE'])
def test_unauthorised_roles_cannot_write_fiscal_data(app, role):
    cl, oid = signup(app)
    decl = prepared(cl, oid)
    c = rt.tenant_cx_direct(oid)
    before = (json.dumps(CT.get_fiscal_profile(c, None), sort_keys=True), json.dumps(CT.get_fiscal_settings(c, None, '2025-12-31'), sort_keys=True),
              c.execute('SELECT COUNT(*) n FROM fiscal_settings').fetchone()['n'], c.execute('SELECT COUNT(*) n FROM accounting_entries').fetchone()['n'])
    c.close()
    set_role(oid, role)
    for url, data in FISCAL_WRITES + [(f'/comptabilite/teledeclarations/{decl}/confirmer', {'verified': '1', 'note': 'non habilité'})]:
        r = post(cl, url, data)
        assert r.status_code == 302, (role, url)
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'a.pdf'), 'filed_on': '2026-05-02'}, content_type='multipart/form-data')
    c = rt.tenant_cx_direct(oid)
    after = (json.dumps(CT.get_fiscal_profile(c, None), sort_keys=True), json.dumps(CT.get_fiscal_settings(c, None, '2025-12-31'), sort_keys=True),
             c.execute('SELECT COUNT(*) n FROM fiscal_settings').fetchone()['n'], c.execute('SELECT COUNT(*) n FROM accounting_entries').fetchone()['n'])
    c.close()
    assert after == before and count(oid) == 1 and status(oid, decl) == 'prepared'


def test_comptable_role_can_write_but_not_mandate(app):
    cl, oid = signup(app, role='COMPTABLE')
    post(cl, '/comptabilite/profil-fiscal', {'tax_regime': 'normal', 'vat_filing': 'ca12', 'signataire': 'Comptable',
                                             'mandate_iban': 'FR7630006000011234567890189', 'mandate_rum': 'X'})
    c = rt.tenant_cx_direct(oid)
    p = CT.get_fiscal_profile(c, None)
    c.close()
    assert p['signataire'] == 'Comptable' and not p['mandate']['iban']


# ------------------------------------------------------------------ 5. montants et dates
@pytest.mark.parametrize('value', ['inf', '-inf', '1e300', 'Infinity', 'nan', '1e13'])
def test_form_float_rejects_non_finite_and_absurd(value):
    with pytest.raises(ValueError):
        rt.form_float(value)
    assert rt.form_float('1 234,56') == 1234.56


@pytest.mark.parametrize('url,data,expected', [
    ('/comptabilite/teledeclarations/preparer-depot',
     {'type': 'TVA_3514', 'du': '2025-01-01', 'au': '2025-12-31', 'acompte': 'inf', 'echeance': '2026-07-15'}, 'hors limites'),
    ('/comptabilite/teledeclarations/preparer-depot',
     {'type': 'TVA_3514', 'du': '2025-01-01', 'au': '2025-12-31', 'acompte': '-500', 'echeance': '2026-07-15'}, 'Aucun acompte'),
    ('/comptabilite/teledeclarations/preparer-depot',
     {'type': 'TVA_3514', 'du': '2025-01-01', 'au': '2025-12-31', 'acompte': '100', 'echeance': '2099-01-01'}, 'deux ans'),
    ('/comptabilite/teledeclarations/preparer-depot', {'type': 'LIASSE', 'du': '1990-01-01', 'au': '2025-12-31'}, 'trop longue'),
    ('/comptabilite/teledeclarations/preparer-depot', {'type': 'TVA_CA12', 'du': '2025-12-31', 'au': '2025-01-01'}, 'invalide'),
    ('/comptabilite/teledeclarations/preparer-depot', {'type': 'TVA_CA12', 'du': 'pas-une-date', 'au': '2025-12-31'}, 'impossible'),
    ('/comptabilite/impot-societes', {'action': 'settings', 'credits_impot': '-50000'}, 'négatif'),
    ('/comptabilite/impot-societes', {'action': 'settings', 'reint_amount_1': '-10', 'reint_label_1': 'x'}, 'négatif'),
    ('/comptabilite/das2', {'annee': '2025', 'key_1': 'x', 'amount_1': '1e300'}, 'invalide'),
    ('/comptabilite/cfe', {'annee': '2026', 'action': 'save', 'montant': 'inf'}, 'hors limites'),
    ('/comptabilite/dividendes-2777', {'mois': '2026-06', 'dispensed': '-1'}, 'négatif'),
    ('/comptabilite/profil-fiscal', {'sh_name_1': 'X', 'sh_pct_1': '10', 'sh_shares_1': '-5'}, 'parts invalide'),
])
def test_incoherent_amounts_and_dates_rejected(app, url, data, expected):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    snapshot = c.execute('SELECT COUNT(*) n FROM fiscal_settings').fetchone()['n']
    c.close()
    r = post(cl, url, data)
    assert r.status_code == 302
    msgs = ' '.join(flashes(cl))
    assert expected in msgs, msgs
    c = rt.tenant_cx_direct(oid)
    assert c.execute('SELECT COUNT(*) n FROM fiscal_settings').fetchone()['n'] == snapshot
    c.close()
    assert count(oid) == 0


def test_filing_date_must_follow_period_end(app):
    cl, oid = signup(app)
    decl = prepared(cl, oid)
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'a.pdf'), 'filed_on': '2025-06-30'}, content_type='multipart/form-data')
    assert any('précéder la fin de la période' in m for m in flashes(cl))
    assert status(oid, decl) == 'prepared'


# ------------------------------------------------------------------ 6. statuts
def test_unknown_partner_status_is_ignored(app):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)

    class Partner:
        name, production = 'partenaire', True
        answer = 'confirmed'

        def submit(self, payload, digest):
            return {'remote_id': 'P-1', 'status': 'deposited'}

        def fetch_status(self, remote_id, current, payload):
            return {'status': self.answer}

    decl = T.submit_declaration(c, built, None, 'x', connector=Partner())
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (decl,)).fetchone()
    for bad in ('confirmed', 'declared', 'filed', 'ACCEPTED', 'n importe quoi'):
        Partner.answer = bad
        assert T.refresh_status(c, row, connector=Partner()) == 'deposited'
    assert c.execute('SELECT status FROM teledeclarations WHERE id=?', (decl,)).fetchone()['status'] == 'deposited'
    c.close()


def test_manual_statuses_never_equal_dgfip_acceptance(app):
    cl, oid = signup(app)
    decl = prepared(cl, oid)
    post(cl, f'/comptabilite/teledeclarations/{decl}/justificatif',
         {'receipt': (io.BytesIO(PDF), 'a.pdf'), 'filed_on': '2026-05-02'}, content_type='multipart/form-data')
    post(cl, f'/comptabilite/teledeclarations/{decl}/confirmer', {'verified': '1', 'status': 'accepted'})
    assert status(oid, decl) == 'confirmed'
    page = cl.get(f'/comptabilite/teledeclarations/{decl}').get_data(as_text=True)
    assert "Acceptée par l" not in page and "ne signifie que l'administration a accepté" in page
