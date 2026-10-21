"""Veille économique adaptée au secteur de chaque entreprise (v469).

Pas de « bourse » : des indicateurs officiels qui pèsent sur les prix, les coûts et les
contrats d'une PME, choisis selon son secteur, plus ceux qu'elle ajoute elle-même :

- index et indices de l'INSEE (BT / TP pour le bâtiment, prix à la consommation, loyers
  commerciaux et tertiaires, coût de la construction…) par le service SDMX de la Banque de
  données macroéconomiques (bdm.insee.fr, gratuit, sans clé) ;
- taux de la Banque centrale européenne (taux de dépôt, Euribor 3 mois) par son API
  publique (data-api.ecb.europa.eu) ;
- toute autre série INSEE ajoutée par l'entreprise avec son identifiant (idbank).

Garde-fou : chaque série du catalogue porte les mots attendus dans son titre officiel.
Si le titre renvoyé par la source ne correspond pas (identifiant erroné, série arrêtée ou
remplacée), les valeurs sont écartées et la série signalée « à vérifier » : ProfitOS
n'affiche jamais un chiffre dont la provenance n'est pas certaine.

Les données publiques sont partagées entre les organisations (base d'authentification) ;
le secteur, les indicateurs suivis et les seuils d'alerte de chaque entreprise sont
enregistrés dans sa propre base. Les montants affichés sont informatifs : ProfitOS ne
donne aucun conseil d'investissement.
"""
import csv
import io
import json
import math
import re
from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET

INSEE_SDMX = 'https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{idbank}'
INSEE_SERIES_PAGE = 'https://www.insee.fr/fr/statistiques/serie/{idbank}'
ECB_DATA = 'https://data-api.ecb.europa.eu/service/data/{flow}/{key}'
ECB_SERIES_PAGE = 'https://data.ecb.europa.eu/data/datasets/{flow}/{flow}.{key}'
TIMEOUT = 15
HISTORY = 40            # observations conservées par lecture (plus de trois ans en mensuel)

# Catalogue : code -> définition. 'expect' = mots qui doivent figurer dans le titre officiel.
CATALOGUE = {
    'IPC': {'source': 'insee', 'idbank': '011814630', 'label': 'Prix à la consommation (inflation)',
            'expect': ['consommation'], 'unit': 'indice', 'freq': 'M',
            'why': "Hausse générale des prix : salaires, tarifs, clauses d'indexation simples."},
    'ILC': {'source': 'insee', 'idbank': '001532540', 'label': 'Indice des loyers commerciaux (ILC)',
            'expect': ['loyers commerciaux'], 'unit': 'indice', 'freq': 'Q',
            'why': 'Révision annuelle du loyer des locaux commerciaux et artisanaux.'},
    'ILAT': {'source': 'insee', 'idbank': '001617112', 'label': 'Indice des loyers des activités tertiaires (ILAT)',
             'expect': ['activités tertiaires'], 'unit': 'indice', 'freq': 'Q',
             'why': 'Révision du loyer des bureaux et locaux d\'activité.'},
    'DFR': {'source': 'ecb', 'flow': 'FM', 'key': 'D.U2.EUR.4F.KR.DFR.LEV', 'label': 'Taux de dépôt de la BCE',
            'expect': ['deposit'], 'unit': '%', 'freq': 'D',
            'why': 'Oriente le coût des crédits et la rémunération de la trésorerie.'},
    'EURIBOR3M': {'source': 'ecb', 'flow': 'FM', 'key': 'M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA',
                  'label': 'Euribor 3 mois (moyenne mensuelle)', 'expect': ['euribor'], 'unit': '%', 'freq': 'M',
                  'why': 'Référence des crédits à taux variable et des découverts.'},
    'BT01': {'source': 'insee', 'idbank': '001710986', 'label': 'Index bâtiment tous corps d\'état (BT01)',
             'expect': ['BT01'], 'unit': 'indice', 'freq': 'M',
             'why': 'Révision des prix des marchés de bâtiment (situations de travaux).'},
    'TP01': {'source': 'insee', 'idbank': '001711007', 'label': 'Index général travaux publics (TP01)',
             'expect': ['TP01'], 'unit': 'indice', 'freq': 'M',
             'why': 'Révision des prix des marchés de travaux publics.'},
    'ICC': {'source': 'insee', 'idbank': '000008630', 'label': 'Indice du coût de la construction (ICC)',
            'expect': ['coût de la construction'], 'unit': 'indice', 'freq': 'Q',
            'why': 'Coût des bâtiments neufs ; ancien indice de révision des baux.'},
}
COMMON = ['IPC', 'ILC', 'ILAT', 'DFR', 'EURIBOR3M']

