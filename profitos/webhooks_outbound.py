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
import ipaddress
import json
import secrets
import socket
from datetime import datetime
from urllib.parse import urlsplit

import requests

WEBHOOK_EVENTS = ['invoice.sent', 'invoice.paid', 'purchase.created', 'purchase.paid']
_DELIVERY_TIMEOUT = 10


def validate_outbound_webhook_url(url):
    """Valide une URL de webhook sortant définie par le client, avec une vraie
    protection contre le SSRF (Server-Side Request Forgery) : HTTPS
    obligatoire, puis résolution DNS et vérification que TOUTES les adresses
    IP résolues sont publiques — ni privées, ni loopback, ni link-local
    (inclut les métadonnées cloud comme 169.254.169.254), ni réservées.
    Contrairement à validate_webhook_url (réservée à Slack/Teams via une
    liste blanche de domaines fixe), cette fonction doit accepter n'importe
    quel domaine public choisi par le client — la protection vient donc de
    l'adresse IP résolue, jamais du nom d'hôte. Retourne (True, None) ou
    (False, raison lisible)."""
    try:
        p = urlsplit(url)
    except Exception:
        return False, "URL invalide."
    if p.scheme != 'https':
        return False, "L'URL doit utiliser https."
    host = (p.hostname or '').strip()
    if not host:
        return False, "URL invalide."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Nom d'hôte introuvable."
    if not infos:
        return False, "Nom d'hôte introuvable."
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, (
                "Cette adresse pointe vers une ressource interne ou réservée "
                "(réseau privé, local ou métadonnées cloud) — refusée."
            )
    return True, None


def new_webhook_secret():
    return secrets.token_hex(24)


def _sign(secret, timestamp, body):
    message = f"{timestamp}.{body}".encode('utf-8')
    return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()


def deliver_webhook(conn, event_type, data, only_subscription_id=None):
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
        subs = conn.execute(
            "SELECT * FROM webhook_subscriptions WHERE is_active=1 AND id=?", (only_subscription_id,)
        ).fetchall()
    else:
        subs = conn.execute(
            "SELECT * FROM webhook_subscriptions WHERE is_active=1"
        ).fetchall()
    if not subs:
        return
    body = json.dumps({'event': event_type, 'data': data, 'created_at': datetime.utcnow().isoformat()},
                       ensure_ascii=False, default=str)
    timestamp = str(int(datetime.utcnow().timestamp()))
    for sub in subs:
        events = (sub['events'] or '').split(',')
        if event_type not in events:
            continue
        ok, reason = validate_outbound_webhook_url(sub['url'])
        status_code, success, snippet = None, 0, None
        if not ok:
            snippet = f"URL refusée au moment de l'envoi : {reason}"
        else:
            signature = _sign(sub['secret'], timestamp, body)
            headers = {
                'Content-Type': 'application/json',
                'X-ProfitOS-Signature': f"t={timestamp},v1={signature}",
                'X-ProfitOS-Event': event_type,
            }
            try:
                resp = requests.post(
                    sub['url'], data=body.encode('utf-8'), headers=headers,
                    timeout=_DELIVERY_TIMEOUT, allow_redirects=False,
                )
                status_code = resp.status_code
                success = 1 if 200 <= resp.status_code < 300 else 0
                snippet = resp.text[:300]
            except requests.RequestException as e:
                snippet = f"Erreur réseau : {e}"[:300]
        conn.execute(
            """INSERT INTO webhook_deliveries
               (subscription_id,event_type,payload,status_code,success,attempted_at,response_snippet)
               VALUES(?,?,?,?,?,?,?)""",
            (sub['id'], event_type, body, status_code, success, datetime.utcnow().isoformat(), snippet),
        )
    conn.commit()
