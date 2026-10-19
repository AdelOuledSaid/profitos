"""v461 — Écart trouvé par le parcours complet (test_v461_parcours_complet) : une CA3 en
retard d'un jour n'était pas relancée, car la recherche des retards (63 jours en arrière)
ne considérait que les deux périodes de TVA voisines de son point de départ. Ces tests
rapides (SQLite) tournent en CI."""
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, automation as A, corporate_tax as CT, teledeclaration as T
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry
from profitos.vat_return import generate_liquidation_entry

IDENTITY = {'name': 'Exemple SAS', 'siren': '732829320', 'siret': '73282932000074', 'address': '1 rue Test',
            'postal_code': '75001', 'city': 'Paris'}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def tenant(app, vat_filing):
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(1,'Org','BUSINESS','ACTIVE_PAID',?,?)",
               (rt.now(), rt.now()))
    ac.commit(); ac.close()
    rt.migrate_tenant(1, force=True)
    c = rt.tenant_cx_direct(1)
    p = CT.get_fiscal_profile(c, None)
    p['vat_filing'] = vat_filing
    CT.save_fiscal_profile(c, None, p, rt.now())
    return c


def sale(c, day, ht):
    create_entry(c, 'VE', day, 'Ventes', [{'account_code': '411000', 'debit': ht * 1.2, 'auxiliary_name': 'C'},
                                          {'account_code': '706000', 'credit': ht},
                                          {'account_code': '445710', 'credit': ht * 0.2, 'vat_rate': 20.0, 'vat_base': ht}])


def late_ca3(c, today):
    return [(str(x['start']), x['days_late']) for x in A.overdue_declarations(c, None, today) if x['type'] == 'TVA_CA3']


@pytest.mark.parametrize('today, expected', [
    (date(2026, 9, 16), [('2026-07-01', 32), ('2026-08-01', 1)]),     # août : un jour de retard
    (date(2026, 9, 22), [('2026-07-01', 38), ('2026-08-01', 7)]),     # une semaine
    (date(2026, 10, 16), [('2026-07-01', 62), ('2026-08-01', 31), ('2026-09-01', 1)]),   # neuf semaines au plus
    (date(2026, 9, 15), [('2026-07-01', 31)]),                         # le jour même : pas encore en retard
])
def test_ca3_mensuelle_relancee_des_le_lendemain(app, today, expected):
    c = tenant(app, 'ca3_mensuelle')
    for day in ('2026-07-10', '2026-08-10', '2026-09-10'):
        sale(c, day, 10000)
    assert late_ca3(c, today) == expected
    c.close()


def test_ca3_justifiee_ou_trimestrielle(app):
    c = tenant(app, 'ca3_mensuelle')
    sale(c, '2026-08-10', 10000)
    generate_liquidation_entry(c, '2026-08-01', '2026-08-31')
    built = T.build_declaration(c, 'TVA_CA3', date(2026, 8, 1), date(2026, 8, 31), identity=IDENTITY)
    decl = T.prepare_manual(c, built, None)
    # Préparée sans justificatif : toujours relancée, avec la mention « préparée ».
    late = [x for x in A.overdue_declarations(c, None, date(2026, 9, 16)) if x['type'] == 'TVA_CA3']
    assert late and late[0]['days_late'] == 1
    row = c.execute('SELECT * FROM teledeclarations WHERE id=?', (decl,)).fetchone()
    T.record_filing(c, row, 'x.pdf', 'accuse.pdf', '2026-09-14')
    assert late_ca3(c, date(2026, 9, 16)) == []
    c.close()


def test_ca3_trimestrielle(app):
    c = tenant(app, 'ca3_trimestrielle')
    sale(c, '2026-08-10', 10000)
    # Troisième trimestre : échéance le 15 octobre.
    assert late_ca3(c, date(2026, 10, 16)) == [('2026-07-01', 1)]
    assert late_ca3(c, date(2026, 10, 15)) == []
    c.close()
