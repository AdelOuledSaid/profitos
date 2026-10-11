"""Numérotation légale des documents de vente (factures, avoirs, bons de livraison).

Règles (CGI art. 242 nonies A, BOFiP BOI-TVA-DECLA-30-20-20-10) : numérotation
unique, continue et chronologique, par entité (chaque filiale a sa propre série).
Le numéro d'une facture est attribué à l'ÉMISSION, jamais à la création du
brouillon : un brouillon annulé ne consomme aucun numéro et ne crée aucun trou.

La séquence est mémorisée par (entité, type de document, année d'émission en
heure de Paris) dans invoice_number_sequences et réservée sous verrou :
- PostgreSQL : verrou consultatif de transaction (pg_advisory_xact_lock) propre au
  schéma du client et à la série — libéré au COMMIT/ROLLBACK ;
- SQLite : BEGIN IMMEDIATE (verrou d'écriture de la base pour la transaction).
Le numéro n'est donc définitivement consommé que si la transaction d'émission
(statut + écriture comptable) est validée.
"""
from profitos import db as dbmod

# type de document -> (préfixe, table, colonne du numéro, chiffres minimum)
SERIES = {
    'invoice': ('FA', 'outgoing_invoices', 'invoice_number', 3),
    'credit': ('AV', 'outgoing_credit_notes', 'credit_number', 3),
    'delivery': ('BL', 'delivery_notes', 'delivery_number', 3),
}


def _is_postgres(conn):
    return isinstance(conn, dbmod.PGConnection)


def acquire_series_lock(conn, key):
    """Sérialise l'attribution des numéros d'une série jusqu'à la fin de la transaction."""
    if _is_postgres(conn):
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(current_schema() || ?))', (f'profitos-seq:{key}',))
        return
    raw = getattr(conn, '_conn', conn)
    if getattr(raw, 'in_transaction', True) is False:
        try:
            conn.execute('BEGIN IMMEDIATE')
        except Exception as e:
            # Seul cas toléré : une transaction est déjà ouverte (le verrou d'écriture est
            # alors détenu). Un verrou tenu par une autre émission (« database is locked »)
            # remonte : continuer sans verrou lirait une séquence périmée.
            if 'within a transaction' not in str(e):
                raise SeriesLockTimeout(str(e)) from e


class SeriesLockTimeout(Exception):
    """La série est verrouillée par une autre émission en cours (SQLite)."""


def series_prefix(kind, entity_id, year):
    prefix = SERIES[kind][0]
    return f"{prefix}-{year}-" if not entity_id else f"{prefix}-E{entity_id}-{year}-"


def _highest_existing(conn, kind, entity_id, prefix):
    _, table, col, _ = SERIES[kind]
    rows = conn.execute(f"SELECT {col} AS n FROM {table} WHERE {col} LIKE ? AND entity_id IS ?",
                        (prefix + '%', entity_id)).fetchall()
    highest = 0
    for row in rows:
        try:
            highest = max(highest, int(str(row['n'] or '').rsplit('-', 1)[1]))
        except (ValueError, IndexError):
            pass
    return highest


def next_document_number(conn, kind, entity_id, document_date):
    """Réserve et retourne le prochain numéro de la série (dans la transaction en cours).

    document_date : date ISO du document (heure de Paris) — son année choisit la série.
    L'appelant DOIT écrire le numéro puis valider (ou annuler) la transaction."""
    year = str(document_date)[:4]
    entity_key = int(entity_id) if entity_id else 0
    series = f"{kind}:{year}"
    acquire_series_lock(conn, f"{entity_key}:{series}")
    prefix = series_prefix(kind, entity_id, year)
    row = conn.execute('SELECT last_sequence FROM invoice_number_sequences WHERE entity_key=? AND fiscal_year=?',
                       (entity_key, series)).fetchone()
    if row is None:
        # Première émission de l'année avec ce mécanisme : on repart du plus grand numéro
        # déjà attribué (données antérieures), jamais de 1 si des numéros existent.
        last = _highest_existing(conn, kind, entity_id, prefix)
        conn.execute('INSERT INTO invoice_number_sequences(entity_key,fiscal_year,last_sequence) VALUES(?,?,?)',
                     (entity_key, series, last))
    else:
        last = int(row['last_sequence'] or 0)
    _, table, col, width = SERIES[kind]
    candidate = last + 1
    while conn.execute(f"SELECT 1 FROM {table} WHERE {col}=? AND entity_id IS ?",
                       (f"{prefix}{candidate:0{width}d}", entity_id)).fetchone():
        candidate += 1
    conn.execute('UPDATE invoice_number_sequences SET last_sequence=? WHERE entity_key=? AND fiscal_year=?',
                 (candidate, entity_key, series))
    return f"{prefix}{candidate:0{width}d}"
