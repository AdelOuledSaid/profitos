"""v464 — Clôture comptable de bout en bout, sur deux exercices, par les écrans de
ProfitOS : écritures, IS, à-nouveaux, verrouillage, export FEC contrôlé (A47 A-1),
affectation du résultat, exercice suivant déficitaire, et cohérence des liasses N / N+1.
Montants calculés à la main (docs/validation-fiscale.md, dossier I)."""
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, corporate_tax as CT
from profitos.config import DevelopmentConfig
from profitos.accounting import AccountingError, create_entry
from profitos.ledger import reverse_entry
from profitos.ledger import add_fiscal_year
from profitos.liasse_normal import liasse_normale
from fec_check import check_fec

CSRF = 'c' * 64
N0, N1 = (date(2024, 1, 1), date(2024, 12, 31)), (date(2025, 1, 1), date(2025, 12, 31))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
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


def signup(app):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'Alice', 'last_name': 'Martin',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                             'company_name': 'Exemple SAS'})
    with cl.session_transaction() as s:
        org_id, uid = s['org_id'], s['user_id']
    post(cl, '/company', {'name': 'Exemple SAS', 'legal_form': 'SAS', 'siret': '73282932000074', 'address': '1 rue Test',
                          'postal_code': '75001', 'city': 'Paris'})
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit(); ac.close()
    flashes(cl)
    return cl, org_id


def E(c, journal, day, label, *lines):
    return create_entry(c, journal, day, label, [dict(zip(('account_code', 'debit', 'credit', 'auxiliary_name'), l)) for l in lines])


def bal(c, prefix, journal=None, upto=None, since=None):
    sql = ("SELECT COALESCE(SUM(l.debit-l.credit),0) n FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id "
           "WHERE l.account_code LIKE ?")
    params = [prefix + '%']
    for cond, val in (('e.journal_code=?', journal), ('e.entry_date<=?', upto), ('e.entry_date>=?', since)):
        if val:
            sql += ' AND ' + cond
            params.append(str(val))
    return round(float(c.execute(sql, params).fetchone()['n']), 2)


def year_2024(c):
    E(c, 'BQ', '2024-01-02', 'Capital', ('512000', 20000, 0, None), ('101300', 0, 20000, None))
    E(c, 'VE', '2024-03-31', 'Ventes', ('411000', 120000, 0, 'Alpha'), ('706000', 0, 100000, None), ('445710', 0, 20000, None))
    E(c, 'BQ', '2024-05-31', 'Encaissement', ('512000', 96000, 0, None), ('411000', 0, 96000, 'Alpha'))
    E(c, 'AC', '2024-04-30', 'Achats', ('607000', 40000, 0, None), ('445660', 8000, 0, None), ('401000', 0, 48000, 'Grossiste'))
    E(c, 'BQ', '2024-06-30', 'Règlement', ('401000', 48000, 0, 'Grossiste'), ('512000', 0, 48000, None))
    E(c, 'OD', '2024-12-31', 'Salaires', ('641000', 20000, 0, None), ('421000', 0, 20000, None))
    E(c, 'BQ', '2024-12-31', 'Salaires payés', ('421000', 20000, 0, None), ('512000', 0, 20000, None))
    E(c, 'AC', '2024-01-15', 'Ordinateurs', ('218300', 6000, 0, None), ('445620', 1200, 0, None), ('404000', 0, 7200, 'Info'))
    E(c, 'BQ', '2024-01-31', 'Règlement ordinateurs', ('404000', 7200, 0, 'Info'), ('512000', 0, 7200, None))
    E(c, 'OD', '2024-12-31', 'Dotation', ('681100', 2000, 0, None), ('281830', 0, 2000, None))
    E(c, 'OD', '2024-12-31', 'Liquidation TVA', ('445710', 20000, 0, None), ('445660', 0, 8000, None),
      ('445620', 0, 1200, None), ('445510', 0, 10800, None))


