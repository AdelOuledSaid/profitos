"""v469 — Veille de votre secteur : indicateurs officiels selon l'activité de chaque
entreprise, garde-fou sur la provenance, alertes, révision de prix, actualités."""
import uuid

import pytest

import profitos.runtime as rt
from profitos import create_app, market_watch as MW
from profitos.config import DevelopmentConfig

CSRF = 'c' * 64

SDMX = """<?xml version="1.0" encoding="UTF-8"?>
<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
  xmlns:ss="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/structurespecific">
 <message:Header><message:ID>x</message:ID></message:Header>
 <message:DataSet ss:dataScope="DataStructure">
  <Series IDBANK="001710986" FREQ="M" TITLE_FR="Index bâtiment - Tous corps d'état (BT01) - Base 2010" LAST_UPDATE="2026-03-13" UNIT_MEASURE="SO">
   <Obs TIME_PERIOD="2026-01" OBS_VALUE="134.7" OBS_STATUS="A"/>
   <Obs TIME_PERIOD="2025-12" OBS_VALUE="134.2" OBS_STATUS="A"/>
   <Obs TIME_PERIOD="2025-01" OBS_VALUE="130.0" OBS_STATUS="A"/>
   <Obs TIME_PERIOD="2024-12" OBS_VALUE="NaN" OBS_STATUS="O"/>
  </Series>
 </message:DataSet>
</message:StructureSpecificData>""".encode()

ECB_CSV = ("KEY,FREQ,REF_AREA,CURRENCY,PROVIDER_FM,INSTRUMENT_FM,PROVIDER_FM_ID,DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE\n"
           "FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,4F,KR,DFR,LEV,2025-06-10,2.25,A,ECB Deposit facility - date of changes (raw data) - Level\n"
           "FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,4F,KR,DFR,LEV,2025-10-20,2.0,A,ECB Deposit facility - date of changes (raw data) - Level\n"
           "FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,4F,KR,DFR,LEV,2026-10-20,1.5,A,ECB Deposit facility - date of changes (raw data) - Level\n")


def fake_data(code):
    """Séries fictives cohérentes avec le catalogue (titres officiels attendus)."""
    defn = MW.definition(code) or {}
    titles = {'IPC': 'Indice des prix à la consommation - Base 2025 - Ensemble', 'ILC': 'Indice des loyers commerciaux (ILC)',
              'ILAT': 'Indice des loyers des activités tertiaires', 'DFR': 'ECB Deposit facility', 'EURIBOR3M': 'Euribor 3-month',
              'BT01': "Index bâtiment - Tous corps d'état (BT01)", 'TP01': 'Index TP01 - Index général tous travaux',
              'ICC': 'Indice du coût de la construction'}
    if defn.get('unit') == '%':
        obs = [('2025-10', 3.0), ('2026-09', 2.2), ('2026-10', 2.0)]
    elif defn.get('freq') == 'Q':
        obs = [('2025-Q2', 130.0), ('2026-Q1', 134.0), ('2026-Q2', 135.0)]
    else:
        obs = [('2025-01', 130.0), ('2025-12', 134.2), ('2026-01', 140.0)]
    return {'title': titles.get(code, f'Série {code}'), 'freq': defn.get('freq'), 'last_update': '2026-03-13', 'obs': obs}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    monkeypatch.delenv('MARKET_NEWS_FEEDS', raising=False)
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


@pytest.fixture
def offline(monkeypatch):
    calls = []

    def fetch(defn, http=None):
        calls.append(defn['code'])
        if defn['code'] == 'INSEE-999999999':
            raise MW.MarketWatchError("série inconnue à l'INSEE")
        if defn['code'].startswith('INSEE-'):
            return {'title': 'Indice de prix de production - Béton prêt à l\'emploi', 'freq': 'M', 'last_update': None,
                    'obs': [('2026-07', 110.0), ('2026-08', 111.5)]}
        return fake_data(defn['code'])
    monkeypatch.setattr(MW, 'fetch', fetch)

    def no_file(wanted, download=None, today=None):
        raise MW.MarketWatchError('fichier de correspondance INSEE introuvable (hors ligne)')
    monkeypatch.setattr(MW, 'download_idbank_mapping', no_file)
    return calls


def post(cl, url, data=None):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data or {}, csrf_token=CSRF))


def flashes(cl):
    with cl.session_transaction() as s:
        return [m for _, m in s.pop('_flashes', [])]


