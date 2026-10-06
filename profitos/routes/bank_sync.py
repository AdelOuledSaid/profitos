import re
import unicodedata
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import flash, redirect, render_template, request, session, url_for

from profitos.feature_access import requires_paid_plan, requires_feature
from profitos import db as dbmod
from profitos.runtime import *
from profitos.accounting import generate_purchase_payment_entry, generate_purchase_partial_payment_entry, generate_sale_payment_entry, generate_sale_partial_payment_entry, AccountingError, DEFAULT_CATEGORY_MAPPING


POWENS_TIMEOUT = 20
BANK_DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((dbmod.psycopg2.IntegrityError,) if dbmod.USE_POSTGRES else ())


def apply_categorization_rule(conn, label, entity_id=None):
    """Catégorie historique, désormais isolée par entité.

    Les règles globales héritées (entity_id NULL) restent utilisables comme
    repli, mais une règle de l'entité active est toujours prioritaire.
    """
    if not label:
        return None
    label_norm = norm(label)
    rules = conn.execute(
        """SELECT pattern,category FROM bank_categorization_rules
           WHERE entity_id IS ? OR entity_id IS NULL
           ORDER BY CASE WHEN entity_id IS ? THEN 0 ELSE 1 END, priority DESC, id ASC""",
        (entity_id, entity_id),
    ).fetchall()
    for r in rules:
        if norm(r['pattern']) in label_norm:
            return r['category']
    return None


def _learning_pattern(label, amount=None):
    """Construit une signature stable et, si possible, sensible au sens du flux."""
    words=[w for w in re.findall(r'[a-z0-9]+', norm(label or '')) if len(w)>=3 and not w.isdigit()]
    base=' '.join(words[:5])[:110]
    if not base or amount is None:
        return base
    try:
        direction='debit' if float(amount) < 0 else 'credit'
    except (TypeError, ValueError):
        return base
    return f"{direction}:{base}"[:120]


def _accounting_suggestion(conn, tx, entity_id):
    """Suggestion prudente issue de l'apprentissage, sans écriture automatique.

    v384 : priorité à une signature exacte + sens débit/crédit. Les anciennes
    règles sans sens restent compatibles, mais une ambiguïté entre plusieurs
    comptes est volontairement bloquée au lieu de choisir arbitrairement.
    """
    label=tx['label'] or ''
    directional=_learning_pattern(label, tx['amount'])
    legacy=_learning_pattern(label)
    rules=conn.execute(
        """SELECT * FROM bank_accounting_learning_rules
           WHERE entity_id IS ?
           ORDER BY confirmations DESC, id DESC""",
        (entity_id,),
    ).fetchall()

    def consensus(pattern):
        matches=[r for r in rules if (r['pattern'] or '') == pattern]
        if not matches:
            return None, False
        # Plusieurs comptes/TVA pour la même signature = cas ambigu : humain.
        choices={(r['account_code'], r['vat_rate'], r['category']) for r in matches}
        if len(choices) != 1:
            return None, True
        return max(matches, key=lambda r: int(r['confirmations'] or 0)), False

    learned, ambiguous=consensus(directional)
    source='directionnelle'
    if learned is None and not ambiguous:
        learned, ambiguous=consensus(legacy)
        source='historique'

    if ambiguous:
        return dict(category=tx['category'], account_code=None, vat_rate=None,
                    counterparty_type=None, counterparty_id=None, confidence_score=0,
                    confidence_label='À vérifier', automation_eligible=False,
                    reason='Habitudes contradictoires : aucune proposition automatique')

    if learned:
        confirmations=int(learned['confirmations'] or 1)
        score=min(95, 70 + min(confirmations, 5)*5)
        label_conf='Élevée' if score >= 90 else ('Moyenne' if score >= 80 else 'À confirmer')
        return dict(category=learned['category'], account_code=learned['account_code'],
                    vat_rate=learned['vat_rate'], counterparty_type=learned['counterparty_type'],
                    counterparty_id=learned['counterparty_id'], confidence_score=score,
                    confidence_label=label_conf,
                    automation_eligible=(source == 'directionnelle' and confirmations >= 4 and score >= 90),
                    reason=f"Habitude {source} validée {confirmations} fois")

    category=tx['category'] or apply_categorization_rule(conn,label,entity_id)
    account=DEFAULT_CATEGORY_MAPPING.get(category) if category else None
    score=55 if account else 0
    reason='Correspondance catégorie → compte PCG' if account else 'Aucune habitude suffisamment fiable'
    return dict(category=category, account_code=account, vat_rate=None,
                counterparty_type=None, counterparty_id=None, confidence_score=score,
                confidence_label='Faible' if account else 'Aucune', automation_eligible=False, reason=reason)


