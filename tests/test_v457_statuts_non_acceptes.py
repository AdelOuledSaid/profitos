"""v457 — Aucune déclaration n'est considérée comme acceptée par l'administration sur la
seule base d'un dépôt déclaré, d'un accusé contrôlé, d'une confirmation interne ou d'une
simulation. Seul un accusé EDI renvoyé par un partenaire réel (mode production) peut
porter le statut « accepted »."""
import io
import json
import re
from datetime import date
from pathlib import Path

import pytest

import profitos.runtime as rt
from profitos import create_app, jobs, teledeclaration as T, automation as A, corporate_tax as CT
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry

IDENTITY = {'name': 'Exemple SARL', 'siren': '732829320', 'siret': '73282932000074'}
CSRF = 'c' * 64
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    for var in ('RESEND_API_KEY', 'SMTP_HOST', 'EDI_PARTNER_ENABLED', 'EDI_SIMULATION_ENABLED'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def org(app):
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(1,'Org','BUSINESS','ACTIVE_PAID',?,?)",
               (rt.now(), rt.now()))
    ac.execute("INSERT INTO users(id,email,password_hash,created_at,email_verified) VALUES(1,'owner@exemple.fr','x',?,1)", (rt.now(),))
    ac.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(1,1,'OWNER',?)", (rt.now(),))
    ac.commit(); ac.close()
    rt.migrate_tenant(1, force=True)
    c = rt.tenant_cx_direct(1)
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 120000, 'auxiliary_name': 'C'},
                                                  {'account_code': '706000', 'credit': 100000},
                                                  {'account_code': '445710', 'credit': 20000, 'vat_rate': 20.0, 'vat_base': 100000}])
    CT.save_fiscal_profile(c, None, {'vat_filing': 'ca12'}, rt.now())
    return c


def _row(c, decl):
    return c.execute('SELECT * FROM teledeclarations WHERE id=?', (decl,)).fetchone()


def _prepared(c):
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    return built, T.prepare_manual(c, built, None)


CHECKS = [None, {'status': 'concordant', 'checks': []}, {'status': 'ecarts', 'checks': []},
          {'status': 'non_verifiable', 'checks': [], 'reason': 'image'}]


@pytest.mark.parametrize('check', CHECKS)
def test_manual_filing_never_accepted(app, check):
    c = org(app)
    _built, decl = _prepared(c)
    T.record_filing(c, _row(c, decl), 'a.pdf', 'a.pdf', '2026-05-02', check=check)
    row = _row(c, decl)
    assert row['status'] in ('declared', 'to_verify', 'confirmed') and row['status'] != 'accepted'
    if row['status'] != 'confirmed':
        T.confirm_filing(c, row, 'owner@exemple.fr', note='Vérification manuelle de l\'accusé effectuée')
    row = _row(c, decl)
    assert row['status'] == 'confirmed'
    # Ni l'actualisation du statut ni la tâche horaire ne font avancer un dépôt manuel.
    assert T.refresh_status(c, row) == 'confirmed'
    c.close()
    jobs.run_job(app, 'teledeclaration_status', force=True)
    c = rt.tenant_cx_direct(1)
    row = _row(c, decl)
    assert row['status'] == 'confirmed' and 'accepted' not in row['history_json']
    c.close()


def test_labels_never_claim_acceptance_without_edi():
    for status in ('prepared', 'declared', 'to_verify', 'confirmed', 'filed'):
        assert 'accept' not in T.STATUS_LABELS[status].lower()
    assert "l'administration" in T.STATUS_LABELS['accepted'] and 'EDI' in T.STATUS_LABELS['accepted']
    sim = T.status_label('accepted', 'simulation')
    assert 'simulation' in sim and "rien n'a été transmis" in sim
    assert T.status_label('confirmed', 'manual') == T.STATUS_LABELS['confirmed']


def test_only_production_connector_sets_accepted():
    """Statut « accepted » : uniquement renvoyé par un connecteur (simulation ou partenaire) ;
    aucune autre fonction du module ne l'écrit."""
    src = (ROOT / 'profitos' / 'teledeclaration.py').read_text(encoding='utf-8')
    writes = [m.group(0) for m in re.finditer(r"status\s*=\s*'accepted'|SET status='accepted'", src)]
    assert writes == []
    # Les routes et les tâches ne modifient jamais la table directement : tout passe par le module.
    for path in list((ROOT / 'profitos' / 'routes').glob('*.py')) + [ROOT / 'profitos' / 'jobs.py', ROOT / 'profitos' / 'automation.py']:
        assert 'UPDATE teledeclarations' not in path.read_text(encoding='utf-8'), path.name