# Secteurs : sections de la nomenclature NAF, indicateurs proposés, conseils pour compléter.
SECTORS = {
    'batiment': {'label': 'Bâtiment et travaux publics', 'naf': 'F', 'series': ['BT01', 'TP01', 'ICC'],
                 'keywords': ['bâtiment', 'batiment', 'btp', 'travaux', 'construction', 'maçonnerie', 'maconnerie',
                              'plomberie', 'électricité générale', 'menuiserie', 'charpente', 'couverture', 'peinture',
                              'génie civil', 'terrassement', 'carrelage', 'chauffage'],
                 'add': "les index BT par métier (BT02 terrassement, BT03 maçonnerie, BT40 chauffage, BT47 électricité…) "
                        "et TP par type de travaux."},
    'industrie': {'label': 'Industrie et fabrication', 'naf': 'C', 'series': [],
                  'keywords': ['industrie', 'fabrication', 'usinage', 'métallurgie', 'plasturgie', 'production'],
                  'add': "les indices de prix de production de l'industrie (IPPI) de vos produits et de vos matières, "
                         "ainsi que les prix de l'énergie."},
    'commerce': {'label': 'Commerce', 'naf': 'G', 'series': [],
                 'keywords': ['commerce', 'boutique', 'magasin', 'négoce', 'negoce', 'vente', 'distribution', 'e-commerce'],
                 'add': "les indices de prix à la consommation de vos familles de produits."},
    'restauration': {'label': 'Hôtellerie et restauration', 'naf': 'I', 'series': [],
                     'keywords': ['restaurant', 'restauration', 'hôtel', 'hotel', 'café', 'traiteur', 'bar'],
                     'add': "les prix à la consommation de l'alimentation et de la restauration."},
    'transport': {'label': 'Transport et logistique', 'naf': 'H', 'series': [],
                  'keywords': ['transport', 'logistique', 'livraison', 'messagerie', 'taxi', 'déménagement'],
                  'add': "les prix des carburants ; les indices du Comité national routier peuvent être saisis à la main "
                         "dans le suivi des marges."},
    'services': {'label': 'Services, conseil, numérique', 'naf': 'J M N', 'series': [],
                 'keywords': ['conseil', 'informatique', 'logiciel', 'agence', 'services', 'développement', 'études',
                              'ingénierie', 'communication', 'formation'],
                 'add': "les indices du coût du travail de votre activité ; l'indice Syntec peut être saisi à la main "
                        "dans le suivi des marges."},
    'agriculture': {'label': 'Agriculture et agroalimentaire', 'naf': 'A', 'series': [],
                    'keywords': ['agricole', 'agriculture', 'élevage', 'viticulture', 'exploitation', 'maraîchage'],
                    'add': "l'indice des prix d'achat des moyens de production agricole (IPAMPA) et ses composantes."},
    'sante': {'label': 'Santé et action sociale', 'naf': 'Q', 'series': [],
              'keywords': ['santé', 'médical', 'cabinet médical', 'infirmier', 'pharmacie', 'soins'],
              'add': "les prix à la consommation des produits et services de santé."},
    'autre': {'label': 'Autre activité', 'naf': '', 'series': [], 'keywords': [],
              'add': "les séries INSEE liées à vos achats, vos ventes ou vos contrats."},
}
NAF_SECTION = {
    'A': 'agriculture', 'C': 'industrie', 'F': 'batiment', 'G': 'commerce', 'H': 'transport', 'I': 'restauration',
    'J': 'services', 'M': 'services', 'N': 'services', 'Q': 'sante',
}
# Divisions NAF (2 premiers chiffres) -> section.
_NAF_DIVISIONS = [(1, 3, 'A'), (5, 9, 'B'), (10, 33, 'C'), (35, 35, 'D'), (36, 39, 'E'), (41, 43, 'F'), (45, 47, 'G'),
                  (49, 53, 'H'), (55, 56, 'I'), (58, 63, 'J'), (64, 66, 'K'), (68, 68, 'L'), (69, 75, 'M'), (77, 82, 'N'),
                  (84, 84, 'O'), (85, 85, 'P'), (86, 88, 'Q'), (90, 93, 'R'), (94, 96, 'S')]

