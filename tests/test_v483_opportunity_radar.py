"""v483 — Opportunity Radar : secteur comparé sur un an (indice INSEE), clients dormants et en
croissance, dépendance, appels d'offres — des faits sourcés, sans promesse."""
import re
from datetime import date, timedelta

import profitos.runtime as rt
from profitos import market_watch as MW
from profitos import opportunity_radar as OR
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, post, signup  # noqa: F401

T = paris_today()


def ym(n):
    """Mois décalé de n mois par rapport au mois courant (AAAA-MM)."""
    return OR._shift(OR._month(T), n)


def sale(org, month, client, ht, siren=None, day=10):
    c = rt.tenant_cx_direct(org)
    c.execute("""INSERT INTO outgoing_invoices(invoice_number,client_name,client_siren,issue_date,due_date,subtotal,vat_amount,total,
                 status,created_at,entity_id) VALUES(?,?,?,?,?,?,?,?,'paid',?,NULL)""",
              (f'F-{month}-{client}-{ht}', client, siren, f'{month}-{day:02d}', f'{month}-{day:02d}', ht, ht * 0.2, ht * 1.2, rt.now()))
    c.commit(); c.close()


def sector(values, naf_check='titre', status_ok=True):
    ac = rt.auth_cx()
    if status_ok:
        MW.store(ac, 'CA_RESTAURATION_RAPIDE', {'title': "Indice de chiffre d'affaires - Restauration rapide (poste 56.10C)",
                                                'freq': 'M', 'last_update': '2026-09-30', 'adjustment': 'CVS-CJO',
                                                'adjustment_source': 'titre', 'naf_check': naf_check, 'obs': values})
    else:
        MW.store(ac, 'CA_RESTAURATION_RAPIDE', error='x — ' + MW.SUSPENDED)
    ac.close()


def text_of(cl):
    html = cl.get('/opportunites').get_data(as_text=True)
    return html, re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'").replace(' ', ' ').replace('\xa0', ' ')


def setup_restaurant(cl):
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'naf': '5610C', 'alert_12m': '5', 'alert_1p': '2'})
    flashes(cl)


def test_moins_vite_que_le_secteur(app):
    cl, org = signup(app)
    setup_restaurant(cl)
    # Secteur : +10 % sur un an (100 → 110) ; vous : +2 % (3 000 → 3 060 sur trois mois).
    sector([(ym(-n), 100.0) for n in range(13, 17)] + [(ym(-n), 110.0) for n in range(1, 5)])
    for n in (15, 14, 13):
        sale(org, ym(-n), 'Client A', 1000.0)
    for n in (3, 2, 1):
        sale(org, ym(-n), 'Client A', 1020.0)
    sale(org, ym(-17), 'Client A', 50.0)                       # historique : dès 17 mois
    html, text = text_of(cl)
    assert "Votre chiffre d'affaires progresse moins vite que votre secteur (+2,0 % contre +10,0 %)" in text
    assert 'Important' in text and "c'est un constat, pas une prévision" in text
    assert '3 060,00 € contre 3 000,00 €' in text and '110,0 contre 100,0' in text
    assert f"{OR._label(ym(-3))} à {OR._label(ym(-1))}" in text
    assert 'Activité de la série vérifiée — Vérifiée sur l\'intitulé officiel' in text
    assert "Voir l'indice du secteur" in text


def test_secteur_conditions(app):
    cl, org = signup(app)
    setup_restaurant(cl)
    values = [(ym(-n), 100.0) for n in range(13, 17)] + [(ym(-n), 101.0) for n in range(1, 5)]
    sector(values, naf_check='non vérifiée')
    for n in (15, 14, 13, 3, 2, 1):
        sale(org, ym(-n), 'Client A', 1000.0)
    sale(org, ym(-16), 'Client A', 10.0)
    _h, text = text_of(cl)
    assert "évolue comme votre secteur (+0,0 % contre +1,0 %)" in text
    assert 'Non vérifiée automatiquement' in text                          # « ? » : activité non vérifiée
    # Série suspendue : plus de comparaison.
    sector(values, status_ok=False)
    _h, text = text_of(cl)
    assert 'contre +1,0 %)' not in text
    # Historique trop court : pas de comparaison.
    cl2, org2 = signup(app)
    setup_restaurant(cl2)
    sector(values)
    for n in (3, 2, 1):
        sale(org2, ym(-n), 'Client A', 1000.0)
    _h, text = text_of(cl2)
    assert 'contre +1,0 %)' not in text and 'Rien à signaler' in text


