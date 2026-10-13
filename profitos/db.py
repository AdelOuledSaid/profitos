"""
db.py — Couche d'abstraction base de données pour ProfitOS.

Objectif : permettre à app.py de fonctionner SANS AUCUNE MODIFICATION de ses
requêtes SQL (placeholders `?`, `PRAGMA table_info`, `last_insert_rowid()`,
`INSERT OR IGNORE`, `AUTOINCREMENT`...) que l'on tourne en local sur SQLite
(par défaut, zéro configuration) ou en production sur PostgreSQL (dès que
DATABASE_URL est défini).

Multi-tenant :
  - SQLite (dev)   : un fichier par organisation dans tenant_data/org_<id>.db
                      (comportement inchangé par rapport à la V1.0 initiale).
  - PostgreSQL (prod) : une base unique, un schéma Postgres par organisation
                      (`org_<id>`), sélectionné via `SET search_path`.
    Les tables auth (organizations/users/memberships/activity_log) vivent
    dans le schéma `public`.

V1.2 : le backend PostgreSQL est la cible de production. La couche conserve
SQLite pour le développement local et PostgreSQL pour le cloud. Les scripts
de préflight et de migration fournis dans scripts/ permettent de valider la
connexion et d'initialiser les schémas avant le démarrage.
"""
import os
import re
import sqlite3

DATABASE_URL = os.environ.get('DATABASE_URL')  # ex: postgresql://user:pass@host:5432/profitos
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    # Certains fournisseurs exposent encore l'ancien préfixe. psycopg2 accepte
    # souvent les deux, mais on normalise pour éviter les différences d'environnement.
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]
PG_CONNECT_TIMEOUT = int(os.environ.get('PG_CONNECT_TIMEOUT', '10'))
PG_APPLICATION_NAME = os.environ.get('PG_APPLICATION_NAME', 'profitos')
# Garde-fou : si la valeur ressemble à un placeholder oublié plutôt qu'à une
# vraie URL Postgres, on retombe sur SQLite avec un avertissement clair au
# lieu de planter avec une erreur de connexion cryptique.
if DATABASE_URL and not DATABASE_URL.startswith(('postgres://', 'postgresql://')):
    print(f"[ProfitOS] ATTENTION : DATABASE_URL définie mais invalide ({DATABASE_URL[:30]}...) — retombe sur SQLite.")
    DATABASE_URL = None
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2  # noqa: à ajouter dans requirements.txt (psycopg2-binary) avant déploiement

# Violation de contrainte (doublon, clé unique...) quel que soit le backend. Attraper
# uniquement sqlite3.IntegrityError laissait passer l'erreur psycopg2 en production
# (erreur 500 au lieu du message prévu, ex. « Ce règlement a déjà été enregistré »).
IntegrityError = (sqlite3.IntegrityError,) + ((psycopg2.IntegrityError,) if USE_POSTGRES else ())