def signup(app, activities=''):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'Alice', 'last_name': 'Martin',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                             'company_name': 'Exemple SAS'})
    with cl.session_transaction() as s:
        org, uid = s['org_id'], s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='BUSINESS',status='ACTIVE_PAID' WHERE id=?", (org,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit(); ac.close()
    if activities:
        c = rt.tenant_cx_direct(org)
        c.execute("INSERT INTO company(id,name,activities) VALUES(1,'Exemple SAS',?) ON CONFLICT(id) DO UPDATE SET activities=excluded.activities",
                  (activities,))
        c.commit(); c.close()
    flashes(cl)
    return cl, org


# ------------------------------------------------------------------- unitaires
def test_lecture_insee_et_bce():
    d = MW.parse_insee_sdmx(SDMX)
    assert d['title'].startswith('Index bâtiment') and d['idbank'] == '001710986' and d['last_update'] == '2026-03-13'
    assert d['obs'] == [('2025-01', 130.0), ('2025-12', 134.2), ('2026-01', 134.7)]      # NaN ignoré, ordre chronologique
    assert MW.title_matches(MW.definition('BT01'), d['title'])
    assert not MW.title_matches(MW.definition('TP01'), d['title'])
    e = MW.parse_ecb_csv(ECB_CSV)
    assert e['obs'][-1] == ('2026-10-20', 1.5) and MW.title_matches(MW.definition('DFR'), e['title'])
    with pytest.raises(MW.MarketWatchError):
        MW.parse_insee_sdmx(b'<html>erreur</html')


def test_fetch_controle_la_provenance():
    def http_ok(url, params):
        assert url == 'https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/001710986' and params['lastNObservations'] == MW.HISTORY
        return 200, SDMX
    assert MW.fetch(MW.definition('BT01'), http_ok)['obs'][-1] == ('2026-01', 134.7)
    # Même réponse pour l'identifiant du TP01 : titre inattendu, refusé.
    with pytest.raises(MW.MarketWatchError, match='à vérifier'):
        MW.fetch(MW.definition('TP01'), lambda u, p: (200, SDMX))
    with pytest.raises(MW.MarketWatchError, match='inconnue'):
        MW.fetch(MW.definition('BT01'), lambda u, p: (404, b''))
    seen = {}

    def http_ecb(url, params):
        seen.update(url=url, params=params)
        return 200, ECB_CSV.encode()
    assert MW.fetch(MW.definition('DFR'), http_ecb)['obs'][-1][1] == 1.5
    assert seen['url'] == 'https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV'
    assert seen['params']['format'] == 'csvdata'


def test_titre_inattendu_efface_les_valeurs(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'm.db')
    conn.row_factory = sqlite3.Row
    MW.refresh(conn, ['BT01'], http=lambda u, p: (200, SDMX))
    assert MW.observations(conn, 'BT01')[-1] == ('2026-01', 134.7)
    MW.refresh(conn, ['TP01'], http=lambda u, p: (200, SDMX))
    assert MW.series_meta(conn, 'TP01')['status'] == 'mismatch' and MW.observations(conn, 'TP01') == []
    # Panne réseau : l'erreur est notée, les valeurs déjà connues restent.

    def down(u, p):
        raise ConnectionError('injoignable')
    assert MW.refresh(conn, ['BT01'], http=down)['BT01'].startswith('source injoignable')
    assert MW.series_meta(conn, 'BT01')['status'] == 'error' and len(MW.observations(conn, 'BT01')) == 3


@pytest.mark.parametrize('naf, sector', [('4321A', 'batiment'), ('43.22B', 'batiment'), ('4120A', 'batiment'),
                                         ('5610A', 'restauration'), ('6201Z', 'services'), ('7022Z', 'services'),
                                         ('0111Z', 'agriculture'), ('2511Z', 'industrie'), ('4711D', 'commerce'),
                                         ('4941A', 'transport'), ('8621Z', 'sante'), ('6810Z', 'autre'), ('', None)])
def test_secteur_selon_le_code_ape(naf, sector):
    assert MW.sector_from_naf(naf) == sector


