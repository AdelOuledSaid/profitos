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
             a.get("iban"), a.get("type"), a.get("currency") or "EUR", balance,
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
        finally:
            c.close()
        return render_template(
            "banking.html",
            connection=connection,
            accounts=accounts,
            transactions=transactions,
            powens_configured=_configured(),
        )

    @app.post("/banking/connect")
    @login_required
    @requires_paid_plan
    def banking_connect():
        # POST -> GET first. This avoids browsers/service-workers keeping the
        # application on /banking when following an external redirect.
        return redirect(url_for("banking_connect_start"), code=303)

    @app.get("/banking/connect/start")
    @login_required
    @requires_paid_plan
    def banking_connect_start():
        if not _configured():
            flash("Connexion bancaire non configurée : ajoutez POWENS_DOMAIN, POWENS_CLIENT_ID et POWENS_CLIENT_SECRET.")
            return redirect(url_for("banking"))

        domain, client_id, client_secret = _cfg()
        c = cx()
        try:
            row = _connection_row(c)
            if row and row["provider_user_id"]:
                token = _renew_token(row["provider_user_id"])
            else:
                data = _json(requests.post(
                    _api_url("/auth/init"),
                    json={"client_id": client_id, "client_secret": client_secret},
                    timeout=POWENS_TIMEOUT,
                ))
                token = data.get("auth_token") or data.get("access_token") or data.get("token")
                provider_user_id = data.get("id_user")
                if not token or provider_user_id is None:
                    raise RuntimeError("Réponse d'initialisation Powens incomplète.")
                now = datetime.utcnow().replace(microsecond=0).isoformat()
                c.execute(
                    """INSERT INTO bank_connections(provider,provider_user_id,status,created_at,updated_at)
                       VALUES('powens',?,'PENDING',?,?)""",
                    (str(provider_user_id), now, now),
                )
                c.commit()

            if not token:
                raise RuntimeError("Powens n'a pas renvoyé de jeton utilisateur.")

            code_data = _json(requests.get(
                _api_url("/auth/token/code"),
                headers=_headers(token),
                params={"type": "singleAccess"},
                timeout=POWENS_TIMEOUT,
            ))
            code = code_data.get("code")
            if not code:
                raise RuntimeError("Powens n'a pas renvoyé de code Webview.")

            state = secrets.token_urlsafe(24)
            session["powens_connect_state"] = state
            callback = _callback_url()

            params = {
                "domain": f"{domain}.biapi.pro",
                "client_id": client_id,
                "redirect_uri": callback,
                "code": code,
                "state": state,
                "connector_capabilities": "bank",
            }
            webview_url = "https://webview.powens.com/fr/connect?" + urlencode(params)

            # Never log the temporary code. Keep only safe information.
            app.logger.info(
                "POWENS_WEBVIEW_REDIRECT domain=%s client_id=%s redirect_uri=%s",
                f"{domain}.biapi.pro", client_id, callback
            )
            return redirect(webview_url, code=303)

        except Exception as exc:
            app.logger.exception("Échec démarrage connexion Powens")
            flash(f"Connexion bancaire indisponible : {exc}")
            return redirect(url_for("banking"))
        finally:
            c.close()

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
            flash("La banque n'a pas été connectée : " + request.args.get("error_description", request.args["error"]))
            return redirect(url_for("banking"))

        connection_id = request.args.get("connection_id") or request.args.get("id_connection")
        c = cx()
        try:
            row = _connection_row(c)
            if not row:
                raise RuntimeError("Connexion locale introuvable.")
            now = datetime.utcnow().replace(microsecond=0).isoformat()
            if connection_id:
                c.execute(
                    "UPDATE bank_connections SET provider_connection_id=?,status='CONNECTED',updated_at=? WHERE id=?",
                    (str(connection_id), now, row["id"]),
                )
                c.commit()
            count_accounts, count_txs = _sync_powens(c, row)
            flash(f"Banque connectée : {count_accounts} compte(s), {count_txs} transaction(s) synchronisée(s).")
        except Exception as exc:
            app.logger.exception("Échec callback/synchronisation Powens")
            flash(f"Banque connectée mais synchronisation incomplète : {exc}")
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