def test_clients_dormants_croissance_dependance(app):
    cl, org = signup(app)
    sale(org, ym(-12), 'Ancien Client', 2500.0, '732829320')
    sale(org, ym(-9), 'ANCIEN CLIENT SARL', 500.0, '732829320')          # même SIREN : même client
    sale(org, ym(-10), 'Petit Client', 100.0)                            # sous le seuil
    sale(org, ym(-9), 'Grand Compte', 2000.0)
    sale(org, ym(-2), 'Grand Compte', 6000.0)
    sale(org, ym(-1), 'Autre', 1000.0)
    c = rt.tenant_cx_direct(org)
    c.execute("INSERT INTO invoicing_clients(name,siren,created_at,entity_id) VALUES('Ancien Client','732829320',?,NULL)", (rt.now(),))
    cid = c.execute('SELECT id FROM invoicing_clients').fetchone()['id']
    c.commit(); c.close()
    html, text = text_of(cl)
    assert "n'a rien commandé depuis le" in text and '3 000,00 € HT facturés' in text and 'Petit Client' not in text
    assert f'/facturation/clients/{cid}' in html
    assert "Grand Compte : +200,0 % d'achats sur les 6 derniers mois" in text
    assert "Grand Compte représente 83,3 % de votre chiffre d'affaires sur 12 mois" in text
    assert 'Simuler son impact' in text
    # Un client dormant qui recommande disparaît du radar (constat résolu).
    sale(org, OR._month(T), 'Ancien Client', 800.0, '732829320', day=1)
    _h, text = text_of(cl)
    assert "n'a rien commandé" not in text


def test_appels_offres_et_isolement(app):
    cl, org = signup(app)
    c = rt.tenant_cx_direct(org)
    for title, score, days in (('Réfection toiture école', 82, 5), ('Entretien voirie', 60, 40), ('Marché clos', 90, -3)):
        c.execute("""INSERT INTO opportunities(type,title,score,buyer,deadline,status,created_at,entity_id)
                     VALUES('GROW',?,?,'Mairie',?,'OPEN',?,NULL)""", (title, score, (T + timedelta(days=days)).isoformat(), rt.now()))
    c.commit(); c.close()
    html, text = text_of(cl)
    assert "2 appel(s) d'offres ouvert(s) correspondant à votre profil" in text and 'Marché clos' not in text
    assert 'Réfection toiture école' in text and '1 avec une date limite dans les 10 jours' in text and 'Important' in text
    cl2, _org2 = signup(app)
    _h, text2 = text_of(cl2)
    assert "appel(s) d'offres" not in text2
    # Menu : entrée dédiée, sans activer « Économies ».
    page = cl.get('/opportunites').get_data(as_text=True)
    assert 'class="active" aria-current="page">Opportunity Radar' in page
    assert 'class="active" aria-current="page">Économies' not in page


def test_controle_indisponible_isole(app, monkeypatch):
    cl, org = signup(app)
    monkeypatch.setattr(OR, 'detect_tenders', lambda c, e, t: 1 / 0)
    _h, text = text_of(cl)
    assert "Contrôle « appels d'offres » indisponible" in text
    # Mission Control ne solde pas le contrôle indisponible du radar.
    cl.get('/mission-control')
    c = rt.tenant_cx_direct(org)
    from profitos import verification as V
    row = [r for r in V.items(c, None) if r['module'] == OR.MODULE][0]
    c.close()
    assert row['status'] == V.OPEN
