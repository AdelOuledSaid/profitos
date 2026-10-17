"""Enregistrement des lignes d'un relevé bancaire importé (CSV, Excel, PDF) comme
opérations bancaires (bank_transactions, fournisseur « import »).

Sans connexion bancaire, les opérations importées passent ainsi par le même module
Banque que les comptes synchronisés : rapprochement avec les factures clients et
fournisseurs, catégorisation comptable, frais bancaires, état de rapprochement.

Dédoublonnage : l'identifiant d'une ligne est une empreinte (compte, date, montant,
libellé, rang parmi les lignes identiques du même fichier). Réimporter le même relevé,
ou un relevé qui chevauche le précédent, n'ajoute pas de doublon ; deux opérations
réellement identiques le même jour dans un relevé restent deux opérations.
"""
import hashlib
import re
import unicodedata
from datetime import datetime

PROVIDER = 'import'


def _slug(text):
    t = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', t).strip('-')[:40] or 'compte'


def account_key(entity_id, name):
    return f"import-{entity_id or 0}-{_slug(name)}"


def ensure_import_account(conn, entity_id, name, now_iso):
    name = (name or '').strip()[:80] or 'Compte importé'
    key = account_key(entity_id, name)
    conn.execute(
        """INSERT INTO bank_accounts(provider,provider_account_id,name,account_type,currency,disabled,last_synced_at,entity_id)
           VALUES(?,?,?,?,?,0,?,?)
           ON CONFLICT(provider,provider_account_id) DO UPDATE SET last_synced_at=excluded.last_synced_at""",
        (PROVIDER, key, name, 'relevé importé', 'EUR', now_iso, entity_id))
    return conn.execute("SELECT * FROM bank_accounts WHERE provider=? AND provider_account_id=?",
                        (PROVIDER, key)).fetchone()


def store_statement_lines(conn, entity_id, account_name, lines, balance=None, balance_date=None,
                          categorize=None):
    """lines : [(date ISO, libellé, montant signé)]. Retourne (compte, ajoutées, déjà présentes)."""
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()
    account = ensure_import_account(conn, entity_id, account_name, now_iso)
    key = account['provider_account_id']
    seen = {}
    added = existing = 0
    for tx_date, label, amount in lines:
        if not tx_date or amount is None or abs(amount) < 0.005:
            continue
        amount = round(float(amount), 2)
        label = ' '.join(str(label or '').split())[:255]
        base = f"{key}|{tx_date}|{amount:.2f}|{label.lower()}"
        rank = seen.get(base, 0)
        seen[base] = rank + 1
        tid = hashlib.sha256(f"{base}|{rank}".encode()).hexdigest()[:40]
        if conn.execute("SELECT 1 FROM bank_transactions WHERE provider=? AND provider_transaction_id=?",
                        (PROVIDER, tid)).fetchone():
            existing += 1
            continue
        category = categorize(conn, label, entity_id) if categorize else None
        conn.execute(
            """INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount,raw_status,last_synced_at,category)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (PROVIDER, tid, key, tx_date, label, amount, 'booked', now_iso, category))
        added += 1
    if balance is not None and balance_date:
        current = account['statement_balance_date']
        if not current or str(balance_date) >= str(current):
            conn.execute("UPDATE bank_accounts SET balance=?,statement_balance_date=? WHERE id=?",
                         (round(float(balance), 2), str(balance_date), account['id']))
    conn.commit()
    return account, added, existing
