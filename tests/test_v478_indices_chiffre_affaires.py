"""v478 — Indices de chiffre d'affaires de l'INSEE (base 100 en 2021) dans la veille :
choix selon le code APE, distinction série brute / CVS-CJO, garde-fous de provenance,
exclusion de la révision de prix et du suivi des marges."""
import sqlite3

import pytest

import profitos.runtime as rt
from profitos import market_watch as MW
from test_v469_veille_secteur import app, flashes, post, signup  # noqa: F401

IDBANKS = {'CA_COMMERCE_GROS': '010770449', 'CA_HABILLEMENT': '010770751', 'CA_HOTELS': '010774374',
           'CA_HEBERGEMENT': '010775052', 'CA_RESTAURATION': '010774499', 'CA_RESTAURATION_RAPIDE': '010774600',
           'CA_SERVICES_ADMIN': '010774925', 'CA_CONSEIL': '010774564', 'CA_TRADUCTION': '010775001',
           'CA_IMPRIMERIE': '010773632'}


def sdmx(title, idbank='010774600', obs=(('2026-08', 112.4), ('2026-07', 118.0), ('2025-08', 108.0))):
    rows = ''.join(f'<Obs TIME_PERIOD="{p}" OBS_VALUE="{v}" OBS_STATUS="A"/>' for p, v in obs)
    return (f'<?xml version="1.0" encoding="UTF-8"?><message:StructureSpecificData '
            f'xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"><message:DataSet>'
            f'<Series IDBANK="{idbank}" FREQ="M" TITLE_FR="{title}" LAST_UPDATE="2026-09-30">{rows}</Series>'
            f'</message:DataSet></message:StructureSpecificData>').encode()


RAPIDE_BRUT = ("Indice de chiffre d'affaires - Restauration rapide (NAF rév. 2, niv. sous-classe poste 56.10C) - "
               "Base 100 en 2021 - Série brute")
RAPIDE_CVS = ("Indice de chiffre d’affaires - Restauration rapide (NAF rév. 2, niv. sous-classe poste 56.10C) - "
              "Base 100 en 2021 - Série CVS-CJO")


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / 'm.db')
    conn.row_factory = sqlite3.Row
    return conn


def test_catalogue_des_dix_series():
    for code, idbank in IDBANKS.items():
        d = MW.definition(code)
        assert d['idbank'] == idbank and d['kind'] == 'activite' and d['base'] == '2021' and d['freq'] == 'M'
        assert not MW.is_price_index(d)
    assert '010542378' not in {d.get('idbank') for d in MW.CATALOGUE.values()}
    assert MW.REPLACED_SERIES['010542378'] == '010770449'
    assert MW.is_price_index(MW.definition('BT01')) and not MW.is_price_index(MW.definition('DFR'))


@pytest.mark.parametrize('naf, expected', [
    ('5610C', ['CA_RESTAURATION_RAPIDE']), ('56.10A', ['CA_RESTAURATION']), ('5630Z', ['CA_RESTAURATION']),
    ('4771Z', ['CA_HABILLEMENT']), ('4711D', []), ('4690Z', ['CA_COMMERCE_GROS']), ('55.10Z', ['CA_HOTELS']),
    ('5520Z', ['CA_HEBERGEMENT']), ('5530Z', []), ('7022Z', ['CA_CONSEIL']), ('7430Z', ['CA_TRADUCTION']),
    ('1812Z', ['CA_IMPRIMERIE']), ('1820Z', []), ('8211Z', ['CA_SERVICES_ADMIN']), ('7711A', ['CA_SERVICES_ADMIN']),
    ('4321A', []),
])
def test_indice_le_plus_precis_selon_le_code_ape(naf, expected):
    assert MW.activity_series(naf) == expected


def test_indicateurs_suivis():
    s = dict(MW.DEFAULT_SETTINGS, sector='restauration', naf='5610C')
    assert MW.series_for(s) == ['CA_RESTAURATION_RAPIDE'] + MW.COMMON
    assert MW.series_for(dict(s, naf='')) == ['CA_RESTAURATION'] + MW.COMMON
    assert MW.series_for(dict(s, sector='industrie', naf='1812Z')) == ['CA_IMPRIMERIE'] + MW.COMMON
    assert MW.series_for(dict(s, sector='batiment', naf='')) == ['BT01', 'TP01', 'ICC'] + MW.COMMON