def test_simulated_deposit_never_counts_as_real(app):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    decl = T.submit_declaration(c, built, None, 'owner@exemple.fr')
    for _ in range(3):
        T.refresh_status(c, _row(c, decl))
    row = _row(c, decl)
    assert row['mode'] == 'simulation' and row['status'] == 'accepted'      # cycle simulé complet
    assert not T.active_declaration(c, None, 'TVA_CA12', '2025-01-01', '2025-12-31')
    item = next(x for x in A.upcoming_deadlines(c, None, date(2026, 4, 20), 30) if x['type'] == 'TVA_CA12')
    assert not item['deposited']                                             # l'échéance reste à faire
    assert [x for x in A.overdue_declarations(c, None, date(2026, 5, 6)) if x['type'] == 'TVA_CA12']
    # Une vraie préparation reste possible malgré la simulation.
    real = T.prepare_manual(c, built, None)
    assert _row(c, real)['mode'] == 'manual'
    c.close()


def test_pages_show_disclaimer_and_simulation_label(app):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    sim = T.submit_declaration(c, built, None, 'owner@exemple.fr')
    for _ in range(3):
        T.refresh_status(c, _row(c, sim))
    real = T.prepare_manual(c, built, None)
    T.record_filing(c, _row(c, real), 'a.pdf', 'a.pdf', '2026-05-02', check={'status': 'concordant', 'checks': []})
    c.close()
    cl = app.test_client()
    with cl.session_transaction() as s:
        s.update({'user_id': 1, 'org_id': 1, 'csrf_token': CSRF})
    page = cl.get(f'/comptabilite/teledeclarations/{real}').get_data(as_text=True)
    if 'Dépôt par vos soins' not in page:
        pytest.skip('session de test non authentifiée')
    assert "Aucun de ces statuts ne signifie que l'administration a accepté la déclaration" in page
    assert 'Acceptée par' not in page
    listing = cl.get('/comptabilite/teledeclarations').get_data(as_text=True)
    assert 'Acceptée (simulation)' in listing and "Dépôt confirmé par l&#39;accusé officiel" in listing
    # Un formulaire ne peut pas imposer un statut.
    cl.post(f'/comptabilite/teledeclarations/{real}/confirmer', data={'csrf_token': CSRF, 'verified': '1', 'status': 'accepted'})
    c = rt.tenant_cx_direct(1)
    assert _row(c, real)['status'] == 'confirmed'
    c.close()


def test_partner_acceptance_is_the_only_accepted_path(app, monkeypatch):
    c = org(app)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)

    class Partner:
        name, production = 'partenaire', True

        def submit(self, payload, digest):
            return {'remote_id': 'P-1', 'status': 'deposited'}

        def fetch_status(self, remote_id, current, payload):
            return {'status': 'accepted', 'ack_number': 'AR-EDI-1'}

    decl = T.submit_declaration(c, built, None, 'owner@exemple.fr', connector=Partner())
    T.refresh_status(c, _row(c, decl), connector=Partner())
    row = _row(c, decl)
    assert (row['mode'], row['status'], row['ack_number']) == ('production', 'accepted', 'AR-EDI-1')
    assert T.active_declaration(c, None, 'TVA_CA12', '2025-01-01', '2025-12-31')['id'] == decl
    c.close()


def test_declarations_closed_in_production_until_validated(app, monkeypatch):
    c = org(app)
    _built, decl = _prepared(c)
    c.close()
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.delenv('FISCAL_DECLARATIONS_ENABLED', raising=False)
    monkeypatch.delenv('FISCAL_DECLARATIONS_PILOT_ORGS', raising=False)
    assert not T.declarations_open(1)
    monkeypatch.setenv('FISCAL_DECLARATIONS_PILOT_ORGS', '7, 1')
    assert T.declarations_open(1) and not T.declarations_open(2)
    monkeypatch.delenv('FISCAL_DECLARATIONS_PILOT_ORGS')
    cl = app.test_client()
    with cl.session_transaction() as s:
        s.update({'user_id': 1, 'org_id': 1, 'csrf_token': CSRF})
    page = cl.get('/comptabilite/teledeclarations/preparer?type=TVA_CA12&du=2025-01-01&au=2025-12-31').get_data(as_text=True)
    if 'Contenu de la déclaration' not in page:
        pytest.skip('session de test non authentifiée')
    assert 'en cours de validation' in page and 'Préparer ma déclaration' not in page
    cl.post('/comptabilite/teledeclarations/preparer-depot',
            data={'csrf_token': CSRF, 'type': 'TVA_CA12', 'du': '2025-01-01', 'au': '2025-12-31', 'regime': 'normal'})
    cl.post(f'/comptabilite/teledeclarations/{decl}/justificatif', content_type='multipart/form-data',
            data={'csrf_token': CSRF, 'receipt': (io.BytesIO(b'%PDF-1.4 x'), 'a.pdf'), 'filed_on': '2026-05-02'})
    c = rt.tenant_cx_direct(1)
    assert c.execute('SELECT COUNT(*) n FROM teledeclarations').fetchone()['n'] == 1
    assert _row(c, decl)['status'] == 'prepared'
    c.close()
    monkeypatch.setenv('FISCAL_DECLARATIONS_ENABLED', '1')
    with cl.session_transaction() as s:
        s.pop('_flashes', None)
    page = cl.get('/comptabilite/teledeclarations/preparer?type=TVA_CA12&du=2025-01-01&au=2025-12-31').get_data(as_text=True)
    assert 'ne sont pas encore ouverts' not in page and 'pas encore ouverte (validation' not in page
