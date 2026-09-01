import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import flash, redirect, render_template, request, session, url_for

from profitos.feature_access import requires_paid_plan
from profitos.runtime import *


POWENS_TIMEOUT = 20


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


def _connection_row(c):
    return c.execute(
        "SELECT * FROM bank_connections WHERE provider='powens' ORDER BY id DESC LIMIT 1"
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
    active_balances = []
    for a in accounts:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        balance = a.get("balance")
        try:
            balance = float(balance) if balance is not None else None
        except (TypeError, ValueError):
            balance = None
        disabled = bool(a.get("disabled"))
        if not disabled and balance is not None:
            active_balances.append(balance)
        c.execute(
            """INSERT INTO bank_accounts(provider,provider_account_id,name,iban,account_type,currency,balance,disabled,last_synced_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,provider_account_id) DO UPDATE SET
                 name=excluded.name,iban=excluded.iban,account_type=excluded.account_type,
                 currency=excluded.currency,balance=excluded.balance,disabled=excluded.disabled,
                 last_synced_at=excluded.last_synced_at""",
            ("powens", aid, a.get("name") or a.get("original_name") or "Compte bancaire",
             a.get("iban"), a.get("type"),
             ((a.get("currency") or {}).get("id") if isinstance(a.get("currency"), dict) else (a.get("currency") or "EUR")),
             balance,
             1 if disabled else 0, now),
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
        label = t.get("simplified_wording") or t.get("wording") or t.get("original_wording") or ""
        tx_date = str(t.get("date") or t.get("application_date") or "")[:10]
        c.execute(
            """INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount,raw_status,last_synced_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,provider_transaction_id) DO UPDATE SET
                 provider_account_id=excluded.provider_account_id,transaction_date=excluded.transaction_date,
                 label=excluded.label,amount=excluded.amount,raw_status=excluded.raw_status,
                 last_synced_at=excluded.last_synced_at""",
            ("powens", tid, account_id, tx_date, label, amount,
             str(t.get("state") or t.get("coming") or ""), now),
        )

    c.execute(
        "UPDATE bank_connections SET status='CONNECTED',last_synced_at=?,updated_at=? WHERE id=?",
        (now, now, row["id"]),
    )
    c.commit()
    return len(accounts), len(txs)


def _reconciliation_suggestions(c, transactions):
    reconciled_tx = {
        r["bank_transaction_id"]
        for r in c.execute("SELECT bank_transaction_id FROM bank_invoice_reconciliations").fetchall()
    }
    invoices = c.execute(
        "SELECT * FROM outgoing_invoices WHERE status='sent' AND total>0 ORDER BY issue_date,id"
    ).fetchall()
    suggestions = []
    for t in transactions:
        if t["id"] in reconciled_tx:
            continue
        try:
            amount = float(t["amount"] or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue

        exact = [inv for inv in invoices if abs(float(inv["total"] or 0) - amount) <= 0.01]
        if not exact:
            continue

        label = (t["label"] or "").lower()
        numbered = [inv for inv in exact if (inv["invoice_number"] or "").lower() in label]
        if len(numbered) == 1:
            candidate = numbered[0]
        elif len(exact) == 1:
            candidate = exact[0]
        else:
            # Same amount on several invoices and no unique reference in wording:
            # do not guess.
            continue

        suggestions.append({"transaction": t, "invoice": candidate})
    return suggestions


def register(app):
    @app.route("/banking")
    @login_required
    @requires_paid_plan
    def banking():
        c = cx()
        try:
            connection = _connection_row(c)
            accounts = c.execute(
                "SELECT * FROM bank_accounts WHERE provider='powens' ORDER BY disabled,balance DESC"
            ).fetchall()
            transactions = c.execute(
                "SELECT * FROM bank_transactions WHERE provider='powens' ORDER BY transaction_date DESC,id DESC LIMIT 50"
            ).fetchall()
            reconciliation_suggestions = _reconciliation_suggestions(c, transactions)
            reconciliations = c.execute(
                '''SELECT r.*,t.transaction_date,t.label,t.amount,i.invoice_number,i.client_name
                   FROM bank_invoice_reconciliations r
                   JOIN bank_transactions t ON t.id=r.bank_transaction_id
                   JOIN outgoing_invoices i ON i.id=r.invoice_id
                   ORDER BY r.id DESC LIMIT 20'''
            ).fetchall()
        finally:
            c.close()
        return render_template(
            "banking.html",
            connection=connection,
            accounts=accounts,
            transactions=transactions,
            reconciliation_suggestions=reconciliation_suggestions,
            reconciliations=reconciliations,
            powens_configured=_configured(),
        )

    @app.get("/banking/connect")
    @login_required
    @requires_paid_plan
    def banking_connect():
        if not _configured():
            flash("Connexion bancaire non configurée.")
            return redirect(url_for("banking"))

        domain, client_id, _ = _cfg()
        state = secrets.token_urlsafe(24)
        session["powens_connect_state"] = state
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
    def banking_callback():
        expected = session.pop("powens_connect_state", None)
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
        try:
            row = _connection_row(c)
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
                           WHERE id=?""",
                        (
                            str(provider_user_id),
                            str(connection_id) if connection_id else row["provider_connection_id"],
                            now,
                            row["id"],
                        ),
                    )
                else:
                    c.execute(
                        """INSERT INTO bank_connections(
                               provider,provider_user_id,provider_connection_id,status,created_at,updated_at
                           ) VALUES('powens',?,?, 'CONNECTED',?,?)""",
                        (
                            str(provider_user_id),
                            str(connection_id) if connection_id else None,
                            now,
                            now,
                        ),
                    )
                c.commit()
                row = _connection_row(c)

            elif row and connection_id:
                now = datetime.utcnow().replace(microsecond=0).isoformat()
                c.execute(
                    """UPDATE bank_connections
                       SET provider_connection_id=?,status='CONNECTED',updated_at=? WHERE id=?""",
                    (str(connection_id), now, row["id"]),
                )
                c.commit()
                row = _connection_row(c)

            if not row or not row["provider_user_id"]:
                raise RuntimeError("Utilisateur Powens introuvable après connexion.")

            count_accounts, count_txs = _sync_powens(c, row)
            flash(
                f"Banque connectée : {count_accounts} compte(s), "
                f"{count_txs} transaction(s) synchronisée(s)."
            )

        except Exception as exc:
            app.logger.exception("Échec callback/synchronisation Powens")
            flash("Connexion bancaire terminée, mais la synchronisation doit être finalisée.")
        finally:
            c.close()

        return redirect(url_for("banking"))

    @app.post("/banking/reconcile/<int:transaction_id>/<int:invoice_id>")
    @login_required
    @requires_paid_plan
    def banking_reconcile(transaction_id, invoice_id):
        c = cx()
        try:
            t = c.execute("SELECT * FROM bank_transactions WHERE id=?",(transaction_id,)).fetchone()
            inv = c.execute("SELECT * FROM outgoing_invoices WHERE id=?",(invoice_id,)).fetchone()
            if not t or not inv:
                abort(404)

            if inv["status"] != "sent":
                flash("Cette facture n'est plus disponible pour le rapprochement.")
                return redirect(url_for("banking"))

            amount = float(t["amount"] or 0)
            total = float(inv["total"] or 0)
            if amount <= 0 or abs(amount-total) > 0.01:
                flash("Rapprochement refusé : le montant bancaire ne correspond pas exactement à la facture.")
                return redirect(url_for("banking"))

            already = c.execute(
                "SELECT 1 FROM bank_invoice_reconciliations WHERE bank_transaction_id=? OR invoice_id=?",
                (transaction_id,invoice_id)
            ).fetchone()
            if already:
                flash("Cette transaction ou cette facture est déjà rapprochée.")
                return redirect(url_for("banking"))

            matched_at = now()
            c.execute(
                "INSERT INTO bank_invoice_reconciliations(bank_transaction_id,invoice_id,matched_amount,matched_at) VALUES(?,?,?,?)",
                (transaction_id,invoice_id,amount,matched_at)
            )
            c.execute(
                "UPDATE outgoing_invoices SET status='paid',paid_at=? WHERE id=?",
                (matched_at,invoice_id)
            )
            c.commit()
            log_activity(
                'INVOICE_BANK_RECONCILED',
                f"Facture {inv['invoice_number']} rapprochée avec une transaction bancaire de {amount:,.2f} €"
            )
            flash(f"Facture {inv['invoice_number']} rapprochée et marquée payée.")
        finally:
            c.close()
        return redirect(url_for("banking"))

    @app.post("/banking/sync")
    @login_required
    @requires_paid_plan
    def banking_sync():
        c = cx()
        try:
            row = _connection_row(c)
            if not row or not row["provider_user_id"]:
                flash("Aucune banque connectée.")
                return redirect(url_for("banking"))
            a, t = _sync_powens(c, row)
            flash(f"Synchronisation terminée : {a} compte(s), {t} transaction(s).")
        except Exception as exc:
            app.logger.exception("Échec synchronisation Powens")
            flash(f"Synchronisation bancaire impossible : {exc}")
        finally:
            c.close()
        return redirect(url_for("banking"))

    @app.post("/banking/use-balance")
    @login_required
    @requires_paid_plan
    def banking_use_balance():
        c = cx()
        try:
            rows = c.execute(
                "SELECT balance FROM bank_accounts WHERE provider='powens' AND disabled=0 AND balance IS NOT NULL"
            ).fetchall()
            if not rows:
                flash("Aucun solde bancaire disponible.")
                return redirect(url_for("banking"))
            total = round(sum(float(r["balance"]) for r in rows), 2)
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            c.execute(
                """INSERT INTO financial_settings(id,cash_balance,cash_as_of,updated_at) VALUES(1,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET cash_balance=excluded.cash_balance,
                   cash_as_of=excluded.cash_as_of,updated_at=excluded.updated_at""",
                (total, now[:10], now),
            )
            c.commit()
            flash(f"Solde bancaire de {total:,.2f} € appliqué au pilotage financier.")
        finally:
            c.close()
        return redirect(url_for("banking"))
