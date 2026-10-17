"""Affichage unique de secrets (secret de signature d'un webhook, lien
d'invitation d'un expert-comptable...) sans les placer dans le cookie de session.

Les messages « flash » de Flask sont stockés dans le cookie de session, signé mais
NON chiffré : un secret mis en flash est lisible dans le navigateur, les journaux
d'un proxy ou une copie du cookie. Ici, le secret est conservé côté serveur,
chiffré, dans une table du tenant, lu UNE seule fois par l'utilisateur qui l'a
créé, puis supprimé (et expiré au bout de 15 minutes s'il n'est jamais lu).
"""
from datetime import datetime, timedelta, timezone

_TTL_MINUTES = 15
_DDL = ('CREATE TABLE IF NOT EXISTS one_time_secrets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, '
        'purpose TEXT NOT NULL, label TEXT, value_enc TEXT NOT NULL, created_at TEXT NOT NULL)')


def _conn():
    from flask import session
    from profitos.runtime import tenant_cx_direct
    c = tenant_cx_direct(session['org_id'])
    c.executescript(_DDL)
    return c


def store_secret(purpose, label, value):
    from flask import session
    from profitos.crypto import encrypt_secret
    c = _conn()
    try:
        c.execute('INSERT INTO one_time_secrets(user_id,purpose,label,value_enc,created_at) VALUES(?,?,?,?,?)',
                  (session['user_id'], purpose, label, encrypt_secret(value, session['org_id']),
                   datetime.now(timezone.utc).isoformat()))
        c.commit()
    finally:
        c.close()


def pop_secrets(purpose):
    """Renvoie [(libellé, valeur)] pour l'utilisateur courant et les supprime."""
    from flask import session
    from profitos.crypto import decrypt_secret
    if not session.get('org_id') or not session.get('user_id'):
        return []
    c = _conn()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_TTL_MINUTES)).isoformat()
        c.execute('DELETE FROM one_time_secrets WHERE created_at<?', (cutoff,))
        rows = c.execute('SELECT * FROM one_time_secrets WHERE user_id=? AND purpose=? ORDER BY id',
                         (session['user_id'], purpose)).fetchall()
        c.execute('DELETE FROM one_time_secrets WHERE user_id=? AND purpose=?', (session['user_id'], purpose))
        c.commit()
    finally:
        c.close()
    out = []
    for r in rows:
        try:
            out.append((r['label'], decrypt_secret(r['value_enc'], session['org_id'])))
        except Exception:
            continue
    return out