def test_correction_lue_dans_le_titre():
    assert MW.detect_adjustment(RAPIDE_BRUT) == 'BRUT'
    assert MW.detect_adjustment(RAPIDE_CVS) == 'CVS-CJO'
    assert MW.detect_adjustment('Indice ... - Série CVS') == 'CVS'
    assert MW.detect_adjustment('Indice ... - Série corrigée CJO') == 'CJO'
    assert MW.detect_adjustment("Index bâtiment - Tous corps d'état (BT01)") is None


def test_garde_fous_de_provenance():
    rapide = MW.definition('CA_RESTAURATION_RAPIDE')
    d = MW.fetch(rapide, lambda u, p: (200, sdmx(RAPIDE_CVS)))           # apostrophe typographique acceptée
    assert d['adjustment'] == 'CVS-CJO' and d['adjustment_source'] == 'titre' and d['naf_check'] == 'titre'
    assert d['obs'][-1] == ('2026-08', 112.4)
    assert MW.fetch(rapide, lambda u, p: (200, sdmx(RAPIDE_BRUT.replace('56.10C', '5610C'))))['adjustment'] == 'BRUT'
    # Mauvais poste NAF (restauration au lieu de restauration rapide) : rejeté.
    with pytest.raises(MW.MarketWatchError, match='à vérifier'):
        MW.fetch(rapide, lambda u, p: (200, sdmx(RAPIDE_CVS.replace('sous-classe poste 56.10C', 'division poste 56'))))
    # Indice de prix renvoyé à la place d'un indice de chiffre d'affaires : rejeté.
    with pytest.raises(MW.MarketWatchError, match='à vérifier'):
        MW.fetch(rapide, lambda u, p: (200, sdmx('Indice des prix - Restauration rapide (poste 56.10C)')))
    # Série arrêtée (ancienne série du commerce de gros) : rejetée, avec la série de remplacement.
    old = dict(MW.definition('CA_COMMERCE_GROS'), idbank='010542378')
    with pytest.raises(MW.MarketWatchError, match='remplacée par 010770449'):
        MW.fetch(old, lambda u, p: (200, sdmx("Indice de chiffre d'affaires - Commerce de gros (NAF rév. 2, niv. "
                                                "division poste 46) - Série arrêtée", '010542378')))
    # Correction attendue imposée par le catalogue : une autre version est refusée.
    with pytest.raises(MW.MarketWatchError, match='BRUT au lieu de CVS-CJO'):
        MW.fetch(dict(rapide, adjustment='CVS-CJO'), lambda u, p: (200, sdmx(RAPIDE_BRUT)))
    # Section N : la lettre seule suffit, pas une autre section.
    n = MW.definition('CA_SERVICES_ADMIN')
    ok = MW.check_metadata(n, {'idbank': '010774925', 'title': "Indice de chiffre d'affaires - Activités de services "
                               "administratifs et de soutien (NAF rév. 2, niv. section poste N) - Série CVS-CJO"})
    assert ok['anomalies'] == [] and ok['naf_check'] == 'titre'
    bad = MW.check_metadata(n, {'idbank': '010774925', 'title': "Indice de chiffre d'affaires - Activités spécialisées "
                                "(NAF rév. 2, niv. section poste M)"})
    assert bad['anomalies'] == ["activité M dans l'intitulé au lieu de N"]


def test_brut_et_cvs_ne_se_melangent_pas(tmp_path):
    conn = db(tmp_path)
    MW.refresh(conn, ['CA_RESTAURATION_RAPIDE'], http=lambda u, p: (200, sdmx(RAPIDE_BRUT)))
    assert MW.series_meta(conn, 'CA_RESTAURATION_RAPIDE')['adjustment'] == 'BRUT'
    assert len(MW.observations(conn, 'CA_RESTAURATION_RAPIDE')) == 3
    # La source passe en CVS-CJO : l'historique brut est écarté, rien n'est mélangé.
    MW.refresh(conn, ['CA_RESTAURATION_RAPIDE'],
               http=lambda u, p: (200, sdmx(RAPIDE_CVS, obs=(('2026-08', 115.0), ('2026-07', 114.1)))))
    assert MW.series_meta(conn, 'CA_RESTAURATION_RAPIDE')['adjustment'] == 'CVS-CJO'
    assert MW.observations(conn, 'CA_RESTAURATION_RAPIDE') == [('2026-07', 114.1), ('2026-08', 115.0)]


