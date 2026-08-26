from flask import Flask, render_template, request, redirect, url_for, flash, abort, session, g, current_app, jsonify, Response
import sqlite3, json, re, math, unicodedata, secrets, functools, os, hashlib, time, io
try:
    from dotenv import load_dotenv
    load_dotenv()  # charge .env s'il existe (facultatif — ignoré silencieusement en son absence)
except ImportError:
    pass
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from urllib.parse import urlsplit
import pandas as pd
import requests
from pypdf import PdfReader
from docx import Document
from . import db as dbmod

BASE=Path(__file__).resolve().parent.parent
AUTH_DB=BASE/'profitos_auth.db'; TENANTS=BASE/'tenant_data'; TENANTS.mkdir(exist_ok=True); UP=Path(os.environ.get('PROFITOS_UPLOAD_DIR') or (BASE/'uploads')); UP.mkdir(parents=True,exist_ok=True)
BOAMP='https://www.boamp.fr/api/explore/v2.1/catalog/datasets/boamp/records'
PARIS=ZoneInfo('Europe/Paris')

def auth_cx():
    return dbmod.connect_auth(AUTH_DB)

def tenant_db(org_id):
    return TENANTS/f"org_{int(org_id)}.db"

def cx():
    org_id=session.get('org_id')
    if not org_id: raise RuntimeError('Organisation non sélectionnée')
    return dbmod.connect_tenant(org_id, tenant_db(org_id))

def now(): return datetime.now(timezone.utc).isoformat()

def init_auth_db():
    c=auth_cx(); c.executescript('''
    CREATE TABLE IF NOT EXISTS organizations(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,slug TEXT UNIQUE,plan TEXT DEFAULT 'TRIAL',status TEXT DEFAULT 'ACTIVE',trial_ends_at TEXT,stripe_customer_id TEXT,stripe_subscription_id TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,full_name TEXT,is_active INTEGER DEFAULT 1,email_verified INTEGER DEFAULT 0,verification_token TEXT,verification_sent_at TEXT,reset_token TEXT,reset_token_expires TEXT,auth_version INTEGER DEFAULT 0,theme_preference TEXT DEFAULT 'dark',last_seen_changelog TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS memberships(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,organization_id INTEGER NOT NULL,role TEXT DEFAULT 'OWNER',created_at TEXT,UNIQUE(user_id,organization_id));
    CREATE TABLE IF NOT EXISTS activity_log(id INTEGER PRIMARY KEY AUTOINCREMENT,organization_id INTEGER,user_id INTEGER,event_type TEXT,description TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS security_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id INTEGER,
        user_id INTEGER,
        event_type TEXT NOT NULL,
        outcome TEXT NOT NULL,
        target TEXT,
        ip_hash TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stripe_webhook_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stripe_event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        processed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS partner_directory(
        organization_id INTEGER PRIMARY KEY,
        company_name TEXT,
        department TEXT,
        activities TEXT,
        contact_email TEXT,
        opted_in INTEGER DEFAULT 0,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS integration_interest(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id INTEGER,
        provider TEXT,
        email TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS demo_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT,
        company TEXT,
        email TEXT,
        phone TEXT,
        sector TEXT,
        company_size TEXT,
        primary_need TEXT,
        message TEXT,
        status TEXT DEFAULT 'NOUVEAU',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS api_keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id INTEGER NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT,
        created_by TEXT,
        created_at TEXT,
        last_used_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS buyer_signals(
        buyer_name_norm TEXT NOT NULL,
        organization_id INTEGER NOT NULL,
        buyer_name_display TEXT,
        invoice_count INTEGER,
        avg_days_overdue REAL,
        updated_at TEXT,
        PRIMARY KEY(buyer_name_norm,organization_id)
    );
    CREATE TABLE IF NOT EXISTS sector_dso_signals(
        organization_id INTEGER NOT NULL,
        snapshot_date TEXT NOT NULL,
        avg_days_overdue REAL,
        updated_at TEXT,
        PRIMARY KEY(organization_id,snapshot_date)
    );
    CREATE TABLE IF NOT EXISTS referrals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_org_id INTEGER NOT NULL,
        referred_org_id INTEGER NOT NULL UNIQUE,
        status TEXT DEFAULT 'PENDING',
        rewarded_at TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS public_invoice_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        invoice_local_id INTEGER NOT NULL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS export_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        export_type TEXT,
        created_at TEXT
    );
    '''); c.commit()
    # Migration douce pour les bases auth créées avant l'ajout des colonnes de vérification/reset.
    cols=[r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()]
    for col,ddl in (
        ('email_verified',"ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0"),
        ('verification_token',"ALTER TABLE users ADD COLUMN verification_token TEXT"),
        ('verification_sent_at',"ALTER TABLE users ADD COLUMN verification_sent_at TEXT"),
        ('reset_token',"ALTER TABLE users ADD COLUMN reset_token TEXT"),
        ('reset_token_expires',"ALTER TABLE users ADD COLUMN reset_token_expires TEXT"),
        ('auth_version',"ALTER TABLE users ADD COLUMN auth_version INTEGER DEFAULT 0"),
        ('theme_preference',"ALTER TABLE users ADD COLUMN theme_preference TEXT DEFAULT 'dark'"),
        ('last_seen_changelog',"ALTER TABLE users ADD COLUMN last_seen_changelog TEXT"),
    ):
        if col not in cols: c.execute(ddl)
    demo_cols=[r['name'] for r in c.execute('PRAGMA table_info(demo_requests)').fetchall()]
    for col,ddl in (
        ('sector',"ALTER TABLE demo_requests ADD COLUMN sector TEXT"),
        ('company_size',"ALTER TABLE demo_requests ADD COLUMN company_size TEXT"),
        ('primary_need',"ALTER TABLE demo_requests ADD COLUMN primary_need TEXT"),
        ('status',"ALTER TABLE demo_requests ADD COLUMN status TEXT DEFAULT 'NOUVEAU'"),
    ):
        if col not in demo_cols: c.execute(ddl)

    org_cols=[r['name'] for r in c.execute('PRAGMA table_info(organizations)').fetchall()]
    for col,ddl in (
        ('stripe_customer_id',"ALTER TABLE organizations ADD COLUMN stripe_customer_id TEXT"),
        ('stripe_subscription_id',"ALTER TABLE organizations ADD COLUMN stripe_subscription_id TEXT"),
        ('referral_code',"ALTER TABLE organizations ADD COLUMN referral_code TEXT"),
    ):
        if col not in org_cols: c.execute(ddl)
    c.commit(); c.close()
    ensure_security_events_table()


def production_dependency_status():
    """Retourne un état non sensible des dépendances de production.

    Aucune URL, clé, credential ou détail de connexion n'est exposé.
    """
    status = {
        'database': {'configured': bool(os.environ.get('DATABASE_URL')), 'ok': False},
        'rate_limit_store': {'configured': bool(os.environ.get('REDIS_URL')), 'ok': None},
        'email': {'configured': bool(os.environ.get('RESEND_API_KEY')) or bool(os.environ.get('SMTP_HOST')), 'ok': None},
    }

    # DB = seul backend testé activement ici, car /readyz doit rester rapide et
    # ne pas provoquer d'appel tiers à chaque health check.
    try:
        ok, backend, error = database_readiness()
        status['database']['ok'] = bool(ok)
        status['database']['backend'] = backend
    except Exception:
        status['database']['ok'] = False
        status['database']['backend'] = 'unknown'

    # Redis/Resend sont signalés comme configurés, sans exposer ni appeler
    # leurs endpoints depuis le health check.
    if status['rate_limit_store']['configured']:
        status['rate_limit_store']['ok'] = True
    if status['email']['configured']:
        status['email']['ok'] = True

    return status


