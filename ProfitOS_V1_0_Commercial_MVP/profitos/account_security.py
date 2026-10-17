"""Sécurité des comptes : schéma, politique de mot de passe, verrouillage par
compte, sessions serveur (déconnexion d'un seul appareil), nouveaux appareils,
double authentification (TOTP) et ré-authentification avant action sensible.

Les fonctions de ce module sont appelées par les routes de compte
(profitos/routes/account.py) et par la gestion de session (profitos/runtime.py).
"""
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app, g, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from profitos import totp as totp_mod

# ---------------------------------------------------------------------------
# Schéma (base AUTH) — idempotent, appelé au démarrage par create_app().
# ---------------------------------------------------------------------------

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS team_invitations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    invited_by INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by INTEGER,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_team_invitations_org ON team_invitations(organization_id);
CREATE TABLE IF NOT EXISTS user_sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    sid_hash TEXT NOT NULL UNIQUE,
    user_agent TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);
CREATE TABLE IF NOT EXISTS user_devices(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    device_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT,
    UNIQUE(user_id,device_hash)
);
CREATE TABLE IF NOT EXISTS outbound_email_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    user_id INTEGER,
    kind TEXT NOT NULL,
    recipient_hash TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbound_email_org ON outbound_email_log(organization_id,created_at);