def test_cycle_de_cloture_sur_deux_exercices(app):
    cl, org = signup(app)
    c = rt.tenant_cx_direct(org)
    add_fiscal_year(c, '2024-01-01', '2024-12-31')
    add_fiscal_year(c, '2025-01-01', '2025-12-31')
    year_2024(c)
    # Résultat avant impôt : 100 000 − 40 000 − 20 000 − 2 000 = 38 000 ; IS PME 15 % = 5 700.
    is_res = CT.compute_is(38000, *N0, 100000, True)
    assert is_res['is_total'] == 5700
    CT.generate_is_entry(c, N0[1], is_res['is_du'])
    l24 = liasse_normale(c, *N0)
    assert l24['balanced'] and l24['cr']['HN'] == 32300                    # 38 000 − 5 700
    closing_2024 = {r['account_code']: round(float(r['n']), 2) for r in c.execute(
        """SELECT l.account_code, SUM(l.debit-l.credit) n FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
           WHERE e.entry_date<='2024-12-31' AND substr(l.account_code,1,1) IN ('1','2','3','4','5')
           GROUP BY l.account_code""").fetchall() if round(float(r['n']), 2)}
    c.close()

    # À-nouveaux puis clôture, par l'écran de clôture.
    post(cl, '/comptabilite/cloture/a-nouveaux', {'exercice_start': '2024-01-01', 'exercice_end': '2024-12-31'})
    assert any('À-nouveaux générés' in m for m in flashes(cl))
    post(cl, '/comptabilite/cloture/a-nouveaux', {'exercice_start': '2024-01-01', 'exercice_end': '2024-12-31'})
    assert any('déjà été générés' in m for m in flashes(cl))              # une seule ouverture
    post(cl, '/comptabilite/cloture', {'closed_until': '2024-12-31'})
    assert any('clôturée jusqu' in m for m in flashes(cl))

    c = rt.tenant_cx_direct(org)
    # Les à-nouveaux reprennent exactement le bilan de clôture, résultat en 120000.
    an = {r['account_code']: round(float(r['n']), 2) for r in c.execute(
        """SELECT l.account_code, SUM(l.debit-l.credit) n FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
           WHERE e.journal_code='AN' AND e.entry_date='2025-01-01' GROUP BY l.account_code""").fetchall()}
    assert an.pop('120000') == -32300
    assert an == closing_2024
    assert bal(c, '512', journal='AN') == 20000 + 96000 - 48000 - 20000 - 7200    # 40 800
    assert bal(c, '444', journal='AN') == -5700 and bal(c, '445510', journal='AN') == -10800
    assert bal(c, '411', journal='AN') == 24000                            # client Alpha, tiers conservé
    assert c.execute("SELECT auxiliary_name FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id "
                     "WHERE e.journal_code='AN' AND l.account_code='411000'").fetchone()['auxiliary_name'] == 'Alpha'
    # Période close : ni saisie ni contrepassation datée dans l'exercice clos.
    with pytest.raises(AccountingError):
        E(c, 'OD', '2024-12-30', 'Tardive', ('606400', 10, 0, None), ('512000', 0, 10, None))
    first = c.execute("SELECT id FROM accounting_entries WHERE label='Ventes'").fetchone()['id']
    with pytest.raises(AccountingError):
        reverse_entry(c, first, '2024-12-31')
    c.close()

    # FEC 2024 : conforme, équilibré, numérotation continue par journal.
    r = post(cl, '/comptabilite/export-fec', {'date_from': '2024-01-01', 'date_to': '2024-12-31'})
    assert r.status_code == 200 and '732829320FEC20241231.txt' in r.headers['Content-Disposition']
    rep = check_fec(r.data)
    assert not rep['errors'], rep['errors']
    assert rep['entries'] == 12 and rep['total_debit'] == rep['total_credit']

    # Exercice 2025 : affectation du résultat (réserve légale 5 %, report à nouveau), puis perte.
    c = rt.tenant_cx_direct(org)
    E(c, 'OD', '2025-06-30', 'Affectation du résultat 2024', ('120000', 32300, 0, None), ('106100', 0, 1615, None),
      ('110000', 0, 30685, None))
    E(c, 'BQ', '2025-04-15', 'Solde IS 2024', ('444000', 5700, 0, None), ('512000', 0, 5700, None))
    E(c, 'BQ', '2025-01-20', 'TVA décembre', ('445510', 10800, 0, None), ('512000', 0, 10800, None))
    E(c, 'BQ', '2025-02-28', 'Encaissement solde Alpha', ('512000', 24000, 0, None), ('411000', 0, 24000, 'Alpha'))
    E(c, 'VE', '2025-09-30', 'Ventes 2025', ('411000', 24000, 0, 'Beta'), ('706000', 0, 20000, None), ('445710', 0, 4000, None))
    E(c, 'OD', '2025-12-31', 'Salaires 2025', ('641000', 30000, 0, None), ('421000', 0, 30000, None))
    E(c, 'BQ', '2025-12-31', 'Salaires payés', ('421000', 30000, 0, None), ('512000', 0, 30000, None))
    E(c, 'OD', '2025-12-31', 'Dotation', ('681100', 2000, 0, None), ('281830', 0, 2000, None))
    l25 = liasse_normale(c, *N1, previous=N0)
    # Perte 2025 : 20 000 − 30 000 − 2 000 = − 12 000.
    assert l25['balanced'] and l25['cr']['HN'] == -12000
    p = {x['box']: x['amount'] for x in l25['passif']}
    assert p['DA'] == 20000 and p['DD'] == 1615 and p['DH'] == 30685 and p['DI'] == -12000
    # Colonne N-1 de la liasse 2025 = liasse 2024.
    assert l25['previous']['totals_a']['CO_net'] == l24['totals_a']['CO_net']
    assert l25['previous']['cr']['HN'] == 32300
    c.close()
    post(cl, '/comptabilite/cloture/a-nouveaux', {'exercice_start': '2025-01-01', 'exercice_end': '2025-12-31'})
    assert any('À-nouveaux générés' in m for m in flashes(cl))
    c = rt.tenant_cx_direct(org)
    an26 = lambda prefix: bal(c, prefix, journal='AN', since='2026-01-01')
    assert an26('129') == 12000 and an26('120') == 0                       # perte en attente d'affectation
    assert an26('110') == -30685 and an26('1061') == -1615 and an26('2818') == -4000
    assert an26('512') == 40800 - 5700 - 10800 + 24000 - 30000             # 18 300
    c.close()
    # FEC 2025 : contient les à-nouveaux d'ouverture, toujours conforme.
    r = post(cl, '/comptabilite/export-fec', {'date_from': '2025-01-01', 'date_to': '2025-12-31'})
    rep = check_fec(r.data)
    assert not rep['errors'], rep['errors']
    assert any(line.startswith('AN|') and '|20250101|' in line for line in r.data.decode('utf-8').split('\r\n'))
