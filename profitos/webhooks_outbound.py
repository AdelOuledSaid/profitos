"""Webhooks sortants de ProfitOS : notifie les systèmes du client (son propre
ERP, une automatisation Zapier/Make, un système comptable maison...) quand un
événement métier se produit dans ProfitOS — l'inverse des webhooks entrants
(WeInvoice, Stripe) que ProfitOS reçoit déjà.

Événements pris en charge : invoice.sent, invoice.paid, purchase.created,
purchase.paid — branchés aux mêmes points d'insertion que la génération
automatique d'écritures comptables (voir profitos/accounting.py).
"""
import hashlib
import hmac
import json
import secrets
from datetime import datetime

from profitos.safe_http import UnsafeDestination, safe_request, validate_public_https_url

WEBHOOK_EVENTS = ['invoice.sent', 'invoice.paid', 'purchase.created', 'purchase.paid']
_DELIVERY_TIMEOUT = 10


def validate_outbound_webhook_url(url):
    """Valide une URL de webhook sortant définie par le client, avec une vraie
    protection contre le SSRF (Server-Side Request Forgery) : HTTPS
    obligatoire, puis résolution DNS et vérification que TOUTES les adresses
    IP résolues sont publiques — ni privées, ni loopback, ni link-local
    (inclut les métadonnées cloud comme 169.254.169.254), ni CGNAT
    (100.64.0.0/10), ni réservées, ni IPv6 ULA/link-local, ni IPv4 encapsulée.
    Contrairement à validate_webhook_url (réservée à Slack/Teams via une
    liste blanche de domaines fixe), cette fonction doit accepter n'importe
    quel domaine public choisi par le client — la protection vient donc de
    l'adresse IP résolue, jamais du nom d'hôte. L'envoi lui-même se connecte à
    l'adresse validée (voir profitos/safe_http.py : pas de seconde résolution
    DNS exploitable par « DNS rebinding »). Retourne (True, None) ou
    (False, raison lisible)."""
    try:
        validate_public_https_url(url)
    except UnsafeDestination as e:
        return False, str(e)
    return True, None


def new_webhook_secret():
    return secrets.token_hex(24)


def _sign(secret, timestamp, body):
    message = f"{timestamp}.{body}".encode('utf-8')
    return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()


def deliver_webhook(conn, event_type, data, only_subscription_id=None, entity_id=None):
    """Envoie l'événement à tous les abonnements actifs de ce type sur cette
    organisation (conn = connexion tenant déjà ouverte) — ou à un seul
    abonnement précis si only_subscription_id est fourni (utilisé par le
    bouton « Tester », pour ne jamais notifier les autres abonnés à tort).
    Signe chaque envoi avec HMAC-SHA256 (en-tête X-ProfitOS-Signature, format
    "t=<timestamp>,v1=<signature>" — même principe que Stripe) pour que le
    client puisse vérifier l'authenticité. N'échoue jamais bruyamment : une
    erreur de livraison est journalisée dans webhook_deliveries, jamais levée
    à l'appelant (un webhook sortant qui échoue ne doit jamais casser l'action
    métier qui l'a déclenché)."""
    if only_subscription_id is not None:
        if entity_id is None:
            subs = conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE is_active=1 AND id=? AND entity_id IS NULL",
                (only_subscription_id,)
            ).fetchall()
        else:
            subs = conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE is_active=1 AND id=? AND entity_id=?",
                (only_subscription_id,entity_id)
            ).fetchall()
    else:
        if entity_id is None:
            subs = conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE is_active=1 AND entity_id IS NULL"
            ).fetchall()
        else:
            subs = conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE is_active=1 AND entity_id=?",(entity_id,)
            ).fetchall()
    if not subs:
        return
    event_id='evt_'+secrets.token_hex(12)
    body = json.dumps({'id':event_id,'event': event_type, 'data': data,
                       'created_at': datetime.utcnow().isoformat()},
                       ensure_ascii=False, default=str)
    timestamp = str(int(datetime.utcnow().timestamp()))
    for sub in subs:
        events = (sub['events'] or '').split(',')
        if event_type not in events:
            continue
        status_code, success, snippet = None, 0, None
        signature = _sign(sub['secret'], timestamp, body)
        headers = {
            'Content-Type': 'application/json',
            'X-ProfitOS-Signature': f"t={timestamp},v1={signature}",
            'X-ProfitOS-Event': event_type,
            'X-ProfitOS-Event-Id': event_id,
        }
        try:
            # Résolution unique + connexion à l'adresse validée (anti DNS rebinding),
            # sans redirection. Le corps de la réponse n'est ni stocké ni affiché :
            # seul le code HTTP est conservé dans le journal des livraisons.
            resp = safe_request('POST', sub['url'], data=body.encode('utf-8'), headers=headers,
                                timeout=_DELIVERY_TIMEOUT, max_bytes=4096)
            status_code = resp.status_code
            success = 1 if 200 <= resp.status_code < 300 else 0
            snippet = f"HTTP {resp.status_code}"
        except UnsafeDestination as e:
            snippet = f"URL refusée au moment de l'envoi : {e}"
        except ConnectionError:
            snippet = "Erreur réseau : destination injoignable ou certificat invalide."
        conn.execute(
            """INSERT INTO webhook_deliveries
               (subscription_id,event_type,payload,status_code,success,attempted_at,response_snippet)
               VALUES(?,?,?,?,?,?,?)""",
            (sub['id'], event_type, body, status_code, success, datetime.utcnow().isoformat(), snippet),
        )
    conn.commit()
