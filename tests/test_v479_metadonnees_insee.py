"""v479 — Fiabilité des indicateurs INSEE : l'identifiant à 9 chiffres est la référence,
les dimensions officielles (fichier « correspondance_idbank_dimension » + service SDMX)
priment sur l'intitulé, la correction a trois états (CVS-CJO, BRUT, INCONNU), un intitulé
formulé autrement ne bloque pas, des métadonnées contradictoires suspendent la série."""
import io
import json
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest

import profitos.runtime as rt
from profitos import market_watch as MW
from test_v469_veille_secteur import app, flashes, post, signup  # noqa: F401
from test_v478_indices_chiffre_affaires import IDBANKS, sdmx

# Poste NAF tel que l'INSEE pourrait le coder dans ses dimensions (tirets, sans point).
NAF_DIM = {'CA_COMMERCE_GROS': '46', 'CA_HABILLEMENT': '47-71', 'CA_HOTELS': '55-10', 'CA_HEBERGEMENT': '55-20',
           'CA_RESTAURATION': '56', 'CA_RESTAURATION_RAPIDE': '56-10C', 'CA_SERVICES_ADMIN': 'N',
           'CA_CONSEIL': '70-22', 'CA_TRADUCTION': '74-3', 'CA_IMPRIMERIE': '18-1'}
WRONG_NAF = {'CA_COMMERCE_GROS': '47', 'CA_HABILLEMENT': '47-19', 'CA_HOTELS': '55-20', 'CA_HEBERGEMENT': '55-10',
             'CA_RESTAURATION': '56-10C', 'CA_RESTAURATION_RAPIDE': '56', 'CA_SERVICES_ADMIN': 'M',
             'CA_CONSEIL': '70-21', 'CA_TRADUCTION': '74-1', 'CA_IMPRIMERIE': '18-2'}


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / 'm.db')
    conn.row_factory = sqlite3.Row
    return conn


def flow_sdmx(idbank, **dims):
    """Réponse SDMX « structure-specific » d'un flux : dimensions nommées en attributs."""
    attrs = ' '.join(f'{k}="{v}"' for k, v in dict(dims, IDBANK=idbank, FREQ='M', TITLE_FR='x').items())
    return (f'<?xml version="1.0"?><m:StructureSpecificData xmlns:m="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">'
            f'<m:DataSet><Series {attrs}><Obs TIME_PERIOD="2026-08" OBS_VALUE="101.2"/></Series></m:DataSet>'
            f'</m:StructureSpecificData>').encode()


