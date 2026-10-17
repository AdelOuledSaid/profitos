"""Chiffrement des secrets de tiers stockés en base (jetons Shopify/WooCommerce,
jetons OAuth Dropbox/Google Drive...).

- Algorithme : AES-256-GCM (chiffrement authentifié) de la bibliothèque
  ``cryptography`` — aucun algorithme « maison ».
- Clé maître : variable d'environnement ``PROFITOS_DATA_KEY`` (obligatoire en
  production dès qu'une intégration stocke un secret). Générer avec :
  ``python -c "import secrets;print(secrets.token_urlsafe(48))"``.
- Une sous-clé par organisation est dérivée par HKDF-SHA256 (RFC 5869, implémenté
  avec ``hmac``/``hashlib`` de la bibliothèque standard) : la fuite d'un secret
  déchiffré d'un client ne donne aucune clé valable pour un autre client.
- Format stocké : ``enc:v1:<base64url(nonce 12 octets || texte chiffré || tag)>``.
  L'identifiant d'organisation est lié au chiffré (données associées GCM) : un
  chiffré recopié dans le schéma d'une autre organisation ne se déchiffre pas.
- Migration transparente : ``decrypt_secret`` renvoie tel quel une valeur
  historique non préfixée (stockée en clair avant cette version) ; toute nouvelle
  écriture est chiffrée. ``reencrypt_legacy_value`` permet de migrer à la volée.

Hors production (PROFITOS_ENV=development/test explicite), en l'absence de
PROFITOS_DATA_KEY, une clé est dérivée de PROFITOS_SECRET_KEY (ou d'une valeur
de développement) afin que l'application reste utilisable localement.
"""
import base64
import hashlib
import hmac
import os

PREFIX = 'enc:v1:'
_NONCE_BYTES = 12


class DataKeyMissing(RuntimeError):
    """Levée en production quand PROFITOS_DATA_KEY n'est pas configurée."""


def _hkdf_sha256(ikm, salt, info, length=32):
    """HKDF (RFC 5869) — extraction puis expansion, HMAC-SHA256."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b'', b'', 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _master_key():
    from profitos.config import is_production
    raw = (os.environ.get('PROFITOS_DATA_KEY') or '').strip()
    if raw:
        return raw.encode('utf-8')
    if is_production():
        raise DataKeyMissing(
            "Clé de chiffrement PROFITOS_DATA_KEY absente : impossible de stocker ou de lire "
            "un identifiant d'intégration en production.")
    fallback = (os.environ.get('PROFITOS_SECRET_KEY') or 'profitos-dev-data-key').encode('utf-8')
    return b'dev-only:' + fallback


def _org_key(org_id):
    return _hkdf_sha256(_master_key(), b'profitos-data-v1', f'org:{int(org_id)}'.encode('ascii'))


def _resolve_org_id(org_id):
    if org_id is not None:
        return int(org_id)
    from flask import session
    value = session.get('org_id')
    if not value:
        raise ValueError('Organisation requise pour chiffrer/déchiffrer un secret.')
    return int(value)


def _aesgcm(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_secret(value, org_id=None):
    """Chiffre une chaîne pour l'organisation donnée (organisation de la session
    par défaut). None/chaîne vide sont conservés tels quels (aucun secret)."""
    if value is None or value == '':
        return value
    if is_encrypted(value):
        return value
    oid = _resolve_org_id(org_id)
    nonce = os.urandom(_NONCE_BYTES)
    aad = f'profitos-org:{oid}'.encode('ascii')
    ct = _aesgcm(_org_key(oid)).encrypt(nonce, str(value).encode('utf-8'), aad)
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode('ascii')


def decrypt_secret(value, org_id=None):
    """Déchiffre une valeur ``enc:v1:``. Une valeur historique en clair est
    renvoyée telle quelle (migration transparente). Lève ValueError si le
    chiffré est altéré ou appartient à une autre organisation."""
    if not is_encrypted(value):
        return value
    oid = _resolve_org_id(org_id)
    try:
        blob = base64.urlsafe_b64decode(value[len(PREFIX):].encode('ascii'))
        nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        aad = f'profitos-org:{oid}'.encode('ascii')
        return _aesgcm(_org_key(oid)).decrypt(nonce, ct, aad).decode('utf-8')
    except DataKeyMissing:
        raise
    except Exception as exc:
        raise ValueError("Secret chiffré illisible (clé de chiffrement modifiée ou donnée altérée).") from exc


def reencrypt_legacy_value(value, org_id=None):
    """Renvoie la forme chiffrée d'une valeur historique en clair (ou la valeur
    inchangée si elle est déjà chiffrée / vide). Utilisé pour migrer en douceur."""
    if value in (None, '') or is_encrypted(value):
        return value
    return encrypt_secret(value, org_id)