DEFAULT_SETTINGS = {'sector': None, 'naf': '', 'series': None, 'custom': [], 'alert_12m': 5.0, 'alert_1p': 2.0}
MAX_CUSTOM = 10


class MarketWatchError(Exception):
    pass


# ------------------------------------------------------------------- secteur
def sector_from_naf(naf):
    """Code APE / NAF (ex. 4120A, 43.22B) -> secteur ProfitOS, ou None."""
    digits = re.sub(r'\D', '', naf or '')[:2]
    if len(digits) < 2:
        return None
    div = int(digits)
    for lo, hi, section in _NAF_DIVISIONS:
        if lo <= div <= hi:
            return NAF_SECTION.get(section, 'autre')
    return None


def suggest_sector(*texts):
    """Secteur suggéré d'après les activités déclarées (profil de l'entreprise, activité
    de la 2065) : le secteur dont les mots-clés apparaissent le plus."""
    blob = ' '.join(t for t in texts if t).lower()
    if not blob.strip():
        return None
    best, score = None, 0
    for code, s in SECTORS.items():
        n = sum(1 for k in s['keywords'] if k in blob)
        if n > score:
            best, score = code, n
    return best


def series_for(settings):
    """Indicateurs suivis : choix de l'entreprise, sinon ceux de son secteur."""
    if settings.get('series') is not None:
        chosen = [c for c in settings['series'] if c in CATALOGUE]
    else:
        sector = SECTORS.get(settings.get('sector') or 'autre', SECTORS['autre'])
        chosen = sector['series'] + COMMON
    return chosen + [custom_code(c['idbank']) for c in settings.get('custom', [])]


def custom_code(idbank):
    return f'INSEE-{idbank}'


def parse_idbank(value):
    """Identifiant INSEE (9 chiffres) saisi tel quel ou dans l'adresse d'une page de série."""
    m = re.search(r'(?<!\d)(\d{9})(?!\d)', str(value or ''))
    if not m:
        raise MarketWatchError("Identifiant INSEE introuvable : collez l'identifiant à 9 chiffres de la série "
                               "(ou l'adresse de sa page sur insee.fr).")
    return m.group(1)


def definition(code, custom=None):
    if code in CATALOGUE:
        return dict(CATALOGUE[code], code=code)
    if code.startswith('INSEE-'):
        idbank = code[6:]
        label = next((c.get('label') for c in (custom or []) if c.get('idbank') == idbank), None)
        return {'code': code, 'source': 'insee', 'idbank': idbank, 'label': label or f'Série INSEE {idbank}',
                'expect': [], 'unit': 'indice', 'freq': None, 'why': 'Indicateur ajouté par votre entreprise.', 'custom': True}
    return None


def source_url(defn):
    if defn['source'] == 'insee':
        return INSEE_SERIES_PAGE.format(idbank=defn['idbank'])
    return ECB_SERIES_PAGE.format(flow=defn['flow'], key=defn['key'])


# -------------------------------------------------------------- lecture des sources
def _local(tag):
    return tag.rsplit('}', 1)[-1]