def zip_bytes(rows, name='correspondance_idbank_dimension.csv'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(name, '﻿nomflow;idbank;cleFlow\n' + '\n'.join(';'.join(r) for r in rows) + '\n')
    return buf.getvalue()


# ----------------------------------------------------------- correction : trois états
def test_absence_de_mention_ne_vaut_pas_serie_brute():
    defn = MW.definition('CA_HOTELS')
    title = "Indice de chiffre d'affaires - Hôtels (NAF rév. 2, niv. sous-classe poste 55.10Z) - Base 100 en 2021"
    meta = MW.check_metadata(defn, {'idbank': '010774374', 'title': title})
    assert meta == {'adjustment': 'INCONNU', 'adjustment_source': None, 'naf_check': 'titre', 'anomalies': []}
    assert MW.detect_adjustment(title) is None
    assert MW.detect_adjustment('Indice … - Série brute') == 'BRUT'
    assert MW.detect_adjustment('Indice … - Données brutes') == 'BRUT'
    assert MW.detect_adjustment('Contribution brute d’exploitation') is None      # « brute » sans rapport
    assert [MW.normalize_adjustment(x) for x in ('CVS-CJO', 'brut', 'BRUT', 'cvs', 'CJO', 'NSA', '')] == \
        ['CVS-CJO', 'BRUT', 'BRUT', 'CVS', 'CJO', None, None]
    assert MW.ADJUSTMENT_SHORT['INCONNU'] == 'Correction non vérifiée' and MW.ADJUSTMENT_SHORT['BRUT'] == 'Brute'


def test_dimension_officielle_prioritaire():
    defn = MW.definition('CA_HOTELS')
    title = "Indice de chiffre d'affaires - Hôtels - Base 100 en 2021"
    meta = MW.check_metadata(defn, {'idbank': '010774374', 'title': title},
                             {'CORRECTION': 'CVS-CJO', 'NAF2': '55-10', 'BASIND': '2021'})
    assert meta == {'adjustment': 'CVS-CJO', 'adjustment_source': 'dimensions', 'naf_check': 'dimensions',
                    'anomalies': []}
    # Contradiction dimensions / intitulé : anomalie.
    meta = MW.check_metadata(defn, {'idbank': '010774374', 'title': title + ' - Série brute'}, {'CORRECTION': 'CVS-CJO'})
    assert meta['anomalies'] == ["correction contradictoire (CVS-CJO dans les dimensions, BRUT dans l'intitulé)"]


@pytest.mark.parametrize('expected, value, ok', [
    ('56.10C', '56-10C', True), ('56.10C', '5610C', True), ('56.10C', '56.10', False), ('56', '56', True),
    ('47.71Z', '47-71', True), ('47.71Z', '4771Z', True), ('47.71Z', '47-19', False), ('N', 'N', True),
    ('N', 'A10-MN', False), ('74.3', '74-3', True), ('18.1', '18-12', False), ('46', '', False),
])
def test_comparaison_des_postes_naf(expected, value, ok):
    assert MW.naf_matches(expected, value) is ok


# ------------------------------------------------------- intitulé formulé autrement
def test_intitule_different_ne_bloque_pas():
    defn = MW.definition('CA_CONSEIL')
    # Ni code NAF ni « chiffre d'affaires » dans l'intitulé, pas de dimensions : affiché, signalé non vérifié.
    meta = MW.check_metadata(defn, {'idbank': '010774564', 'title': 'Indice de ventes - Conseil pour les affaires et '
                                                                     'autres conseils de gestion - Série CVS-CJO'})
    assert meta['anomalies'] == [] and meta['naf_check'] == 'non vérifiée' and meta['adjustment'] == 'CVS-CJO'
    # Même intitulé, dimensions officielles disponibles : activité vérifiée.
    meta = MW.check_metadata(defn, {'idbank': '010774564', 'title': 'Indice de ventes - Conseil de gestion'},
                             {'NAF2': '70-22', 'CORRECTION': 'BRUT'})
    assert meta['anomalies'] == [] and meta['naf_check'] == 'dimensions' and meta['adjustment'] == 'BRUT'


@pytest.mark.parametrize('data, dims, message', [
    ({'idbank': '010774565', 'title': "Indice de chiffre d'affaires"}, {}, 'renvoyé la série 010774565 au lieu de 010774564'),
    ({'idbank': '010774564', 'title': "Indice de chiffre d'affaires"}, {'NAF2': '70-21'},
     'activité 70-21 dans les dimensions officielles au lieu de 70.22Z'),
    ({'idbank': '010774564', 'title': "Indice de chiffre d'affaires - Base 100 en 2015"}, {}, "base 2015 dans l'intitulé"),
    ({'idbank': '010774564', 'title': "Indice de chiffre d'affaires"}, {'BASIND': '2015'}, 'base 2015 dans les dimensions'),
    ({'idbank': '010774564', 'title': 'Indice des prix de production - Conseil de gestion'}, {}, 'autre indicateur'),
    ({'idbank': '010774564', 'title': "Indice de chiffre d'affaires - Série arrêtée"}, {}, 'série arrêtée'),
    ({'idbank': '010774564', 'title': "Indice de chiffre d'affaires (poste 70.22Z)"}, {'NAF2': '70-21'}, '70-21'),
])
def test_metadonnees_contradictoires(data, dims, message):
    meta = MW.check_metadata(MW.definition('CA_CONSEIL'), data, dims)
    assert any(message in a for a in meta['anomalies']), meta['anomalies']


def test_suspension_efface_les_valeurs(tmp_path):
    conn = db(tmp_path)
    title = "Indice de chiffre d'affaires - Hôtels (poste 55.10Z) - Série CVS-CJO"
    MW.refresh(conn, ['CA_HOTELS'], http=lambda u, p: (200, sdmx(title, '010774374')))
    assert MW.series_meta(conn, 'CA_HOTELS')['status'] == 'ok' and MW.observations(conn, 'CA_HOTELS')
    # Les dimensions officielles disent BRUT : contradiction, indicateur suspendu sans valeur.
    MW.ensure_dimensions_table(conn)
    MW._store_dims(conn, '010774374', 'ok', 'ICA-FLOW', 'M.X', {'CORRECTION': 'BRUT'})
    report = MW.refresh(conn, ['CA_HOTELS'], http=lambda u, p: (200, sdmx(title, '010774374')))
    assert 'Métadonnées contradictoires' in report['CA_HOTELS'] and 'suspendu' in report['CA_HOTELS']
    assert MW.series_meta(conn, 'CA_HOTELS')['status'] == 'mismatch' and MW.observations(conn, 'CA_HOTELS') == []


def test_ancienne_valeur_enregistree_normalisee(tmp_path):
    conn = db(tmp_path)
    MW.ensure_tables(conn)
    conn.execute("INSERT INTO market_series(code,status,adjustment) VALUES('CA_HOTELS','ok','brut')")
    conn.commit()
    assert MW.series_meta(conn, 'CA_HOTELS')['adjustment'] == 'BRUT'


# ---------------------------------------------- fichier de correspondance et flux SDMX
def test_fichier_de_correspondance(tmp_path):
    rows = [('ICA-SERV-2021', '010774564', 'M.ICA.CVS-CJO.70-22.2021'), ('AUTRE', '001710986', 'M.BT01'),
            ('ICA-COM-2021', '10770449', 'M.ICA.BRUT.46.2021')]      # zéro de tête perdu : rétabli
    path = tmp_path / 'f.zip'
    path.write_bytes(zip_bytes(rows, '202610_correspondance_idbank_dimension.csv'))
    assert MW.read_idbank_zip(path, ['010774564', '010770449', '999999999']) == {
        '010774564': ('ICA-SERV-2021', 'M.ICA.CVS-CJO.70-22.2021'), '010770449': ('ICA-COM-2021', 'M.ICA.BRUT.46.2021')}
    urls = MW.idbank_file_urls(date(2026, 10, 26), env={})
    assert urls[0].endswith('/2862759/202610_correspondance_idbank_dimension.zip') and urls[1].startswith(
        'https://www.insee.fr/fr/statistiques/fichier/2862759/202609') and len(urls) == 13
    assert MW.idbank_file_urls(date(2026, 1, 5), env={'MARKET_INSEE_IDBANK_FILE': 'https://x/f.zip'})[:3] == [
        'https://x/f.zip', MW.IDBANK_FILE_URL.format(yyyymm='202601'), MW.IDBANK_FILE_URL.format(yyyymm='202512')]
    tried = []

    def download(url, p):
        tried.append(url)
        if '202610' in url:
            return 404                                       # fichier du mois pas encore publié
        open(p, 'wb').write(zip_bytes(rows))
        return 200
    mapping, url = MW.download_idbank_mapping(['010774564'], download, date(2026, 10, 26))
    assert mapping == {'010774564': ('ICA-SERV-2021', 'M.ICA.CVS-CJO.70-22.2021')} and '202609' in url and len(tried) == 2
    with pytest.raises(MW.MarketWatchError, match='introuvable'):
        MW.download_idbank_mapping(['010774564'], lambda u, p: 404, date(2026, 10, 26))


def test_dimensions_du_flux():
    seen = []

    def http(url, params):
        seen.append(url)
        return 200, flow_sdmx('010774564', CORRECTION='CVS-CJO', NAF2='70-22', BASIND='2021', NATURE='INDICE')
    dims = MW.fetch_dimensions('ICA-SERV-2021', 'M.ICA.CVS-CJO.70-22.2021', '010774564', http)
    assert dims == {'CORRECTION': 'CVS-CJO', 'NAF2': '70-22', 'BASIND': '2021', 'NATURE': 'INDICE'}
    assert seen == ['https://bdm.insee.fr/series/sdmx/data/ICA-SERV-2021/M.ICA.CVS-CJO.70-22.2021']
    with pytest.raises(MW.MarketWatchError, match='au lieu de 010774564'):
        MW.fetch_dimensions('F', 'K', '010774564', lambda u, p: (200, flow_sdmx('010000001', CORRECTION='BRUT')))


def test_mise_a_jour_mensuelle_des_dimensions(tmp_path):
    conn = db(tmp_path)
    rows = [('ICA-SERV-2021', '010774564', 'M.ICA.CVS-CJO.70-22.2021')]
    downloads = []

    def download(url, p):
        downloads.append(url)
        open(p, 'wb').write(zip_bytes(rows))
        return 200
    http = lambda u, p: (200, flow_sdmx('010774564', CORRECTION='CVS-CJO', NAF2='70-22'))  # noqa: E731
    report = MW.refresh_dimensions(conn, ['CA_CONSEIL', 'CA_HOTELS', 'DFR'], http=http, download=download)
    assert report == {'010774564': 'ok', '010774374': 'absent du fichier de correspondance'}
    stored = MW.stored_dimensions(conn, '010774564')
    assert stored['dimensions'] == {'CORRECTION': 'CVS-CJO', 'NAF2': '70-22'} and stored['dataflow'] == 'ICA-SERV-2021'
    # Moins d'un mois après : rien à refaire pour la série vérifiée ; l'absente sera retentée dans 7 jours.
    assert MW.refresh_dimensions(conn, ['CA_CONSEIL', 'CA_HOTELS'], http=http, download=download) == {}
    assert len(downloads) == 1
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert MW.dimensions_due(conn, '010774564', later) and not MW.dimensions_due(conn, '010774564')
    # Fichier injoignable : les dimensions déjà vérifiées sont conservées.
    MW.refresh_dimensions(conn, ['CA_CONSEIL'], download=lambda u, p: 503, force=True)
    assert MW.stored_dimensions(conn, '010774564')['status'] == 'ok'
    # La lecture des valeurs s'appuie sur ces dimensions : intitulé muet, correction confirmée.
    title = 'Indice de ventes - Conseil de gestion'
    assert MW.refresh(conn, ['CA_CONSEIL'], http=lambda u, p: (200, sdmx(title, '010774564')))['CA_CONSEIL'] == 'ok'
    meta = MW.series_meta(conn, 'CA_CONSEIL')
    assert (meta['adjustment'], meta['adjustment_source'], meta['naf_check']) == ('CVS-CJO', 'dimensions', 'dimensions')


# ------------------------------------------------------------ chacune des dix séries
@pytest.mark.parametrize('code', sorted(IDBANKS))
def test_chaque_serie_sectorielle(code, tmp_path):
    defn = MW.definition(code)
    idbank = IDBANKS[code]
    assert defn['idbank'] == idbank
    plain = f"Indice de chiffre d'affaires - {defn['label'].split('— ')[-1]} - Base 100 en 2021"
    good = {'CORRECTION': 'CVS-CJO', 'NAF2': NAF_DIM[code], 'BASIND': '2021'}
    # 1. Dimensions officielles cohérentes, intitulé sans code NAF : valide, vérifiée par les dimensions.
    meta = MW.check_metadata(defn, {'idbank': idbank, 'title': plain}, good)
    assert meta == {'adjustment': 'CVS-CJO', 'adjustment_source': 'dimensions', 'naf_check': 'dimensions',
                    'anomalies': []}, meta
    # 2. Série brute confirmée par les dimensions.
    assert MW.check_metadata(defn, {'idbank': idbank, 'title': plain}, dict(good, CORRECTION='BRUT'))['adjustment'] == 'BRUT'
    # 3. Sans dimensions ni mention : correction INCONNU, activité non vérifiée, pas de blocage.
    meta = MW.check_metadata(defn, {'idbank': idbank, 'title': plain})
    assert (meta['adjustment'], meta['naf_check'], meta['anomalies']) == ('INCONNU', 'non vérifiée', [])
    # 4. Mauvaise activité dans les dimensions : suspendue.
    assert MW.check_metadata(defn, {'idbank': idbank, 'title': plain}, dict(good, NAF2=WRONG_NAF[code]))['anomalies']
    # 5. Chaîne complète : lecture, enregistrement, affichage prudent tant que la correction est inconnue.
    conn = db(tmp_path)
    MW.refresh(conn, [code], http=lambda u, p: (200, sdmx(plain, idbank)))
    m = MW.series_meta(conn, code)
    assert m['status'] == 'ok' and m['adjustment'] == 'INCONNU'
    st = MW.series_stats(defn, MW.observations(conn, code), m)
    assert st['raw_seasonal'] and st['change_1p'] is None and st['change_12m'] is not None


# ------------------------------------------------------------------ écran et script
def fake_fetch(defn, http=None):
    title = "Indice de chiffre d'affaires - Restauration - Base 100 en 2021"
    return {'title': title, 'freq': 'M', 'last_update': '2026-09-30', 'adjustment': 'INCONNU',
            'adjustment_source': None, 'naf_check': 'non vérifiée',
            'obs': [('2025-08', 100.0), ('2026-07', 104.0), ('2026-08', 101.0)]} if defn['code'] == 'CA_RESTAURATION' else \
        {'title': 'x', 'freq': defn.get('freq'), 'last_update': None, 'adjustment': None,
         'obs': [('2025-08', 2.0), ('2026-08', 2.0)] if defn['unit'] == '%' else [('2025-08', 100.0), ('2026-08', 100.5)]}


def test_ecran_correction_non_verifiee(app, monkeypatch):
    monkeypatch.setattr(MW, 'fetch', fake_fetch)
    monkeypatch.setattr(MW, 'download_idbank_mapping', lambda *a, **k: (_ for _ in ()).throw(MW.MarketWatchError('hors ligne')))
    cl, org = signup(app)
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'alert_12m': '5', 'alert_1p': '2'})
    html = cl.get('/veille').get_data(as_text=True)
    assert '<strong>Correction non vérifiée</strong>' in html
    assert 'par prudence, seule la comparaison sur un an' in html and 'Activité non vérifiée automatiquement' in html
    page = cl.get('/veille/indicateur/CA_RESTAURATION').get_data(as_text=True)
    assert 'Identifiant INSEE (référence) : <strong>010774499</strong>' in page
    assert "dont la correction n&#39;a pas pu être vérifiée" in page and 'pas encore relues' in page
    # Dimensions relues : elles apparaissent sur la fiche.
    ac = rt.auth_cx()
    MW.ensure_dimensions_table(ac)
    MW._store_dims(ac, '010774499', 'ok', 'ICA-HR-2021', 'M.ICA.CVS-CJO.56.2021', {'CORRECTION': 'CVS-CJO', 'NAF2': '56'})
    ac.close()
    page = cl.get('/veille/indicateur/CA_RESTAURATION').get_data(as_text=True)
    assert 'flux ICA-HR-2021, clé M.ICA.CVS-CJO.56.2021' in page and 'CORRECTION = CVS-CJO, NAF2 = 56' in page


