"""Import de commandes e-commerce — Shopify et WooCommerce.

AVERTISSEMENT : construit à partir de la documentation publique de ces deux
plateformes telle que je la connais, jamais vérifié contre leurs vrais
serveurs (aucun accès réseau dans cet environnement). À valider avec une
vraie boutique de test avant tout usage réel.

Shopify : connexion via une « app personnalisée » créée par le marchand
lui-même dans son admin Shopify (Paramètres > Apps > Développer des apps),
qui donne un jeton d'accès Admin API — pas de flux OAuth public, plus
simple et plus fiable pour un usage marchand-à-marchand comme celui-ci.

WooCommerce : authentification par clé/secret consommateur (Consumer
Key/Secret), générés depuis WooCommerce > Réglages > Avancé > REST API.
"""
import base64

import requests

SHOPIFY_API_VERSION = '2024-01'


def is_connected(conn, platform):
    row = conn.execute('SELECT 1 FROM ecommerce_connections WHERE platform=?', (platform,)).fetchone()
    return row is not None


def get_connection(conn, platform):
    return conn.execute('SELECT * FROM ecommerce_connections WHERE platform=?', (platform,)).fetchone()


def save_connection(conn, platform, shop_domain, credential_1, credential_2, connected_by):
    from datetime import datetime
    conn.execute(
        """INSERT INTO ecommerce_connections(platform,shop_domain,credential_1,credential_2,connected_at,connected_by)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(platform) DO UPDATE SET
             shop_domain=excluded.shop_domain,credential_1=excluded.credential_1,
             credential_2=excluded.credential_2,connected_at=excluded.connected_at,connected_by=excluded.connected_by""",
        (platform, shop_domain, credential_1, credential_2, datetime.utcnow().isoformat(), connected_by),
    )
    conn.commit()


def fetch_shopify_orders(shop_domain, access_token, limit=20):
    """Liste les commandes récentes (les plus récentes en premier). Renvoie
    une liste normalisée, indépendante du format brut Shopify."""
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    try:
        resp = requests.get(
            url, headers={'X-Shopify-Access-Token': access_token},
            params={'status': 'any', 'limit': limit, 'order': 'created_at desc'},
            timeout=20,
        )
    except requests.RequestException as e:
        raise ValueError(f"Connexion à Shopify impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"Appel Shopify échoué ({resp.status_code}) : {resp.text[:300]}")
    orders = resp.json().get('orders', [])
    result = []
    for o in orders:
        customer = o.get('customer') or {}
        name = ' '.join(filter(None, [customer.get('first_name'), customer.get('last_name')])) or o.get('email') or 'Client Shopify'
        line_items = [
            {'label': li.get('name', ''), 'qty': float(li.get('quantity') or 1), 'unit_price': float(li.get('price') or 0)}
            for li in o.get('line_items', [])
        ]
        result.append({
            'external_id': str(o.get('id')), 'order_number': o.get('name'),
            'customer_name': name, 'customer_email': o.get('email') or customer.get('email'),
            'total': float(o.get('total_price') or 0), 'currency': o.get('currency'),
            'order_date': (o.get('created_at') or '')[:10], 'line_items': line_items,
        })
    return result


def fetch_woocommerce_orders(shop_domain, consumer_key, consumer_secret, limit=20):
    """Liste les commandes récentes via l'API REST WooCommerce (clé/secret
    consommateur, authentification HTTP Basic)."""
    url = f"https://{shop_domain}/wp-json/wc/v3/orders"
    auth_bytes = f"{consumer_key}:{consumer_secret}".encode('utf-8')
    auth_header = 'Basic ' + base64.b64encode(auth_bytes).decode('ascii')
    try:
        resp = requests.get(
            url, headers={'Authorization': auth_header},
            params={'per_page': limit, 'orderby': 'date', 'order': 'desc'},
            timeout=20,
        )
    except requests.RequestException as e:
        raise ValueError(f"Connexion à WooCommerce impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"Appel WooCommerce échoué ({resp.status_code}) : {resp.text[:300]}")
    orders = resp.json()
    result = []
    for o in orders:
        billing = o.get('billing') or {}
        name = ' '.join(filter(None, [billing.get('first_name'), billing.get('last_name')])) or billing.get('email') or 'Client WooCommerce'
        line_items = [
            {'label': li.get('name', ''), 'qty': float(li.get('quantity') or 1),
             'unit_price': float(li.get('price') or 0)}
            for li in o.get('line_items', [])
        ]
        result.append({
            'external_id': str(o.get('id')), 'order_number': f"#{o.get('number', o.get('id'))}",
            'customer_name': name, 'customer_email': billing.get('email'),
            'total': float(o.get('total') or 0), 'currency': o.get('currency'),
            'order_date': (o.get('date_created') or '')[:10], 'line_items': line_items,
        })
    return result