def parse_insee_sdmx(xml_bytes):
    """Réponse SDMX-ML de la BDM -> {'title', 'idbank', 'freq', 'last_update', 'obs': [(période, valeur)]}."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise MarketWatchError(f'réponse INSEE illisible ({exc})') from exc
    for el in root.iter():
        if _local(el.tag) == 'Series':
            obs = []
            for o in el:
                if _local(o.tag) != 'Obs':
                    continue
                period, raw = o.get('TIME_PERIOD'), o.get('OBS_VALUE')
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(value):
                    continue                     # valeur manquante (NaN, secret statistique)
                if period:
                    obs.append((period, value))
            return {'title': el.get('TITLE_FR') or el.get('TITLE_EN') or '', 'idbank': el.get('IDBANK') or '',
                    'freq': el.get('FREQ'), 'last_update': el.get('LAST_UPDATE'), 'obs': sorted(obs)}
    for el in root.iter():
        if _local(el.tag) in ('Error', 'ErrorMessage'):
            raise MarketWatchError('série inconnue à l\'INSEE')
    raise MarketWatchError('aucune série dans la réponse INSEE')


def parse_ecb_csv(text):
    """Réponse csvdata de l'API de la BCE -> même structure que parse_insee_sdmx."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise MarketWatchError('réponse BCE vide')
    obs = []
    for r in rows:
        try:
            value = float(r['OBS_VALUE'])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and r.get('TIME_PERIOD'):
            obs.append((r['TIME_PERIOD'], value))
    first = rows[0]
    return {'title': first.get('TITLE') or first.get('TITLE_COMPL') or '', 'idbank': first.get('KEY', ''),
            'freq': first.get('FREQ'), 'last_update': None, 'obs': sorted(obs)}


def title_matches(defn, title):
    if not defn.get('expect'):
        return True
    if not title:
        return defn['source'] == 'ecb'           # clé BCE sémantique : titre facultatif dans le CSV
    low = title.lower()
    return all(word.lower() in low for word in defn['expect'])


def fetch(defn, http=None):
    """Interroge la source officielle. http(url, params) -> (status, bytes) ; requests par défaut."""
    if http is None:
        import requests

        def http(url, params):
            r = requests.get(url, params=params, timeout=TIMEOUT, headers={'Accept-Encoding': 'gzip'})
            return r.status_code, r.content
    if defn['source'] == 'insee':
        status, body = http(INSEE_SDMX.format(idbank=defn['idbank']), {'lastNObservations': HISTORY})
        if status == 404:
            raise MarketWatchError('série inconnue à l\'INSEE')
        if status != 200:
            raise MarketWatchError(f'INSEE indisponible (HTTP {status})')
        data = parse_insee_sdmx(body)
    else:
        status, body = http(ECB_DATA.format(flow=defn['flow'], key=defn['key']),
                            {'lastNObservations': 800 if defn['freq'] == 'D' else HISTORY, 'format': 'csvdata'})
        if status != 200:
            raise MarketWatchError(f'BCE indisponible (HTTP {status})')
        data = parse_ecb_csv(body.decode('utf-8', 'replace'))
    if not data['obs']:
        raise MarketWatchError('aucune valeur publiée')
    if not title_matches(defn, data['title']):
        raise MarketWatchError(f"titre officiel inattendu (« {data['title'][:120]} ») : identifiant à vérifier")
    return data


