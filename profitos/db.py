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

    if stripped.upper().startswith('INSERT OR IGNORE INTO'):
        # "INSERT OR IGNORE INTO t(...) VALUES(...)" -> "INSERT INTO t(...) VALUES(...) ON CONFLICT DO NOTHING"
        rewritten = re.sub(r'^INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', stripped, flags=re.I)
        return rewritten + ' ON CONFLICT DO NOTHING'

    return _translate_scalar_minmax(stripped)


def _translate_ddl(script):
    """Traduit un script executescript() (CREATE TABLE ...) vers une syntaxe
    compatible PostgreSQL."""
    script = re.sub(r'INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY', script, flags=re.I)
    return script


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


class PGConnection:
    def __init__(self, dsn, schema=None):
        self._conn = psycopg2.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT, application_name=PG_APPLICATION_NAME)
        self._schema = re.sub(r'[^a-zA-Z0-9_]', '', schema) if schema else None
        if self._schema:
            cur = self._conn.cursor()
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            self._conn.commit()  # commité immédiatement : certains poolers (ex. PgBouncer en mode
                                  # transaction, utilisé par défaut sur les connexions "pooled" de Neon)
                                  # peuvent réattribuer la connexion physique entre deux transactions,
                                  # donc on ne peut pas compter sur un simple SET/CREATE non committé.
            cur.close()
            self._set_search_path()

    def _set_search_path(self):
        """Réapplique le schéma actif. Appelé avant chaque requête (pas seulement à l'ouverture
        de la connexion) car un pooler en mode transaction peut faire perdre un SET search_path
        entre deux transactions sur ce qui semble être "la même" connexion côté client."""
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
        self._conn.close()


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


class SQLiteConnection:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)

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