def test_colonne_ajoutee_aux_bases_existantes(tmp_path):
    conn = db(tmp_path)
    conn.execute('CREATE TABLE market_series(code TEXT PRIMARY KEY, title TEXT, freq TEXT, last_update TEXT, '
                 'fetched_at TEXT, status TEXT, error TEXT)')
    conn.execute("INSERT INTO market_series(code,status) VALUES('BT01','ok')")
    conn.commit()
    assert MW.series_meta(conn, 'BT01')['adjustment'] is None


def test_serie_brute_comparee_sur_un_an():
    obs = [('2025-08', 108.0), ('2026-07', 118.0), ('2026-08', 112.4)]
    defn = MW.definition('CA_RESTAURATION_RAPIDE')
    raw = MW.series_stats(defn, obs, {'adjustment': 'brut'})
    assert raw['change_1p'] is None and raw['change_12m'] == 4.07 and raw['raw_seasonal']
    cvs = MW.series_stats(defn, obs, {'adjustment': 'CVS-CJO'})
    assert cvs['change_1p'] == -4.75 and not cvs['raw_seasonal']
    # Correction inconnue : prudence, comme une série brute.
    assert MW.series_stats(defn, obs, {})['change_1p'] is None
    # Les indices de prix ne sont pas concernés.
    assert MW.series_stats(MW.definition('BT01'), obs, {})['change_1p'] == -4.75
    alerts = MW.alerts(raw, {'alert_12m': 3, 'alert_1p': 2})
    assert alerts == ['+4,07 % sur un an']


def fake_fetch(defn, http=None):
    if defn['code'] == 'CA_RESTAURATION_RAPIDE':
        return {'title': RAPIDE_BRUT, 'freq': 'M', 'last_update': '2026-09-30', 'adjustment': 'brut',
                'obs': [('2025-08', 108.0), ('2026-07', 118.0), ('2026-08', 112.4)]}
    if defn['code'] == 'CA_RESTAURATION':
        return {'title': "Indice de chiffre d'affaires - Restauration (poste 56) - Série CVS-CJO", 'freq': 'M',
                'last_update': '2026-09-30', 'adjustment': 'CVS-CJO',
                'obs': [('2025-08', 100.0), ('2026-07', 104.0), ('2026-08', 105.0)]}
    return {'title': f'Série {defn["code"]}', 'freq': defn.get('freq'), 'last_update': None, 'adjustment': None,
            'obs': [('2025-08', 2.0), ('2026-08', 2.0)] if defn['unit'] == '%' else [('2025-08', 100.0), ('2026-08', 101.0)]}


def test_veille_restaurant_rapide(app, monkeypatch):
    monkeypatch.setattr(MW, 'fetch', fake_fetch)
    cl, org = signup(app)
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'naf': '5610C', 'alert_12m': '3', 'alert_1p': '2'})
    html = cl.get('/veille').get_data(as_text=True)
    assert 'Chiffre d&#39;affaires — restauration rapide' in html
    assert "Indice d'activité · base 100 en 2021 · <strong>Brute</strong>" in html
    assert 'comparez sur un an' in html and '+4,07' in html
    assert 'Activité de votre secteur' in html and 'À surveiller' not in html
    page = cl.get('/veille/indicateur/CA_RESTAURATION_RAPIDE').get_data(as_text=True)
    assert 'ne sert pas à réviser un contrat' in page and 'brute (non corrigée' in page
    assert 'Révision de prix</a>' not in page
    # Ni la révision de prix ni le suivi des marges ne proposent un indice d'activité.
    rev = cl.get('/veille/revision?code=CA_RESTAURATION_RAPIDE').get_data(as_text=True)
    assert 'value="CA_RESTAURATION_RAPIDE"' not in rev
    r = post(cl, '/veille/revision/facture', {'code': 'CA_RESTAURATION_RAPIDE', 'amount': '1000', 'base': '2025-08',
                                               'new': '2026-08', 'invoice_id': '1'})
    assert r.status_code == 302 and any('Indice inconnu' in m for m in flashes(cl))
    marges = cl.get('/margin-watch')
    assert marges.status_code == 200 and 'CA_RESTAURATION_RAPIDE' not in marges.get_data(as_text=True)
    # Sans code APE : l'indice de la restauration (CVS-CJO), avec la variation mensuelle.
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'naf': '', 'alert_12m': '3', 'alert_1p': '2'})
    html = cl.get('/veille').get_data(as_text=True)
    assert 'Chiffre d&#39;affaires — restauration</a>' in html and '<strong>CVS-CJO</strong>' in html
    ac = rt.auth_cx()
    assert MW.series_meta(ac, 'CA_RESTAURATION')['adjustment'] == 'CVS-CJO'
    ac.close()
