"""Pièces demandées (v475) : l'expert-comptable (ou un administrateur) demande le
justificatif d'une écriture ; l'entreprise le voit dans sa liste « Pièces demandées », sur
l'écriture et dans son rapport du lundi ; la demande est soldée dès qu'une pièce est jointe
à l'écriture."""

REQUEST_ROLES = ('OWNER', 'ADMIN', 'COMPTABLE')


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS entry_document_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER NOT NULL, entity_id INTEGER,
        message TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, created_by TEXT,
        resolved_at TEXT, resolved_by TEXT)""")
    conn.commit()


def open_for_entry(conn, entry_id):
    ensure_table(conn)
    return conn.execute("SELECT * FROM entry_document_requests WHERE entry_id=? AND status='open' ORDER BY id",
                        (entry_id,)).fetchall()


def create(conn, entry_id, entity_id, message, now_iso, user):
    ensure_table(conn)
    if open_for_entry(conn, entry_id):
        return None
    cur = conn.execute("""INSERT INTO entry_document_requests(entry_id,entity_id,message,status,created_at,created_by)
                          VALUES(?,?,?,'open',?,?)""", (entry_id, entity_id, (message or '').strip()[:300], now_iso, user))
    conn.commit()
    return cur.lastrowid


def resolve_for_entry(conn, entry_id, now_iso, user):
    ensure_table(conn)
    n = conn.execute("UPDATE entry_document_requests SET status='resolved',resolved_at=?,resolved_by=? "
                     "WHERE entry_id=? AND status='open'", (now_iso, user, entry_id)).rowcount
    conn.commit()
    return n


def open_requests(conn, entity_id=None, all_entities=False):
    ensure_table(conn)
    where = "r.status='open'" + ('' if all_entities else ' AND r.entity_id IS ?')
    params = () if all_entities else (entity_id,)
    return conn.execute(f"""SELECT r.*, e.entry_date, e.label, e.piece_number, e.journal_code
                            FROM entry_document_requests r JOIN accounting_entries e ON e.id=r.entry_id
                            WHERE {where} ORDER BY r.created_at""", params).fetchall()


def open_count(conn):
    try:
        ensure_table(conn)
        return conn.execute("SELECT COUNT(*) n FROM entry_document_requests WHERE status='open'").fetchone()['n']
    except Exception:
        return 0