def test_secteur_suggere_et_indicateurs():
    assert MW.suggest_sector('Maçonnerie générale et gros œuvre de bâtiment') == 'batiment'
    assert MW.suggest_sector('Restaurant traditionnel') == 'restauration'
    assert MW.suggest_sector('Conseil en informatique et développement logiciel') == 'services'
    assert MW.suggest_sector('') is None
    s = dict(MW.DEFAULT_SETTINGS, sector='batiment')
    assert MW.series_for(s) == ['BT01', 'TP01', 'ICC'] + MW.COMMON
    assert MW.series_for(dict(s, sector='restauration')) == ['CA_RESTAURATION'] + MW.COMMON
    custom = dict(s, series=['IPC', 'INCONNU'], custom=[{'idbank': '010538820'}])
    assert MW.series_for(custom) == ['IPC', 'INSEE-010538820']
    assert MW.parse_idbank('https://www.insee.fr/fr/statistiques/serie/001710986') == '001710986'
    assert MW.parse_idbank(' 001710986 ') == '001710986'
    for bad in ('12345', 'abc', '0017109861'):
        with pytest.raises(MW.MarketWatchError):
            MW.parse_idbank(bad)


def test_variations_et_alertes():
    obs = [('2025-01', 130.0), ('2025-12', 134.2), ('2026-01', 140.0)]
    st = MW.stats(obs)
    assert st['label'] == 'janvier 2026' and st['change_1p'] == round((140 - 134.2) / 134.2 * 100, 2)
    assert st['change_12m'] == round(10 / 130 * 100, 2)                       # 7,69 %
    assert MW.alerts(st, MW.DEFAULT_SETTINGS) == ['+7,69 % sur un an', '+4,32 % sur la dernière période']
    assert MW.alerts(st, dict(MW.DEFAULT_SETTINGS, alert_12m=10, alert_1p=5)) == []
    q = MW.stats([('2025-Q2', 130.0), ('2026-Q1', 134.0), ('2026-Q2', 135.0)])
    assert q['label'] == '2e trimestre 2026' and q['change_12m'] == round(5 / 130 * 100, 2)
    rate = MW.stats([('2025-10-20', 2.0), ('2026-09-01', 1.75), ('2026-10-20', 1.5)], '%')
    assert rate['points'] and rate['change_12m'] == -0.5 and rate['change_1p'] == -0.25
    assert MW.alerts(rate, MW.DEFAULT_SETTINGS) == ['-0,50 pt sur un an', '-0,25 pt sur la dernière période']
    assert MW.stats([('2026-01', 1.0)])['change_12m'] is None and MW.stats([]) is None
    assert MW.sparkline(obs).count(',') == 3 and MW.sparkline(obs[:1]) == ''


def test_revision_de_prix():
    # 10 000 € × (0,15 + 0,85 × 134,7 / 130) = 10 000 × 1,030731 → coefficient arrondi à 4 décimales 1,0307.
    r = MW.revision(10000, 130.0, 134.7, 15, 4)
    assert r['coefficient'] == 1.0307 and r['revised'] == 10307.0 and r['difference'] == 307.0
    assert MW.revision(10000, 130.0, 134.7, 15, 3)['revised'] == 10310.0
    assert MW.revision(10000, 130.0, 134.7, 15, None)['revised'] == 10307.31
    assert MW.revision(10000, 130.0, 126.1, 12.5, 4)['revised'] == round(10000 * round(0.125 + 0.875 * 126.1 / 130, 4), 2)
    for bad in ((10000, 0, 134.7, 15), (10000, 130, 134.7, 100), (10000, 130, 134.7, -1)):
        with pytest.raises(MW.MarketWatchError):
            MW.revision(*bad)