def test_tache_quotidienne_dimensions(app, monkeypatch):
    from profitos import jobs
    calls = []
    monkeypatch.setattr(MW, 'fetch', fake_fetch)
    monkeypatch.setattr(MW, 'refresh_dimensions', lambda conn, codes, custom=None, **k: calls.append(set(codes)) or
                        {'010774499': 'ok', '010774600': 'absent du fichier de correspondance'})
    signup(app)
    result = jobs.JOBS['market_watch_refresh'].func({'id': 0, 'name': 'global'}, {})
    assert calls and set(IDBANKS) <= calls[0] and 'dimensions INSEE : 1/2' in result


def test_script_de_controle_individuel(capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('chk', Path(__file__).resolve().parent.parent / 'scripts' / 'check_insee_series.py')
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    rows = [('ICA-HR-2021', '010774499', 'M.ICA.CVS-CJO.56.2021'), ('ICA-HR-2021', '010774600', 'M.ICA.BRUT.56-10C.2021')]

    def download(url, p):
        open(p, 'wb').write(zip_bytes(rows))
        return 200

    def http(url, params):
        if 'SERIES_BDM/010774499' in url:
            return 200, sdmx("Indice de chiffre d'affaires - Restauration", '010774499')
        if 'SERIES_BDM/010774600' in url:
            return 200, sdmx("Indice de chiffre d'affaires - Restauration rapide - Série CVS-CJO", '010774600')
        if '/ICA-HR-2021/M.ICA.CVS-CJO.56.2021' in url:
            return 200, flow_sdmx('010774499', CORRECTION='CVS-CJO', NAF2='56', BASIND='2021')
        if '/ICA-HR-2021/M.ICA.BRUT.56-10C.2021' in url:
            return 200, flow_sdmx('010774600', CORRECTION='BRUT', NAF2='56-10C', BASIND='2021')
        return 404, b''
    lines = []
    res = chk.check(['CA_RESTAURATION', 'CA_RESTAURATION_RAPIDE', 'CA_HOTELS'], http=http, download=download,
                    out=lines.append)
    by = {r['code']: r for r in res}
    assert by['CA_RESTAURATION']['status'] == 'ok' and by['CA_RESTAURATION']['adjustment'] == 'CVS-CJO'
    assert by['CA_RESTAURATION']['naf_check'] == 'dimensions'
    # Dimensions BRUT, intitulé CVS-CJO : suspendue.
    assert by['CA_RESTAURATION_RAPIDE']['status'] == 'suspendue' and 'contradictoire' in by['CA_RESTAURATION_RAPIDE']['error']
    assert by['CA_HOTELS']['status'] == 'erreur' and by['CA_HOTELS']['dims_error'] == 'absent du fichier de correspondance'
    text = '\n'.join(lines)
    assert 'flux ICA-HR-2021, clé M.ICA.CVS-CJO.56.2021' in text and 'correction : CVS-CJO (dimensions officielles)' in text
    assert chk.main(['INCONNU_X']) == 2