class Row(dict):
    """Ligne de résultat supportant à la fois l'accès par nom (row['col'], comme
    sqlite3.Row) et par position (row[0], utilisé par `SELECT last_insert_rowid()`).
    dict(row) fonctionne aussi nativement (sous-classe de dict)."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _sqlite_placeholders_to_pg(sql):
    """Convertit les `?` en `%s`, en ignorant les `?` à l'intérieur de littéraux
    entre quotes simples (aucun cas de ce type dans le code actuel, mais on
    reste prudent)."""
    out = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
        if ch == '?' and not in_string:
            out.append('%s')
        else:
            out.append(ch)
    return ''.join(out)


def _translate_scalar_minmax(sql):
    """SQLite autorise MAX(a,b)/MIN(a,b) comme fonctions scalaires à 2+ arguments
    (le plus grand/petit de plusieurs valeurs). PostgreSQL réserve MAX()/MIN() à
    l'agrégation sur plusieurs lignes ; l'équivalent scalaire est GREATEST()/LEAST().
    On ne renomme QUE les appels contenant une virgule au niveau racine des
    parenthèses (signe d'un usage scalaire multi-arguments) ; MAX(colonne) /
    MIN(colonne) sans virgule restent des agrégats inchangés."""
    out = []
    i = 0
    pattern = re.compile(r'\b(MAX|MIN)\s*\(', re.I)
    while True:
        m = pattern.search(sql, i)
        if not m:
            out.append(sql[i:])
            break
        out.append(sql[i:m.start()])
        depth = 1
        j = m.end()
        has_top_level_comma = False
        while j < len(sql) and depth > 0:
            if sql[j] == '(':
                depth += 1
            elif sql[j] == ')':
                depth -= 1
            elif sql[j] == ',' and depth == 1:
                has_top_level_comma = True
            j += 1
        inner = sql[m.end():j-1]
        name = 'GREATEST' if m.group(1).upper() == 'MAX' else 'LEAST'
        if has_top_level_comma:
            out.append(f'{name}({inner})')
        else:
            out.append(sql[m.start():j])
        i = j
    return ''.join(out)


def _translate_statement(sql):
    """Traduit les particularités SQLite vers PostgreSQL pour les quelques
    requêtes non-standard identifiées dans app.py."""
    stripped = sql.strip()

    if re.match(r'^SELECT\s+last_insert_rowid\(\)\s*$', stripped, re.I):
        return 'SELECT lastval() AS id'

    m = re.match(r'^PRAGMA\s+table_info\((\w+)\)\s*$', stripped, re.I)
    if m:
        table = m.group(1)
        return (
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name=%s AND table_schema=current_schema()",
            (table,),
        )

    # SQLite « BEGIN IMMEDIATE » prend le verrou d'écriture de toute la base pour la
    # transaction. Équivalent PostgreSQL : un verrou consultatif de transaction propre au
    # schéma du client (libéré automatiquement au COMMIT/ROLLBACK). Sans cette traduction,
    # la création d'un avoir levait une erreur de syntaxe en production.
    if re.match(r'^BEGIN\s+(IMMEDIATE|EXCLUSIVE)\s*;?$', stripped, re.I):
        return 'SELECT pg_advisory_xact_lock(hashtext(current_schema()))'

    # Les colonnes REAL de SQLite sont des flottants 64 bits ; « REAL » en PostgreSQL est
    # un float4 (≈ 7 chiffres) qui arrondit les montants (1 234 567,89 -> 1 234 567,9).
    if re.match(r'^(CREATE\s+TABLE|ALTER\s+TABLE)\b', stripped, re.I):
        # Même traduction que executescript() : un CREATE TABLE passé par execute()
        # (ex. job_runs) échouait en production sur « AUTOINCREMENT ».
        stripped = _translate_ddl(stripped)

    if stripped.upper().startswith('INSERT OR IGNORE INTO'):
        # "INSERT OR IGNORE INTO t(...) VALUES(...)" -> "INSERT INTO t(...) VALUES(...) ON CONFLICT DO NOTHING"
        rewritten = re.sub(r'^INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', stripped, flags=re.I)
        return rewritten + ' ON CONFLICT DO NOTHING'

    rewritten = _translate_scalar_minmax(stripped)

    # SQLite uses `IS` as a null-safe equality operator not only with NULL,
    # but also with bound parameters and identifiers. PostgreSQL only accepts
    # `IS` with predicates such as NULL/TRUE/FALSE; its null-safe equality is
    # `IS NOT DISTINCT FROM` (and the negation is `IS DISTINCT FROM`).
    # Keep the application SQL portable and translate these SQLite forms here.
    rewritten = re.sub(r'\bIS\s+NOT\s+\?(?=\s|$|[),])', 'IS DISTINCT FROM ?', rewritten, flags=re.I)
    rewritten = re.sub(r'\bIS\s+\?(?=\s|$|[),])', 'IS NOT DISTINCT FROM ?', rewritten, flags=re.I)
    rewritten = re.sub(
        r'\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s+IS\s+'
        r'([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\b',
        r'\1 IS NOT DISTINCT FROM \2', rewritten, flags=re.I
    )
    return rewritten


def _translate_ddl(script):
    """Traduit un script executescript() (CREATE TABLE ...) vers une syntaxe
    compatible PostgreSQL."""
    script = re.sub(r'INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY', script, flags=re.I)
    # REAL (float4 en PostgreSQL) arrondit les montants : on garde la précision 64 bits de SQLite.
    script = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', script, flags=re.I)
    return script


def upgrade_real_columns(conn):
    """Migration PostgreSQL : convertit en DOUBLE PRECISION les colonnes créées en REAL
    (float4) par les versions précédentes, dans le schéma courant. Sans effet sous SQLite."""
    if not isinstance(conn, PGConnection):
        return 0
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND data_type='real'").fetchall()
    for r in rows:
        t = re.sub(r'[^A-Za-z0-9_]', '', r['table_name']); col = re.sub(r'[^A-Za-z0-9_]', '', r['column_name'])
        conn.execute(f'ALTER TABLE "{t}" ALTER COLUMN "{col}" TYPE DOUBLE PRECISION')
    conn.commit()
    return len(rows)


class PGCursorResult:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        cols = [d[0] for d in cur.description] if cur.description else None
        self._cols = cols

    def _wrap(self, raw):
        if raw is None or self._cols is None:
            return raw
        return Row(zip(self._cols, raw))

    def fetchone(self):
        raw = self._cur.fetchone()
        r = self._wrap(raw)
        self._cur.close()
        return r

    def fetchall(self):
        raw = self._cur.fetchall()
        self._cur.close()
        return [self._wrap(r) for r in raw]

    @property
    def lastrowid(self):
        """Compatibilité sqlite3.Cursor.lastrowid pour PostgreSQL.

        Les tables ProfitOS à identifiant auto-incrémenté utilisent des séquences
        PostgreSQL. Juste après l'INSERT, lastval() renvoie l'identifiant produit
        par la dernière séquence utilisée dans cette session.
        """
        self._cur.execute('SELECT lastval()')
        raw = self._cur.fetchone()
        return raw[0] if raw else None


# ---------------------------------------------------------------------------
# Pool de connexions PostgreSQL.
#
# Avant : une connexion psycopg2 neuve par appel à cx()/auth_cx() (11 à 14 par
# page), un CREATE SCHEMA IF NOT EXISTS + COMMIT à chaque ouverture et un
# SET search_path avant chaque requête.
#
# Maintenant :
#   - un ThreadedConnectionPool par process (taille PG_POOL_MAX, défaut 10) ;
#   - CREATE SCHEMA exécuté une seule fois par process et par schéma ;
#   - SET search_path exécuté (et commité, pour survivre à un rollback) une
#     seule fois à chaque sortie du pool ;
#   - pendant une requête Flask (ou un app_context de job), une connexion
#     fermée par le code applicatif (`c.close()`) n'est pas rendue au pool :
#     elle est annulée (rollback, comme un vrai close) puis gardée dans
#     `flask.g` pour être réutilisée par le prochain cx() du même schéma.
#     Toutes les connexions de la requête sont rendues au pool au teardown.
#     Deux connexions ouvertes simultanément restent deux connexions
#     distinctes : la sémantique transactionnelle de SQLite est conservée.
#   - hors contexte Flask (scripts, cron), close() rend directement la
#     connexion au pool ; `pg_connection()` offre un context manager.
#
# PG_SEARCH_PATH_EACH_QUERY=1 restaure l'ancien comportement (SET avant chaque
# requête) si un pooler externe en mode "transaction" est intercalé.
# ---------------------------------------------------------------------------
import threading
from contextlib import contextmanager

PG_POOL_MIN = max(0, int(os.environ.get('PG_POOL_MIN', '1') or 1))
PG_POOL_MAX = max(1, int(os.environ.get('PG_POOL_MAX', '10') or 10))
PG_SEARCH_PATH_EACH_QUERY = os.environ.get('PG_SEARCH_PATH_EACH_QUERY', '').strip().lower() in ('1', 'true', 'yes')

_pool = None
_pool_lock = threading.Lock()
_ensured_schemas = set()
_schema_lock = threading.Lock()
_G_KEY = '_profitos_pg_idle'


def _safe_schema(schema):
    return re.sub(r'[^a-zA-Z0-9_]', '', schema) if schema else None


def _make_pool():
    from psycopg2 import pool as pg_pool
    return pg_pool.ThreadedConnectionPool(
        min(PG_POOL_MIN, PG_POOL_MAX), PG_POOL_MAX, DATABASE_URL,
        connect_timeout=PG_CONNECT_TIMEOUT, application_name=PG_APPLICATION_NAME,
    )


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _make_pool()
    return _pool


def reset_pool():
    """Ferme le pool (tests, fin de job). Le prochain accès en recrée un."""
    global _pool
    with _pool_lock:
        p, _pool = _pool, None
    with _schema_lock:
        _ensured_schemas.clear()
    if p is not None:
        try:
            p.closeall()
        except Exception:
            pass


def _flask_g():
    try:
        from flask import g, has_app_context
        return g if has_app_context() else None
    except Exception:
        return None


def _ensure_schema(raw, schema):
    if not schema or schema == 'public' or schema in _ensured_schemas:
        return
    cur = raw.cursor()
    try:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        raw.commit()
    finally:
        cur.close()
    with _schema_lock:
        _ensured_schemas.add(schema)


def _apply_search_path(raw, schema):
    if not schema:
        return
    cur = raw.cursor()
    try:
        cur.execute(f'SET search_path TO "{schema}", public')
        raw.commit()  # un SET non commité serait annulé par le prochain rollback
    finally:
        cur.close()


def _is_pool_exhausted(exc):
    return exc.__class__.__name__ == 'PoolError'


def _checkout(schema):
    """Retourne (connexion brute, origine) avec le search_path déjà positionné."""
    g = _flask_g()
    if g is not None:
        idle = getattr(g, _G_KEY, None)
        if idle and idle.get(schema):
            return idle[schema].pop(), 'pool'
    origin = 'pool'
    try:
        pool = get_pool()
        raw = pool.getconn()
        if getattr(raw, 'closed', 0):
            pool.putconn(raw, close=True)
            raw = pool.getconn()
    except Exception as exc:
        # Pool saturé : on préfère servir la requête avec une connexion directe
        # (fermée à la libération) plutôt que d'échouer.
        if not _is_pool_exhausted(exc):
            raise
        import logging
        logging.getLogger('profitos.db').warning('PG pool saturé (PG_POOL_MAX=%s) : connexion directe', PG_POOL_MAX)
        raw = psycopg2.connect(DATABASE_URL, connect_timeout=PG_CONNECT_TIMEOUT, application_name=PG_APPLICATION_NAME)
        origin = 'direct'
    try:
        _ensure_schema(raw, schema)
        _apply_search_path(raw, schema)
    except Exception:
        _discard(raw, origin)
        raise
    return raw, origin


def _discard(raw, origin):
    try:
        if origin == 'pool' and _pool is not None:
            _pool.putconn(raw, close=True)
        else:
            raw.close()
    except Exception:
        pass


def _give_back(raw, origin):
    """Rend une connexion au pool (ou la ferme si elle n'en vient pas)."""
    if origin != 'pool' or _pool is None or getattr(raw, 'closed', 0):
        _discard(raw, origin)
        return
    try:
        raw.rollback()
    except Exception:
        _discard(raw, origin)
        return
    try:
        _pool.putconn(raw)
    except Exception:
        _discard(raw, origin)


def _release(raw, schema, origin):
    """Appelé par PGConnection.close()."""
    try:
        raw.rollback()  # même sémantique qu'un close() : le non-commité est perdu
    except Exception:
        _discard(raw, origin)
        return
    g = _flask_g()
    if g is not None and origin == 'pool':
        idle = getattr(g, _G_KEY, None)
        if idle is None:
            idle = {}
            setattr(g, _G_KEY, idle)
        idle.setdefault(schema, []).append(raw)
        return
    _give_back(raw, origin)


def release_request_connections(exc=None):
    """Teardown Flask : rend au pool toutes les connexions de la requête."""
    g = _flask_g()
    if g is None:
        return
    idle = getattr(g, _G_KEY, None)
    if not idle:
        return
    setattr(g, _G_KEY, {})
    for conns in idle.values():
        for raw in conns:
            _give_back(raw, 'pool')


def init_app(app):
    """Branche la libération des connexions en fin de requête / d'app_context."""
    app.teardown_appcontext(release_request_connections)


class PGConnection:
    def __init__(self, dsn=None, schema=None):
        # `dsn` est conservé pour compatibilité de signature : le pool utilise DATABASE_URL.
        self._schema = _safe_schema(schema)
        self._conn, self._origin = _checkout(self._schema)
        self._closed = False

    def _set_search_path(self):
        """Réapplique le schéma actif. Par défaut le search_path est positionné
        une seule fois à la sortie du pool ; PG_SEARCH_PATH_EACH_QUERY=1 force
        l'ancien comportement (utile derrière un pooler en mode transaction)."""
        if self._schema:
            cur = self._conn.cursor()
            cur.execute(f'SET search_path TO "{self._schema}", public')
            cur.close()

    def execute(self, sql, params=()):
        translated = _translate_statement(sql)
        if isinstance(translated, tuple):
            pg_sql, extra_params = translated
            params = extra_params
        else:
            pg_sql = _sqlite_placeholders_to_pg(translated)
        if PG_SEARCH_PATH_EACH_QUERY:
            self._set_search_path()
        cur = self._conn.cursor()
        try:
            cur.execute(pg_sql, params)
        except Exception:
            self._conn.rollback()  # évite de laisser la connexion dans un état "transaction avortée"
                                    # qui ferait échouer TOUTES les requêtes suivantes sur cette connexion.
            raise
        return PGCursorResult(cur)

    def executescript(self, script):
        script = _translate_ddl(script)
        stmts = [s.strip() for s in script.split(';') if s.strip()]
        for stmt in stmts:
            if PG_SEARCH_PATH_EACH_QUERY:
                self._set_search_path()
            cur = self._conn.cursor()
            try:
                cur.execute(stmt)
                self._conn.commit()  # commit chaque instruction séparément : une instruction en échec
                                      # n'annule jamais les tables déjà créées avec succès juste avant.
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        _release(self._conn, self._schema, self._origin)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class SQLiteCursorResult:
    """Enrobe le curseur sqlite3 pour renvoyer des Row (comportement identique
    à sqlite3.Row mais avec la classe Row commune aux deux backends)."""
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        self._cols = [d[0] for d in cur.description] if cur.description else None

    def _wrap(self, raw):
        if raw is None or self._cols is None:
            return raw
        return Row(zip(self._cols, raw))

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self):
        # Même interface que PGCursorResult.lastrowid : sans elle, l'enregistrement d'un
        # règlement (et 6 autres écrans) levait AttributeError en local sur SQLite.
        return self._cur.lastrowid


class SQLiteConnection:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        # LEFT(texte, n) existe sous PostgreSQL mais pas sous SQLite. La révision comptable
        # l'utilise (comptes 401/411/471...) : sans cet équivalent, le diagnostic et la
        # clôture d'une révision levaient « no such function: LEFT » en local.
        self._conn.create_function(
            'LEFT', 2, lambda value, n: None if value is None else str(value)[:max(int(n or 0), 0)],
            deterministic=True)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return SQLiteCursorResult(cur)

    def executescript(self, script):
        self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def connect_auth(sqlite_path):
    if USE_POSTGRES:
        return PGConnection(DATABASE_URL, schema='public')  # schéma auth explicite
    return SQLiteConnection(sqlite_path)


def connect_tenant(org_id, sqlite_path):
    if USE_POSTGRES:
        return PGConnection(DATABASE_URL, schema=f'org_{int(org_id)}')
    return SQLiteConnection(sqlite_path)


@contextmanager
def pg_connection(org_id=None, sqlite_path=None):
    """Context manager pour les jobs et scripts hors requête HTTP.

    org_id=None -> base AUTH (schéma public). La connexion est toujours
    libérée (rendue au pool en PostgreSQL, fermée en SQLite)."""
    conn = connect_auth(sqlite_path) if org_id is None else connect_tenant(org_id, sqlite_path)
    try:
        yield conn
    finally:
        conn.close()


def delete_tenant_storage(org_id, sqlite_path):
    """Supprime définitivement le stockage privé d'une organisation.

    PostgreSQL : supprime tout le schéma ``org_<id>`` avec CASCADE. Cela évite
    d'oublier une table tenant ajoutée dans une version future.
    SQLite : supprime le fichier tenant ``org_<id>.db``.

    Cette fonction ne touche jamais aux tables AUTH du schéma public ; leur
    nettoyage est effectué séparément par la route de suppression de compte.
    """
    org_id = int(org_id)
    if USE_POSTGRES:
        schema = f'org_{org_id}'
        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=PG_CONNECT_TIMEOUT,
            application_name=PG_APPLICATION_NAME,
        )
        try:
            cur = conn.cursor()
            try:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                conn.commit()
                with _schema_lock:
                    _ensured_schemas.discard(schema)
            finally:
                cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    path = os.fspath(sqlite_path)
    if os.path.exists(path):
        os.remove(path)


def backend_name():
    return 'postgresql' if USE_POSTGRES else 'sqlite'

def database_url_configured():
    return bool(DATABASE_URL)

def ping_auth(sqlite_path):
    c = connect_auth(sqlite_path)
    try:
        row = c.execute('SELECT 1 AS ok').fetchone()
        return bool(row and row['ok'] == 1)
    finally:
        c.close()