def test_actualites_flux_officiels(tmp_path):
    import sqlite3
    env = {'MARKET_NEWS_FEEDS': '[{"url": "https://exemple.gouv.fr/rss.xml", "source": "Officiel", "sectors": ["batiment"]},'
                                ' {"url": "http://non-chiffre.example/rss"}, {"url": "https://tous.example/atom", "sectors": ["all", "inconnu"]}]'}
    feeds = MW.configured_feeds(env)
    assert [f['url'] for f in feeds] == ['https://exemple.gouv.fr/rss.xml', 'https://tous.example/atom']
    assert feeds[1]['sectors'] == ['all'] and MW.configured_feeds({'MARKET_NEWS_FEEDS': 'pas du json'}) == []
    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
           '<item><title>Index BT : publication de janvier</title><link>https://exemple.gouv.fr/a</link>'
           '<pubDate>Fri, 13 Mar 2026 12:00:00 +0100</pubDate></item>'
           '<item><title>Lien dangereux</title><link>javascript:alert(1)</link></item></channel></rss>').encode()
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Taux de la BCE</title>'
            b'<link href="https://tous.example/b"/><updated>2026-10-20T10:00:00Z</updated></entry></feed>')
    assert MW.parse_feed(rss) == [{'title': 'Index BT : publication de janvier', 'url': 'https://exemple.gouv.fr/a', 'published': '2026-03-13'}]
    conn = sqlite3.connect(tmp_path / 'n.db')
    conn.row_factory = sqlite3.Row
    report = MW.refresh_news(conn, feeds, http=lambda url, p: (200, rss if 'gouv' in url else atom))
    assert report == {'Officiel': 1, 'tous.example': 1}
    assert [n['title'] for n in MW.news_for(conn, 'batiment')] == ['Taux de la BCE', 'Index BT : publication de janvier']
    assert [n['title'] for n in MW.news_for(conn, 'restauration')] == ['Taux de la BCE']


# --------------------------------------------------------------------- écrans
def test_veille_adaptee_a_l_entreprise(app, offline):
    cl, org = signup(app, 'Maçonnerie générale, plomberie et rénovation de bâtiments')
    html = cl.get('/veille').get_data(as_text=True)
    assert 'Bâtiment et travaux publics' in html and 'Secteur proposé' in html
    assert 'En attente de la première mise à jour' in html
    post(cl, '/veille', {'action': 'refresh'})
    html = cl.get('/veille').get_data(as_text=True)
    assert set(offline) >= {'BT01', 'TP01', 'ICC', 'IPC', 'ILC', 'ILAT', 'DFR', 'EURIBOR3M'}
    assert 'Index bâtiment tous corps d&#39;état (BT01)' in html and '140,0' in html and 'janvier 2026' in html
    assert 'À surveiller' in html and '+7,69 % sur un an' in html
    assert 'ne constituent pas un conseil financier' in html
    # Actualiser deux fois de suite : pas de nouvel appel aux sources (10 minutes).
    n = len(offline)
    post(cl, '/veille', {'action': 'refresh'})
    assert len(offline) == n
    # Changement de secteur : les indicateurs suivent.
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'alert_12m': '5', 'alert_1p': '2'})
    html = cl.get('/veille').get_data(as_text=True)
    assert 'Hôtellerie et restauration' in html and 'Secteur proposé' not in html and '(BT01)</a>' not in html
    # Code APE seul : secteur déduit.
    post(cl, '/veille', {'action': 'settings', 'sector': '', 'naf': '4941A', 'alert_12m': '5', 'alert_1p': '2'})
    assert 'Transport et logistique' in cl.get('/veille').get_data(as_text=True)
    # Choix personnel des indicateurs.
    post(cl, '/veille', {'action': 'settings', 'sector': 'transport', 'custom_selection': '1', 'series': ['DFR', 'IPC'],
                         'alert_12m': '5', 'alert_1p': '2'})
    c = rt.tenant_cx_direct(org)
    assert MW.series_for(MW.load_settings(c)) == ['DFR', 'IPC']
    c.close()
    post(cl, '/veille', {'action': 'settings', 'alert_12m': '-3', 'alert_1p': '2'})
    assert any('invalides' in m for m in flashes(cl))