def log_ops_event(event_type, outcome='INFO', detail=None):
    """Log JSON structuré destiné aux logs Render.

    Ne jamais passer de secret/token/URL complète dans detail.
    """
    try:
        payload = {
            'event': str(event_type)[:80],
            'outcome': str(outcome)[:24],
            'request_id': getattr(g, 'request_id', None),
            'path': request.path if request else None,
            'method': request.method if request else None,
            'detail': (str(detail)[:200] if detail else None),
            'ts': now(),
        }
        current_app.logger.info('OPS %s', json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    except Exception:
        pass


def database_readiness():
    """Vérifie la base auth sans dépendre d'une session utilisateur."""
    try:
        c=auth_cx(); row=c.execute('SELECT 1 AS ok').fetchone(); c.close()
        return bool(row and row['ok']==1), dbmod.backend_name(), None
    except Exception as e:
        return False, dbmod.backend_name(), str(e)

def list_organization_ids():
    c=auth_cx()
    try:
        return [int(x['id']) for x in c.execute('SELECT id FROM organizations ORDER BY id').fetchall()]
    finally:
        c.close()

def initialize_all_tenant_schemas():
    count=0
    for org_id in list_organization_ids():
        init_tenant_db(org_id)
        count += 1
    return count

def ensure_security_events_table():
    """Crée le journal de sécurité dans la base AUTH active si nécessaire.

    Cette fonction est volontairement idempotente. Elle permet à une instance
    déjà déployée d'appliquer la migration sans shell Render ni suppression de
    données.
    """
    c=auth_cx()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS security_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER,
            user_id INTEGER,
            event_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            target TEXT,
            ip_hash TEXT,
            user_agent TEXT,
            created_at TEXT NOT NULL
        );
        """)
        c.commit()
    finally:
        c.close()


def log_activity(kind, desc):
    try:
        c=auth_cx(); c.execute('INSERT INTO activity_log(organization_id,user_id,event_type,description,created_at) VALUES(?,?,?,?,?)',(session.get('org_id'),session.get('user_id'),kind,desc,now())); c.commit(); c.close()
    except: pass

def _audit_ip_hash():
    """Pseudonymise l'IP : aucune adresse IP brute n'est stockée."""
    try:
        raw=(request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        if not raw:
            return None
        pepper=os.environ.get('PROFITOS_SECRET_KEY','profitos-audit')
        return hashlib.sha256((pepper+'|'+raw).encode('utf-8')).hexdigest()
    except Exception:
        return None

def log_security_event(event_type, outcome='SUCCESS', user_id=None, organization_id=None, target=None):
    """Journal de sécurité minimal, sans secret, mot de passe, token ni IP brute.

    `target` doit rester non sensible (ex: user:42, member:7). Les erreurs de
    journalisation ne doivent jamais casser l'action métier.
    """
    try:
        ensure_security_events_table()
        uid=user_id if user_id is not None else session.get('user_id')
        oid=organization_id if organization_id is not None else session.get('org_id')
        ua=(request.headers.get('User-Agent') or '')[:240] or None
        safe_target=(str(target)[:160] if target is not None else None)
        c=auth_cx()
        c.execute(
            'INSERT INTO security_events(organization_id,user_id,event_type,outcome,target,ip_hash,user_agent,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (oid,uid,str(event_type)[:80],str(outcome)[:24],safe_target,_audit_ip_hash(),ua,now())
        )
        c.commit(); c.close()
    except Exception:
        pass

def log_status_change(entity_type, entity_id, kind, old_status, new_status, note=None):
    """Journalise un changement de statut (facture, opportunité SAVE/GROW, ou action)
    pour la timeline affichée sur l'écran détail. entity_id est toujours l'identifiant
    de l'opportunité/facture d'origine (pas l'id de l'action), pour pouvoir retracer
    tout l'historique d'un même dossier au même endroit, quelle que soit la source
    du changement (statut direct, ou statut d'une action liée)."""
    try:
        u=current_user(); who=(u['full_name'] or u['email']) if u else 'system'
        c=cx(); c.execute('INSERT INTO status_history(entity_type,entity_id,kind,old_status,new_status,changed_by,note,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (entity_type,entity_id,kind,old_status,new_status,who,note,now())); c.commit(); c.close()
    except Exception:
        pass  # la traçabilité ne doit jamais faire échouer l'action métier elle-même

def current_user():
    uid=session.get('user_id')
    if not uid:
        return None
    c=auth_cx()
    r=c.execute('SELECT * FROM users WHERE id=? AND is_active=1',(uid,)).fetchone()
    c.close()
    if not r:
        return None

    # V1.3.3: chaque changement de mot de passe incrémente auth_version.
    # Les anciennes sessions sont alors invalidées à la requête suivante.
    db_version=int(r.get('auth_version') or 0)
    session_version=session.get('auth_version')
    if session_version is None:
        # Compatibilité douce avec les sessions ouvertes avant le déploiement V1.3.3.
        session['auth_version']=db_version
    elif int(session_version) != db_version:
        return None
    return r

def current_org():
    oid=session.get('org_id')
    if not oid:return None
    c=auth_cx(); r=c.execute('SELECT * FROM organizations WHERE id=?',(oid,)).fetchone(); c.close(); return r

def current_membership():
    """Return the membership stored in the auth DB for the active user/org.

    Security rule: authorization is never trusted from the signed session alone.
    The database is the source of truth, so role changes/removals take effect on
    the very next request.
    """
    uid=session.get('user_id'); oid=session.get('org_id')
    if not uid or not oid: return None
    cached=getattr(g,'current_membership',None)
    if cached is not None: return cached
    c=auth_cx(); m=c.execute('SELECT * FROM memberships WHERE user_id=? AND organization_id=?',(uid,oid)).fetchone(); c.close()
    g.current_membership=m
    if m and session.get('role')!=m['role']:
        session['role']=m['role']
    return m

def current_role(default='MEMBER'):
    m=current_membership()
    return m['role'] if m else default

def _recover_valid_membership():
    """If the active org membership disappeared, move to another valid org.
    Returns True when a valid membership exists after recovery.
    """
    uid=session.get('user_id')
    if not uid: return False
    c=auth_cx(); m=c.execute('SELECT * FROM memberships WHERE user_id=? ORDER BY id LIMIT 1',(uid,)).fetchone(); c.close()
    if not m: return False
    session['org_id']=m['organization_id']; session['role']=m['role']
    if hasattr(g,'current_membership'): delattr(g,'current_membership')
    init_tenant_db(m['organization_id'])
    return True

def login_required(fn):
    @functools.wraps(fn)
    def wrapped(*a,**kw):
        if not session.get('user_id'):
            return redirect(url_for('login',next=request.path))
        if not current_user():
            session.clear(); flash('Votre session n’est plus valide. Veuillez vous reconnecter.')
            return redirect(url_for('login'))
        if not current_membership():
            if not _recover_valid_membership():
                session.clear(); flash('Vous n’avez plus accès à cette organisation.')
                return redirect(url_for('login'))
            flash('Votre organisation active a changé car vos droits ont été mis à jour.')
        return fn(*a,**kw)
    return wrapped

# ---------------------------------------------------------------------------
# Security helpers / uploads
# ---------------------------------------------------------------------------
ALLOWED_INVOICE_EXTENSIONS={'.csv','.xlsx','.xls','.pdf'}
ALLOWED_DCE_EXTENSIONS={'.pdf','.docx','.txt','.md'}
WEBHOOK_HOST_SUFFIXES=('hooks.slack.com','webhook.office.com','logic.azure.com','outlook.office.com')


def safe_next_url(target):
    """N'autorise que les redirections relatives internes (anti open-redirect)."""
    if not target or not isinstance(target,str): return None
    parsed=urlsplit(target)
    if parsed.scheme or parsed.netloc or not target.startswith('/') or target.startswith('//'):
        return None
    return target


def password_error(password):
    if len(password)<10: return 'Le mot de passe doit contenir au moins 10 caractères.'
    if not re.search(r'[A-Za-z]',password) or not re.search(r'\d',password):
        return 'Le mot de passe doit contenir au moins une lettre et un chiffre.'
    return None


def validate_webhook_url(url):
    if not url: return True
    try:
        p=urlsplit(url)
        host=(p.hostname or '').lower()
        if p.scheme!='https' or not host: return False
        return any(host==suffix or host.endswith('.'+suffix) for suffix in WEBHOOK_HOST_SUFFIXES)
    except Exception:
        return False

def validate_hex_color(value):
    """Format strict #RGB ou #RRGGBB uniquement — jamais de valeur CSS libre injectée
    dans un attribut style (défense en profondeur, même si Jinja échappe déjà les guillemets)."""
    if not value: return True
    return bool(re.fullmatch(r'#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}',value))

def validate_logo_url(url):
    """Logo hébergé ailleurs, https uniquement — pas de data: ni de schéma arbitraire."""
    if not url: return True
    try:
        p=urlsplit(url)
        return p.scheme=='https' and bool(p.hostname)
    except Exception:
        return False


def _signature_ok(path, ext):
    try:
        with path.open('rb') as fh:
            head=fh.read(16)
    except Exception:
        return False
    if ext=='.pdf': return head.startswith(b'%PDF-')
    if ext in ('.xlsx','.docx'): return head.startswith(b'PK')
    if ext=='.xls': return head.startswith(bytes.fromhex('D0CF11E0A1B11AE1'))
    if ext in ('.csv','.txt','.md'):
        return b'\x00' not in head
    return False


def save_upload(file_storage, category, allowed_extensions):
    """Sauvegarde temporaire isolée par organisation avec nom aléatoire et validation légère."""
    if not file_storage or not file_storage.filename:
        raise ValueError('Fichier manquant.')
    original=secure_filename(Path(file_storage.filename).name)
    if not original:
        raise ValueError('Nom de fichier invalide.')
    ext=Path(original).suffix.lower()
    if ext not in allowed_extensions:
        raise ValueError('Type de fichier non autorisé.')
    org_id=session.get('org_id')
    if not org_id:
        raise ValueError('Organisation non sélectionnée.')
    target_dir=UP/f'org_{int(org_id)}'/category
    target_dir.mkdir(parents=True,exist_ok=True)
    target=target_dir/f'{secrets.token_hex(16)}{ext}'
    file_storage.save(target)
    try:
        if not _signature_ok(target,ext):
            raise ValueError('Le contenu du fichier ne correspond pas au format attendu.')
        return target, original
    except Exception:
        target.unlink(missing_ok=True)
        raise


def cleanup_upload(path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Stripe — facturation. Import paresseux : ne plante jamais si la lib n'est
# pas installée ou si les clés ne sont pas configurées (mode "billing désactivé").
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY=os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY=os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET=os.environ.get('STRIPE_WEBHOOK_SECRET')
STRIPE_PRICE_STARTER_ID=os.environ.get('STRIPE_PRICE_STARTER_ID')
STRIPE_PRICE_PRO_ID=os.environ.get('STRIPE_PRICE_PRO_ID') or os.environ.get('STRIPE_PRICE_ID')
STRIPE_PRICE_BUSINESS_ID=os.environ.get('STRIPE_PRICE_BUSINESS_ID')
STRIPE_PLANS={
    'STARTER': {'name':'Starter','price_eur':49,'price_id':STRIPE_PRICE_STARTER_ID},
    'PRO': {'name':'Pro','price_eur':99,'price_id':STRIPE_PRICE_PRO_ID},
    'BUSINESS': {'name':'Business','price_eur':249,'price_id':STRIPE_PRICE_BUSINESS_ID},
}
STRIPE_PRICE_TO_PLAN={v['price_id']:k for k,v in STRIPE_PLANS.items() if v.get('price_id')}
# Un abonnement ne doit jamais être activé sans webhook signé : c'est Stripe,
# et non la page de succès du navigateur, qui fait foi.
BILLING_ENABLED=bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_TO_PLAN)

def get_stripe():
    if not BILLING_ENABLED: return None
    import stripe
    stripe.api_key=STRIPE_SECRET_KEY
    return stripe

def trial_days_left(org):
    if not org or not org['trial_ends_at']: return None
    end=parse_dt(org['trial_ends_at'])
    if not end: return None
    return max(0,(end-datetime.now(timezone.utc)).days)

def org_has_access(org):
    """True si l'organisation peut utiliser l'app : abonnement actif, ou trial non expiré,
    ou billing désactivé (mode démo/dev sans Stripe configuré — jamais bloquant)."""
    if not org: return False
    if not BILLING_ENABLED: return True
    if org['status']=='ACTIVE_PAID': return True
    if org['plan']=='TRIAL':
        days=trial_days_left(org)
        return days is None or days>0
    return False

def requires_active_plan(fn):
    """À poser sur les routes de valeur (dashboard, recover/save/grow...). Redirige
    vers /billing si le trial est expiré et qu'aucun abonnement actif n'existe."""
    @functools.wraps(fn)
    def wrapped(*a,**kw):
        org=current_org()
        if not org_has_access(org):
            flash("Votre période d'essai est terminée. Choisissez un plan pour continuer à utiliser ProfitOS.")
            return redirect(url_for('billing'))
        return fn(*a,**kw)
    return wrapped

# ---------------------------------------------------------------------------
# Rôles équipe — accès par module. Un OWNER a toujours tout ; les autres
# rôles sont volontairement restreints à leur périmètre métier.
# ---------------------------------------------------------------------------
ROLES=['OWNER','ADMIN','COMPTABLE','COMMERCIAL','MEMBER']
ROLE_LABELS={'OWNER':'Propriétaire','ADMIN':'Administrateur','COMPTABLE':'Comptable (Recover/Save)',
             'COMMERCIAL':'Commercial (Grow)','MEMBER':'Membre (accès complet)'}
AREA_ACCESS={
    'OWNER':{'recover','save','grow','actions','impact','weekly','uploads','settings','billing','team'},
    'ADMIN':{'recover','save','grow','actions','impact','weekly','uploads','settings','team'},
    'COMPTABLE':{'recover','save','impact','weekly','uploads','actions'},
    'COMMERCIAL':{'grow','actions'},
    'MEMBER':{'recover','save','grow','actions','impact','weekly','uploads'},
}
KIND_TO_AREA={'RECOVER':'recover','SAVE':'save','GROW':'grow'}

def can_access(area):
    return area in AREA_ACCESS.get(current_role(),set())

def require_area(area):
    """Bloque l'accès à une route si le rôle actif n'a pas la permission sur ce module."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*a,**kw):
            if not can_access(area):
                flash("Votre rôle ne donne pas accès à cette section.")
                return redirect(url_for('home'))
            return fn(*a,**kw)
        return wrapped
    return deco


# ---------------------------------------------------------------------------
# Rate limiting distribué — Flask-Limiter + Render Key Value (Valkey/Redis).
#
# La clé est l'adresse IP cliente après ProxyFix. En production, REDIS_URL
# pointe vers le connectionString privé du Key Value Render. En local,
# memory:// reste disponible. Le fallback mémoire protège encore le service
# si le datastore est brièvement indisponible.
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

def init_rate_limiter(app):
    limiter.init_app(app)
    storage = app.config.get('RATELIMIT_STORAGE_URI', 'memory://')
    backend = 'render-key-value' if str(storage).startswith(('redis://','rediss://')) else 'memory'
    app.logger.info('Rate limiter initialized backend=%s', backend)

def _seconds_limit_string(max_calls, per_seconds):
    return f"{int(max_calls)} per {int(per_seconds)} seconds"

def rate_limit(max_calls, per_seconds):
    """Compatibilité avec les décorateurs historiques de ProfitOS.

    Seules les requêtes POST sont comptabilisées, comme dans l'ancienne
    implémentation. L'état est désormais partagé entre workers/instances
    lorsque REDIS_URL est configuré.
    """
    return limiter.limit(
        _seconds_limit_string(max_calls, per_seconds),
        methods=['POST'],
        override_defaults=True,
    )

# ---------------------------------------------------------------------------
# CSRF — protection basique par token de session, appliquée à toutes les
# requêtes POST/PUT/PATCH/DELETE. Chaque formulaire doit inclure le champ
# caché {{ csrf_token() }} (voir templates). Pour une protection plus robuste
# (rotation de token, cookies SameSite, etc.), envisager Flask-WTF en V1.
# ---------------------------------------------------------------------------
def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token']=secrets.token_hex(32)
    return session['csrf_token']

def csrf_protect():
    if request.method in ('POST','PUT','PATCH','DELETE'):
        if request.path=='/billing/webhook':
            return
        token=session.get('csrf_token'); sent=request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not token or not sent or not secrets.compare_digest(token,sent):
            abort(400,description='CSRF token invalide ou manquant.')
        # Défense supplémentaire en production : si Origin/Referer est présent, il doit être same-origin.
        source=request.headers.get('Origin') or request.headers.get('Referer')
        if source and os.environ.get('PROFITOS_ENV','development').lower()=='production':
            try:
                p=urlsplit(source)
                if p.netloc and p.netloc.lower()!=request.host.lower():
                    abort(400,description='Origine de requête refusée.')
            except Exception:
                abort(400,description='Origine de requête invalide.')


# ---------------------------------------------------------------------------
# Auto-migration : garantit que le schéma tenant est à jour (nouvelles tables/
# colonnes) sans exiger une reconnexion manuelle après chaque déploiement.
# Exécuté une seule fois par organisation et par process (mise en cache en
# mémoire) pour éviter de relancer la migration à chaque requête.
# ---------------------------------------------------------------------------
_tenant_schema_checked=set()

def ensure_tenant_schema():
    org_id=session.get('org_id')
    if org_id and org_id not in _tenant_schema_checked:
        try:
            init_tenant_db()
            _tenant_schema_checked.add(org_id)
        except Exception as e:
            # Affiché dans la console pour diagnostic — ne bloque jamais la requête,
            # mais on ne veut plus jamais avaler une vraie erreur en silence.
            print(f"[ProfitOS] ATTENTION : échec de la migration du schéma pour l'organisation {org_id} : {e}")

def init_tenant_db(org_id=None):
    org_id = org_id or session.get('org_id')
    if not org_id:
        raise RuntimeError('Organisation requise pour initialiser le schéma tenant')
    c=dbmod.connect_tenant(org_id, tenant_db(org_id)); c.executescript('''
    CREATE TABLE IF NOT EXISTS app_settings(id INTEGER PRIMARY KEY CHECK(id=1),onboarding_complete INTEGER DEFAULT 0,currency TEXT DEFAULT 'EUR',locale TEXT DEFAULT 'fr-FR',notifications_enabled INTEGER DEFAULT 1,slack_webhook_url TEXT,teams_webhook_url TEXT,accountant_email TEXT,weekly_export_enabled INTEGER DEFAULT 0,logo_url TEXT,accent_color TEXT,price_index_name TEXT DEFAULT 'INDICE',created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS dso_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,snapshot_date TEXT UNIQUE,avg_days_overdue REAL,total_outstanding REAL,invoice_count INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS company(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT,city TEXT,department TEXT,allowed_departments TEXT,activities TEXT,certifications TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_number TEXT,customer TEXT,amount REAL,paid_amount REAL DEFAULT 0,issue_date TEXT,due_date TEXT,status TEXT,days_overdue INTEGER,score INTEGER,created_at TEXT,kind TEXT DEFAULT 'STANDARD',retention_release_date TEXT,retention_pct REAL,customer_email TEXT,customer_phone TEXT,public_token TEXT);
    CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,vendor TEXT,description TEXT,amount REAL,expense_date TEXT,category TEXT);
    CREATE TABLE IF NOT EXISTS opportunities(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,title TEXT,value REAL DEFAULT 0,score INTEGER,details TEXT,source TEXT,source_url TEXT,buyer TEXT,departments TEXT,deadline TEXT,reasons TEXT,warnings TEXT,raw_json TEXT,status TEXT DEFAULT 'OPEN',created_at TEXT);
    CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT,opportunity_id INTEGER,kind TEXT,title TEXT,draft TEXT,status TEXT DEFAULT 'PENDING',expected_value REAL DEFAULT 0,created_at TEXT,sent_at TEXT,sent_to TEXT);
    CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,action_id INTEGER,outcome_type TEXT,amount REAL,verified INTEGER DEFAULT 0,note TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,run_type TEXT,rows_processed INTEGER,signals_found INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS dce_documents(id INTEGER PRIMARY KEY AUTOINCREMENT,opportunity_id INTEGER,filename TEXT,filetype TEXT,text_content TEXT,analysis_json TEXT,go_score INTEGER,recommendation TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS weekly_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,period_start TEXT,period_end TEXT,payload_json TEXT,sent_at TEXT,recipient TEXT,status TEXT DEFAULT 'PENDING');
    CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT,entity_id INTEGER,kind TEXT,old_status TEXT,new_status TEXT,changed_by TEXT,note TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS customer_tags(customer_name_norm TEXT PRIMARY KEY,customer_name_display TEXT,tag TEXT,note TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS price_index_readings(id INTEGER PRIMARY KEY AUTOINCREMENT,index_name TEXT DEFAULT 'INDICE',reading_date TEXT,value REAL,created_at TEXT);
    CREATE TABLE IF NOT EXISTS fixed_price_contracts(id INTEGER PRIMARY KEY AUTOINCREMENT,project_name TEXT,customer TEXT,amount REAL,signed_date TEXT,materials_share_pct REAL DEFAULT 30,status TEXT DEFAULT 'ACTIVE',created_at TEXT);
    CREATE TABLE IF NOT EXISTS financial_settings(id INTEGER PRIMARY KEY CHECK(id=1),cash_balance REAL,cash_as_of TEXT,updated_at TEXT);
    '''); c.commit()
    # Migration douce pour les bases tenant créées avant l'ajout de created_at / retenues contractuelles.
    for table,col in (('invoices','created_at'),('opportunities','created_at'),
                       ('invoices','kind'),('invoices','retention_release_date'),('invoices','retention_pct'),
                       ('invoices','customer_email'),('actions','sent_at'),('actions','sent_to'),
                       ('invoices','customer_phone'),
                       ('invoices','public_token'),
                       ('app_settings','slack_webhook_url'),('app_settings','teams_webhook_url'),
                       ('app_settings','accountant_email'),('app_settings','weekly_export_enabled'),
                       ('app_settings','logo_url'),('app_settings','accent_color'),
                       ('app_settings','price_index_name')):
        try:
            cols=[r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()]
            if col not in cols:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col} TEXT'); c.commit()
        except Exception as e:
            print(f"[ProfitOS] ATTENTION : migration colonne {table}.{col} ignorée ({e})")
    c.close()

def norm(x):
    s=unicodedata.normalize('NFKD',str(x or '')).encode('ascii','ignore').decode('ascii')
    return re.sub(r'\s+',' ',s.lower()).strip()

def parse_date(v):
    if v is None or (isinstance(v,float) and pd.isna(v)): return None
    if isinstance(v,datetime): return v.date()
    if isinstance(v,date): return v
    try: return pd.to_datetime(v).date()
    except: return None

def parse_dt(v):
    if not v:return None
    try:
        d=pd.to_datetime(v).to_pydatetime()
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
    except:return None

def fmt_deadline(v):
    d=parse_dt(v)
    if not d:return None
    d=d.astimezone(PARIS); months=['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
    return f'{d.day} {months[d.month-1]} {d.year} à {d:%H:%M}'

def days_left(v):
    d=parse_dt(v)
    if not d:return None
    return math.ceil((d.astimezone(timezone.utc)-datetime.now(timezone.utc)).total_seconds()/86400)

def jlist(v):
    if not v:return []
    if isinstance(v,list):return [str(x) for x in v]
    s=str(v).strip()
    if s.startswith('['):
        try:return [str(x) for x in json.loads(s.replace("'",'"'))]
        except:pass
    return [x.strip() for x in re.split(r'[,;|]',s) if x.strip()]

def money(v):
    try:return float(v or 0)
    except:return 0.0

def profile():
    c=cx(); r=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close(); return r

def allowed_deps(p):
    if not p:return []
    vals=jlist(p['allowed_departments'])+jlist(p['department'])
    return sorted(set(x.upper() for x in vals if x))

def map_cols(df,aliases):
    m={norm(c):c for c in df.columns}; out={}
    for k,opts in aliases.items():
        for o in opts:
            if norm(o) in m: out[k]=m[norm(o)]; break
    return out

def invoice_score(amount,days,paid=0):
    due=max(0,money(amount)-money(paid))
    return min(100,min(40,int(due/20000*40))+min(45,int(max(days,0)/90*45))+15)

def sparkline_svg(values,width=260,height=56,color='#5fe0ac'):
    """Petite courbe SVG en ligne, générée côté serveur (pas de lib JS de chart).
    values : liste de nombres (chronologique, le plus ancien en premier)."""
    if not values or len(values)<2:
        return None
    lo,hi=min(values),max(values)
    span=(hi-lo) or 1
    n=len(values); pad=4
    pts=[]
    for i,v in enumerate(values):
        x=pad+i*(width-2*pad)/(n-1)
        y=height-pad-((v-lo)/span)*(height-2*pad)
        pts.append(f"{x:.1f},{y:.1f}")
    path='M'+' L'.join(pts)
    last_x,last_y=pts[-1].split(',')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg"><path d="{path}" fill="none" stroke="{color}" '
            f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="3.5" fill="{color}"/></svg>')

def bars_svg(labels_values,width=420,height=140,color='#5fe0ac'):
    """Barres verticales SVG simples (pas de lib JS), pour la prévision de trésorerie.
    labels_values : liste de tuples (label, valeur)."""
    if not labels_values: return None
    vals=[v for _,v in labels_values]
    hi=max(max(vals,default=0),1)
    n=len(labels_values); pad=10; gap=14
    bar_w=(width-2*pad-gap*(n-1))/n
    bars=[]; labels=[]
    for i,(label,v) in enumerate(labels_values):
        x=pad+i*(bar_w+gap)
        h=(v/hi)*(height-40) if hi else 0
        y=height-30-h
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="4" fill="{color}"/>')
        bars.append(f'<text x="{x+bar_w/2:.1f}" y="{y-6:.1f}" font-size="11" fill="#dbe6ff" text-anchor="middle">{v:,.0f}</text>'.replace(',',' '))
        labels.append(f'<text x="{x+bar_w/2:.1f}" y="{height-10:.1f}" font-size="11" fill="#8fa9d3" text-anchor="middle">{label}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg">'+''.join(bars)+''.join(labels)+'</svg>')

TAX={
 'renovation':['rénovation','renovation','réhabilitation','rehabilitation'],
 'plomberie':['plomberie','sanitaire','canalisation'],
 'electricite':['électricité','electricite','électrique','electrique','courant fort','courant faible'],
 'cvc':['cvc','chauffage','ventilation','climatisation','thermique'],
 'isolation':['isolation thermique','isolation'],
 'toiture':['toiture','couverture','étanchéité','etancheite'],
 'menuiserie':['menuiserie','fenêtre','fenetre'],
 'maconnerie':['maçonnerie','maconnerie','gros œuvre','gros oeuvre'],
 'vrd':['vrd','voirie','réseaux divers','reseaux divers'],
 'peinture':['peinture','revêtement mural','revetement mural'],
 'construction':['construction','bâtiment','batiment','travaux']}

def profile_terms(p):
    raw=norm(p['activities'] if p else ''); terms=[]
    for fam,kws in TAX.items():
        if fam in raw or any(norm(k) in raw for k in kws):terms+=kws
    terms += [x.strip() for x in re.split(r'[,;/|]+',raw) if len(x.strip())>=4]
    return sorted(set(t for t in terms if len(norm(t))>=4))

def first(rec,*keys):
    for k in keys:
        if rec.get(k) not in (None,'',[]): return rec[k]

def score_market(rec,p):
    blob=norm(' '.join(str(rec.get(k,'')) for k in ['objet','famille_libelle','descripteur_libelle','type_marche','type_marche_facette','nature_libelle','criteres']))
    hits=[]
    for t in profile_terms(p):
        nt=norm(t); ok=(nt in blob) if ' ' in nt else bool(re.search(rf'(?<!\w){re.escape(nt)}(?!\w)',blob))
        if ok:hits.append(t)
    deps=[]
    for k in ['code_departement_prestation','code_departement']:deps += jlist(rec.get(k))
    deps=sorted(set(x.upper() for x in deps)); allowed=allowed_deps(p)
    s=15; reasons=[]; warns=[]
    if hits:s+=min(45,22+7*min(len(set(hits))-1,3)); reasons.append('activité compatible : '+', '.join(sorted(set(hits))[:4]))
    else:warns.append('aucune correspondance métier forte')
    if 'travaux' in norm(first(rec,'type_marche','type_marche_facette')):s+=15; reasons.append('type de marché : travaux')
    if deps and allowed:
        if set(deps)&set(allowed):s+=20; reasons.append('zone géographique compatible')
        else:s-=30; warns.append('hors des départements configurés')
    else:warns.append('localisation insuffisamment précise')
    d=days_left(first(rec,'datelimitereponse','date_limite_reponse'))
    if d is not None:
        if d<0:s-=50; warns.append('date limite dépassée')
        elif d<=3:s-=8; warns.append('échéance très proche')
        elif d<=30:s+=5; reasons.append(f'{d} jours avant clôture')
        else:s+=3; reasons.append('délai de réponse disponible')
    return max(0,min(100,s)),deps,reasons,warns

def sync_grow():
    p=profile()
    if not p:return 0
    r=requests.get(BOAMP,params={'limit':100,'order_by':'dateparution DESC'},timeout=20); r.raise_for_status(); rows=r.json().get('results',[])
    c=cx(); c.execute("DELETE FROM opportunities WHERE type='GROW'"); n=0
    for rec in rows:
        score,deps,reasons,warns=score_market(rec,p)
        if score<55:continue
        title=first(rec,'objet','title','titre') or 'Marché public BOAMP'; buyer=first(rec,'nomacheteur','acheteur') or 'Acheteur public'
        c.execute("""INSERT INTO opportunities(type,title,value,score,details,source,source_url,buyer,departments,deadline,reasons,warnings,raw_json,status,created_at)
                     VALUES('GROW',?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)""",
                  (title,0,score,f'Acheteur : {buyer}','BOAMP',first(rec,'url_avis','url'),buyer,json.dumps(deps,ensure_ascii=False),first(rec,'datelimitereponse','date_limite_reponse'),json.dumps(reasons,ensure_ascii=False),json.dumps(warns,ensure_ascii=False),json.dumps(rec,ensure_ascii=False,default=str),now())); n+=1
    c.execute('INSERT INTO audit_runs(run_type,rows_processed,signals_found,created_at) VALUES(?,?,?,?)',('GROW',len(rows),n,now())); c.commit(); c.close(); return n



def tenant_cx_direct(org_id):
    """Connexion tenant sans dépendre de la session (utilisée par le digest hebdomadaire, hors requête HTTP)."""
    return dbmod.connect_tenant(org_id, tenant_db(org_id))

def compute_weekly_digest(org_id, days=7):
    """Calcule les nouveautés RECOVER/SAVE/GROW des `days` derniers jours pour une organisation.
    Retourne un dict prêt à être injecté dans le template email + utilisé pour la preview HTML."""
    since=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    c=tenant_cx_direct(org_id)
    new_recover=c.execute(
        "SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 AND created_at>=? ORDER BY score DESC",
        (since,)).fetchall()
    new_save=c.execute("SELECT * FROM opportunities WHERE type='SAVE' AND status='OPEN' AND created_at>=? ORDER BY score DESC",(since,)).fetchall()
    new_grow=c.execute("SELECT * FROM opportunities WHERE type='GROW' AND status='OPEN' AND created_at>=? ORDER BY score DESC",(since,)).fetchall()
    c.close()
    recover_total=sum(max(r['amount']-r['paid_amount'],0) for r in new_recover)
    save_total=sum(r['value'] for r in new_save)
    return {
        'period_days':days,
        'recover_total':recover_total,'recover_count':len(new_recover),'recover_top':list(new_recover[:3]),
        'save_total':save_total,'save_count':len(new_save),'save_top':list(new_save[:3]),
        'grow_count':len(new_grow),'grow_top':list(new_grow[:3]),
        'has_signal':bool(new_recover or new_save or new_grow),
    }

def send_email(to_email, subject, html, dry_run=None, reply_to=None):
    """Envoie un email transactionnel.

    Ordre de préférence en production :
    1. Resend API si RESEND_API_KEY est configurée ;
    2. SMTP historique si SMTP_HOST est configuré ;
    3. dry-run local sinon.

    La clé Resend reste uniquement dans les variables d'environnement Render.
    """
    resend_key=os.environ.get('RESEND_API_KEY','').strip()
    smtp_host=os.environ.get('SMTP_HOST','').strip()
    from_email=os.environ.get('RESEND_FROM_EMAIL','ProfitOS <noreply@profitos.fr>').strip()

    if dry_run is None:
        dry_run = not (resend_key or smtp_host)
    if dry_run:
        return {'sent':False,'dry_run':True,'provider':'dry-run','to':to_email,'subject':subject,'html':html}

    if resend_key:
        try:
            import resend
            resend.api_key=resend_key
            params: resend.Emails.SendParams = {
                'from': from_email,
                'to': [to_email],
                'subject': subject,
                'html': html,
            }
            if reply_to:
                params['reply_to'] = reply_to
            result=resend.Emails.send(params)
            email_id = result.get('id') if isinstance(result,dict) else getattr(result,'id',None)
            return {'sent':True,'provider':'resend','id':email_id,'to':to_email,'subject':subject}
        except Exception as exc:
            current_app.logger.exception('Resend email failed for %s', to_email)
            return {'sent':False,'provider':'resend','error':str(exc),'to':to_email,'subject':subject}

    # Compatibilité SMTP conservée pour le développement ou un fournisseur secondaire.
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg=MIMEMultipart('alternative')
        msg['Subject']=subject
        msg['From']=os.environ.get('SMTP_FROM',from_email)
        msg['To']=to_email
        if reply_to:
            msg['Reply-To']=reply_to
        msg.attach(MIMEText(html,'html'))
        with smtplib.SMTP(smtp_host,int(os.environ.get('SMTP_PORT',587))) as server:
            server.starttls()
            server.login(os.environ.get('SMTP_USER',''),os.environ.get('SMTP_PASSWORD',''))
            server.sendmail(msg['From'],[to_email],msg.as_string())
        return {'sent':True,'provider':'smtp','to':to_email,'subject':subject}
    except Exception as exc:
        current_app.logger.exception('SMTP email failed for %s', to_email)
        return {'sent':False,'provider':'smtp','error':str(exc),'to':to_email,'subject':subject}

def send_sms(to_phone, body, dry_run=None):
    """Envoie un SMS via Twilio si TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER
    sont configurés, sinon simule (dry-run). Même principe que send_email()."""
    sid=os.environ.get('TWILIO_ACCOUNT_SID','').strip()
    token=os.environ.get('TWILIO_AUTH_TOKEN','').strip()
    from_number=os.environ.get('TWILIO_FROM_NUMBER','').strip()

    if dry_run is None:
        dry_run = not (sid and token and from_number)
    if dry_run:
        return {'sent':False,'dry_run':True,'provider':'dry-run','to':to_phone,'body':body}

    try:
        from twilio.rest import Client
        client=Client(sid,token)
        message=client.messages.create(body=body,from_=from_number,to=to_phone)
        return {'sent':True,'provider':'twilio','id':message.sid,'to':to_phone}
    except ImportError:
        return {'sent':False,'provider':'twilio','error':"paquet 'twilio' non installé — pip install twilio",'to':to_phone}
    except Exception as exc:
        current_app.logger.exception('Twilio SMS failed for %s', to_phone)
        return {'sent':False,'provider':'twilio','error':str(exc),'to':to_phone}

def generate_api_key():
    """Nouvelle clé API en clair — n'est montrée qu'une seule fois à la création,
    seul son hash est conservé en base (même principe qu'un mot de passe)."""
    return 'pos_live_'+secrets.token_urlsafe(32)

def hash_api_key(raw_key):
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

def api_key_required(fn):
    """Authentifie une requête API via 'Authorization: Bearer <clé>'. Résout
    l'organisation correspondante dans g.api_org_id, sans dépendre de la session
    (les appels API n'ont pas de cookie de session)."""
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        auth_header=request.headers.get('Authorization','')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error':'missing_api_key','message':'En-tête Authorization: Bearer <clé> requis.'}),401
        raw_key=auth_header[7:].strip()
        key_hash=hash_api_key(raw_key)
        c=auth_cx()
        row=c.execute('SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL',(key_hash,)).fetchone()
        if row:
            c.execute('UPDATE api_keys SET last_used_at=? WHERE id=?',(now(),row['id'])); c.commit()
        c.close()
        if not row:
            return jsonify({'error':'invalid_api_key','message':'Clé API invalide ou révoquée.'}),401
        g.api_org_id=row['organization_id']
        return fn(*args,**kwargs)
    return wrapped

def sync_buyer_signals(org_id, invoices_rows):
    """Met à jour les signaux de risque acheteur partagés entre organisations, à partir
    des factures en retard de CETTE organisation. Agrégation anonymisée : les autres
    organisations ne voient jamais QUI a signalé quoi, seulement un compte et une moyenne."""
    from collections import defaultdict
    by_customer=defaultdict(list)
    for r in invoices_rows:
        if r.get('days_overdue',0)>0 and norm(r.get('status','') or '')!='paid':
            by_customer[r['customer']].append(r['days_overdue'])
    if not by_customer: return
    c=auth_cx()
    for customer,days_list in by_customer.items():
        key=norm(customer)
        if not key: continue
        avg=sum(days_list)/len(days_list)
        c.execute('''INSERT INTO buyer_signals(buyer_name_norm,organization_id,buyer_name_display,invoice_count,avg_days_overdue,updated_at)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(buyer_name_norm,organization_id) DO UPDATE SET
                       buyer_name_display=excluded.buyer_name_display,invoice_count=excluded.invoice_count,
                       avg_days_overdue=excluded.avg_days_overdue,updated_at=excluded.updated_at''',
            (key,org_id,customer,len(days_list),avg,now()))
    c.commit(); c.close()

def buyer_risk_lookup(customer_name, exclude_org_id):
    """Cherche si d'autres organisations ProfitOS (que la sienne) ont aussi signalé
    ce même acheteur en retard de paiement. Retourne None si aucun autre signal."""
    key=norm(customer_name)
    if not key: return None
    c=auth_cx()
    rows=c.execute('SELECT * FROM buyer_signals WHERE buyer_name_norm=? AND organization_id!=?',(key,exclude_org_id)).fetchall()
    c.close()
    if not rows: return None
    org_count=len(rows)
    total_invoices=sum(r['invoice_count'] for r in rows)
    avg_days=sum(r['avg_days_overdue']*r['invoice_count'] for r in rows)/total_invoices if total_invoices else 0
    return {'org_count':org_count,'total_invoices':total_invoices,'avg_days':round(avg_days)}

def sync_sector_dso(org_id, avg_days_overdue):
    """Alimente le benchmark sectoriel anonymisé (moyenne DSO inter-organisations)."""
    c=auth_cx()
    c.execute('''INSERT INTO sector_dso_signals(organization_id,snapshot_date,avg_days_overdue,updated_at)
                 VALUES(?,?,?,?)
                 ON CONFLICT(organization_id,snapshot_date) DO UPDATE SET
                   avg_days_overdue=excluded.avg_days_overdue,updated_at=excluded.updated_at''',
        (org_id,date.today().isoformat(),avg_days_overdue,now()))
    c.commit(); c.close()

def sector_dso_benchmark(exclude_org_id):
    """Moyenne DSO des autres organisations (leur dernier relevé chacune), anonymisée."""
    c=auth_cx()
    rows=c.execute('SELECT organization_id,MAX(snapshot_date) d FROM sector_dso_signals WHERE organization_id!=? GROUP BY organization_id',(exclude_org_id,)).fetchall()
    if not rows: c.close(); return None
    values=[]
    for r in rows:
        v=c.execute('SELECT avg_days_overdue FROM sector_dso_signals WHERE organization_id=? AND snapshot_date=?',(r['organization_id'],r['d'])).fetchone()
        if v and v['avg_days_overdue'] is not None: values.append(v['avg_days_overdue'])
    c.close()
    if not values: return None
    return {'org_count':len(values),'avg_days':round(sum(values)/len(values))}

def export_response(rows, filename_base):
    """Exporte une liste de dicts en CSV ou XLSX selon ?format=csv|xlsx (défaut xlsx).
    Partagé par tous les blueprints (RECOVER/SAVE/GROW, audit, etc.)."""
    fmt=request.args.get('format','xlsx').lower()
    df=pd.DataFrame(rows)
    if fmt=='csv':
        csv_bytes=df.to_csv(index=False).encode('utf-8-sig')  # BOM : accents corrects dans Excel FR
        return Response(csv_bytes,mimetype='text/csv',
            headers={'Content-Disposition':f'attachment; filename="{filename_base}.csv"'})
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine='openpyxl') as writer:
        df.to_excel(writer,index=False,sheet_name='Export')
    buf.seek(0)
    return Response(buf.getvalue(),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':f'attachment; filename="{filename_base}.xlsx"'})

def register_public_invoice_token(org_id, invoice_local_id):
    """Génère un token public non-devinable pour une facture, et l'enregistre dans la
    base auth (mapping token -> organisation) pour permettre la résolution du portail
    client sans exposer la structure multi-tenant. Retourne le token."""
    token=secrets.token_urlsafe(20)
    c=auth_cx()
    c.execute('INSERT INTO public_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
        (token,org_id,invoice_local_id,now())); c.commit(); c.close()
    return token

def resolve_public_invoice_token(token):
    """Retrouve l'organisation et l'id local d'une facture à partir de son token public."""
    c=auth_cx()
    row=c.execute('SELECT * FROM public_invoice_tokens WHERE token=?',(token,)).fetchone()
    c.close()
    return row

def send_accountant_export(org, email, dry_run=None):
    """Envoie au comptable un LIEN de téléchargement sécurisé (pas de pièce jointe brute) —
    le lien régénère l'export RECOVER à la demande, toujours à jour au moment du clic."""
    token=secrets.token_urlsafe(20)
    c=auth_cx()
    c.execute('INSERT INTO export_tokens(token,organization_id,export_type,created_at) VALUES(?,?,?,?)',
        (token,org['id'],'recover',now())); c.commit(); c.close()
    base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050')
    link=f"{base}{url_for('export_download',token=token)}"
    html=render_template('email_transactional.html',title=f"Export ProfitOS — {org['name']}",
        intro=f"Voici le lien pour télécharger l'export des créances de {org['name']}, toujours à jour au moment du clic.",
        cta_label='Télécharger l\'export',cta_url=link,footer='Ce lien reste valable — contacte l\'organisation si tu n\'es pas concerné(e).')
    return send_email(email,f"Export ProfitOS — {org['name']}",html,dry_run=dry_run)

def reward_referrer_if_any(acx, referred_org_id):
    """Accorde 1 mois gratuit au parrain quand l'organisation parrainée devient payante.
    'acx' est une connexion auth déjà ouverte (appelé depuis le webhook Stripe, qui gère
    lui-même le commit global) — on ne ferme pas la connexion ici, on ne fait qu'écrire.
    Best-effort : ne doit jamais faire échouer le traitement du webhook Stripe."""
    try:
        ref=acx.execute("SELECT * FROM referrals WHERE referred_org_id=? AND status='PENDING'",(referred_org_id,)).fetchone()
        if not ref: return
        acx.execute("UPDATE referrals SET status='REWARDED',rewarded_at=? WHERE id=?",(now(),ref['id']))
        # Récompense simple et robuste : prolonge le trial_ends_at du parrain de 30 jours.
        # Fonctionne aussi bien pour un parrain encore en essai que déjà payant (le champ
        # est simplement stocké, sans effet si le parrain a déjà un abonnement Stripe actif).
        referrer=acx.execute('SELECT * FROM organizations WHERE id=?',(ref['referrer_org_id'],)).fetchone()
        if referrer:
            base=parse_dt(referrer['trial_ends_at']) or datetime.now(timezone.utc)
            new_end=max(base,datetime.now(timezone.utc))+timedelta(days=30)
            acx.execute('UPDATE organizations SET trial_ends_at=?,updated_at=? WHERE id=?',(new_end.isoformat(),now(),ref['referrer_org_id']))
            log_activity('REFERRAL_REWARDED',f"Parrainage récompensé (organisation #{referred_org_id} devenue payante)")
    except Exception:
        current_app.logger.exception('Referral reward failed for referred_org_id=%s', referred_org_id)

CHANGELOG_ENTRIES=[
    {'date':'2026-08-22','title':'Segmentation clients, performance équipe, export comptable automatisé'},
    {'date':'2026-08-22','title':'Score de risque acheteur croisé, benchmark sectoriel DSO, calendrier unifié'},
    {'date':'2026-08-22','title':'Programme de parrainage, portail client public, rapprochement bancaire'},
    {'date':'2026-08-20','title':'Prévision de trésorerie, simulateur de caution, radar de partenaires'},
    {'date':'2026-08-18','title':'Rôles équipe, PWA installable, export CSV/Excel'},
]

def notify_org(text):
    """Envoie une notification Slack/Teams pour l'organisation courante, si un webhook
    est configuré dans Settings. Silencieux (no-op) si aucun webhook n'est renseigné —
    ne bloque jamais le flux principal (upload, etc.) en cas d'échec réseau."""
    try:
        org=current_org()
        if not org: return
        c=cx(); s=c.execute('SELECT * FROM app_settings WHERE id=1').fetchone(); c.close()
        if not s or not s['notifications_enabled']: return
        for url in (s['slack_webhook_url'],s['teams_webhook_url']):
            if url and validate_webhook_url(url):
                try: requests.post(url,json={'text':text},timeout=5,allow_redirects=False)
                except Exception: pass
    except Exception:
        pass


def send_weekly_email(org, digest, dry_run=None):
    """Envoie (ou simule) le rapport hebdomadaire par email pour une organisation."""
    c=auth_cx(); recipient=c.execute(
        "SELECT users.email FROM memberships JOIN users ON users.id=memberships.user_id WHERE memberships.organization_id=? AND memberships.role='OWNER' ORDER BY memberships.id LIMIT 1",
        (org['id'],)).fetchone(); c.close()
    if not recipient: return {'sent':False,'reason':'no_owner_email'}
    to_email=recipient['email']
    html=render_template('email_weekly.html',org=org,digest=digest,app_url=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050'))
    subject=f"Your Weekly Profit Report — {org['name']}"
    return send_email(to_email,subject,html,dry_run=dry_run)

def gen_token():
    # Le token brut n'est jamais stocké en base à partir de V1.3.3.
    return secrets.token_urlsafe(32)

def token_digest(token):
    if not token or not isinstance(token,str):
        return ''
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def _token_user(kind, raw_token):
    """Retourne (user, stored_value).

    Les nouveaux tokens sont stockés sous SHA-256. Le fallback plaintext permet
    de ne pas casser immédiatement les liens émis avant le déploiement V1.3.3.
    """
    if kind not in ('verification','reset'):
        raise ValueError('Unknown token kind')
    column='verification_token' if kind=='verification' else 'reset_token'
    digest=token_digest(raw_token)
    c=auth_cx()
    u=c.execute(f'SELECT * FROM users WHERE {column}=?',(digest,)).fetchone()
    stored=digest
    if not u:
        # Compatibilité transitoire avec les anciens liens V1.3.2 stockés en clair.
        u=c.execute(f'SELECT * FROM users WHERE {column}=?',(raw_token,)).fetchone()
        stored=raw_token
    c.close()
    return u,stored

def send_verification_email(user, dry_run=None):
    token=gen_token()
    digest=token_digest(token)
    c=auth_cx()
    c.execute('UPDATE users SET verification_token=?,verification_sent_at=? WHERE id=?',(digest,now(),user['id']))
    c.commit(); c.close()
    base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050')
    link=f"{base}{url_for('verify_email',token=token)}"
    html=render_template('email_transactional.html',title='Confirm your email',
        intro='Click below to confirm your ProfitOS account email address. This link expires in 24 hours.',
        cta_label='Verify email',cta_url=link,footer='If you did not create a ProfitOS account, you can ignore this email.')
    return send_email(user['email'],'Confirm your ProfitOS account',html,dry_run=dry_run)

def send_reset_email(user, dry_run=None):
    token=gen_token()
    digest=token_digest(token)
    expires=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    c=auth_cx()
    c.execute('UPDATE users SET reset_token=?,reset_token_expires=? WHERE id=?',(digest,expires,user['id']))
    c.commit(); c.close()
    base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050')
    link=f"{base}{url_for('reset_password',token=token)}"
    html=render_template('email_transactional.html',title='Reset your password',
        intro='Click below to choose a new password. This link expires in 1 hour.',
        cta_label='Reset password',cta_url=link,footer='If you did not request this, you can ignore this email — your password will not change.')
    return send_email(user['email'],'Reset your ProfitOS password',html,dry_run=dry_run)

def extract_document_text(path):
    ext=path.suffix.lower()
    if ext=='.pdf':
        reader=PdfReader(str(path)); parts=[]
        for page in reader.pages[:120]:
            try: parts.append(page.extract_text() or '')
            except Exception: pass
        return '\n'.join(parts)
    if ext=='.docx':
        doc=Document(str(path)); return '\n'.join(p.text for p in doc.paragraphs)
    if ext in ('.txt','.md'):
        return path.read_text(encoding='utf-8',errors='ignore')
    raise ValueError('Format non pris en charge. Utilise PDF, DOCX ou TXT.')

def find_terms(text, terms):
    n=norm(text); return [label for label,patterns in terms.items() if any(norm(p) in n for p in patterns)]

def analyze_dce_text(text, opp, p):
    n=norm(text)
    cert_terms={
      'RGE':['rge'], 'Qualibat':['qualibat'], 'Qualifelec':['qualifelec'],
      'ISO 9001':['iso 9001'], 'ISO 14001':['iso 14001'], 'MASE':['mase'],
      'Assurance décennale':['assurance decennale','garantie decennale']}
    doc_terms={
      'DC1':['dc1'], 'DC2':['dc2'], 'DUME':['dume'],
      'Attestation fiscale':['attestation fiscale'], 'Attestation sociale':['attestation sociale','urssaf'],
      'Mémoire technique':['memoire technique'], 'Références':['references similaires','references professionnelles'],
      'Assurance':['attestation assurance','assurance responsabilite'], 'RIB':['rib','releve identite bancaire']}
    risk_terms={
      'Visite obligatoire':['visite obligatoire','visite de site obligatoire'],
      'Insertion sociale':['clause insertion','insertion professionnelle'],
      'Pénalités':['penalites de retard','penalite de retard'],
      'Retenue de garantie':['retenue de garantie'],
      'Groupement':['groupement momentane','cotraitance'],
      'Sous-traitance encadrée':['sous-traitance','sous traitance']}
    tender_terms={
      'Prix':['critere prix','prix des prestations','prix :'],
      'Valeur technique':['valeur technique','critere technique','memoire technique'],
      'Délai':['delai execution','delai d execution','planning'],
      'Environnement':['environnement','performance environnementale','developpement durable']}
    certs=find_terms(text,cert_terms); docs=find_terms(text,doc_terms); risks=find_terms(text,risk_terms); criteria=find_terms(text,tender_terms)
    amounts=[]
    for m in re.finditer(r'(?<!\d)(\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{1,2})?)\s*(?:€|euros?)', text, flags=re.I):
        amounts.append(m.group(0).strip())
    amounts=list(dict.fromkeys(amounts))[:10]
    deadlines=[]
    for m in re.finditer(r'\b(?:0?[1-9]|[12]\d|3[01])[/-](?:0?[1-9]|1[0-2])[/-](?:20\d{2})\b', text):
        deadlines.append(m.group(0))
    deadlines=list(dict.fromkeys(deadlines))[:12]
    lots=[]
    for line in text.splitlines():
        ls=line.strip()
        if re.search(r'\bLOT\s*(?:N[°º]?\s*)?\d+', ls, re.I) and 4<len(ls)<240:
            lots.append(ls)
    lots=list(dict.fromkeys(lots))[:15]
    profile_certs=norm(p['certifications'] if p else '')
    missing=[c for c in certs if c not in ('Assurance décennale',) and norm(c) not in profile_certs]
    score=int(opp['score'] or 50)
    reasons=[]; warnings=[]
    if certs:
        reasons.append(f"{len(certs)} exigence(s)/certification(s) détectée(s)")
    if missing:
        score-=min(30,10*len(missing)); warnings.append('Certifications à vérifier : '+', '.join(missing))
    else:
        if certs: score+=5; reasons.append('aucune certification manquante évidente dans le profil')
    if 'Visite obligatoire' in risks:
        score-=5; warnings.append('visite obligatoire détectée')
    if 'Mémoire technique' in docs:
        reasons.append('mémoire technique identifié')
    if len(text)<800:
        score-=15; warnings.append('document très court : analyse potentiellement incomplète')
    score=max(0,min(100,score))
    reco='GO' if score>=75 else ('REVIEW' if score>=55 else 'NO-GO')
    return {
      'go_score':score,'recommendation':reco,'certifications':certs,'missing_certifications':missing,
      'documents':docs,'risks':risks,'criteria':criteria,'amounts':amounts,'dates':deadlines,'lots':lots,
      'reasons':reasons,'warnings':warnings,'characters_analyzed':len(text)
    }


# Feature flag Phase 2 : Bid Intelligence / DCE / Partners / Consortium / Outreach Agent.
# Code et templates conservés (voir templates/_phase2/) mais désactivés pour le V0
# afin de rester strictement sur RECOVER / SAVE / GROW.
PHASE2_ENABLED = False

def user_organizations():
    uid=session.get('user_id')
    if not uid: return []
    c=auth_cx(); rows=c.execute('SELECT organizations.* FROM memberships JOIN organizations ON organizations.id=memberships.organization_id WHERE memberships.user_id=? ORDER BY organizations.name',(uid,)).fetchall(); c.close()
    return rows

def live_notifications():
    """Alertes urgentes calculées à la volée (pas de table dédiée — toujours à jour) :
    factures en retard critique, retenues libérables sous 7 jours, deadlines GROW proches."""
    if not session.get('org_id'): return []
    try:
        c=cx(); notifs=[]
        for r in c.execute("SELECT invoice_number,customer,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 AND score>=90 LIMIT 5").fetchall():
            notifs.append({'icon':'🔴','text':f"Facture #{r['invoice_number']} — {r['customer']} — {r['outstanding']:,.0f} €",'url':url_for('recover')})
        soon=(date.today()+timedelta(days=7)).isoformat(); today_iso=date.today().isoformat()
        for r in c.execute("SELECT invoice_number,customer,retention_release_date FROM invoices WHERE kind='RETENTION' AND LOWER(COALESCE(status,''))!='paid' AND retention_release_date BETWEEN ? AND ? LIMIT 5",(today_iso,soon)).fetchall():
            notifs.append({'icon':'🟡','text':f"Retenue libérable bientôt — {r['customer']} (#{r['invoice_number']})",'url':url_for('recover',filter='retention')})
        if can_access('grow'):
            for r in c.execute("SELECT title,deadline FROM opportunities WHERE type='GROW' AND status='OPEN' AND deadline BETWEEN ? AND ? LIMIT 5",(today_iso,soon)).fetchall():
                notifs.append({'icon':'🟢','text':f"Deadline proche — {r['title']}",'url':url_for('grow')})
        c.close()
        return notifs
    except Exception:
        return []

def org_branding():
    """Logo/couleur d'accent personnalisés, si configurés dans Settings. Best-effort :
    ne doit jamais faire échouer le rendu d'une page si la table n'est pas encore prête."""
    if not session.get('org_id'): return {}
    try:
        c=cx(); s=c.execute('SELECT logo_url,accent_color FROM app_settings WHERE id=1').fetchone(); c.close()
        if not s: return {}
        return {'logo_url':s['logo_url'],'accent_color':s['accent_color']}
    except Exception:
        return {}

def commercial_context():
    u=current_user()
    unseen_changelog=False
    if u and CHANGELOG_ENTRIES:
        last_seen=u['last_seen_changelog'] or ''
        unseen_changelog=CHANGELOG_ENTRIES[0]['date']>last_seen
    return {'auth_user':u,'auth_org':current_org(),'phase2_enabled':PHASE2_ENABLED,'user_orgs':user_organizations(),'app_version':current_app.config.get('APP_VERSION',''),'notifications':live_notifications(),'branding':org_branding(),'changelog_entries':CHANGELOG_ENTRIES,'unseen_changelog':unseen_changelog}




def security_session_context():
    if session.get('user_id'):
        session.permanent=True

def asset_url(filename):
    """URL statique avec un paramètre de version basé sur la date de modification
    du fichier — force le navigateur à recharger l'asset dès qu'il change, sans
    jamais avoir besoin de mettre à jour un numéro de version à la main."""
    try:
        path = current_app.static_folder and (Path(current_app.static_folder)/filename)
        v = int(path.stat().st_mtime) if path and path.exists() else 0
    except Exception:
        v = 0
    return url_for('static', filename=filename, v=v)

def init_runtime(app):
    """Attach shared request hooks and Jinja globals to a Flask app instance."""
    app.jinja_env.globals['can_access'] = can_access
    app.jinja_env.globals['ROLE_LABELS'] = ROLE_LABELS
    app.jinja_env.globals['ROLES'] = ROLES
    app.jinja_env.globals['csrf_token'] = csrf_token
    app.jinja_env.globals['trial_days_left'] = trial_days_left
    app.jinja_env.globals['current_role'] = current_role
    app.jinja_env.globals['asset_url'] = asset_url
    app.before_request(csrf_protect)
    app.before_request(security_session_context)
    app.before_request(ensure_tenant_schema)
    app.context_processor(commercial_context)