CREATE INDEX IF NOT EXISTS idx_outbound_email_rcpt ON outbound_email_log(recipient_hash,kind,created_at);
CREATE TABLE IF NOT EXISTS swan_account_bindings(
    swan_account_id TEXT PRIMARY KEY,
    organization_id INTEGER NOT NULL,
    entity_id INTEGER,
    environment TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS swan_onboarding_bindings(
    onboarding_id TEXT PRIMARY KEY,
    organization_id INTEGER NOT NULL,
    entity_id INTEGER,
    environment TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS swan_webhook_inbox(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT,
    resource_id TEXT,
    organization_id INTEGER,
    received_at TEXT NOT NULL
)
'''

_COLUMNS = {
    'users': (
        ('failed_login_count', 'INTEGER DEFAULT 0'),
        ('failed_login_window_start', 'TEXT'),
        ('locked_until', 'TEXT'),
        ('totp_secret', 'TEXT'),
        ('totp_enabled', 'INTEGER DEFAULT 0'),
        ('totp_recovery_codes', 'TEXT'),
        ('totp_last_step', 'INTEGER'),
        ('pending_email', 'TEXT'),
        ('pending_email_token', 'TEXT'),
        ('pending_email_sent_at', 'TEXT'),
        ('password_changed_at', 'TEXT'),
        ('membership_lost_at', 'TEXT'),
        ('anonymized_at', 'TEXT'),
    ),
    'organizations': (
        ('require_2fa', 'INTEGER DEFAULT 0'),
        ('deleted_at', 'TEXT'),
        ('purge_after', 'TEXT'),
        ('deletion_requested_by', 'INTEGER'),
    ),
    'api_keys': (
        ('expires_at', 'TEXT'),
    ),
}


def ensure_account_security_schema():
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        c.executescript(_SCHEMA)
        c.commit()
        for table, cols in _COLUMNS.items():
            existing = {r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
            for col, ddl in cols:
                if col not in existing:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')
        c.commit()
    finally:
        c.close()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row_get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


# ---------------------------------------------------------------------------
# Politique de mot de passe
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 12
_COMMON_PASSWORDS = None


def common_passwords():
    global _COMMON_PASSWORDS
    if _COMMON_PASSWORDS is None:
        path = Path(__file__).resolve().parent / 'data' / 'common_passwords.txt'
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            lines = []
        _COMMON_PASSWORDS = {l.strip().lower() for l in lines if l.strip() and not l.startswith('#')}
    return _COMMON_PASSWORDS


def password_policy_error(password, email=None):
    """Message d'erreur (français) si le mot de passe est refusé, sinon None.

    - au moins 12 caractères ;
    - absent de la liste des mots de passe les plus courants ;
    - ne contient pas la partie locale de l'adresse e-mail (avant « @ »)."""
    password = password or ''
    if len(password) < MIN_PASSWORD_LENGTH:
        return f'Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères.'
    if len(password) > 256:
        return 'Le mot de passe est trop long (256 caractères maximum).'
    lowered = password.lower()
    if lowered in common_passwords():
        return 'Ce mot de passe fait partie des plus utilisés : choisissez-en un autre.'
    if len(set(password)) < 5:
        return 'Le mot de passe est trop répétitif : variez les caractères.'
    local = (email or '').split('@')[0].strip().lower()
    if len(local) >= 3 and local in lowered:
        return 'Le mot de passe ne doit pas contenir votre adresse e-mail.'
    return None


# ---------------------------------------------------------------------------
# Vérification du mot de passe à temps constant + verrouillage par compte
# ---------------------------------------------------------------------------

_DUMMY_HASH = None
LOCK_THRESHOLD = int(os.environ.get('LOGIN_LOCK_THRESHOLD', '10'))
LOCK_WINDOW_MINUTES = int(os.environ.get('LOGIN_LOCK_WINDOW_MINUTES', '15'))
LOCK_DURATION_MINUTES = int(os.environ.get('LOGIN_LOCK_MINUTES', '15'))


def dummy_password_check(password):
    """Calcule un hachage factice : une adresse inconnue répond dans le même
    temps qu'une adresse existante (pas d'énumération par chronométrage)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash(secrets.token_hex(16))
    check_password_hash(_DUMMY_HASH, password or '')
    return False


def account_locked_until(user):
    until = _parse(_row_get(user, 'locked_until'))
    if until and until > _now():
        return until
    return None


def register_login_failure(user):
    """Compte un échec ; au-delà du seuil dans la fenêtre, verrouille le compte
    temporairement et prévient son titulaire par e-mail. Renvoie True si le
    compte vient d'être verrouillé."""
    from profitos.runtime import auth_cx
    now = _now()
    start = _parse(_row_get(user, 'failed_login_window_start'))
    count = int(_row_get(user, 'failed_login_count', 0) or 0)
    if not start or now - start > timedelta(minutes=LOCK_WINDOW_MINUTES):
        start, count = now, 0
    count += 1
    locked = count >= LOCK_THRESHOLD
    c = auth_cx()
    try:
        if locked:
            c.execute('UPDATE users SET failed_login_count=0,failed_login_window_start=NULL,locked_until=? WHERE id=?',
                      (_iso(now + timedelta(minutes=LOCK_DURATION_MINUTES)), user['id']))
        else:
            c.execute('UPDATE users SET failed_login_count=?,failed_login_window_start=? WHERE id=?',
                      (count, _iso(start), user['id']))
        c.commit()
    finally:
        c.close()
    if locked:
        notify_user(user, 'Compte temporairement verrouillé',
                    f"Plusieurs tentatives de connexion infructueuses ont eu lieu sur votre compte ProfitOS. "
                    f"Par sécurité, la connexion est bloquée pendant {LOCK_DURATION_MINUTES} minutes. "
                    f"Si ce n'était pas vous, nous vous conseillons de changer votre mot de passe.",
                    cta_label='Réinitialiser mon mot de passe', cta_endpoint='forgot_password')
    return locked


def reset_login_failures(user_id):
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        c.execute('UPDATE users SET failed_login_count=0,failed_login_window_start=NULL,locked_until=NULL WHERE id=?',
                  (user_id,))
        c.commit()
    finally:
        c.close()


def notify_user(user, title, intro, cta_label=None, cta_endpoint=None):
    """E-mail de sécurité au titulaire du compte (jamais bloquant)."""
    from profitos.runtime import app_base_url, send_email
    try:
        cta_url = f"{app_base_url()}{url_for(cta_endpoint)}" if cta_endpoint else None
        html = render_template('email_transactional.html', title=title, intro=intro,
                               cta_label=cta_label or 'Ouvrir ProfitOS', cta_url=cta_url or app_base_url(),
                               footer="Message de sécurité automatique envoyé par ProfitOS.")
        return send_email(user['email'], f'ProfitOS — {title}', html, kind='system')
    except Exception:
        current_app.logger.exception('Security notification failed user_id=%s', _row_get(user, 'id'))
        return {'sent': False}


# ---------------------------------------------------------------------------
# Sessions serveur : chaque connexion a un identifiant révocable.
# ---------------------------------------------------------------------------

def _digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


DEVICE_COOKIE = 'profitos_device'


def start_user_session(user, org_id, role):
    """Ouvre une session authentifiée (après mot de passe et, le cas échéant,
    second facteur). Remplace entièrement la session précédente."""
    from profitos.runtime import auth_cx
    sid = secrets.token_urlsafe(32)
    ts = _iso(_now())
    pending_ref = session.get('pending_ref_code')
    session.clear()
    if pending_ref:
        session['pending_ref_code'] = pending_ref
    session.permanent = True
    session['user_id'] = user['id']
    session['org_id'] = org_id
    session['role'] = role
    session['auth_version'] = int(_row_get(user, 'auth_version', 0) or 0)
    session['sid'] = sid
    session['login_at'] = ts
    session['last_seen'] = ts
    c = auth_cx()
    try:
        c.execute('INSERT INTO user_sessions(user_id,sid_hash,user_agent,created_at,last_seen_at) VALUES(?,?,?,?,?)',
                  (user['id'], _digest(sid), (request.headers.get('User-Agent') or '')[:240] or None, ts, ts))
        c.commit()
    finally:
        c.close()
    _register_device(user)


def session_sid_valid(user_id):
    """True si la session courante n'a pas été révoquée (déconnexion de cet
    appareil). Les sessions ouvertes avant cette version n'ont pas de « sid » :
    elles restent valides jusqu'à leur expiration (ou un changement de mot de passe)."""
    from profitos.runtime import auth_cx
    sid = session.get('sid')
    if not sid:
        return True
    cached = getattr(g, '_sid_valid', None)
    if cached is not None:
        return cached
    c = auth_cx()
    try:
        row = c.execute('SELECT revoked_at FROM user_sessions WHERE sid_hash=? AND user_id=?',
                        (_digest(sid), user_id)).fetchone()
    finally:
        c.close()
    ok = bool(row) and not row['revoked_at']
    g._sid_valid = ok
    return ok


def revoke_current_session():
    """Déconnecte uniquement cet appareil. Renvoie False pour une session
    historique sans identifiant (l'appelant invalide alors toutes les sessions)."""
    from profitos.runtime import auth_cx
    sid = session.get('sid')
    if not sid:
        return False
    c = auth_cx()
    try:
        c.execute('UPDATE user_sessions SET revoked_at=? WHERE sid_hash=? AND revoked_at IS NULL',
                  (_iso(_now()), _digest(sid)))
        c.commit()
    finally:
        c.close()
    return True


def revoke_all_sessions(user_id, except_current=False):
    from profitos.runtime import auth_cx
    keep = _digest(session.get('sid')) if except_current and session.get('sid') else None
    c = auth_cx()
    try:
        if keep:
            c.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND sid_hash!=?',
                      (_iso(_now()), user_id, keep))
        else:
            c.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',
                      (_iso(_now()), user_id))
        c.commit()
    finally:
        c.close()


def bump_auth_version_keep_current(user_id):
    """Invalide toutes les AUTRES sessions (changement de mot de passe/e-mail,
    activation 2FA) tout en gardant celle de l'utilisateur qui agit."""
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        c.execute('UPDATE users SET auth_version=COALESCE(auth_version,0)+1,updated_at=? WHERE id=?',
                  (_iso(_now()), user_id))
        row = c.execute('SELECT auth_version FROM users WHERE id=?', (user_id,)).fetchone()
        c.commit()
    finally:
        c.close()
    session['auth_version'] = int(row['auth_version'] or 0) if row else 0
    revoke_all_sessions(user_id, except_current=True)


def _register_device(user):
    """Mémorise l'appareil (cookie aléatoire longue durée) et prévient le
    titulaire par e-mail lors d'une connexion depuis un nouvel appareil."""
    from profitos.runtime import auth_cx
    raw = request.cookies.get(DEVICE_COOKIE) or ''
    if len(raw) < 20 or len(raw) > 100:
        raw = secrets.token_urlsafe(24)
        g.set_device_cookie = raw
    device_hash = _digest(raw)
    ts = _iso(_now())
    c = auth_cx()
    try:
        known = c.execute('SELECT id FROM user_devices WHERE user_id=? AND device_hash=?',
                          (user['id'], device_hash)).fetchone()
        has_any = c.execute('SELECT 1 FROM user_devices WHERE user_id=? LIMIT 1', (user['id'],)).fetchone()
        if known:
            c.execute('UPDATE user_devices SET last_seen_at=? WHERE id=?', (ts, known['id']))
        else:
            c.execute('INSERT INTO user_devices(user_id,device_hash,first_seen_at,last_seen_at) VALUES(?,?,?,?)',
                      (user['id'], device_hash, ts, ts))
        c.commit()
    finally:
        c.close()
    if not known and has_any:
        agent = (request.headers.get('User-Agent') or 'navigateur inconnu')[:120]
        notify_user(user, 'Nouvelle connexion à votre compte',
                    f"Une connexion à votre compte ProfitOS vient d'avoir lieu depuis un nouvel appareil ({agent}). "
                    "Si c'était vous, aucune action n'est nécessaire. Sinon, changez immédiatement votre mot de passe.",
                    cta_label='Sécuriser mon compte', cta_endpoint='forgot_password')


def set_device_cookie(response):
    value = getattr(g, 'set_device_cookie', None)
    if value:
        from profitos.config import is_production
        response.set_cookie(DEVICE_COOKIE, value, max_age=2 * 365 * 86400, httponly=True,
                            secure=is_production(), samesite='Lax')
    return response


# ---------------------------------------------------------------------------
# Expiration de session (inactivité + durée absolue)
# ---------------------------------------------------------------------------

def session_expired():
    """True si la session dépasse la durée d'inactivité ou la durée absolue.
    Met à jour l'horodatage d'activité au plus une fois par minute."""
    if not session.get('user_id'):
        return False
    now = _now()
    idle_minutes = int(current_app.config.get('SESSION_IDLE_MINUTES', 120) or 120)
    absolute = current_app.config.get('PERMANENT_SESSION_LIFETIME') or timedelta(hours=12)
    last_seen = _parse(session.get('last_seen'))
    login_at = _parse(session.get('login_at'))
    if last_seen and now - last_seen > timedelta(minutes=idle_minutes):
        return True
    if login_at and now - login_at > absolute:
        return True
    if not last_seen or (now - last_seen).total_seconds() >= 60:
        session['last_seen'] = _iso(now)
        if not login_at:
            session['login_at'] = _iso(now)
    return False


# ---------------------------------------------------------------------------
# Ré-authentification avant une action sensible
# ---------------------------------------------------------------------------

def verify_reauth(user, field='current_password'):
    """True si le mot de passe actuel saisi dans le formulaire est correct.
    Si la double authentification est active, un code TOTP valide est accepté
    en plus du mot de passe (champ « totp_code ») mais n'est pas exigé."""
    password = request.form.get(field, '')
    if not user or not password:
        return False
    ok = check_password_hash(user['password_hash'], password)
    if not ok:
        from profitos.runtime import log_security_event
        log_security_event('REAUTH_FAILED', 'FAILURE', user_id=user['id'], target='account')
    return ok


# ---------------------------------------------------------------------------
# Double authentification (TOTP)
# ---------------------------------------------------------------------------

def user_has_2fa(user):
    return bool(int(_row_get(user, 'totp_enabled', 0) or 0)) and bool(_row_get(user, 'totp_secret'))


def totp_secret_for(user):
    """Le secret TOTP est chiffré au repos (clé dérivée « utilisateur »)."""
    from profitos.crypto import decrypt_secret
    value = _row_get(user, 'totp_secret')
    return decrypt_secret(value, org_id=_user_scope(user)) if value else None


def _user_scope(user):
    # Les secrets TOTP ne sont rattachés à aucune organisation : sous-clé dérivée
    # d'un espace distinct (identifiants négatifs) pour ne jamais partager celle d'un client.
    return -int(user['id'])


def encrypt_totp_secret(user, secret):
    from profitos.crypto import encrypt_secret
    return encrypt_secret(secret, org_id=_user_scope(user))


def verify_second_factor(user, code):
    """Accepte un code TOTP (anti-rejeu) ou un code de secours (usage unique).
    Renvoie 'totp', 'recovery' ou None."""
    from profitos.runtime import auth_cx
    secret = totp_secret_for(user)
    step = totp_mod.verify(secret, code, last_step=_row_get(user, 'totp_last_step'))
    c = auth_cx()
    try:
        if step is not None:
            c.execute('UPDATE users SET totp_last_step=? WHERE id=?', (step, user['id']))
            c.commit()
            return 'totp'
        hashes = json.loads(_row_get(user, 'totp_recovery_codes', '[]') or '[]')
        candidate = totp_mod.hash_recovery_code(code)
        if candidate in hashes:
            hashes.remove(candidate)
            c.execute('UPDATE users SET totp_recovery_codes=? WHERE id=?', (json.dumps(hashes), user['id']))
            c.commit()
            return 'recovery'
        return None
    finally:
        c.close()


def org_requires_2fa(org):
    return bool(int(_row_get(org, 'require_2fa', 0) or 0))