def test_indicateur_personnel_revision_et_isolement(app, offline):
    cl, org = signup(app, 'Fabrication de béton')
    post(cl, '/veille', {'action': 'add_custom', 'idbank': 'https://www.insee.fr/fr/statistiques/serie/010001234'})
    assert any('Indicateur ajouté' in m for m in flashes(cl))
    html = cl.get('/veille').get_data(as_text=True)
    assert 'Béton prêt à l&#39;emploi' in html and '111,5' in html
    post(cl, '/veille', {'action': 'add_custom', 'idbank': '010001234'})
    assert any('déjà suivi' in m for m in flashes(cl))
    post(cl, '/veille', {'action': 'add_custom', 'idbank': '999999999'})
    assert any('introuvable' in m for m in flashes(cl))
    post(cl, '/veille', {'action': 'add_custom', 'idbank': 'pas un identifiant'})
    assert any('9 chiffres' in m for m in flashes(cl))
    detail = cl.get('/veille/indicateur/INSEE-010001234')
    assert detail.status_code == 200 and 'août 2026' in detail.get_data(as_text=True)
    # Révision sur cet indice : 5 000 € × (0,15 + 0,85 × 111,5 / 110) = 5 000 × 1,0116 → 5 058,00 €.
    page = cl.get('/veille/revision?code=INSEE-010001234&amount=5000&base=2026-07&new=2026-08&fixed=15&decimals=4').get_data(as_text=True)
    assert '1,0116' in page and '5 058,00' in page.replace(' ', ' ').replace('\xa0', ' ')
    page = cl.get('/veille/revision?code=INSEE-010001234&amount=5000&base_value=100&new_value=110&fixed=0').get_data(as_text=True)
    assert '5 500,00' in page.replace(' ', ' ').replace('\xa0', ' ')
    assert 'Choisissez les deux' in cl.get('/veille/revision?code=INSEE-010001234&amount=5000&base=1999-01').get_data(as_text=True)
    # Une autre entreprise ne voit pas l'indicateur personnel de la première.
    other, _ = signup(app, 'Restaurant')
    assert other.get('/veille/indicateur/INSEE-010001234').status_code == 404
    assert other.get('/veille/indicateur/BT01').status_code == 200
    assert other.get('/veille/indicateur/INCONNU').status_code == 404
    # Retrait.
    post(cl, '/veille', {'action': 'remove_custom', 'idbank': '010001234'})
    assert 'Béton' not in cl.get('/veille').get_data(as_text=True)


def test_lecture_seule_et_formule(app, offline):
    cl, org = signup(app)
    ac = rt.auth_cx()
    ac.execute("UPDATE memberships SET role='LECTURE' WHERE organization_id=?", (org,))
    ac.commit(); ac.close()
    assert cl.get('/veille').status_code == 200
    post(cl, '/veille', {'action': 'add_custom', 'idbank': '010001234'})
    c = rt.tenant_cx_direct(org)
    assert MW.load_settings(c)['custom'] == []
    c.close()


def test_tache_quotidienne(app, offline, monkeypatch):
    cl, org = signup(app, 'Restaurant')
    post(cl, '/veille', {'action': 'add_custom', 'idbank': '010001234'})
    offline.clear()
    from profitos import jobs
    result = jobs.JOBS['market_watch_refresh'].func({'id': 0, 'name': 'global'}, {})
    assert 'INSEE-010001234' in offline and set(offline) >= {'BT01', 'TP01', 'ICC', 'IPC', 'DFR'}
    assert 'indicateur(s) à jour' in result


def test_comparaison_par_secteur_et_aides(app, offline):
    """v476 : la comparaison anonyme des délais de paiement porte sur le secteur quand au
    moins 5 autres participantes du même secteur existent ; sinon sur toutes."""
    cl, org = signup(app, 'Restaurant')
    page = cl.get('/veille').get_data(as_text=True)
    assert 'Aides et financements' in page and 'les-aides.fr' in page
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'alert_12m': '5', 'alert_1p': '2'})
    ac = rt.auth_cx()
    assert ac.execute('SELECT market_sector FROM organizations WHERE id=?', (org,)).fetchone()['market_sector'] == 'restauration'
    ac.execute('UPDATE organizations SET benchmark_opt_in=1 WHERE id=?', (org,))
    now = rt.now()
    for i, (sector, days) in enumerate([('restauration', 10)] * 5 + [('batiment', 60)] * 5):
        oid = 1000 + i
        ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at,benchmark_opt_in,market_sector) "
                   "VALUES(?,?,?,?,?,?,1,?)", (oid, f'Org {oid}', 'BUSINESS', 'ACTIVE_PAID', now, now, sector))
        ac.execute('INSERT INTO sector_dso_signals(organization_id,snapshot_date,avg_days_overdue,updated_at) VALUES(?,?,?,?)',
                   (oid, '2026-10-01', days, now))
    ac.commit(); ac.close()
    b = rt.sector_dso_benchmark(org)
    assert b['scope'] == 'sector' and b['avg_days'] == 10 and b['org_count'] == 5 and b['sector_label'] == 'Hôtellerie et restauration'
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET market_sector='commerce' WHERE id=?", (org,))      # secteur sans 5 pairs
    ac.commit(); ac.close()
    b = rt.sector_dso_benchmark(org)
    assert b['scope'] == 'all' and b['avg_days'] == 35 and b['org_count'] == 10