def _cfg():
    domain = os.environ.get("POWENS_DOMAIN", "").strip()
    client_id = os.environ.get("POWENS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("POWENS_CLIENT_SECRET", "").strip()
    if domain.endswith(".biapi.pro"):
        domain = domain[:-10]
    return domain, client_id, client_secret


def _configured():
    return all(_cfg())


def _api_url(path):
    domain, _, _ = _cfg()
    return f"https://{domain}.biapi.pro/2.0{path}"


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _json(resp):
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = resp.json()
            detail = payload.get("message") or payload.get("error_description") or payload.get("error") or str(payload)
        except Exception:
            detail = (resp.text or "").strip()[:300]
        raise RuntimeError(
            f"Powens HTTP {resp.status_code}" + (f" : {detail}" if detail else "")
        ) from exc
    return resp.json() if resp.content else {}


def _callback_url():
    configured = os.environ.get("POWENS_REDIRECT_URI", "").strip()
    if configured:
        return configured
    if os.environ.get("PROFITOS_ENV", "").lower() == "production":
        return "https://app.profitos.fr/banking/callback"
    return url_for("banking_callback", _external=True)


def _renew_token(provider_user_id):
    _, client_id, client_secret = _cfg()
    r = requests.post(
        _api_url("/auth/renew"),
        json={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "id_user": int(provider_user_id),
            "revoke_previous": False,
        },
        timeout=POWENS_TIMEOUT,
    )
    data = _json(r)
    return data.get("access_token") or data.get("auth_token") or data.get("token")


def _connection_row(c, entity_id=None):
    return c.execute(
        "SELECT * FROM bank_connections WHERE provider='powens' AND entity_id IS ? ORDER BY id DESC LIMIT 1",
        (entity_id,),
    ).fetchone()


def _sync_powens(c, row):
    token = _renew_token(row["provider_user_id"])
    if not token:
        raise RuntimeError("Powens n'a pas renvoyé de jeton utilisateur.")

    accounts = _json(requests.get(
        _api_url("/users/me/accounts"),
        headers=_headers(token),
        timeout=POWENS_TIMEOUT,
    )).get("accounts", [])

    now = datetime.utcnow().replace(microsecond=0).isoformat()
    entity_id = row['entity_id']
    synced_account_ids = set()
    active_balances = []
    for a in accounts:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        # A provider account already attached to another ProfitOS entity must
        # never be silently reassigned by a synchronization.
        existing_account = c.execute(
            "SELECT entity_id FROM bank_accounts WHERE provider=? AND provider_account_id=?",
            ("powens", aid),
        ).fetchone()
        if existing_account and existing_account["entity_id"] != entity_id:
            raise RuntimeError("Compte bancaire déjà rattaché à une autre entité ProfitOS.")
        synced_account_ids.add(aid)
        balance = a.get("balance")
        try:
            balance = float(balance) if balance is not None else None
        except (TypeError, ValueError):
            balance = None
        disabled = bool(a.get("disabled"))
        if not disabled and balance is not None:
            active_balances.append(balance)
        c.execute(
            """INSERT INTO bank_accounts(provider,provider_account_id,name,iban,account_type,currency,balance,disabled,last_synced_at,entity_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,provider_account_id) DO UPDATE SET
                 name=excluded.name,iban=excluded.iban,account_type=excluded.account_type,
                 currency=excluded.currency,balance=excluded.balance,disabled=excluded.disabled,
                 last_synced_at=excluded.last_synced_at,entity_id=excluded.entity_id""",
            ("powens", aid, a.get("name") or a.get("original_name") or "Compte bancaire",
             a.get("iban"), a.get("type"),
             ((a.get("currency") or {}).get("id") if isinstance(a.get("currency"), dict) else (a.get("currency") or "EUR")),
             balance,
             1 if disabled else 0, now, row['entity_id']),
        )

    txs = _json(requests.get(
        _api_url("/users/me/transactions"),
        headers=_headers(token),
        params={"limit": 1000},
        timeout=POWENS_TIMEOUT,
    )).get("transactions", [])

    for t in txs:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        amount = t.get("value", t.get("amount"))
        try:
            amount = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        account_id = str(t.get("id_account") or t.get("account_id") or "")
        if not account_id or account_id not in synced_account_ids:
            # Never attach a provider transaction to an account that was not
            # returned for this exact Powens user/entity during this sync.
            continue
        label = t.get("simplified_wording") or t.get("wording") or t.get("original_wording") or ""
        tx_date = str(t.get("date") or t.get("application_date") or "")[:10]
        # Same protection for provider transactions: a remote transaction
        # cannot migrate between entity-owned bank accounts during an upsert.
        existing_tx = c.execute(
            """SELECT a.entity_id
                 FROM bank_transactions bt
                 JOIN bank_accounts a
                   ON a.provider=bt.provider
                  AND a.provider_account_id=bt.provider_account_id
                WHERE bt.provider=? AND bt.provider_transaction_id=?""",
            ("powens", tid),
        ).fetchone()
        if existing_tx and existing_tx["entity_id"] != entity_id:
            raise RuntimeError("Transaction bancaire déjà rattachée à une autre entité ProfitOS.")
        auto_category = apply_categorization_rule(c, label, entity_id)
        c.execute(
            """INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount,raw_status,last_synced_at,category)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,provider_transaction_id) DO UPDATE SET
                 provider_account_id=excluded.provider_account_id,transaction_date=excluded.transaction_date,
                 label=excluded.label,amount=excluded.amount,raw_status=excluded.raw_status,
                 last_synced_at=excluded.last_synced_at,
                 category=COALESCE(NULLIF(bank_transactions.category,''),excluded.category)""",
            ("powens", tid, account_id, tx_date, label, amount,
             str(t.get("state") or t.get("coming") or ""), now, auto_category),
        )

    c.execute(
        "UPDATE bank_connections SET status='CONNECTED',last_synced_at=?,updated_at=? WHERE id=? AND entity_id IS ?",
        (now, now, row["id"], entity_id),
    )
    c.commit()
    return len(accounts), len(txs)




def _mark_powens_sync_error(c, row):
    """Persist a reconnectable state without storing provider secrets or raw errors."""
    if not row:
        return
    ts = datetime.utcnow().replace(microsecond=0).isoformat()
    c.execute(
        "UPDATE bank_connections SET status='SYNC_ERROR',updated_at=? WHERE id=? AND entity_id IS ?",
        (ts, row["id"], row["entity_id"]),
    )
    c.commit()

def _norm_text(value):
    value = (value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _token_typo_match(a, b):
    """Conservative one-edit tolerance for long company-name tokens only."""
    if a == b:
        return True
    if min(len(a),len(b)) < 5 or abs(len(a)-len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs=[i for i,(x,y) in enumerate(zip(a,b)) if x != y]
        if len(diffs) <= 1:
            return True
        # One adjacent transposition, e.g. AMAZON -> AMAZNO.
        return (
            len(diffs) == 2
            and diffs[1] == diffs[0] + 1
            and a[diffs[0]] == b[diffs[1]]
            and a[diffs[1]] == b[diffs[0]]
        )
    short,long = (a,b) if len(a) < len(b) else (b,a)
    i=j=errors=0
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1; j += 1
        else:
            errors += 1; j += 1
            if errors > 1:
                return False
    return True


def _name_similarity(client_name, bank_label):
    """Conservative lexical score with one-edit tolerance on long tokens."""
    ignore = {"sas","sasu","sarl","eurl","sa","sc","sci","societe","entreprise",
              "company","ltd","limited","inc","gmbh","bv","the","de","du","des","et"}
    client_tokens = [t for t in _norm_text(client_name).split() if len(t) >= 3 and t not in ignore]
    label_tokens = [t for t in _norm_text(bank_label).split() if len(t) >= 3 and t not in ignore]
    if not client_tokens:
        return 0
    hits = sum(1 for t in client_tokens if any(_token_typo_match(t,lt) for lt in label_tokens))
    if hits == 0:
        return 0
    ratio = hits / len(client_tokens)
    if ratio >= 1:
        return 30
    if ratio >= 0.5:
        return 20
    return 10

def _purchase_reconciliation_suggestions(c, tx):
    # Supplier payments are negative bank transactions.
    amount = float(tx['amount'] or 0)
    if amount >= 0:
        return []
    eid=tx['entity_id'] if 'entity_id' in tx.keys() else None
    allocated=c.execute(
        "SELECT COALESCE(SUM(matched_amount),0) AS x FROM bank_purchase_allocations WHERE entity_id IS ? AND bank_transaction_id=?",
        (eid,tx['id'])
    ).fetchone()['x'] or 0
    target=max(0.0,round(abs(amount)-float(allocated),2))
    if target <= .005:
        return []

    rows=c.execute("""
        SELECT p.*,
               COALESCE((SELECT SUM(pp.amount) FROM purchase_invoice_payments pp
                         WHERE pp.purchase_invoice_id=p.id AND pp.entity_id IS ?),0) AS paid_total
        FROM purchase_invoices p
        WHERE p.entity_id IS ?
          AND COALESCE(p.status,'unpaid')!='paid'
        ORDER BY p.due_date ASC, p.id ASC
    """,(eid,eid)).fetchall()

    tx_label=_norm_text(tx['label'] or '')
    out=[]
    for p in rows:
        total=max(0.0,float(p['total'] or 0)-float(p['paid_total'] or 0))
        if total<=.005:
            continue
        if abs(total-target)<=0.01:
            score=60
            reasons=['montant exact']
            suggested_amount=target
        elif target < total and target > .005:
            score=30
            reasons=['paiement partiel possible']
            suggested_amount=target
        elif total < target and total > .005:
            score=30
            reasons=['paiement groupé possible']
            suggested_amount=total
        else:
            continue
        inv_no=_norm_text(p['invoice_number'] or '')
        supplier=_norm_text(p['supplier_name'] or '')
        if inv_no and inv_no in tx_label:
            score += 35
            reasons.append('n° facture')
        sim=_name_similarity(supplier, tx_label) if supplier else 0
        if sim >= 30:
            score += 30; reasons.append('fournisseur très proche')
        elif sim >= 20:
            score += 20; reasons.append('fournisseur proche')
        elif sim >= 10:
            score += 10; reasons.append('fournisseur partiel')
        confidence='Élevée' if score>=90 else ('Moyenne' if score>=75 else 'Faible')
        remaining_after=max(0.0,round(target-suggested_amount,2))
        small_difference=(
            remaining_after > .005
            and remaining_after <= 5.00
            and remaining_after <= max(0.01, round(total*0.02,2))
        )
        # A score of 30 means "amount relation only" (partial/grouped) with no
        # invoice/supplier signal. Do not surface these noisy suggestions.
        if score <= 30:
            continue
        out.append({'purchase':p,'score':score,'confidence':confidence,
                    'reasons':', '.join(reasons),'amount':suggested_amount,
                    'remaining_after':remaining_after,
                    'small_difference':small_difference})
    out.sort(key=lambda x:(-x['score'], x['purchase']['due_date'] or '', x['purchase']['id']))
    # Exact same amount can be ambiguous: keep all suggestions visible, never auto-pay.
    return out

def _bank_allocated_total(c, transaction_id, entity_id):
    row=c.execute("SELECT COALESCE(SUM(matched_amount),0) n FROM bank_invoice_allocations WHERE bank_transaction_id=? AND entity_id IS ?",(transaction_id,entity_id)).fetchone()
    return round(float(row['n'] or 0),2)

def _invoice_bank_balance(c, invoice):
    row=c.execute("SELECT COALESCE(SUM(amount),0) n FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ?",(invoice['id'],invoice['entity_id'])).fetchone()
    return max(0.0,round(float(invoice['total'] or 0)-float(row['n'] or 0),2))

def _bank_match_score(invoice, tx, balance, available):
    score=0; reasons=[]
    delta=abs(balance-available)
    if delta <= .01:
        score += 60; reasons.append('solde exact')
    elif available < balance and available > 0:
        score += 30; reasons.append('paiement partiel possible')
    ref=_norm_text(invoice['invoice_number'] or '')
    label=_norm_text(tx['label'] or '')
    if ref and ref in label:
        score += 35; reasons.append('n° facture')
    ns=_name_similarity(invoice['client_name'],tx['label'] or '')
    if ns:
        score += ns; reasons.append('client reconnu')
    return score,reasons

def _reconciliation_suggestions(c, transactions):
    # Advanced matching: use remaining invoice balance and remaining bank amount.
    # Never auto-post: suggestions remain subject to explicit confirmation.
    from profitos.entities import current_entity_id
    eid=current_entity_id()
    invoices=c.execute("SELECT * FROM outgoing_invoices WHERE entity_id IS ? AND status IN ('sent','partially_paid') AND total>0 ORDER BY issue_date,id",(eid,)).fetchall()
    suggestions=[]
    for t in transactions:
        try: total_amount=float(t['amount'] or 0)
        except (TypeError,ValueError): continue
        if total_amount <= 0: continue
        available=round(total_amount-_bank_allocated_total(c,t['id'],eid),2)
        if available <= .005: continue
        scored=[]
        for inv in invoices:
            balance=_invoice_bank_balance(c,inv)
            if balance <= .005: continue
            score,reasons=_bank_match_score(inv,t,balance,available)
            if score < 30: continue
            allocation=min(balance,available)
            scored.append((score,inv,reasons,allocation,balance))
        scored.sort(key=lambda x:(-x[0],x[1]['id']))
        if not scored: continue
        best=scored[0]
        second=scored[1][0] if len(scored)>1 else None
        # Ambiguous weak matches are deliberately not proposed.
        if second is not None and best[0] < 80 and best[0]-second < 15: continue
        confidence='Élevée' if best[0]>=90 else ('Moyenne' if best[0]>=60 else 'Faible')
        suggestions.append({'transaction':t,'invoice':best[1],'score':best[0],'confidence':confidence,
                            'reasons':', '.join(best[2]),'matched_amount':best[3],
                            'invoice_balance':best[4],'transaction_available':available})
    return suggestions



def register(app):
    @app.route("/banking/comptes/<int:account_id>/entite", methods=["POST"])
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def bank_account_set_entity(account_id):
        from profitos.entities import resolve_entity, current_entity_id, user_can_access_entity
        c = cx()
        current_eid = current_entity_id()
        account = c.execute("SELECT id FROM bank_accounts WHERE id=? AND entity_id IS ?", (account_id,current_eid)).fetchone()
        if not account:
            c.close(); abort(404)
        entity_id_raw = request.form.get("entity_id")
        entity_id = int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None
        if not user_can_access_entity(c, session.get('user_id'), entity_id):
            c.close(); abort(403)
        try:
            identity = resolve_entity(c, entity_id)
        except ValueError:
            c.close()
            flash("Entité introuvable.")
            return redirect(url_for("banking"))
        c.execute("UPDATE bank_accounts SET entity_id=? WHERE id=? AND entity_id IS ?", (entity_id, account_id, current_eid))
        c.commit(); c.close()
        log_activity('BANK_ACCOUNT_ENTITY_SET', f"Compte bancaire #{account_id} rattaché à {identity['name']}")
        flash(f"Compte rattaché à {identity['name']}.")
        return redirect(url_for("banking"))

    def _ensure_bank_workflow_table(c):
        # Created by the official runtime schema (v370).
        # Legacy schema reference kept for regression compatibility only:
        # CREATE TABLE IF NOT EXISTS bank_transaction_workflow
        # UNIQUE(entity_id,bank_transaction_id)
        # Never execute SQLite-specific DDL from a request on PostgreSQL.
        return None

    def _bank_transaction_states(c, transactions, eid):
        _ensure_bank_workflow_table(c)
        states={}
        for tx in transactions:
            ignored=c.execute(
                "SELECT state FROM bank_transaction_workflow WHERE entity_id IS ? AND bank_transaction_id=?",
                (eid,tx['id'])
            ).fetchone()
            customer=c.execute(
                "SELECT COALESCE(SUM(matched_amount),0) AS x FROM bank_invoice_allocations WHERE entity_id IS ? AND bank_transaction_id=?",
                (eid,tx['id'])
            ).fetchone()['x'] or 0
            supplier=c.execute(
                "SELECT COALESCE(SUM(matched_amount),0) AS x FROM bank_purchase_allocations WHERE entity_id IS ? AND bank_transaction_id=?",
                (eid,tx['id'])
            ).fetchone()['x'] or 0
            fee=c.execute(
                '''SELECT COALESCE(SUM(l.debit),0) AS x
                   FROM accounting_entries e JOIN accounting_entry_lines l ON l.entry_id=e.id
                   WHERE e.entity_id IS ? AND e.source_type='bank_fee' AND e.source_id=?
                     AND l.account_code LIKE '627%%' ''',
                (eid,tx['id'])
            ).fetchone()['x'] or 0
            accounting_validation=c.execute(
                '''SELECT account_code FROM bank_accounting_validations
                   WHERE entity_id IS ? AND bank_transaction_id=?
                     AND account_code IS NOT NULL AND TRIM(account_code) <> ''
                   LIMIT 1''',
                (eid,tx['id'])
            ).fetchone()
            total=round(abs(float(tx['amount'] or 0)),2)
            allocated=round(float(customer)+float(supplier)+float(fee),2)
            if ignored and ignored['state']=='ignored':
                state='ignored'
            elif total > .005 and allocated >= total-.005:
                state='reconciled'
            elif accounting_validation:
                # Accounting categorization is not an invoice reconciliation.
                # Keep the transaction eligible for later customer/supplier matching.
                state='accounted'
            else:
                state='pending'
            remaining=max(0.0,round(total-allocated,2))
            fee_eligible=(
                float(tx['amount'] or 0) < 0
                and allocated > .005
                and remaining > .005
                and remaining <= 5.00
                and remaining <= max(0.01, round(allocated*0.02,2))
            )
            states[tx['id']]={'state':state,'allocated':allocated,'remaining':remaining,
                              'fee_eligible':fee_eligible}
        return states

    @app.route("/banking")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        c = cx()
        try:
            connection = _connection_row(c, eid)
            accounts = c.execute(
                f"SELECT * FROM bank_accounts WHERE provider IN ('powens','swan') AND {ef} ORDER BY disabled,balance DESC",
                ep,
            ).fetchall()
            transactions = c.execute(
                f"""SELECT t.* FROM bank_transactions t
                    JOIN bank_accounts a ON a.provider_account_id=t.provider_account_id AND a.provider=t.provider
                    WHERE t.provider IN ('powens','swan') AND a.{ef}
                      AND (t.provider!='swan' OR COALESCE(t.raw_status,'')='Booked')
                    ORDER BY t.transaction_date DESC,t.id DESC LIMIT 50""",
                ep,
            ).fetchall()
            transaction_states = _bank_transaction_states(c, transactions, eid)
            reconciliation_candidates = [
                t for t in transactions
                if transaction_states[t['id']]['state'] in ('pending','accounted')
            ]
            reconciliation_suggestions = _reconciliation_suggestions(c, reconciliation_candidates)
            accounting_suggestions = {
                t['id']: _accounting_suggestion(c, t, eid)
                for t in transactions
                if transaction_states[t['id']]['state']=='pending'
            }

            # v387 : file de traitement explicite pour prioriser les opérations
            # comptables sans jamais déclencher une écriture automatiquement.
            accounting_review_queue = {'stable': [], 'confirm': [], 'anomaly': []}
            for t in transactions:
                if transaction_states[t['id']]['state'] != 'pending':
                    continue
                sug = accounting_suggestions.get(t['id']) or {}
                item = {'transaction': t, 'suggestion': sug}
                if sug.get('automation_eligible'):
                    accounting_review_queue['stable'].append(item)
                elif (sug.get('confidence_score') or 0) <= 0 or 'contradictoires' in (sug.get('reason') or '').lower():
                    accounting_review_queue['anomaly'].append(item)
                else:
                    accounting_review_queue['confirm'].append(item)

            # Lot 14: suggestions de rapprochement des paiements fournisseurs.
            # La vue banking.html attend un dictionnaire indexé par l'id
            # de la transaction bancaire.
            purchase_reconciliation_suggestions = {}
            for tx in reconciliation_candidates:
                suggestions = _purchase_reconciliation_suggestions(c, tx)
                if suggestions:
                    purchase_reconciliation_suggestions[tx["id"]] = suggestions

            reconciliations = c.execute(
                f'''SELECT r.*,t.transaction_date,t.label,t.amount,i.invoice_number,i.client_name
                   FROM bank_invoice_allocations r
                   JOIN bank_transactions t ON t.id=r.bank_transaction_id
                   JOIN outgoing_invoices i ON i.id=r.invoice_id
                   WHERE r.{ef}
                   ORDER BY r.id DESC LIMIT 50''',
                ep,
            ).fetchall()
            purchase_reconciliations = c.execute(
                f'''SELECT r.*,t.transaction_date,t.label,t.amount,
                           p.invoice_number,p.supplier_name
                   FROM bank_purchase_allocations r
                   JOIN bank_transactions t ON t.id=r.bank_transaction_id
                   JOIN purchase_invoices p ON p.id=r.purchase_invoice_id
                   WHERE r.{ef}
                   ORDER BY r.id DESC LIMIT 50''',
                ep,
            ).fetchall()
            categories = sorted(DEFAULT_CATEGORY_MAPPING.keys())
        finally:
            c.close()
        return render_template(
            "banking.html",
            connection=connection,
            accounts=accounts,
            transactions=transactions,
            transaction_states=transaction_states,
            reconciliation_suggestions=reconciliation_suggestions,
            accounting_suggestions=accounting_suggestions,
            accounting_review_queue=accounting_review_queue,
            purchase_reconciliation_suggestions=purchase_reconciliation_suggestions,
            reconciliations=reconciliations,
            purchase_reconciliations=purchase_reconciliations,
            powens_configured=_configured(),
            swan_configured=__import__('profitos.swan_baas', fromlist=['is_configured']).is_configured(),
            swan_environment=__import__('profitos.swan_baas', fromlist=['current_environment']).current_environment(),
            categories=categories,
        )

    @app.post("/banking/swan/sync")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_swan_sync():
        """Importe le read-model Swan dans le moteur de rapprochement bancaire.

        Sécurité: cette passerelle est volontairement limitée au Sandbox tant
        que le rattachement Live compte<->entité n'est pas explicitement validé.
        Les transactions Pending restent visibles côté Swan mais ne sont pas
        proposées au rapprochement; seules les Booked sont importées ici.
        """
        from profitos.entities import current_entity_id
        from profitos.swan_baas import is_configured as swan_is_configured, current_environment as swan_environment, get_server_token, list_accounts
        if not swan_is_configured() or swan_environment() != 'sandbox':
            flash("Synchronisation Swan disponible uniquement dans le Sandbox pour ce test.")
            return redirect(url_for('banking'))
        eid=current_entity_id()
        c=cx()
        try:
            token=get_server_token()
            accounts=list_accounts(token, environment='sandbox')
            now_ts=datetime.utcnow().replace(microsecond=0).isoformat()
            imported=0
            for a in accounts:
                aid=str(a.get('id') or '')
                if not aid: continue
                booked=a.get('booked_balance') or {}
                try: balance=float(booked.get('value') or 0)
                except (TypeError,ValueError): balance=0.0
                currency=booked.get('currency') or 'EUR'
                c.execute("""INSERT INTO bank_accounts(provider,provider_account_id,name,iban,account_type,currency,balance,disabled,last_synced_at,entity_id)
                             VALUES('swan',?,?,?,?,?,?,0,?,?)
                             ON CONFLICT(provider,provider_account_id) DO UPDATE SET
                               name=excluded.name,iban=excluded.iban,account_type=excluded.account_type,
                               currency=excluded.currency,balance=excluded.balance,disabled=0,
                               last_synced_at=excluded.last_synced_at,entity_id=excluded.entity_id""",
                          (aid,a.get('name') or 'Compte Swan',a.get('IBAN'),'Payment services',currency,balance,now_ts,eid))
                for t in a.get('recent_transactions') or []:
                    if (t.get('status') or '') != 'Booked':
                        continue
                    tid=str(t.get('id') or '')
                    if not tid: continue
                    amt=t.get('amount') or {}
                    try: value=abs(float(amt.get('value') or 0))
                    except (TypeError,ValueError): value=0.0
                    side=(t.get('side') or '').lower()
                    signed=-value if side in ('debit','debited','out') or 'out' in str(t.get('type') or '').lower() else value
                    label=t.get('label') or t.get('reference') or str(t.get('type') or 'Transaction Swan')
                    tx_date=str(t.get('createdAt') or t.get('updatedAt') or '')[:10]
                    category=apply_categorization_rule(c,label,eid)
                    c.execute("""INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount,raw_status,last_synced_at,category)
                                 VALUES('swan',?,?,?,?,?,'Booked',?,?)
                                 ON CONFLICT(provider,provider_transaction_id) DO UPDATE SET
                                   provider_account_id=excluded.provider_account_id,transaction_date=excluded.transaction_date,
                                   label=excluded.label,amount=excluded.amount,raw_status=excluded.raw_status,last_synced_at=excluded.last_synced_at""",
                              (tid,aid,tx_date,label,signed,now_ts,category))
                    imported += 1
            c.commit()
            flash(f"Swan synchronisé : {len(accounts)} compte(s), {imported} transaction(s) comptabilisée(s).")
        except Exception:
            c.rollback()
            app.logger.exception("Échec synchronisation Swan vers rapprochement bancaire")
            flash("Synchronisation Swan impossible pour le moment.")
        finally:
            c.close()
        return redirect(url_for('banking'))

    @app.get("/banking/connect")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_connect():
        if not _configured():
            flash("Connexion bancaire non configurée.")
            return redirect(url_for("banking"))

        domain, client_id, _ = _cfg()
        state = secrets.token_urlsafe(24)
        session["powens_connect_state"] = state
        from profitos.entities import current_entity_id
        session["powens_connect_entity_id"] = current_entity_id()
        callback = _callback_url()

        # Powens officially supports a Connect Webview without a pre-created
        # API user/code. Powens then creates the anonymous user and returns a
        # one-time code on the callback. This is also the flow used by the
        # Console Webview tester and avoids failures before opening Webview.
        params = {
            "domain": f"{domain}.biapi.pro",
            "client_id": client_id,
            "redirect_uri": callback,
            "state": state,
            "connector_capabilities": "bank",
        }
        webview_url = "https://webview.powens.com/fr/connect?" + urlencode(params)

        app.logger.info(
            "POWENS_CONNECT_REDIRECT domain=%s client_id=%s redirect_uri=%s",
            f"{domain}.biapi.pro", client_id, callback
        )
        return redirect(webview_url, code=303)

    @app.get("/banking/callback")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_callback():
        expected = session.pop("powens_connect_state", None)
        connect_entity_id = session.pop("powens_connect_entity_id", None)
        received = request.args.get("state")
        if not expected or not received or not secrets.compare_digest(expected, received):
            flash("Retour bancaire refusé : état de sécurité invalide.")
            return redirect(url_for("banking"))

        if request.args.get("error"):
            flash("La banque n'a pas été connectée.")
            app.logger.warning(
                "Powens callback error=%s description=%s",
                request.args.get("error"),
                request.args.get("error_description"),
            )
            return redirect(url_for("banking"))

        connection_id = (
            request.args.get("connection_id")
            or request.args.get("id_connection")
        )
        callback_code = request.args.get("code")

        c = cx()
        row = None
        try:
            from profitos.entities import user_can_access_entity
            if not user_can_access_entity(c, session.get("user_id"), connect_entity_id):
                abort(403)
            row = _connection_row(c, connect_entity_id)
            provider_user_id = row["provider_user_id"] if row else None

            # When Connect was started without an initial user-scoped code,
            # Powens returns a temporary authorization code. Exchange it for
            # a permanent user token, then retrieve /users/me to get the user id.
            if callback_code:
                _, client_id, client_secret = _cfg()
                token_data = _json(requests.post(
                    _api_url("/auth/token/access"),
                    json={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": callback_code,
                    },
                    timeout=POWENS_TIMEOUT,
                ))
                token = (
                    token_data.get("access_token")
                    or token_data.get("auth_token")
                    or token_data.get("token")
                )
                if not token:
                    raise RuntimeError("Powens n'a pas renvoyé de jeton permanent.")

                user_data = _json(requests.get(
                    _api_url("/users/me"),
                    headers=_headers(token),
                    timeout=POWENS_TIMEOUT,
                ))
                provider_user_id = user_data.get("id")
                if provider_user_id is None:
                    raise RuntimeError("Identifiant utilisateur Powens introuvable.")

                now = datetime.utcnow().replace(microsecond=0).isoformat()
                if row:
                    c.execute(
                        """UPDATE bank_connections
                           SET provider_user_id=?,provider_connection_id=?,status='CONNECTED',updated_at=?
                           WHERE id=? AND entity_id IS ?""",
                        (
                            str(provider_user_id),
                            str(connection_id) if connection_id else row["provider_connection_id"],
                            now,
                            row["id"],
                            connect_entity_id,
                        ),
                    )
                else:
                    c.execute(
                        """INSERT INTO bank_connections(
                               provider,provider_user_id,provider_connection_id,status,created_at,updated_at,entity_id
                           ) VALUES('powens',?,?, 'CONNECTED',?,?,?)""",
                        (
                            str(provider_user_id),
                            str(connection_id) if connection_id else None,
                            now,
                            now,
                            connect_entity_id,
                        ),
                    )
                c.commit()
                row = _connection_row(c, connect_entity_id)

            elif row and connection_id:
                now = datetime.utcnow().replace(microsecond=0).isoformat()
                c.execute(
                    """UPDATE bank_connections
                       SET provider_connection_id=?,status='CONNECTED',updated_at=? WHERE id=? AND entity_id IS ?""",
                    (str(connection_id), now, row["id"], connect_entity_id),
                )
                c.commit()
                row = _connection_row(c, connect_entity_id)

            if not row or not row["provider_user_id"]:
                raise RuntimeError("Utilisateur Powens introuvable après connexion.")

            count_accounts, count_txs = _sync_powens(c, row)
            flash(
                f"Banque connectée : {count_accounts} compte(s), "
                f"{count_txs} transaction(s) synchronisée(s)."
            )

        except Exception as exc:
            try:
                _mark_powens_sync_error(c, row)
            except Exception:
                c.rollback()
            app.logger.exception("Échec callback/synchronisation Powens")
            flash("Connexion bancaire terminée, mais la synchronisation doit être finalisée.")
        finally:
            c.close()

        return redirect(url_for("banking"))

    @app.post("/banking/reconcile/<int:transaction_id>/<int:invoice_id>")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_reconcile(transaction_id, invoice_id):
        c=cx()
        try:
            from profitos.entities import current_entity_id
            eid=current_entity_id()
            t=c.execute("""SELECT t.* FROM bank_transactions t JOIN bank_accounts a
                              ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
                              WHERE t.id=? AND a.entity_id IS ?""",(transaction_id,eid)).fetchone()
            inv=c.execute("SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?",(invoice_id,eid)).fetchone()
            if not t or not inv: abort(404)
            if inv['status'] not in ('sent','partially_paid'):
                flash("Cette facture n'est plus disponible pour le rapprochement."); return redirect(url_for('banking'))
            tx_amount=float(t['amount'] or 0)
            available=round(tx_amount-_bank_allocated_total(c,transaction_id,eid),2)
            balance=_invoice_bank_balance(c,inv)
            if tx_amount <= 0 or available <= .005 or balance <= .005:
                flash("Aucun montant restant à rapprocher."); return redirect(url_for('banking'))
            requested=(request.form.get('matched_amount') or '').replace(',','.').strip()
            amount=round(float(requested),2) if requested else min(available,balance)
            if amount <= 0 or amount > available+.001 or amount > balance+.001:
                flash("Montant de rapprochement invalide."); return redirect(url_for('banking'))
            idem=f"bank:{eid}:{transaction_id}:{invoice_id}:{amount:.2f}"
            matched_at=now()
            cur=c.execute("""INSERT INTO outgoing_invoice_payments(entity_id,invoice_id,amount,payment_date,payment_method,reference,idempotency_key,created_at)
                             VALUES(?,?,?,?,?,?,?,?)""",(eid,invoice_id,amount,t['transaction_date'] or datetime.utcnow().date().isoformat(),'bank',t['label'] or 'Rapprochement bancaire',idem,matched_at))
            payment_id=cur.lastrowid
            payment=c.execute("SELECT * FROM outgoing_invoice_payments WHERE id=? AND entity_id IS ?",(payment_id,eid)).fetchone()
            generate_sale_partial_payment_entry(c,inv,payment)
            c.execute("""INSERT INTO bank_invoice_allocations(entity_id,bank_transaction_id,invoice_id,payment_id,matched_amount,match_method,matched_at,idempotency_key)
                         VALUES(?,?,?,?,?,'manual',?,?)""",(eid,transaction_id,invoice_id,payment_id,amount,matched_at,idem))
            new_balance=round(balance-amount,2)
            status='paid' if new_balance <= .005 else 'partially_paid'
            c.execute("UPDATE outgoing_invoices SET status=?,paid_at=? WHERE id=? AND entity_id IS ?",(status,matched_at if status=='paid' else None,invoice_id,eid))
            c.commit()
            flash(f"{fr_number(amount,2)} € rapprochés avec la facture {inv['invoice_number']}.")
        except (sqlite3.IntegrityError,ValueError):
            c.rollback(); flash("Ce rapprochement a déjà été enregistré ou son montant est invalide.")
        except AccountingError as e:
            c.rollback(); log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"rapprochement facture {invoice_id}: {e}"); flash(f"Rapprochement annulé : {e}")
        finally:
            c.close()
        return redirect(url_for('banking'))

    @app.post("/banking/sync")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_sync():
        c = cx()
        try:
            from profitos.entities import current_entity_id
            eid = current_entity_id()
            row = _connection_row(c, eid)
            if not row or not row["provider_user_id"]:
                flash("Aucune banque connectée.")
                return redirect(url_for("banking"))
            a, t = _sync_powens(c, row)
            flash(f"Synchronisation terminée : {a} compte(s), {t} transaction(s).")
        except Exception as exc:
            try:
                _mark_powens_sync_error(c, row)
            except Exception:
                c.rollback()
            app.logger.exception("Échec synchronisation Powens")
            flash("Synchronisation bancaire impossible. Reconnectez la banque si le consentement a expiré.")
        finally:
            c.close()
        return redirect(url_for("banking"))

    @app.post("/banking/use-balance")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_use_balance():
        from profitos.entities import set_cash_balance, current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        c = cx()
        try:
            rows = c.execute(
                f"SELECT balance FROM bank_accounts WHERE provider IN ('powens','swan') AND disabled=0 AND balance IS NOT NULL AND {ef}",
                ep,
            ).fetchall()
            if not rows:
                flash("Aucun solde bancaire disponible pour cette entité — vérifie que ses comptes bancaires lui sont bien rattachés.")
                return redirect(url_for("banking"))
            total = round(sum(float(r["balance"]) for r in rows), 2)
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            # Chaque compte bancaire est maintenant rattachable à une entité
            # (voir /banking/comptes/<id>/entite) — ce solde ne somme que les
            # comptes de l'entité actuellement active, jamais ceux des autres.
            set_cash_balance(c, eid, total, now[:10], now)
            flash(f"Solde bancaire de {fr_number(total,2)} € appliqué au pilotage financier.")
        finally:
            c.close()
        return redirect(url_for("banking"))


    @app.post('/banking/rapprochement-achat/confirmer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('banking')
    @require_area('invoicing')
    def confirm_purchase_reconciliation():
        tx_id=int(request.form.get('bank_transaction_id') or 0); purchase_id=int(request.form.get('purchase_invoice_id') or 0)
        requested=request.form.get('matched_amount','').strip(); c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        tx=c.execute("""SELECT t.* FROM bank_transactions t JOIN bank_accounts a ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id WHERE t.id=? AND a.entity_id IS ?""",(tx_id,eid)).fetchone()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
        if not tx or not p: c.close(); abort(404)
        if float(tx['amount'] or 0)>=0: c.close(); flash("Cette opération n'est pas une sortie d'argent."); return redirect(url_for('banking'))
        paid=c.execute("SELECT COALESCE(SUM(amount),0) AS x FROM purchase_invoice_payments WHERE entity_id IS ? AND purchase_invoice_id=?",(eid,purchase_id)).fetchone()['x'] or 0
        balance=max(0.0,round(float(p['total'] or 0)-float(paid),2))
        allocated=c.execute("SELECT COALESCE(SUM(matched_amount),0) AS x FROM bank_purchase_allocations WHERE entity_id IS ? AND bank_transaction_id=?",(eid,tx_id)).fetchone()['x'] or 0
        available=max(0.0,round(abs(float(tx['amount'] or 0))-float(allocated),2))
        amount=round(float(requested),2) if requested else min(balance,available)
        if amount<=0 or amount>balance+.001 or amount>available+.001: c.close(); flash("Montant de rapprochement fournisseur invalide."); return redirect(url_for('banking'))
        key=f"bank-purchase:{eid}:{tx_id}:{purchase_id}:{amount:.2f}"
        try:
            c.execute("INSERT INTO purchase_invoice_payments(entity_id,purchase_invoice_id,amount,payment_date,payment_method,reference,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",(eid,purchase_id,amount,tx['transaction_date'] or date.today().isoformat(),'bank',tx['label'],key,now()))
            payment=c.execute("SELECT * FROM purchase_invoice_payments WHERE entity_id IS ? AND idempotency_key=?",(eid,key)).fetchone()
            generate_purchase_partial_payment_entry(c,p,payment)
            c.execute("INSERT INTO bank_purchase_allocations(entity_id,bank_transaction_id,purchase_invoice_id,payment_id,matched_amount,idempotency_key,matched_at) VALUES(?,?,?,?,?,?,?)",(eid,tx_id,purchase_id,payment['id'],amount,key,now()))
            new_balance=round(balance-amount,2); status='paid' if new_balance<=.005 else 'partially_paid'
            c.execute("UPDATE purchase_invoices SET status=?,paid_at=? WHERE id=? AND entity_id IS ?",(status,now() if status=='paid' else None,purchase_id,eid))
            c.commit()
        except (AccountingError, sqlite3.IntegrityError, ValueError) as e:
            c.rollback(); c.close(); flash(f"Rapprochement annulé : {e}"); return redirect(url_for('banking'))
        c.close(); flash("Rapprochement fournisseur enregistré."); return redirect(url_for('banking'))

    @app.post("/banking/transaction/<int:tx_id>/book-fee")
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('banking')
    @require_area('invoicing')
    def banking_transaction_book_fee(tx_id):
        """Book only a small residual debit as a bank fee: Dr 627 / Cr 512."""
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        tx=c.execute("""SELECT t.* FROM bank_transactions t JOIN bank_accounts a
                        ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
                        WHERE t.id=? AND a.entity_id IS ?""",(tx_id,eid)).fetchone()
        if not tx:
            c.close(); abort(404)
        if float(tx['amount'] or 0) >= 0:
            c.close(); flash("Seule une sortie bancaire peut être comptabilisée en frais bancaire.")
            return redirect(url_for('banking'))
        existing=c.execute("""SELECT id FROM accounting_entries
                              WHERE entity_id IS ? AND source_type='bank_fee' AND source_id=? LIMIT 1""",
                           (eid,tx_id)).fetchone()
        if existing:
            c.close(); flash("Les frais de ce mouvement bancaire sont déjà comptabilisés.")
            return redirect(url_for('banking'))
        customer_alloc=c.execute("SELECT COALESCE(SUM(matched_amount),0) AS x FROM bank_invoice_allocations WHERE entity_id IS ? AND bank_transaction_id=?",(eid,tx_id)).fetchone()['x'] or 0
        supplier_alloc=c.execute("SELECT COALESCE(SUM(matched_amount),0) AS x FROM bank_purchase_allocations WHERE entity_id IS ? AND bank_transaction_id=?",(eid,tx_id)).fetchone()['x'] or 0
        allocated=round(float(customer_alloc)+float(supplier_alloc),2)
        residual=max(0.0,round(abs(float(tx['amount'] or 0))-allocated,2))
        reference=max(float(supplier_alloc),float(customer_alloc),0.0)
        limit=max(0.01,round(reference*0.02,2))
        if residual <= .005 or residual > 5.00 or residual > limit:
            c.close(); flash("Écart trop important pour être comptabilisé automatiquement en frais bancaire.")
            return redirect(url_for('banking'))
        expense=c.execute("""SELECT code FROM accounting_chart_of_accounts WHERE code LIKE '627%%'
                             AND (entity_id IS ? OR entity_id IS NULL) AND COALESCE(is_active,1)=1
                             ORDER BY code LIMIT 1""",(eid,)).fetchone()
        bank_account=c.execute("""SELECT code FROM accounting_chart_of_accounts WHERE code LIKE '512%%'
                                  AND (entity_id IS ? OR entity_id IS NULL) AND COALESCE(is_active,1)=1
                                  ORDER BY code LIMIT 1""",(eid,)).fetchone()
        if not expense or not bank_account:
            c.close(); flash("Comptes 627 (services bancaires) ou 512 (banque) absents du plan comptable.")
            return redirect(url_for('banking'))
        entry_date=tx['transaction_date'] or date.today().isoformat()
        label=f"Frais bancaire — {tx['label'] or 'mouvement bancaire'}"; piece=f"BANKFEE-{tx_id}"
        try:
            c.execute("""INSERT INTO accounting_entries
                         (journal_code,piece_number,entry_date,label,source_type,source_id,is_locked,created_by,created_at,entity_id)
                         VALUES('BQ',?,?,?,?,?,0,?,?,?)""",
                      (piece,entry_date,label,'bank_fee',tx_id,session.get('user_email') or session.get('email') or 'system',now(),eid))
            entry=c.execute("""SELECT id FROM accounting_entries WHERE entity_id IS ?
                               AND source_type='bank_fee' AND source_id=? ORDER BY id DESC LIMIT 1""",(eid,tx_id)).fetchone()
            c.execute("""INSERT INTO accounting_entry_lines
                         (entry_id,account_code,auxiliary_name,label,debit,credit,lettrage_code,line_order)
                         VALUES(?,?,?,?,?,?,?,?)""",(entry['id'],expense['code'],None,label,residual,0.0,None,1))
            c.execute("""INSERT INTO accounting_entry_lines
                         (entry_id,account_code,auxiliary_name,label,debit,credit,lettrage_code,line_order)
                         VALUES(?,?,?,?,?,?,?,?)""",(entry['id'],bank_account['code'],None,label,0.0,residual,None,2))
            c.commit()
        except BANK_DB_INTEGRITY_ERRORS:
            # La contrainte unique en base arbitre aussi deux requêtes concurrentes.
            c.rollback(); c.close(); flash("Les frais de ce mouvement bancaire sont déjà comptabilisés.")
            return redirect(url_for('banking'))
        except ValueError as e:
            c.rollback(); c.close(); flash(f"Comptabilisation des frais annulée : {e}")
            return redirect(url_for('banking'))
        c.close(); flash(f"Frais bancaire de {fr_number(residual,2)} € comptabilisé (627 / 512).")
        return redirect(url_for('banking'))


    @app.post("/banking/transaction/<int:tx_id>/ignore")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_transaction_ignore(tx_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        tx=c.execute(
            """SELECT t.id
               FROM bank_transactions t
               JOIN bank_accounts a
                 ON a.provider=t.provider
                AND a.provider_account_id=t.provider_account_id
               WHERE t.id=? AND a.entity_id IS ?""",
            (tx_id,eid),
        ).fetchone()
        if not tx:
            c.close(); abort(404)
        _ensure_bank_workflow_table(c)
        c.execute("""INSERT INTO bank_transaction_workflow(entity_id,bank_transaction_id,state,updated_at)
                     VALUES(?,?,'ignored',?)
                     ON CONFLICT(entity_id,bank_transaction_id)
                     DO UPDATE SET state='ignored',updated_at=excluded.updated_at""",(eid,tx_id,now()))
        c.commit(); c.close()
        flash("Opération bancaire ignorée.")
        return redirect(url_for('banking'))

    @app.post("/banking/transaction/<int:tx_id>/restore")
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_transaction_restore(tx_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        tx=c.execute(
            """SELECT t.id
               FROM bank_transactions t
               JOIN bank_accounts a
                 ON a.provider=t.provider
                AND a.provider_account_id=t.provider_account_id
               WHERE t.id=? AND a.entity_id IS ?""",
            (tx_id,eid),
        ).fetchone()
        if not tx:
            c.close(); abort(404)
        _ensure_bank_workflow_table(c)
        c.execute("DELETE FROM bank_transaction_workflow WHERE entity_id IS ? AND bank_transaction_id=?",(eid,tx_id))
        c.commit(); c.close()
        flash("Opération bancaire remise à traiter.")
        return redirect(url_for('banking'))

    @app.route('/banking/regles', methods=['GET', 'POST'])
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_rules():
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c = cx()
        error = None
        if request.method == 'POST':
            pattern = (request.form.get('pattern') or '').strip()
            category = (request.form.get('category') or '').strip()
            try:
                priority = int(request.form.get('priority') or 0)
            except ValueError:
                priority = 0
            if not pattern or not category:
                error = "Le motif et la catégorie sont obligatoires."
            else:
                c.execute(
                    'INSERT INTO bank_categorization_rules(entity_id,pattern,category,priority,created_at) VALUES(?,?,?,?,?)',
                    (eid, pattern, category, priority, now()),
                )
                c.commit()
                flash(f"Règle ajoutée : « {pattern} » → {category}.")
                return redirect(url_for('banking_rules'))

        rules = c.execute(
            'SELECT * FROM bank_categorization_rules WHERE entity_id IS ? ORDER BY priority DESC, id ASC', (eid,)
        ).fetchall()
        uncategorized_count = c.execute(
            '''SELECT COUNT(*) n FROM bank_transactions t JOIN bank_accounts a ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
           WHERE t.category IS NULL AND a.entity_id IS ?''', (eid,)
        ).fetchone()['n']
        c.close()
        return render_template('banking_rules.html', rules=rules, error=error,
                                uncategorized_count=uncategorized_count,
                                categories=sorted(DEFAULT_CATEGORY_MAPPING.keys()))

    @app.route('/banking/regles/<int:rule_id>/supprimer', methods=['POST'])
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_rule_delete(rule_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c = cx()
        c.execute('DELETE FROM bank_categorization_rules WHERE id=? AND entity_id IS ?', (rule_id,eid))
        c.commit(); c.close()
        flash("Règle supprimée.")
        return redirect(url_for('banking_rules'))

    @app.route('/banking/regles/appliquer', methods=['POST'])
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_rules_apply():
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c = cx()
        rows = c.execute(
            '''SELECT t.id,t.label FROM bank_transactions t JOIN bank_accounts a ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
               WHERE t.category IS NULL AND a.entity_id IS ?''', (eid,)
        ).fetchall()
        applied = 0
        for r in rows:
            category = apply_categorization_rule(c, r['label'], eid)
            if category:
                c.execute('UPDATE bank_transactions SET category=? WHERE id=?', (category, r['id']))
                applied += 1
        c.commit(); c.close()
        flash(f"{applied} transaction(s) catégorisée(s) sur {len(rows)} sans catégorie.")
        return redirect(url_for('banking_rules'))

    @app.route('/banking/transactions/<int:tx_id>/categoriser', methods=['POST'])
    @login_required
    @requires_paid_plan
    @requires_feature('banking')
    def banking_transaction_categorize(tx_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        category=(request.form.get('category') or '').strip() or None
        account_code=(request.form.get('account_code') or '').strip() or None
        vat_raw=(request.form.get('vat_rate') or '').strip()
        vat_rate=float(vat_raw) if vat_raw else None
        c=cx()
        tx=c.execute("""SELECT t.* FROM bank_transactions t JOIN bank_accounts a
                        ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
                        WHERE t.id=? AND a.entity_id IS ?""",(tx_id,eid)).fetchone()
        if not tx:
            c.close(); flash('Transaction bancaire introuvable pour cette entité.')
            return redirect(url_for('banking'))
        if account_code:
            valid=c.execute("SELECT 1 FROM accounting_chart_of_accounts WHERE code=? AND (entity_id IS ? OR entity_id IS NULL) LIMIT 1",(account_code,eid)).fetchone()
            if not valid:
                c.close(); flash('Compte comptable invalide pour cette entité.')
                return redirect(url_for('banking'))
        c.execute('UPDATE bank_transactions SET category=? WHERE id=?',(category,tx_id))
        if account_code:
            signature=_learning_pattern(tx['label'], tx['amount'])
            suggestion=_accounting_suggestion(c,tx,eid)
            c.execute("""INSERT INTO bank_accounting_validations
                (entity_id,bank_transaction_id,category,account_code,vat_rate,counterparty_type,counterparty_id,confidence_score,suggestion_reason,validated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(entity_id,bank_transaction_id) DO UPDATE SET
                category=excluded.category,account_code=excluded.account_code,vat_rate=excluded.vat_rate,
                confidence_score=excluded.confidence_score,suggestion_reason=excluded.suggestion_reason,validated_at=excluded.validated_at""",
                (eid,tx_id,category,account_code,vat_rate,None,None,suggestion['confidence_score'],suggestion['reason'],now()))
            if signature:
                row=c.execute("""SELECT id,confirmations FROM bank_accounting_learning_rules
                    WHERE entity_id IS ? AND pattern=? AND account_code=? AND vat_rate IS ?
                    AND counterparty_type IS NULL AND counterparty_id IS NULL""",(eid,signature,account_code,vat_rate)).fetchone()
                if row:
                    c.execute('UPDATE bank_accounting_learning_rules SET confirmations=?,category=?,last_confirmed_at=? WHERE id=?',
                              (int(row['confirmations'] or 0)+1,category,now(),row['id']))
                else:
                    c.execute("""INSERT INTO bank_accounting_learning_rules
                        (entity_id,pattern,category,account_code,vat_rate,counterparty_type,counterparty_id,confirmations,last_confirmed_at)
                        VALUES(?,?,?,?,?,NULL,NULL,1,?)""",(eid,signature,category,account_code,vat_rate,now()))
        c.commit(); c.close()
        flash('Catégorisation comptable validée et apprise.' if account_code else ('Catégorie mise à jour.' if category else 'Catégorie retirée.'))
        return redirect(url_for('banking'))

