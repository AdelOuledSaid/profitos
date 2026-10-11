"""Requêtes HTTP sortantes vers des URL choisies par les clients (webhooks
sortants, boutiques WooCommerce/Shopify) — protection SSRF.

Principe :
1. le nom d'hôte est résolu UNE SEULE FOIS ;
2. TOUTES les adresses obtenues doivent être publiques (refus des réseaux privés,
   loopback, link-local dont 169.254.169.254, CGNAT 100.64.0.0/10, multicast,
   réservés, non spécifiés, IPv6 ULA fc00::/7 et link-local fe80::/10, adresses
   IPv4 encapsulées dans IPv6 : ::ffff:a.b.c.d, 64:ff9b::/96, 6to4 2002::/16) ;
3. la connexion TCP est ouverte vers l'adresse validée elle-même (pas de seconde
   résolution DNS : impossible de « rebinder » le nom vers 127.0.0.1 entre la
   vérification et l'envoi), avec l'en-tête Host et le SNI du nom d'origine ; le
   certificat TLS reste vérifié pour ce nom ;
4. aucune redirection n'est suivie, aucun proxy d'environnement n'est utilisé ;
5. le corps de la réponse est lu de façon bornée et n'est jamais stocké ni affiché
   par les appelants (seul le code HTTP l'est).
"""
import ipaddress
import json as _json
import socket
from urllib.parse import urlsplit, urlencode

_BLOCKED_NETWORKS = [ipaddress.ip_network(n) for n in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
    '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16',
    '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4',
    '255.255.255.255/32',
    '::/128', '::1/128', '64:ff9b::/96', '64:ff9b:1::/48', '100::/64', '2001::/23', '2001:db8::/32',
    '2002::/16', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8',
)]

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class UnsafeDestination(ValueError):
    """Destination refusée (adresse interne/réservée ou URL invalide)."""


def is_public_ip(ip):
    """True si l'adresse est routable publiquement (et non une adresse interne
    déguisée, par exemple ::ffff:127.0.0.1)."""
    try:
        ip = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or not ip.is_global):
        return False
    return not any(ip in net for net in _BLOCKED_NETWORKS if net.version == ip.version)


def resolve_public_host(host, port=443):
    """Résout ``host`` une fois et renvoie la liste des adresses (toutes
    publiques). Lève UnsafeDestination sinon."""
    host = (host or '').strip().rstrip('.')
    if not host:
        raise UnsafeDestination("URL invalide.")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise UnsafeDestination("Nom d'hôte introuvable.")
    addresses = []
    for info in infos:
        addr = info[4][0]
        if '%' in addr:  # identifiant de zone IPv6 (fe80::1%eth0)
            raise UnsafeDestination(
                "Cette adresse pointe vers une ressource interne ou réservée — refusée.")
        if not is_public_ip(addr):
            raise UnsafeDestination(
                "Cette adresse pointe vers une ressource interne ou réservée "
                "(réseau privé, local ou métadonnées cloud) — refusée.")
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise UnsafeDestination("Nom d'hôte introuvable.")
    return addresses


def validate_public_https_url(url):
    """Contrôle statique + résolution : renvoie (host, port, path_with_query, addresses)."""
    try:
        parts = urlsplit(url or '')
    except Exception:
        raise UnsafeDestination("URL invalide.")
    if parts.scheme != 'https':
        raise UnsafeDestination("L'URL doit utiliser https.")
    if parts.username or parts.password:
        raise UnsafeDestination("Les identifiants dans l'URL ne sont pas acceptés.")
    host = (parts.hostname or '').strip()
    if not host:
        raise UnsafeDestination("URL invalide.")
    try:
        port = parts.port or 443
    except ValueError:
        raise UnsafeDestination("URL invalide.")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal and not is_public_ip(host):
        raise UnsafeDestination(
            "Cette adresse pointe vers une ressource interne ou réservée "
            "(réseau privé, local ou métadonnées cloud) — refusée.")
    addresses = [host] if literal else resolve_public_host(host, port)
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    return host, port, path, addresses


class SafeResponse:
    def __init__(self, status_code, content, headers):
        self.status_code = status_code
        self.content = content
        self.headers = headers

    @property
    def text(self):
        return self.content.decode('utf-8', errors='replace')

    def json(self):
        return _json.loads(self.content.decode('utf-8'))


def _ca_bundle():
    try:
        import certifi
        return certifi.where()
    except Exception:  # pragma: no cover - certifi est une dépendance de requests
        return None


def _open_pool(ip, port, hostname, timeout):
    """Pool urllib3 connecté à l'IP validée ; SNI et vérification du certificat
    portent sur le nom d'hôte d'origine. Point d'injection pour les tests."""
    import urllib3
    return urllib3.HTTPSConnectionPool(
        host=ip, port=port, server_hostname=hostname, assert_hostname=hostname,
        cert_reqs='CERT_REQUIRED', ca_certs=_ca_bundle(), timeout=timeout, retries=False,
        maxsize=1,
    )


def safe_request(method, url, *, headers=None, params=None, data=None, json=None, timeout=10,
                 max_bytes=MAX_RESPONSE_BYTES):
    """Envoie une requête HTTPS vers une destination publique validée.

    Lève UnsafeDestination (destination refusée) ou ConnectionError (erreur
    réseau/TLS, message générique). Ne suit jamais les redirections."""
    host, port, path, addresses = validate_public_https_url(url)
    if params:
        path += ('&' if '?' in path else '?') + urlencode(params)
    body = data
    hdrs = dict(headers or {})
    if json is not None:
        body = _json.dumps(json).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/json')
    if isinstance(body, str):
        body = body.encode('utf-8')
    hdrs['Host'] = host if port == 443 else f'{host}:{port}'
    hdrs.setdefault('User-Agent', 'ProfitOS/1.0')
    last_error = None
    for ip in addresses[:3]:
        pool = _open_pool(ip, port, host, timeout)
        try:
            resp = pool.urlopen(method.upper(), path, body=body, headers=hdrs, redirect=False,
                                retries=False, preload_content=False, assert_same_host=False)
            try:
                content = resp.read(max_bytes + 1)[:max_bytes]
            finally:
                resp.release_conn()
            return SafeResponse(resp.status, content, dict(resp.headers))
        except Exception as exc:  # urllib3 : TLS, connexion, délai...
            last_error = exc
        finally:
            try:
                pool.close()
            except Exception:
                pass
    raise ConnectionError("Connexion au serveur distant impossible.") from last_error
