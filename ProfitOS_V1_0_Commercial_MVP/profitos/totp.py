"""Mots de passe à usage unique basés sur le temps (TOTP, RFC 6238 / HOTP RFC 4226),
implémentés avec la bibliothèque standard (hmac, hashlib, base64, struct).

Compatible avec les applications d'authentification usuelles (Google
Authenticator, Microsoft Authenticator, 1Password, Aegis...) : SHA-1, 6 chiffres,
pas de 30 secondes. Pas de QR code (aucune dépendance) : l'URI ``otpauth://`` et la
clé en groupes de 4 caractères sont affichées pour une saisie manuelle.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30


def new_secret():
    """Clé aléatoire de 160 bits, encodée en base32 (sans « = »)."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _key(secret_b32):
    s = (secret_b32 or '').replace(' ', '').upper()
    s += '=' * (-len(s) % 8)
    return base64.b32decode(s)


def hotp(secret_b32, counter, digits=DIGITS):
    mac = hmac.new(_key(secret_b32), struct.pack('>Q', int(counter)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def current_step(at=None):
    return int((time.time() if at is None else at) // PERIOD)


def totp(secret_b32, at=None):
    return hotp(secret_b32, current_step(at))


def verify(secret_b32, code, at=None, window=1, last_step=None):
    """Vérifie un code (tolérance ±1 pas de 30 s). Renvoie le pas validé, ou None.

    ``last_step`` empêche le rejeu d'un code déjà utilisé (le pas renvoyé doit
    être enregistré par l'appelant)."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != DIGITS or not secret_b32:
        return None
    step = current_step(at)
    for delta in range(-window, window + 1):
        candidate = step + delta
        if last_step is not None and candidate <= int(last_step):
            continue
        if hmac.compare_digest(hotp(secret_b32, candidate), code):
            return candidate
    return None


def provisioning_uri(secret_b32, account_name, issuer='ProfitOS'):
    label = quote(f'{issuer}:{account_name}')
    return (f'otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}'
            f'&algorithm=SHA1&digits={DIGITS}&period={PERIOD}')


def grouped(secret_b32):
    return ' '.join(secret_b32[i:i + 4] for i in range(0, len(secret_b32), 4))


def new_recovery_codes(n=10):
    """Codes de secours lisibles (xxxxx-xxxxx) ; seuls leurs hachés sont stockés."""
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    return ['-'.join(''.join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2)) for _ in range(n)]


def hash_recovery_code(code):
    normalized = ''.join(ch for ch in str(code or '').lower() if ch.isalnum())
    return hashlib.sha256(('profitos-recovery:' + normalized).encode('utf-8')).hexdigest()