# --------------------------------------------------------------------- stockage
def ensure_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS market_series(
        code TEXT PRIMARY KEY, title TEXT, freq TEXT, last_update TEXT, fetched_at TEXT,
        status TEXT, error TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS market_observations(
        code TEXT NOT NULL, period TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY(code, period))''')
    conn.commit()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def store(conn, code, data=None, error=None):
    """Enregistre une lecture réussie, ou l'erreur (les valeurs déjà connues restent)."""
    ensure_tables(conn)
    if data is not None:
        conn.execute("""INSERT INTO market_series(code,title,freq,last_update,fetched_at,status,error) VALUES(?,?,?,?,?,'ok',NULL)
                        ON CONFLICT(code) DO UPDATE SET title=excluded.title, freq=excluded.freq,
                        last_update=excluded.last_update, fetched_at=excluded.fetched_at, status='ok', error=NULL""",
                     (code, data['title'][:300], data.get('freq'), data.get('last_update'), _now()))
        for period, value in data['obs']:
            conn.execute("""INSERT INTO market_observations(code,period,value) VALUES(?,?,?)
                            ON CONFLICT(code,period) DO UPDATE SET value=excluded.value""", (code, period, value))
    else:
        status = 'mismatch' if error and 'à vérifier' in error else 'error'
        conn.execute("""INSERT INTO market_series(code,fetched_at,status,error) VALUES(?,?,?,?)
                        ON CONFLICT(code) DO UPDATE SET fetched_at=excluded.fetched_at, status=excluded.status,
                        error=excluded.error""", (code, _now(), status, (error or '')[:300]))
        if status == 'mismatch':
            # Provenance incertaine : on ne garde aucune valeur.
            conn.execute('DELETE FROM market_observations WHERE code=?', (code,))
    conn.commit()


def refresh(conn, codes, http=None, custom=None):
    """Met à jour les séries demandées. Retourne {code: 'ok' | message d'erreur}."""
    out = {}
    for code in codes:
        defn = definition(code, custom)
        if not defn:
            continue
        try:
            data = fetch(defn, http)
        except Exception as exc:                 # réseau, format, titre inattendu
            msg = str(exc) if isinstance(exc, MarketWatchError) else f'source injoignable ({type(exc).__name__})'
            store(conn, code, error=msg)
            out[code] = msg
        else:
            store(conn, code, data)
            out[code] = 'ok'
    return out


def observations(conn, code):
    ensure_tables(conn)
    return [(r['period'], float(r['value'])) for r in conn.execute(
        'SELECT period, value FROM market_observations WHERE code=? ORDER BY period', (code,)).fetchall()]


def series_meta(conn, code):
    ensure_tables(conn)
    row = conn.execute('SELECT * FROM market_series WHERE code=?', (code,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------- calculs
def _period_date(period):
    """'2026-01' / '2026-Q1' / '2026-01-15' / '2026' -> date (début de période)."""
    p = str(period)
    m = re.fullmatch(r'(\d{4})-Q([1-4])', p)
    if m:
        return date(int(m.group(1)), (int(m.group(2)) - 1) * 3 + 1, 1)
    m = re.fullmatch(r'(\d{4})-(\d{2})(?:-(\d{2}))?', p)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    if re.fullmatch(r'\d{4}', p):
        return date(int(p), 1, 1)
    raise ValueError(p)


def period_label(period):
    p = str(period)
    months = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre',
              'novembre', 'décembre']
    m = re.fullmatch(r'(\d{4})-Q([1-4])', p)
    if m:
        return f"{m.group(2)}{'er' if m.group(2) == '1' else 'e'} trimestre {m.group(1)}"
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', p)
    if m:
        return f"{int(m.group(3))} {months[int(m.group(2)) - 1]} {m.group(1)}"
    m = re.fullmatch(r'(\d{4})-(\d{2})', p)
    if m:
        return f"{months[int(m.group(2)) - 1]} {m.group(1)}"
    return p


def _value_at_or_before(obs, target):
    best = None
    for period, value in obs:
        if _period_date(period) <= target:
            best = (period, value)
    return best


def stats(obs, unit='indice'):
    """Dernière valeur, variation sur la période précédente et sur un an. Pour un taux (%),
    les variations sont en points ; pour un indice, en pourcentage."""
    if not obs:
        return None
    last_p, last_v = obs[-1]
    d = _period_date(last_p)
    prev = obs[-2] if len(obs) > 1 else None
    year = _value_at_or_before(obs, date(d.year - 1, d.month, min(d.day, 28)))

    def change(old):
        if not old:
            return None
        if unit == '%':
            return round(last_v - old[1], 2)
        if not old[1]:
            return None
        return round((last_v - old[1]) / old[1] * 100, 2)
    return {'period': last_p, 'label': period_label(last_p), 'value': last_v,
            'change_1p': change(prev), 'change_12m': change(year) if year and year[0] != last_p else None,
            'points': unit == '%'}


RATE_ALERT_12M, RATE_ALERT_1P = 0.5, 0.25    # taux : seuils en points


def alerts(stat, settings):
    """Variations au-delà des seuils : ceux de l'entreprise pour les indices (en %),
    0,5 point sur un an et 0,25 point sur une période pour les taux."""
    out = []
    if not stat:
        return out
    if stat['points']:
        t12, t1, unit = RATE_ALERT_12M, RATE_ALERT_1P, 'point'
    else:
        t12, t1, unit = float(settings.get('alert_12m') or 0), float(settings.get('alert_1p') or 0), '%'

    def fmt(x):
        return (f"{'+' if x > 0 else ''}{x:.2f}".replace('.', ',') + (' %' if unit == '%' else ' pt'))
    if stat['change_12m'] is not None and t12 and abs(stat['change_12m']) >= t12:
        out.append(f"{fmt(stat['change_12m'])} sur un an")
    if stat['change_1p'] is not None and t1 and abs(stat['change_1p']) >= t1:
        out.append(f"{fmt(stat['change_1p'])} sur la dernière période")
    return out


def sparkline(obs, width=220, height=44, pad=3):
    """Points d'une courbe SVG (chaîne pour l'attribut points), sur les 24 dernières valeurs."""
    pts = obs[-24:]
    if len(pts) < 2:
        return ''
    vals = [v for _p, v in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / (len(pts) - 1)
    return ' '.join(f'{pad + i * step:.1f},{height - pad - (v - lo) / span * (height - 2 * pad):.1f}'
                    for i, v in enumerate(vals))


def revision(amount, base_value, new_value, fixed_part_pct=15.0, decimals=4):
    """Révision de prix : P = P0 × (a + b × I / I0), a = partie fixe, b = 1 − a. Le
    coefficient est arrondi selon le contrat (3 ou 4 décimales le plus souvent)."""
    amount, base_value, new_value = float(amount), float(base_value), float(new_value)
    a = float(fixed_part_pct) / 100
    if not 0 <= a < 1:
        raise MarketWatchError('La partie fixe doit être comprise entre 0 et 99 %.')
    if base_value <= 0 or new_value <= 0:
        raise MarketWatchError("Les valeurs de l'indice doivent être positives.")
    coef = a + (1 - a) * new_value / base_value
    if decimals is not None:
        coef = round(coef, int(decimals))
    revised = round(amount * coef, 2)
    return {'coefficient': coef, 'revised': revised, 'difference': round(revised - amount, 2),
            'fixed': a, 'variable': round(1 - a, 4)}


# ------------------------------------------------------- réglages de l'entreprise
STORE_KEY = 'market-watch'


def load_settings(conn, entity_id=None):
    from profitos.tiers_declarations import load_store
    data = dict(DEFAULT_SETTINGS)
    data.update(load_store(conn, entity_id, STORE_KEY) or {})
    data['custom'] = [c for c in data.get('custom') or [] if isinstance(c, dict) and re.fullmatch(r'\d{9}', str(c.get('idbank', '')))]
    return data


def save_settings(conn, entity_id, data, now_iso, user=None):
    from profitos.tiers_declarations import save_store
    clean = {k: data.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    if clean['sector'] not in SECTORS:
        clean['sector'] = None
    if clean['series'] is not None:
        clean['series'] = [c for c in clean['series'] if c in CATALOGUE]
    clean['custom'] = clean['custom'][:MAX_CUSTOM]
    save_store(conn, entity_id, STORE_KEY, clean, now_iso, user)
    return clean


def all_followed_codes(settings_list):
    """Union des séries suivies par toutes les organisations (pour la tâche quotidienne)."""
    codes, custom = [], []
    for s in settings_list:
        for c in series_for(s):
            if c not in codes:
                codes.append(c)
        custom += s.get('custom', [])
    return codes, custom


# ----------------------------------------------------------------- actualités
# Flux choisis par l'éditeur (variable MARKET_NEWS_FEEDS, liste JSON), uniquement des
# sources dont la licence autorise la reprise des titres avec un lien :
#   [{"url": "https://…/rss.xml", "source": "Nom", "sectors": ["batiment"]}]  ("all" : tous)
# Seuls le titre, la date et le lien sont conservés ; l'article reste sur le site source.
MAX_NEWS = 300


def configured_feeds(env=None):
    import os
    env = os.environ if env is None else env
    raw = (env.get('MARKET_NEWS_FEEDS') or '').strip()
    if not raw:
        return []
    try:
        feeds = json.loads(raw)
    except ValueError:
        return []
    out = []
    for f in feeds if isinstance(feeds, list) else []:
        if not isinstance(f, dict):
            continue
        url = str(f.get('url') or '')
        if not url.startswith('https://'):
            continue
        sectors = [x for x in (f.get('sectors') or ['all']) if x == 'all' or x in SECTORS]
        out.append({'url': url, 'source': str(f.get('source') or url.split('/')[2])[:80], 'sectors': sectors or ['all']})
    return out


def parse_feed(xml_bytes):
    """RSS 2.0 ou Atom -> [{'title', 'url', 'published'}] (texte brut, liens http(s) seulement)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        if _local(el.tag) not in ('item', 'entry'):
            continue
        title = url = published = ''
        for child in el:
            cn = _local(child.tag)
            if cn == 'title':
                title = ''.join(child.itertext()).strip()
            elif cn == 'link':
                url = (child.get('href') or (child.text or '')).strip() or url
            elif cn in ('pubDate', 'published', 'updated', 'date') and not published:
                published = (child.text or '').strip()
        if title and url.startswith(('https://', 'http://')):
            items.append({'title': re.sub(r'\s+', ' ', title)[:300], 'url': url[:500], 'published': _iso_date(published)})
    return items


def _iso_date(text):
    from email.utils import parsedate_to_datetime
    if not text:
        return ''
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r'(\d{4}-\d{2}-\d{2})', text)
    return m.group(1) if m else ''


def ensure_news_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS market_news(
        url TEXT PRIMARY KEY, title TEXT, source TEXT, sectors TEXT, published TEXT, fetched_at TEXT)""")
    conn.commit()


def refresh_news(conn, feeds, http=None):
    if http is None:
        import requests

        def http(url, params):
            r = requests.get(url, params=params, timeout=TIMEOUT)
            return r.status_code, r.content
    ensure_news_table(conn)
    report = {}
    for f in feeds:
        try:
            status, body = http(f['url'], None)
        except Exception as exc:
            report[f['source']] = type(exc).__name__
            continue
        if status != 200:
            report[f['source']] = f'HTTP {status}'
            continue
        items = parse_feed(body)
        report[f['source']] = len(items)
        for it in items[:50]:
            conn.execute("""INSERT INTO market_news(url,title,source,sectors,published,fetched_at) VALUES(?,?,?,?,?,?)
                            ON CONFLICT(url) DO UPDATE SET title=excluded.title, sectors=excluded.sectors""",
                         (it['url'], it['title'], f['source'], ','.join(f['sectors']), it['published'] or None, _now()))
    old = conn.execute('SELECT url FROM market_news ORDER BY COALESCE(published, fetched_at) DESC').fetchall()[MAX_NEWS:]
    for r in old:
        conn.execute('DELETE FROM market_news WHERE url=?', (r['url'],))
    conn.commit()
    return report


def news_for(conn, sector, limit=8):
    """Titres des flux destinés à tous ou au secteur, ou dont le titre contient un mot-clé du secteur."""
    ensure_news_table(conn)
    rows = conn.execute('SELECT * FROM market_news ORDER BY COALESCE(published, fetched_at) DESC').fetchall()
    keys = SECTORS.get(sector or 'autre', SECTORS['autre'])['keywords']
    out = []
    for r in rows:
        tags = (r['sectors'] or 'all').split(',')
        title = (r['title'] or '').lower()
        if 'all' in tags or (sector and sector in tags) or any(k in title for k in keys):
            out.append(dict(r))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------- suivi des marges (v473)
VEILLE_PREFIX = 'VEILLE:'


def index_readings(conn, index_name):
    """Relevés de l'indice de référence du suivi des marges : ceux de la veille (mis à jour
    automatiquement) pour « VEILLE:<code> », sinon les relevés saisis à la main. Format
    commun : [{'id', 'reading_date' (AAAA-MM-JJ, début de période), 'value'}], chronologique."""
    if str(index_name or '').startswith(VEILLE_PREFIX):
        from profitos.runtime import auth_cx
        code = index_name[len(VEILLE_PREFIX):]
        ac = auth_cx()
        try:
            obs = observations(ac, code)
        finally:
            ac.close()
        return [{'id': f'{code}:{p}', 'reading_date': _period_date(p).isoformat(), 'value': v, 'period': p}
                for p, v in obs]
    return [dict(r) for r in conn.execute('SELECT * FROM price_index_readings WHERE index_name=? ORDER BY reading_date ASC',
                                          (index_name,)).fetchall()]


def index_label(index_name, custom=None):
    if str(index_name or '').startswith(VEILLE_PREFIX):
        defn = definition(index_name[len(VEILLE_PREFIX):], custom)
        return defn['label'] if defn else index_name
    return index_name


def json_dumps(x):
    return json.dumps(x, ensure_ascii=False)
