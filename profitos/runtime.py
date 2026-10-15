from flask import Flask, render_template, request, redirect, url_for, flash, abort, session, g, current_app, jsonify, Response, send_file
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
from . import storage as _storage
from .plan_limits import feature_enabled as plan_feature_enabled
from profitos.accounting import paris_today

BASE=Path(__file__).resolve().parent.parent
AUTH_DB=BASE/'profitos_auth.db'; TENANTS=BASE/'tenant_data'; TENANTS.mkdir(exist_ok=True); UP=_storage.UPLOAD_ROOT  # disque persistant en production (PROFITOS_UPLOAD_DIR)
# Le disque Render n'est monté que sur l'instance en service (ni pendant le build, ni
# pendant la commande pre-deploy) : on ne plante jamais à l'import, les dossiers sont
# créés à la première écriture et /readyz signale un disque absent.
try:
    UP.mkdir(parents=True,exist_ok=True)
except OSError as _exc:
    __import__('logging').getLogger('profitos.storage').warning('Dossier des documents indisponible (%s) : %s', UP, _exc)
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
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,full_name TEXT,is_active INTEGER DEFAULT 1,email_verified INTEGER DEFAULT 0,verification_token TEXT,verification_sent_at TEXT,reset_token TEXT,reset_token_expires TEXT,auth_version INTEGER DEFAULT 0,theme_preference TEXT DEFAULT 'dark',last_seen_changelog TEXT,ui_mode TEXT DEFAULT 'simple',created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS memberships(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,organization_id INTEGER NOT NULL,role TEXT DEFAULT 'OWNER',created_at TEXT,UNIQUE(user_id,organization_id));
    CREATE TABLE IF NOT EXISTS push_device_tokens(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT,
        UNIQUE(user_id,token)
    );
    CREATE TABLE IF NOT EXISTS cabinet_time_entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        organization_id INTEGER NOT NULL,
        entry_date TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        description TEXT,
        billable INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cabinet_documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id INTEGER NOT NULL,
        uploaded_by INTEGER,
        filename TEXT NOT NULL,
        category TEXT,
        stored_name TEXT NOT NULL,
        uploaded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_cabinet_time_org ON cabinet_time_entries(organization_id);
    CREATE INDEX IF NOT EXISTS idx_cabinet_docs_org ON cabinet_documents(organization_id);
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
        created_at TEXT,
        siren TEXT
    );
    CREATE TABLE IF NOT EXISTS api_keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id INTEGER NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT,
        created_by TEXT,
        created_at TEXT,
        last_used_at TEXT,
        revoked_at TEXT,
        scope TEXT DEFAULT 'read',
        scopes TEXT DEFAULT 'read',
        entity_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS api_audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key_id INTEGER,
        organization_id INTEGER NOT NULL,
        entity_id INTEGER,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        scope_required TEXT,
        status_code INTEGER,
        idempotency_key TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_api_audit_org ON api_audit_log(organization_id,created_at);
    CREATE TABLE IF NOT EXISTS api_idempotency(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id INTEGER NOT NULL,
        entity_key INTEGER NOT NULL DEFAULT 0,
        api_key_id INTEGER,
        idempotency_key TEXT NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        response_status INTEGER,
        response_body TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(organization_id,entity_key,idempotency_key,method,path)
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
    CREATE TABLE IF NOT EXISTS accounting_fec_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        date_from TEXT NOT NULL,
        date_to TEXT NOT NULL,
        created_at TEXT,
        entity_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS supplier_inbox_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL UNIQUE,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS supplier_inbox_entity_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        entity_id INTEGER,
        entity_key INTEGER NOT NULL,
        created_at TEXT,
        UNIQUE(organization_id,entity_key)
    );
    CREATE TABLE IF NOT EXISTS outgoing_invoice_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        invoice_local_id INTEGER NOT NULL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS outgoing_quote_tokens(
        token TEXT PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        quote_local_id INTEGER NOT NULL,
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
        # Menu simplifié par défaut : les modules experts restent accessibles via « Afficher toutes les fonctionnalités ».
        ('ui_mode',"ALTER TABLE users ADD COLUMN ui_mode TEXT DEFAULT 'simple'"),
    ):
        if col not in cols: c.execute(ddl)
    demo_cols=[r['name'] for r in c.execute('PRAGMA table_info(demo_requests)').fetchall()]
    for col,ddl in (
        ('sector',"ALTER TABLE demo_requests ADD COLUMN sector TEXT"),
        ('company_size',"ALTER TABLE demo_requests ADD COLUMN company_size TEXT"),
        ('primary_need',"ALTER TABLE demo_requests ADD COLUMN primary_need TEXT"),
        ('status',"ALTER TABLE demo_requests ADD COLUMN status TEXT DEFAULT 'NOUVEAU'"),
        ('siren',"ALTER TABLE demo_requests ADD COLUMN siren TEXT"),
    ):
        if col not in demo_cols: c.execute(ddl)

    org_cols=[r['name'] for r in c.execute('PRAGMA table_info(organizations)').fetchall()]
    for col,ddl in (
        ('stripe_customer_id',"ALTER TABLE organizations ADD COLUMN stripe_customer_id TEXT"),
        ('stripe_subscription_id',"ALTER TABLE organizations ADD COLUMN stripe_subscription_id TEXT"),
        ('referral_code',"ALTER TABLE organizations ADD COLUMN referral_code TEXT"),
        # Organisation qui porte l'abonnement (formule Multi-sociétés) : une
        # organisation fille hérite du plan et du statut de son parent.
        ('parent_org_id',"ALTER TABLE organizations ADD COLUMN parent_org_id INTEGER"),
        # Début de l'impayé : sert au délai de grâce PAST_DUE.
        ('past_due_since',"ALTER TABLE organizations ADD COLUMN past_due_since TEXT"),
        # Mois offerts par le parrainage, en attente d'imputation Stripe.
        ('referral_credit_months',"ALTER TABLE organizations ADD COLUMN referral_credit_months INTEGER DEFAULT 0"),
        # Benchmark des délais de paiement : participation volontaire (désactivée par défaut).
        ('benchmark_opt_in',"ALTER TABLE organizations ADD COLUMN benchmark_opt_in INTEGER DEFAULT 0"),
        # Acceptation des CGU/CGV à l'inscription (date et version).
        ('terms_accepted_at',"ALTER TABLE organizations ADD COLUMN terms_accepted_at TEXT"),
        ('terms_version',"ALTER TABLE organizations ADD COLUMN terms_version TEXT"),
    ):
        if col not in org_cols: c.execute(ddl)
    api_keys_cols=[r['name'] for r in c.execute('PRAGMA table_info(api_keys)').fetchall()]
    for col,ddl in (
        # Les clés créées avant l'ajout de ce champ restent en lecture seule
        # (comportement d'origine préservé — jamais d'élévation silencieuse
        # vers l'écriture pour une clé déjà distribuée à un client).
        ('scope',"ALTER TABLE api_keys ADD COLUMN scope TEXT DEFAULT 'read'"),
        ('scopes',"ALTER TABLE api_keys ADD COLUMN scopes TEXT DEFAULT 'read'"),
        ('entity_id',"ALTER TABLE api_keys ADD COLUMN entity_id INTEGER"),
    ):
        if col not in api_keys_cols: c.execute(ddl)
    fec_tokens_cols=[r['name'] for r in c.execute('PRAGMA table_info(accounting_fec_tokens)').fetchall()]
    for col,ddl in (
        ('entity_id',"ALTER TABLE accounting_fec_tokens ADD COLUMN entity_id INTEGER"),
    ):
        if col not in fec_tokens_cols: c.execute(ddl)
    c.commit()
    try:
        dbmod.upgrade_real_columns(c)
    except Exception as e:
        c.rollback()
        __import__("logging").getLogger("profitos.migrations").warning("conversion des colonnes REAL (auth) non faite (%s)", e)
    c.close()
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
    """Compatibilité : migre tous les tenants (isolés) et lève si l'un échoue."""
    report=migrate_all_tenant_schemas()
    if report['failed']:
        raise RuntimeError(f"Migration échouée pour {len(report['failed'])} organisation(s) : {sorted(report['failed'])}")
    return len(report['up_to_date'])+len(report['migrated'])

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
        # request.remote_addr est déjà corrigé par ProxyFix (proxy Render de confiance) ;
        # lire X-Forwarded-For brut permettait au client d'imposer une fausse adresse.
        raw=(request.remote_addr or '').strip()
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
    from profitos.account_security import session_sid_valid
    if not session_sid_valid(r['id']):
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
    session['org_id']=m['organization_id']; session['role']=m['role']; session.pop('current_entity_id',None)
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
ALLOWED_INVOICE_EXTENSIONS={'.csv','.xlsx','.xls'}
ALLOWED_DCE_EXTENSIONS={'.pdf','.docx','.txt','.md'}
WEBHOOK_HOST_SUFFIXES=('hooks.slack.com','webhook.office.com','logic.azure.com','outlook.office.com')


def safe_next_url(target):
    """N'autorise que les redirections relatives internes (anti open-redirect)."""
    if not target or not isinstance(target,str): return None
    parsed=urlsplit(target)
    if parsed.scheme or parsed.netloc or not target.startswith('/') or target.startswith('//'):
        return None
    return target


def password_error(password, email=None):
    """Politique de mot de passe : 12 caractères minimum, absent de la liste des
    mots de passe les plus courants, sans la partie locale de l'adresse e-mail."""
    from profitos.account_security import password_policy_error
    return password_policy_error(password, email)


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
    target_dir=_storage.upload_dir(org_id,category)
    target=target_dir/f'{secrets.token_hex(16)}{ext}'
    file_storage.save(target)
    try:
        if not _signature_ok(target,ext):
            raise ValueError('Le contenu du fichier ne correspond pas au format attendu.')
        check_spreadsheet_bomb(target)
        return target, original
    except Exception:
        target.unlink(missing_ok=True)
        raise


MAX_SPREADSHEET_UNCOMPRESSED=80*1024*1024   # 80 Mo décompressés
MAX_SPREADSHEET_RATIO=100                     # un xlsx réel se compresse rarement au-delà de 1:30

def check_spreadsheet_bomb(path):
    """Refuse un classeur Excel « bombe » : quelques centaines de Ko qui se décompressent en
    centaines de Mo et saturent la mémoire du serveur (un seul worker en production)."""
    p=Path(path)
    if p.suffix.lower() not in ('.xlsx','.xlsm'):
        return
    import zipfile
    try:
        with zipfile.ZipFile(p) as z:
            total=sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile:
        raise ValueError('Fichier Excel illisible ou corrompu.')
    size=max(p.stat().st_size,1)
    if total>MAX_SPREADSHEET_UNCOMPRESSED or total/size>MAX_SPREADSHEET_RATIO:
        raise ValueError('Fichier Excel trop volumineux une fois décompressé : découpez-le en plusieurs fichiers.')

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
STRIPE_PRICE_MULTI_ID = os.environ.get('STRIPE_PRICE_MULTI_ID','')

# ---------------------------------------------------------------------------
# Lot 23 — connexion à la Plateforme Agréée WeInvoice/Weproc (facturation
# électronique réglementaire). Le client_secret ne quitte jamais le serveur :
# jamais en base, jamais dans le HTML, jamais dans Git.
# ---------------------------------------------------------------------------
WEINVOICE_ENV=os.environ.get('WEINVOICE_ENV','sandbox')
# Clé "Management : Compte" — portefeuille clients, onboarding/KYB (Lot 23.2).
WEINVOICE_CLIENT_ID=os.environ.get('WEINVOICE_CLIENT_ID')
WEINVOICE_CLIENT_SECRET=os.environ.get('WEINVOICE_CLIENT_SECRET')
# Clé "Données : Facturation" — émission/lecture de factures (Lot 23.3). Distincte
# de la clé Management : WeInvoice sépare les deux types de clés, chacune avec ses
# propres permissions (confirmé par le tableau de bord WeInvoice — "invoice:write"
# n'est porté que par une clé de type "Données : Facturation").
WEINVOICE_INVOICE_CLIENT_ID=os.environ.get('WEINVOICE_INVOICE_CLIENT_ID')
WEINVOICE_INVOICE_CLIENT_SECRET=os.environ.get('WEINVOICE_INVOICE_CLIENT_SECRET')
WEINVOICE_WEBHOOK_SECRET=os.environ.get('WEINVOICE_WEBHOOK_SECRET')
# Secret distinct pour le webhook invoice.status.* — WeInvoice délivre un secret de
# signature séparé par point de terminaison (même logique que les 2 clés API
# Management/Facturation). Si un seul secret existe côté WeInvoice, cette
# variable peut rester vide : le code retombe alors sur WEINVOICE_WEBHOOK_SECRET.
WEINVOICE_INVOICE_WEBHOOK_SECRET=os.environ.get('WEINVOICE_INVOICE_WEBHOOK_SECRET')

# ---------------------------------------------------------------------------
# Lot 24 — extraction IA des factures d'achat (PDF scannés / photos, en repli de
# l'extraction texte gratuite existante). Clé API Anthropic distincte de tout ce
# qui précède — jamais en base, jamais dans le HTML, jamais dans Git.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY=os.environ.get('ANTHROPIC_API_KEY')
ANTHROPIC_MODEL=os.environ.get('ANTHROPIC_MODEL','claude-sonnet-5')
WEINVOICE_BASE_URL='https://api-sandbox.weinvoice.fr' if WEINVOICE_ENV!='production' else 'https://api.weinvoice.fr'
# Tarifs HT. Mensuel sans engagement ; annuel payé d'avance = 10 mois (2 mois offerts),
# disponible dès que le tarif annuel Stripe correspondant est configuré.
ANNUAL_MONTHS_BILLED=10
STRIPE_PLANS={
    'STARTER': {'name':'Starter','price_eur':9,'price_id':STRIPE_PRICE_STARTER_ID,
                'price_id_year':os.environ.get('STRIPE_PRICE_STARTER_YEARLY_ID','')},
    'PRO': {'name':'Pro','price_eur':29,'price_id':STRIPE_PRICE_PRO_ID,
            'price_id_year':os.environ.get('STRIPE_PRICE_PRO_YEARLY_ID','')},
    'BUSINESS': {'name':'Business','price_eur':59,'price_id':STRIPE_PRICE_BUSINESS_ID,
                 'price_id_year':os.environ.get('STRIPE_PRICE_BUSINESS_YEARLY_ID','')},
    'MULTI': {'name':'Multi-sociétés','price_eur':199,'price_id':STRIPE_PRICE_MULTI_ID,
              'price_id_year':os.environ.get('STRIPE_PRICE_MULTI_YEARLY_ID','')},
}
for _p in STRIPE_PLANS.values():
    _p['price_year_eur']=_p['price_eur']*ANNUAL_MONTHS_BILLED
PLAN_ORDER=('STARTER','PRO','BUSINESS','MULTI')
STRIPE_PRICE_TO_PLAN={v['price_id']:k for k,v in STRIPE_PLANS.items() if v.get('price_id')}
STRIPE_PRICE_TO_PLAN.update({v['price_id_year']:k for k,v in STRIPE_PLANS.items() if v.get('price_id_year')})
STRIPE_PRICE_INTERVAL={v['price_id']:'month' for v in STRIPE_PLANS.values() if v.get('price_id')}
STRIPE_PRICE_INTERVAL.update({v['price_id_year']:'year' for v in STRIPE_PLANS.values() if v.get('price_id_year')})


def plan_price_id(plan, interval='month'):
    """price_id Stripe d'une formule pour une périodicité ('month' ou 'year'), ou None."""
    p=STRIPE_PLANS.get((plan or '').upper())
    if not p: return None
    return (p.get('price_id_year') if interval=='year' else p.get('price_id')) or None


def annual_billing_available():
    return any(v.get('price_id_year') for v in STRIPE_PLANS.values())
# Un abonnement ne doit jamais être activé sans webhook signé : c'est Stripe,
# et non la page de succès du navigateur, qui fait foi.
BILLING_ENABLED=bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_TO_PLAN)
# TVA française sur l'abonnement SaaS : calcul automatique Stripe Tax (désactivable
# uniquement si Stripe Tax n'est pas encore activé sur le compte).
STRIPE_AUTOMATIC_TAX=os.environ.get('STRIPE_AUTOMATIC_TAX','1').strip().lower() not in ('0','false','no','off')
# Délai de grâce après un échec de paiement (statut PAST_DUE) avant blocage.
PAST_DUE_GRACE_DAYS=int(os.environ.get('PROFITOS_PAST_DUE_GRACE_DAYS','7') or 7)

def get_stripe():
    if not BILLING_ENABLED: return None
    import stripe
    stripe.api_key=STRIPE_SECRET_KEY
    return stripe

def _row_get(row, key, default=None):
    if row is None: return default
    try:
        value=row.get(key, default) if hasattr(row,'get') else row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value

def billing_org(org):
    """Organisation qui porte l'abonnement : le parent pour une organisation
    rattachée à une formule Multi-sociétés, sinon l'organisation elle-même."""
    if not org: return org
    parent_id=_row_get(org,'parent_org_id')
    if not parent_id or str(parent_id)==str(_row_get(org,'id')): return org
    c=auth_cx()
    try:
        parent=c.execute('SELECT * FROM organizations WHERE id=?',(parent_id,)).fetchone()
    finally:
        c.close()
    if parent: return parent
    # Parent supprimé : l'organisation fille ne doit jamais conserver seule un
    # accès payant hérité.
    orphan=dict(org)
    orphan['status']='CANCELED'
    return orphan

def effective_plan(org):
    """Plan réellement applicable (celui du parent pour une organisation fille)."""
    from .plan_limits import normalize_plan
    b=billing_org(org)
    return normalize_plan(_row_get(b,'plan','TRIAL'))

def trial_days_left(org):
    if not org or not org['trial_ends_at']: return None
    end=parse_dt(org['trial_ends_at'])
    if not end: return None
    remaining=(end-datetime.now(timezone.utc)).total_seconds()
    # Arrondi au jour supérieur : un essai de 14 jours affiche « 14 jours restants » le
    # jour de l'inscription (et non 13), puis 0 seulement une fois l'échéance passée.
    return max(0, int(-(-remaining // 86400)))

def trial_is_active(org):
    if not org or (_row_get(org,'plan','TRIAL') or 'TRIAL').upper()!='TRIAL': return False
    if _row_get(org,'status')=='CANCELED': return False
    if not _row_get(org,'trial_ends_at'): return True
    end=parse_dt(org['trial_ends_at'])
    return end is None or end>datetime.now(timezone.utc)

def past_due_grace_end(org):
    """Fin du délai de grâce d'une organisation PAST_DUE (datetime UTC) ou None."""
    if not org or _row_get(org,'status')!='PAST_DUE': return None
    start=parse_dt(_row_get(org,'past_due_since') or _row_get(org,'updated_at') or '')
    if not start: return None
    return start+timedelta(days=PAST_DUE_GRACE_DAYS)

def subscription_state(org):
    """État d'abonnement lisible : ACTIVE, TRIAL, TRIAL_EXPIRED, PAST_DUE_GRACE,
    PAST_DUE_BLOCKED, CANCELED ou INACTIVE (calculé sur l'organisation payeuse)."""
    b=billing_org(org)
    if not b: return 'INACTIVE'
    status=_row_get(b,'status')
    if status=='ACTIVE_PAID': return 'ACTIVE'
    if status=='PAST_DUE':
        end=past_due_grace_end(b)
        return 'PAST_DUE_GRACE' if (end is None or end>datetime.now(timezone.utc)) else 'PAST_DUE_BLOCKED'
    if status=='CANCELED': return 'CANCELED'
    if (_row_get(b,'plan','TRIAL') or 'TRIAL').upper()=='TRIAL':
        return 'TRIAL' if trial_is_active(b) else 'TRIAL_EXPIRED'
    return 'INACTIVE'

def org_has_access(org):
    """True si l'organisation peut utiliser l'app.

    Règles (sur l'organisation payeuse, c.-à-d. le parent Multi-sociétés) :
    abonnement ACTIVE_PAID, essai en cours, ou impayé PAST_DUE encore dans le
    délai de grâce. Un essai valide reste utilisable même si Stripe n'est pas
    encore configuré ; le mode permissif total reste réservé au développement.
    """
    if not org:
        return False
    if not BILLING_ENABLED:
        # Mode permissif réservé aux environnements explicitement non production
        # (PROFITOS_ENV absent ou inconnu = production, cf. config.is_production).
        from profitos.config import is_production
        if not is_production():
            return True
        # Production sans Stripe : on n'ouvre jamais l'accès par défaut, on
        # applique les règles ci-dessous (essai valide, abonnement existant).
    # PAST_DUE = paiement refusé : accès maintenu pendant le délai de grâce
    # (PAST_DUE_GRACE_DAYS, bandeau d'alerte), puis bloqué.
    return subscription_state(org) in ('ACTIVE','TRIAL','PAST_DUE_GRACE')

def requires_active_plan(fn):
    """À poser sur les routes de valeur (dashboard, recover/save/grow...). Redirige
    vers la page « Abonnement inactif » (accessible à tous les rôles, sans boucle
    de redirection) si ni essai ni abonnement actif n'existe."""
    @functools.wraps(fn)
    def wrapped(*a,**kw):
        org=current_org()
        if not org_has_access(org):
            from profitos.feature_access import is_readonly_export_request
            if is_readonly_export_request():
                return fn(*a,**kw)  # exports en lecture seule toujours disponibles
            return redirect(url_for('subscription_inactive'))
        return fn(*a,**kw)
    return wrapped

def sync_child_orgs(acx, parent_org_id):
    """Recopie plan/statut du parent sur ses organisations rattachées.
    'acx' est une connexion auth ouverte : l'appelant gère le commit."""
    parent=acx.execute('SELECT plan,status FROM organizations WHERE id=?',(parent_org_id,)).fetchone()
    if not parent: return
    acx.execute('UPDATE organizations SET plan=?,status=?,updated_at=? WHERE parent_org_id=?',
                (parent['plan'],parent['status'],now(),parent_org_id))

def team_seat_available(org_id):
    """True si l'organisation peut accueillir un membre de plus selon sa formule
    effective (après une baisse de formule, aucune nouvelle adhésion au-delà)."""
    from .plan_limits import within_limit
    c=auth_cx()
    try:
        org=c.execute('SELECT * FROM organizations WHERE id=?',(org_id,)).fetchone()
        if not org: return False
        n=c.execute('SELECT COUNT(*) AS n FROM memberships WHERE organization_id=?',(org_id,)).fetchone()['n']
    finally:
        c.close()
    return within_limit(effective_plan(org),'team_members',n,1)

def plan_overage(org):
    """Ce qui dépasse la formule actuelle après un changement de formule
    (aucune donnée n'est supprimée) : {'members': [...], 'organizations': [...],
    'team_limit': n, 'org_limit': n}. Listes vides si tout est conforme."""
    from .plan_limits import plan_limit
    result={'members':[],'organizations':[],'team_limit':None,'org_limit':None}
    if not org: return result
    plan=effective_plan(org); payer=billing_org(org) or org
    team_limit=plan_limit(plan,'team_members'); org_limit=plan_limit(plan,'organizations')
    result['team_limit']=team_limit; result['org_limit']=org_limit
    c=auth_cx()
    try:
        if team_limit is not None:
            members=c.execute('SELECT u.email,u.full_name,m.role FROM memberships m JOIN users u ON u.id=m.user_id '
                              'WHERE m.organization_id=? ORDER BY m.id',(org['id'],)).fetchall()
            result['members']=[(m['full_name'] or m['email']) for m in members[int(team_limit):]]
        if org_limit is not None:
            orgs=c.execute('SELECT id,name FROM organizations WHERE id=? OR parent_org_id=? ORDER BY id',
                           (payer['id'],payer['id'])).fetchall()
            result['organizations']=[o['name'] for o in orgs[int(org_limit):]]
    finally:
        c.close()
    return result

# ---------------------------------------------------------------------------
# Rôles équipe — accès par module. Un OWNER a toujours tout ; les autres
# rôles sont volontairement restreints à leur périmètre métier.
# ---------------------------------------------------------------------------
ROLES=['OWNER','ADMIN','COMPTABLE','COMMERCIAL','MEMBER','LECTURE']
ROLE_LABELS={'OWNER':'Propriétaire','ADMIN':'Administrateur','COMPTABLE':'Comptable',
             'COMMERCIAL':'Commercial','MEMBER':'Membre','LECTURE':'Lecture seule'}
AREA_ACCESS={
    'OWNER':{'recover','save','grow','actions','impact','weekly','uploads','settings','billing','team','invoicing','accounting','banking','pilotage','cabinet'},
    'ADMIN':{'recover','save','grow','actions','impact','weekly','uploads','settings','team','invoicing','accounting','banking','pilotage','cabinet'},
    'COMPTABLE':{'recover','save','impact','weekly','uploads','actions','invoicing','accounting','banking','pilotage','cabinet'},
    'COMMERCIAL':{'grow','actions'},
    'MEMBER':{'recover','save','grow','actions','impact','weekly','uploads','invoicing','accounting','banking','pilotage'},
    # Lecture seule : consultation des modules, aucune écriture (voir enforce_role_areas).
    'LECTURE':{'recover','save','grow','actions','impact','weekly','invoicing','accounting','banking','pilotage'},
}
# Seules écritures permises au rôle LECTURE : son propre compte et sa session.
LECTURE_ALLOWED_POST={'logout','toggle_theme','changelog_seen','org_switch','entity_switch','verify_resend',
                      'account_name_change','account_password_change','account_email_change','two_factor_setup','two_factor_disable',
                      'two_factor_recovery_codes','login_two_factor','team_invitation','delete_account'}

# Contrôle des rôles par module entier : toute route d'un module sensible exige la zone
# correspondante, même si le décorateur @require_area a été oublié sur une route.
MODULE_AREAS={
    'profitos.routes.accounting':'accounting','profitos.routes.loans':'accounting',
    'profitos.routes.finance_leases':'accounting','profitos.routes.analytics':'accounting',
    'profitos.routes.reviews':'accounting','profitos.routes.payroll':'accounting',
    'profitos.routes.ledger':'accounting',
    'profitos.routes.bank_sync':'banking','profitos.routes.swan_baas':'banking',
    'profitos.routes.gocardless_baas':'banking',
    'profitos.routes.cash_intelligence':'pilotage','profitos.routes.financial_brain':'pilotage',
    'profitos.routes.decision_simulator':'pilotage','profitos.routes.money_hunter':'pilotage',
    'profitos.routes.cabinet':'cabinet',
}
ENDPOINT_AREAS={
    'cash_forecast':'pilotage','monthly_report_pdf':'pilotage',
    'entity_new':'settings','entity_detail':'settings',
    'mileage_rate_settings':'settings',
}
POST_ONLY_AREAS={'onboarding':'settings','company':'settings'}
ROLE_GUARD_EXEMPT={'accountant_invite_accept','entity_switch','static'}

def enforce_role_areas():
    if not session.get('user_id') or not session.get('org_id'):
        return
    endpoint=request.endpoint
    if not endpoint or endpoint in ROLE_GUARD_EXEMPT:
        return
    if (request.method in ('POST','PUT','PATCH','DELETE') and endpoint not in LECTURE_ALLOWED_POST
            and current_role()=='LECTURE'):
        log_security_event('ROLE_READ_ONLY_DENIED','BLOCKED',user_id=session.get('user_id'),
                           organization_id=session.get('org_id'),target=f'write:{endpoint}')
        flash("Votre rôle « Lecture seule » ne permet pas de modifier les données.")
        return redirect(request.referrer if safe_next_url(urlsplit(request.referrer or '').path) else url_for('home'))
    fn=current_app.view_functions.get(endpoint)
    area=ENDPOINT_AREAS.get(endpoint) or MODULE_AREAS.get(getattr(fn,'__module__',''))
    if not area and request.method=='POST':
        area=POST_ONLY_AREAS.get(endpoint)
    if area and not can_access(area):
        log_security_event('ROLE_AREA_DENIED','BLOCKED',user_id=session.get('user_id'),
                           organization_id=session.get('org_id'),target=f'{area}:{endpoint}')
        flash("Votre rôle ne donne pas accès à cette section.")
        return redirect(url_for('home'))
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
        if request.path in ('/billing/webhook','/webhooks/weinvoice/client-onboarding','/webhooks/weinvoice/invoice-status','/webhooks/supplier-inbox','/webhooks/swan'):
            return
        # Les routes /api/v1/* s'authentifient par clé API (Authorization: Bearer),
        # jamais par cookie de session — un navigateur ne rejoue jamais un en-tête
        # Authorization sur une requête cross-site, donc le CSRF ne s'applique pas
        # à ces routes par construction (contrairement aux formulaires web classiques
        # ci-dessous, qui s'appuient sur le cookie de session).
        if request.path.startswith('/api/v1/'):
            return
        token=session.get('csrf_token'); sent=request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        # compare_digest lève TypeError sur une chaîne non ASCII : comparer des octets.
        if (not isinstance(token,str) or not isinstance(sent,str) or not token or not sent
                or not secrets.compare_digest(token.encode('utf-8'),sent.encode('utf-8'))):
            abort(400,description='CSRF token invalide ou manquant.')
        # Défense supplémentaire en production : si Origin/Referer est présent, il doit être same-origin.
        source=request.headers.get('Origin') or request.headers.get('Referer')
        from profitos.config import is_production
        if source and is_production():
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
# v400 : le set est protégé par un verrou (gunicorn --threads) et la
# migration complète n'est rejouée que si la version du script de schéma
# stockée dans le tenant (table schema_version) diffère de celle du code.
import threading as _threading
import logging as _logging
_tenant_schema_checked=set()
_tenant_schema_lock=_threading.Lock()
_tenant_migration_locks={}
_migration_log=_logging.getLogger('profitos.migrations')
_migration_state=_threading.local()
_TENANT_SCHEMA_VERSION=None


def _migration_warn(message):
    """Remplace les print() « ATTENTION … ignorée » : log WARNING visible dans
    les logs Render et collecte pour le rapport de migration du preDeploy."""
    _migration_log.warning('%s', message)
    collected=getattr(_migration_state,'warnings',None)
    if collected is not None:
        collected.append(str(message)[:300])


def tenant_schema_version():
    """Empreinte du script de migration tenant (code + seeds).

    Toute modification de _migrate_tenant_schema (nouvelle table, colonne,
    index) change l'empreinte et déclenche la migration des tenants au
    prochain preDeploy ou au premier accès."""
    global _TENANT_SCHEMA_VERSION
    if _TENANT_SCHEMA_VERSION is None:
        import inspect
        h=hashlib.sha256()
        try:
            from profitos.accounting import seed_accounting_defaults
            from profitos.expenses import seed_mileage_rate_table
            for fn in (_migrate_tenant_schema, seed_accounting_defaults, seed_mileage_rate_table):
                h.update(inspect.getsource(fn).encode('utf-8'))
        except Exception:
            h.update(Path(__file__).read_bytes())
        _TENANT_SCHEMA_VERSION=h.hexdigest()[:20]
    return _TENANT_SCHEMA_VERSION


def _tenant_schema_current(org_id):
    """Vérification bon marché (1 requête) de la version du schéma tenant."""
    try:
        c=dbmod.connect_tenant(org_id, tenant_db(org_id))
    except Exception:
        return False
    try:
        row=c.execute('SELECT version FROM schema_version WHERE id=1').fetchone()
        return bool(row and row['version']==tenant_schema_version())
    except Exception:
        return False
    finally:
        c.close()


def _record_tenant_schema_version(org_id):
    c=dbmod.connect_tenant(org_id, tenant_db(org_id))
    try:
        c.execute('CREATE TABLE IF NOT EXISTS schema_version(id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL,applied_at TEXT)')
        c.execute('INSERT INTO schema_version(id,version,applied_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,applied_at=excluded.applied_at',
                  (tenant_schema_version(),now()))
        c.commit()
    finally:
        c.close()


def migrate_tenant(org_id, force=False):
    """Migre un tenant si nécessaire. Retourne un dict de rapport.

    status : 'up_to_date' | 'migrated' | 'migrated_with_warnings'.
    Les exceptions remontent à l'appelant (isolation gérée par
    migrate_all_tenant_schemas)."""
    org_id=int(org_id)
    if not force and _tenant_schema_current(org_id):
        return {'org_id':org_id,'status':'up_to_date','warnings':[]}
    with _tenant_schema_lock:
        lock=_tenant_migration_locks.setdefault(org_id,_threading.Lock())
    with lock:
        if not force and _tenant_schema_current(org_id):
            return {'org_id':org_id,'status':'up_to_date','warnings':[]}
        previous=getattr(_migration_state,'warnings',None)
        _migration_state.warnings=[]
        try:
            _migrate_tenant_schema(org_id)
            warnings=list(_migration_state.warnings)
        finally:
            _migration_state.warnings=previous
        if warnings:
            # Version non enregistrée : la migration sera retentée au prochain
            # déploiement / accès, et le rapport preDeploy la signale.
            return {'org_id':org_id,'status':'migrated_with_warnings','warnings':warnings}
        _record_tenant_schema_version(org_id)
        return {'org_id':org_id,'status':'migrated','warnings':[]}


def migrate_all_tenant_schemas(force=False):
    """Migre tous les tenants avec isolation des erreurs par organisation."""
    report={'version':tenant_schema_version(),'up_to_date':[],'migrated':[],'warnings':{},'failed':{}}
    for org_id in list_organization_ids():
        try:
            r=migrate_tenant(org_id, force=force)
        except Exception as exc:
            _migration_log.exception('Migration tenant org_%s ÉCHOUÉE', org_id)
            report['failed'][org_id]=f'{type(exc).__name__}: {str(exc)[:200]}'
            continue
        if r['status']=='up_to_date':
            report['up_to_date'].append(org_id)
        else:
            report['migrated'].append(org_id)
            if r['warnings']:
                report['warnings'][org_id]=r['warnings']
    return report


def ensure_tenant_schema():
    org_id=session.get('org_id')
    if org_id and org_id not in _tenant_schema_checked:
        try:
            init_tenant_db()
            with _tenant_schema_lock:
                _tenant_schema_checked.add(org_id)
        except Exception as e:
            # Fail closed : ne jamais servir une organisation avec un schéma
            # potentiellement partiellement migré. Les détails restent côté logs.
            current_app.logger.exception(
                "Tenant schema migration failed org_id=%s request_id=%s",
                org_id, getattr(g, 'request_id', '-'),
            )
            abort(503, description='Schéma de données temporairement indisponible.')

_aging_refreshed={}


def refresh_invoice_aging(c, today=None):
    """Recalcule le retard (days_overdue) et le score des créances importées à la date
    du jour (Europe/Paris). Le retard stocké à l'import vieillissait : une facture
    échue la veille de l'import restait « 1 jour de retard » indéfiniment."""
    from zoneinfo import ZoneInfo
    today = today or datetime.now(ZoneInfo('Europe/Paris')).date()
    rows = c.execute("SELECT id,amount,paid_amount,due_date,retention_release_date,kind,status,days_overdue,score FROM invoices "
                     "WHERE LOWER(COALESCE(status,''))!='paid'").fetchall()
    changed = 0
    for r in rows:
        ref = parse_date(r['retention_release_date']) if r['kind'] == 'RETENTION' and r['retention_release_date'] else parse_date(r['due_date'])
        days = max(0, (today - ref).days) if ref and ref <= today else 0
        score = invoice_score(r['amount'] or 0, days, r['paid_amount'] or 0) if days > 0 else 0
        if days != (r['days_overdue'] or 0) or score != (r['score'] or 0):
            c.execute('UPDATE invoices SET days_overdue=?,score=? WHERE id=?', (days, score, r['id']))
            changed += 1
    if changed:
        c.commit()
    return changed


def refresh_invoice_aging_hook():
    """Avant requête : retard des créances calculé au jour de lecture (une fois par
    organisation et par jour, coût négligeable ensuite)."""
    org_id = session.get('org_id')
    if not org_id or request.endpoint in ('static', 'healthz', 'readyz', 'ops_health'):
        return
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Europe/Paris')).date()
    if _aging_refreshed.get(org_id) == today:
        return
    try:
        c = tenant_cx_direct(org_id)
        try:
            refresh_invoice_aging(c, today)
        finally:
            c.close()
        _aging_refreshed[org_id] = today
    except Exception:
        current_app.logger.warning('Actualisation du retard des créances impossible', exc_info=True)


_ENTITY_GUARD_EXEMPT=('static','logout','login','healthz','readyz','ops_health','org_switch')

def enforce_entity_access():
    """Applique côté serveur la restriction d'un utilisateur à certaines entités (filiales).

    - l'entité active en session est toujours une entité autorisée (sinon : la première
      autorisée ; aucune -> 403) ;
    - tout ``entity_id`` envoyé par un formulaire ou dans l'URL est vérifié, pour qu'un
      utilisateur limité à une filiale ne puisse ni lire ni écrire dans une autre.
    Sans restriction configurée (cas par défaut), rien ne change."""
    uid=session.get('user_id'); org_id=session.get('org_id')
    if not uid or not org_id or request.endpoint in _ENTITY_GUARD_EXEMPT:
        return
    from profitos.entities import user_has_entity_restrictions, user_can_access_entity, accessible_entities
    tc=tenant_cx_direct(org_id)
    try:
        if not user_has_entity_restrictions(tc, uid):
            return
        active=session.get('current_entity_id')
        if not user_can_access_entity(tc, uid, active):
            allowed=accessible_entities(tc, uid)
            if not allowed:
                abort(403)
            session['current_entity_id']=allowed[0]['id']
        for source in (request.form, request.args):
            if 'entity_id' not in source:
                continue
            raw=(source.get('entity_id') or '').strip()
            if raw in ('', 'None', 'null', '0'):
                target=None
            else:
                try:
                    target=int(raw)
                except ValueError:
                    abort(400)
            if not user_can_access_entity(tc, uid, target):
                log_security_event('ENTITY_ACCESS_DENIED','BLOCKED',user_id=uid,organization_id=org_id,target=str(target))
                abort(403)
    finally:
        tc.close()

def init_tenant_db(org_id=None, force=False):
    """Initialise/migre le schéma d'une organisation (no-op si déjà à jour)."""
    org_id = org_id or session.get('org_id')
    if not org_id:
        raise RuntimeError('Organisation requise pour initialiser le schéma tenant')
    return migrate_tenant(org_id, force=force)


def _migrate_tenant_schema(org_id=None):
    org_id = org_id or session.get('org_id')
    if not org_id:
        raise RuntimeError('Organisation requise pour initialiser le schéma tenant')
    c=dbmod.connect_tenant(org_id, tenant_db(org_id)); c.executescript('''
    CREATE TABLE IF NOT EXISTS app_settings(id INTEGER PRIMARY KEY CHECK(id=1),onboarding_complete INTEGER DEFAULT 0,currency TEXT DEFAULT 'EUR',locale TEXT DEFAULT 'fr-FR',notifications_enabled INTEGER DEFAULT 1,slack_webhook_url TEXT,teams_webhook_url TEXT,accountant_email TEXT,weekly_export_enabled INTEGER DEFAULT 0,logo_url TEXT,accent_color TEXT,price_index_name TEXT DEFAULT 'INDICE',weinvoice_status TEXT DEFAULT 'disconnected',weinvoice_last_check_at TEXT,weinvoice_last_error TEXT,weinvoice_company_id TEXT,weinvoice_kyb_status TEXT DEFAULT 'not_started',weinvoice_onboarded_at TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS dso_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,snapshot_date TEXT UNIQUE,avg_days_overdue REAL,total_outstanding REAL,invoice_count INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS dso_entity_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date TEXT NOT NULL,
        entity_key TEXT NOT NULL,
        entity_id INTEGER,
        avg_days_overdue REAL,
        total_outstanding REAL,
        invoice_count INTEGER,
        created_at TEXT,
        UNIQUE(snapshot_date, entity_key)
    );
    CREATE TABLE IF NOT EXISTS company(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT,city TEXT,department TEXT,allowed_departments TEXT,activities TEXT,certifications TEXT,siret TEXT,address TEXT,vat_number TEXT,postal_code TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_number TEXT,customer TEXT,amount REAL,paid_amount REAL DEFAULT 0,issue_date TEXT,due_date TEXT,status TEXT,days_overdue INTEGER,score INTEGER,created_at TEXT,kind TEXT DEFAULT 'STANDARD',retention_release_date TEXT,retention_pct REAL,customer_email TEXT,customer_phone TEXT,public_token TEXT,entity_id INTEGER);
    CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,vendor TEXT,description TEXT,amount REAL,expense_date TEXT,category TEXT);
    CREATE TABLE IF NOT EXISTS opportunities(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,title TEXT,value REAL DEFAULT 0,score INTEGER,details TEXT,source TEXT,source_url TEXT,buyer TEXT,departments TEXT,deadline TEXT,reasons TEXT,warnings TEXT,raw_json TEXT,status TEXT DEFAULT 'OPEN',created_at TEXT,entity_id INTEGER);
    CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT,opportunity_id INTEGER,kind TEXT,title TEXT,draft TEXT,status TEXT DEFAULT 'PENDING',expected_value REAL DEFAULT 0,created_at TEXT,sent_at TEXT,sent_to TEXT,entity_id INTEGER);
    CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,action_id INTEGER,outcome_type TEXT,amount REAL,verified INTEGER DEFAULT 0,note TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,run_type TEXT,rows_processed INTEGER,signals_found INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS dce_documents(id INTEGER PRIMARY KEY AUTOINCREMENT,opportunity_id INTEGER,filename TEXT,filetype TEXT,text_content TEXT,analysis_json TEXT,go_score INTEGER,recommendation TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS weekly_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,period_start TEXT,period_end TEXT,payload_json TEXT,sent_at TEXT,recipient TEXT,status TEXT DEFAULT 'PENDING');
    CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT,entity_id INTEGER,kind TEXT,old_status TEXT,new_status TEXT,changed_by TEXT,note TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS customer_tags(customer_name_norm TEXT PRIMARY KEY,customer_name_display TEXT,tag TEXT,note TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS price_index_readings(id INTEGER PRIMARY KEY AUTOINCREMENT,index_name TEXT DEFAULT 'INDICE',reading_date TEXT,value REAL,created_at TEXT);
    CREATE TABLE IF NOT EXISTS fixed_price_contracts(id INTEGER PRIMARY KEY AUTOINCREMENT,project_name TEXT,customer TEXT,amount REAL,signed_date TEXT,materials_share_pct REAL DEFAULT 30,status TEXT DEFAULT 'ACTIVE',created_at TEXT);
    CREATE TABLE IF NOT EXISTS financial_settings(id INTEGER PRIMARY KEY CHECK(id=1),cash_balance REAL,cash_as_of TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS entity_financial_settings(entity_id INTEGER PRIMARY KEY,cash_balance REAL,cash_as_of TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS user_entity_access(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        entity_id INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(user_id,entity_id)
    );
    CREATE INDEX IF NOT EXISTS idx_user_entity_access_user ON user_entity_access(user_id);
    CREATE TABLE IF NOT EXISTS swan_connections(
        id INTEGER PRIMARY KEY CHECK(id=1),
        environment TEXT DEFAULT 'sandbox',
        client_id TEXT,
        connected_at TEXT,
        connected_by TEXT
    );
    CREATE TABLE IF NOT EXISTS swan_accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        swan_account_id TEXT NOT NULL UNIQUE,
        name TEXT,
        iban TEXT,
        status TEXT,
        consent_url TEXT,
        requested_at TEXT,
        activated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS swan_cards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        swan_account_row_id INTEGER NOT NULL,
        swan_card_id TEXT UNIQUE,
        holder_name TEXT,
        status TEXT,
        consent_url TEXT,
        requested_at TEXT,
        activated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS swan_webhook_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT,
        resource_id TEXT,
        project_id TEXT,
        event_date TEXT,
        processed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS weinvoice_agreements(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signatory_name TEXT NOT NULL,
        signatory_quality TEXT NOT NULL,
        signed_at TEXT NOT NULL,
        ip_address TEXT,
        proof_ref TEXT UNIQUE NOT NULL,
        user_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS weinvoice_entity_settings(
        entity_key INTEGER PRIMARY KEY,
        weinvoice_status TEXT DEFAULT 'disconnected',
        weinvoice_last_check_at TEXT,
        weinvoice_last_error TEXT,
        weinvoice_company_id TEXT,
        weinvoice_kyb_status TEXT DEFAULT 'not_started',
        weinvoice_onboarded_at TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_weinvoice_entity_company_id
        ON weinvoice_entity_settings(weinvoice_company_id) WHERE weinvoice_company_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS bank_connections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        provider_user_id TEXT,
        provider_connection_id TEXT,
        status TEXT DEFAULT 'PENDING',
        last_synced_at TEXT,
        created_at TEXT,
        updated_at TEXT,
        entity_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS bank_accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        provider_account_id TEXT NOT NULL,
        name TEXT,
        iban TEXT,
        account_type TEXT,
        currency TEXT,
        balance REAL,
        disabled INTEGER DEFAULT 0,
        last_synced_at TEXT,
        entity_id INTEGER,
        UNIQUE(provider,provider_account_id)
    );
    CREATE TABLE IF NOT EXISTS bank_transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        provider_transaction_id TEXT NOT NULL,
        provider_account_id TEXT,
        transaction_date TEXT,
        label TEXT,
        amount REAL,
        raw_status TEXT,
        last_synced_at TEXT,
        category TEXT,
        UNIQUE(provider,provider_transaction_id)
    );
    CREATE TABLE IF NOT EXISTS bank_categorization_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        pattern TEXT NOT NULL,
        category TEXT NOT NULL,
        priority INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS bank_accounting_learning_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        pattern TEXT NOT NULL,
        category TEXT,
        account_code TEXT NOT NULL,
        vat_rate REAL,
        counterparty_type TEXT,
        counterparty_id INTEGER,
        confirmations INTEGER NOT NULL DEFAULT 1,
        last_confirmed_at TEXT NOT NULL,
        UNIQUE(entity_id,pattern,account_code,vat_rate,counterparty_type,counterparty_id)
    );
    CREATE INDEX IF NOT EXISTS idx_bank_learning_entity_pattern ON bank_accounting_learning_rules(entity_id,pattern);
    CREATE TABLE IF NOT EXISTS bank_accounting_validations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        bank_transaction_id INTEGER NOT NULL,
        category TEXT,
        account_code TEXT NOT NULL,
        vat_rate REAL,
        counterparty_type TEXT,
        counterparty_id INTEGER,
        confidence_score INTEGER,
        suggestion_reason TEXT,
        validated_at TEXT NOT NULL,
        UNIQUE(entity_id,bank_transaction_id)
    );
    CREATE TABLE IF NOT EXISTS bank_purchase_reconciliations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_transaction_id INTEGER NOT NULL UNIQUE,
        purchase_invoice_id INTEGER NOT NULL UNIQUE,
        matched_amount REAL NOT NULL,
        matched_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_invoice_reconciliations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_transaction_id INTEGER NOT NULL UNIQUE,
        invoice_id INTEGER NOT NULL UNIQUE,
        matched_amount REAL NOT NULL,
        matched_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bank_invoice_allocations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        bank_transaction_id INTEGER NOT NULL,
        invoice_id INTEGER NOT NULL,
        payment_id INTEGER,
        matched_amount REAL NOT NULL,
        match_method TEXT NOT NULL DEFAULT 'manual',
        confidence_score INTEGER,
        matched_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        UNIQUE(entity_id,idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_bank_invoice_alloc_tx ON bank_invoice_allocations(entity_id,bank_transaction_id);
    CREATE INDEX IF NOT EXISTS idx_bank_invoice_alloc_inv ON bank_invoice_allocations(entity_id,invoice_id);
    CREATE TABLE IF NOT EXISTS bank_transaction_workflow(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        bank_transaction_id INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'ignored',
        updated_at TEXT NOT NULL,
        UNIQUE(entity_id,bank_transaction_id)
    );
    CREATE INDEX IF NOT EXISTS idx_bank_transaction_workflow_tx
        ON bank_transaction_workflow(entity_id,bank_transaction_id);
    CREATE TABLE IF NOT EXISTS invoice_reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        invoice_id INTEGER NOT NULL,
        recipient_email TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        reminder_number INTEGER NOT NULL,
        UNIQUE(invoice_id,reminder_number)
    );
    CREATE TABLE IF NOT EXISTS suppliers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        address TEXT,
        siret TEXT,
        vat_number TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER,
        supplier_name TEXT NOT NULL,
        invoice_number TEXT NOT NULL,
        issue_date TEXT,
        due_date TEXT,
        subtotal REAL NOT NULL DEFAULT 0,
        vat_amount REAL NOT NULL DEFAULT 0,
        total REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'unpaid',
        notes TEXT,
        created_at TEXT NOT NULL,
        paid_at TEXT,
        document_path TEXT,
        category TEXT DEFAULT 'autre',
        weinvoice_invoice_id TEXT,
        weinvoice_status TEXT,
        weinvoice_regulatory_code TEXT,
        weinvoice_last_sync_at TEXT
    );
    CREATE TABLE IF NOT EXISTS purchase_credit_notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        original_purchase_id INTEGER,
        original_invoice_number TEXT,
        credit_number TEXT NOT NULL,
        supplier_name TEXT NOT NULL,
        issue_date TEXT,
        subtotal REAL NOT NULL DEFAULT 0,
        vat_amount REAL NOT NULL DEFAULT 0,
        total REAL NOT NULL DEFAULT 0,
        category TEXT DEFAULT 'autre',
        notes TEXT,
        document_path TEXT,
        weinvoice_invoice_id TEXT,
        weinvoice_status TEXT,
        weinvoice_regulatory_code TEXT,
        weinvoice_last_sync_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_credit_weinvoice_remote
        ON purchase_credit_notes(entity_id,weinvoice_invoice_id);
    CREATE TABLE IF NOT EXISTS purchase_budgets(
        category TEXT NOT NULL,
        entity_key INTEGER NOT NULL DEFAULT 0,
        entity_id INTEGER,
        monthly_amount REAL NOT NULL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY(category,entity_key)
    );
    CREATE TABLE IF NOT EXISTS purchase_invoice_payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        purchase_invoice_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date TEXT NOT NULL,
        payment_method TEXT DEFAULT 'bank',
        reference TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(entity_id,idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_purchase_invoice_payments_invoice ON purchase_invoice_payments(entity_id,purchase_invoice_id);
    CREATE TABLE IF NOT EXISTS sepa_export_batches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        message_id TEXT NOT NULL,
        execution_date TEXT NOT NULL,
        total_amount REAL NOT NULL,
        payment_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'exported',
        file_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT,
        UNIQUE(entity_id,message_id),
        UNIQUE(entity_id,file_sha256)
    );
    CREATE TABLE IF NOT EXISTS sepa_export_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL,
        entity_id INTEGER,
        purchase_invoice_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        supplier_name TEXT,
        invoice_number TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(batch_id,purchase_invoice_id)
    );
    CREATE INDEX IF NOT EXISTS idx_sepa_export_items_invoice ON sepa_export_items(entity_id,purchase_invoice_id);
    CREATE TABLE IF NOT EXISTS bank_purchase_allocations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        bank_transaction_id INTEGER NOT NULL,
        purchase_invoice_id INTEGER NOT NULL,
        payment_id INTEGER NOT NULL,
        matched_amount REAL NOT NULL,
        idempotency_key TEXT NOT NULL,
        matched_at TEXT NOT NULL,
        UNIQUE(entity_id,idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS outgoing_invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_number TEXT,client_name TEXT,client_address TEXT,client_email TEXT,issue_date TEXT,due_date TEXT,line_items TEXT,subtotal REAL,vat_amount REAL,total REAL,notes TEXT,status TEXT DEFAULT 'draft',public_token TEXT,created_at TEXT,sent_at TEXT,paid_at TEXT,client_siren TEXT,operation_nature TEXT DEFAULT 'services',vat_on_debits INTEGER DEFAULT 0,delivery_address TEXT,entity_id INTEGER,invoice_kind TEXT DEFAULT 'standard',source_quote_id INTEGER,deposit_percent REAL,deposit_applied_subtotal REAL DEFAULT 0,deposit_applied_vat REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS outgoing_invoice_payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        invoice_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date TEXT NOT NULL,
        payment_method TEXT DEFAULT 'bank',
        reference TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(entity_id, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_outgoing_invoice_payments_invoice ON outgoing_invoice_payments(entity_id, invoice_id);
    CREATE TABLE IF NOT EXISTS einvoice_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        invoice_id INTEGER NOT NULL,
        provider TEXT NOT NULL DEFAULT 'weinvoice',
        event_type TEXT NOT NULL,
        remote_id TEXT,
        status TEXT,
        regulatory_code TEXT,
        idempotency_key TEXT,
        detail TEXT,
        occurred_at TEXT NOT NULL,
        UNIQUE(entity_id, provider, idempotency_key, event_type)
    );
    CREATE INDEX IF NOT EXISTS idx_einvoice_events_invoice ON einvoice_events(entity_id, invoice_id, occurred_at);
    CREATE TABLE IF NOT EXISTS ereporting_records(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        record_type TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_id INTEGER NOT NULL,
        period_date TEXT NOT NULL,
        amount_ht REAL NOT NULL DEFAULT 0,
        vat_amount REAL NOT NULL DEFAULT 0,
        amount_ttc REAL NOT NULL DEFAULT 0,
        payment_amount REAL NOT NULL DEFAULT 0,
        operation_nature TEXT,
        counterparty_country TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        provider_reference TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(entity_id, record_type, source_type, source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ereporting_pending ON ereporting_records(entity_id, status, period_date);
    CREATE TABLE IF NOT EXISTS ereporting_transmissions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        provider TEXT NOT NULL DEFAULT 'weinvoice',
        transmission_number TEXT NOT NULL,
        flow_type TEXT NOT NULL,
        anchor_date TEXT NOT NULL,
        type_code TEXT NOT NULL DEFAULT 'IN',
        provider_reference TEXT,
        status TEXT NOT NULL DEFAULT 'prepared',
        late_deposit INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        response_json TEXT,
        proof_json TEXT,
        rejection_motifs_json TEXT,
        flux_status TEXT,
        last_checked_at TEXT,
        last_error TEXT,
        submitted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(entity_id,provider,transmission_number)
    );
    CREATE INDEX IF NOT EXISTS idx_ereporting_transmissions_entity
        ON ereporting_transmissions(entity_id,created_at);
    CREATE TABLE IF NOT EXISTS recurring_invoice_templates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        client_name TEXT NOT NULL,
        client_address TEXT,
        client_email TEXT,
        client_siren TEXT,
        line_items TEXT NOT NULL,
        notes TEXT,
        operation_nature TEXT DEFAULT 'services',
        vat_on_debits INTEGER DEFAULT 0,
        delivery_address TEXT,
        frequency TEXT NOT NULL,
        interval_count INTEGER NOT NULL DEFAULT 1,
        due_days INTEGER NOT NULL DEFAULT 30,
        next_run_date TEXT NOT NULL,
        end_date TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS recurring_invoice_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id INTEGER NOT NULL,
        entity_key INTEGER NOT NULL DEFAULT 0,
        run_date TEXT NOT NULL,
        invoice_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(template_id,entity_key,run_date)
    );
    CREATE TABLE IF NOT EXISTS outgoing_quotes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quote_number TEXT UNIQUE,
        client_name TEXT NOT NULL,
        client_address TEXT,
        client_email TEXT,
        issue_date TEXT,
        valid_until TEXT,
        line_items TEXT,
        subtotal REAL,
        vat_amount REAL,
        total REAL,
        notes TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT,
        sent_at TEXT,
        accepted_at TEXT,
        refused_at TEXT,
        converted_invoice_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS invoicing_clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT, address TEXT, siret TEXT, vat_number TEXT,
        phone TEXT, notes TEXT, created_at TEXT, updated_at TEXT, siren TEXT
    );
    CREATE TABLE IF NOT EXISTS outgoing_credit_notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credit_number TEXT UNIQUE,
        original_invoice_id INTEGER NOT NULL,
        original_invoice_number TEXT NOT NULL,
        client_name TEXT,
        issue_date TEXT,
        line_items TEXT,
        subtotal REAL,
        vat_amount REAL,
        total REAL,
        reason TEXT,
        status TEXT DEFAULT 'issued',
        created_at TEXT,
        entity_id INTEGER,
        weinvoice_invoice_id TEXT,
        weinvoice_status TEXT,
        weinvoice_regulatory_code TEXT,
        weinvoice_last_sync_at TEXT,
        weinvoice_last_error TEXT
    );
    CREATE TABLE IF NOT EXISTS accounting_chart_of_accounts(
        code TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        account_class INTEGER NOT NULL,
        is_collective INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        is_default INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS accounting_journals(
        code TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        journal_type TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS accounting_piece_sequences(
        entity_key INTEGER NOT NULL DEFAULT 0,
        journal_code TEXT NOT NULL,
        fiscal_year TEXT NOT NULL,
        last_sequence INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(entity_key,journal_code,fiscal_year)
    );
    CREATE TABLE IF NOT EXISTS accounting_source_claims(
        entity_key INTEGER NOT NULL DEFAULT 0,
        source_type TEXT NOT NULL,
        source_id INTEGER NOT NULL,
        entry_id INTEGER,
        created_at TEXT NOT NULL,
        PRIMARY KEY(entity_key,source_type,source_id)
    );
    CREATE TABLE IF NOT EXISTS accounting_entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        journal_code TEXT NOT NULL,
        piece_number TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        label TEXT NOT NULL,
        source_type TEXT,
        source_id INTEGER,
        is_locked INTEGER DEFAULT 0,
        created_by TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payroll_imports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        provider TEXT NOT NULL,
        period_label TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        total_debit REAL NOT NULL DEFAULT 0,
        total_credit REAL NOT NULL DEFAULT 0,
        lines_json TEXT NOT NULL,
        error_message TEXT,
        accounting_entry_id INTEGER,
        created_by TEXT,
        created_at TEXT NOT NULL,
        validated_by TEXT,
        validated_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_payroll_import_entity_hash
        ON payroll_imports(entity_id,file_sha256);
    CREATE TABLE IF NOT EXISTS accounting_entry_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id INTEGER NOT NULL,
        account_code TEXT NOT NULL,
        auxiliary_name TEXT,
        label TEXT,
        debit REAL NOT NULL DEFAULT 0,
        credit REAL NOT NULL DEFAULT 0,
        lettrage_code TEXT,
        line_order INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS accounting_category_mapping(
        category TEXT PRIMARY KEY,
        account_code TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS accounting_closure(
        id INTEGER PRIMARY KEY CHECK(id=1),
        closed_until TEXT,
        closed_at TEXT,
        closed_by TEXT
    );
    CREATE TABLE IF NOT EXISTS accounting_entity_closure(
        entity_key INTEGER PRIMARY KEY,
        entity_id INTEGER,
        closed_until TEXT,
        closed_at TEXT,
        closed_by TEXT
    );
    CREATE TABLE IF NOT EXISTS accounting_fiscal_years(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_key INTEGER NOT NULL DEFAULT 0,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(entity_key,start_date)
    );
    CREATE TABLE IF NOT EXISTS accounting_entry_attachments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        uploaded_by TEXT,
        uploaded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_entry_attachments_entry ON accounting_entry_attachments(entry_id);
    CREATE TABLE IF NOT EXISTS accounting_import_batches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_key INTEGER NOT NULL DEFAULT 0,
        file_sha256 TEXT NOT NULL,
        filename TEXT,
        mode TEXT,
        entries_count INTEGER,
        lines_count INTEGER,
        imported_by TEXT,
        imported_at TEXT NOT NULL,
        UNIQUE(entity_key,file_sha256)
    );
    CREATE TABLE IF NOT EXISTS vat_declarations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'prepared',
        collected_amount REAL NOT NULL DEFAULT 0,
        deductible_amount REAL NOT NULL DEFAULT 0,
        balance_amount REAL NOT NULL DEFAULT 0,
        entry_count INTEGER NOT NULL DEFAULT 0,
        snapshot_max_entry_id INTEGER NOT NULL DEFAULT 0,
        prepared_at TEXT,
        prepared_by TEXT,
        locked_at TEXT,
        locked_by TEXT,
        filed_at TEXT,
        filed_by TEXT,
        filing_reference TEXT,
        notes TEXT,
        updated_at TEXT,
        UNIQUE(entity_id,period_start,period_end)
    );
    CREATE INDEX IF NOT EXISTS idx_vat_declarations_entity_period ON vat_declarations(entity_id,period_start,period_end);
    CREATE TABLE IF NOT EXISTS tax_declaration_preparations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        declaration_type TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        collected_amount REAL NOT NULL DEFAULT 0,
        deductible_amount REAL NOT NULL DEFAULT 0,
        balance_amount REAL NOT NULL DEFAULT 0,
        entry_count INTEGER NOT NULL DEFAULT 0,
        snapshot_max_entry_id INTEGER NOT NULL DEFAULT 0,
        checks_json TEXT,
        prepared_at TEXT,
        prepared_by TEXT,
        validated_at TEXT,
        validated_by TEXT,
        notes TEXT,
        updated_at TEXT,
        UNIQUE(entity_id,declaration_type,period_start,period_end)
    );
    CREATE INDEX IF NOT EXISTS idx_tax_decl_prep_entity_period ON tax_declaration_preparations(entity_id,declaration_type,period_start,period_end);
    CREATE TABLE IF NOT EXISTS fiscal_workpapers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, workpaper_type TEXT NOT NULL,
        period_start TEXT NOT NULL, period_end TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
        trial_balance_json TEXT NOT NULL, checks_json TEXT NOT NULL, snapshot_max_entry_id INTEGER NOT NULL DEFAULT 0,
        debit_total REAL NOT NULL DEFAULT 0, credit_total REAL NOT NULL DEFAULT 0, prepared_at TEXT, prepared_by TEXT,
        validated_at TEXT, validated_by TEXT, notes TEXT, updated_at TEXT,
        UNIQUE(entity_id,workpaper_type,period_start,period_end)
    );
    CREATE INDEX IF NOT EXISTS idx_fiscal_workpapers_entity_period ON fiscal_workpapers(entity_id,workpaper_type,period_start,period_end);
    CREATE INDEX IF NOT EXISTS idx_accounting_entries_journal_date ON accounting_entries(journal_code, entry_date);
    CREATE INDEX IF NOT EXISTS idx_accounting_entry_lines_entry ON accounting_entry_lines(entry_id);
    CREATE INDEX IF NOT EXISTS idx_accounting_entry_lines_account ON accounting_entry_lines(account_code);
    CREATE TABLE IF NOT EXISTS webhook_subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        secret TEXT NOT NULL,
        events TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT,
        created_by TEXT,
        entity_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS webhook_deliveries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT,
        status_code INTEGER,
        success INTEGER,
        attempted_at TEXT,
        response_snippet TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_sub ON webhook_deliveries(subscription_id);
    CREATE TABLE IF NOT EXISTS expense_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_email TEXT NOT NULL,
        period_label TEXT,
        status TEXT DEFAULT 'draft',
        submitted_at TEXT,
        approved_by TEXT,
        approved_at TEXT,
        rejection_reason TEXT,
        reimbursed_at TEXT,
        entity_id INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS expense_report_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER NOT NULL,
        expense_date TEXT,
        category TEXT NOT NULL,
        description TEXT,
        amount REAL NOT NULL DEFAULT 0,
        vat_amount REAL DEFAULT 0,
        receipt_path TEXT,
        vehicle_fiscal_power TEXT,
        km_driven REAL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS mileage_rate_table(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fiscal_power TEXT NOT NULL,
        bracket TEXT NOT NULL,
        rate_per_km REAL DEFAULT 0,
        fixed_amount REAL DEFAULT 0,
        UNIQUE(fiscal_power,bracket)
    );
    CREATE INDEX IF NOT EXISTS idx_expense_report_lines_report ON expense_report_lines(report_id);
    CREATE TABLE IF NOT EXISTS fixed_assets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        label TEXT NOT NULL,
        asset_account TEXT NOT NULL,
        depreciation_account TEXT NOT NULL,
        expense_account TEXT NOT NULL,
        purchase_date TEXT NOT NULL,
        purchase_amount REAL NOT NULL,
        useful_life_years REAL NOT NULL,
        method TEXT DEFAULT 'linear',
        source_type TEXT,
        source_id INTEGER,
        status TEXT DEFAULT 'active',
        disposed_at TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fixed_asset_depreciation_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL,
        period_label TEXT NOT NULL,
        amount REAL NOT NULL,
        entry_id INTEGER,
        created_at TEXT,
        UNIQUE(asset_id,period_label)
    );
    CREATE INDEX IF NOT EXISTS idx_depreciation_runs_asset ON fixed_asset_depreciation_runs(asset_id);
    CREATE TABLE IF NOT EXISTS cloud_storage_connections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        token_expires_at TEXT,
        account_email TEXT,
        folder_path TEXT,
        connected_at TEXT,
        connected_by TEXT,
        last_sync_at TEXT,
        last_sync_error TEXT,
        status TEXT DEFAULT 'active',
        UNIQUE(provider)
    );
    CREATE TABLE IF NOT EXISTS cloud_storage_imported_files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER NOT NULL,
        provider_file_id TEXT NOT NULL,
        purchase_invoice_id INTEGER,
        imported_at TEXT,
        UNIQUE(connection_id,provider_file_id)
    );
    CREATE TABLE IF NOT EXISTS purchase_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER,
        supplier_name TEXT NOT NULL,
        order_number TEXT UNIQUE NOT NULL,
        order_date TEXT NOT NULL,
        expected_delivery_date TEXT,
        status TEXT DEFAULT 'draft',
        notes TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT,
        entity_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS purchase_order_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        quantity REAL NOT NULL DEFAULT 1,
        unit_price REAL NOT NULL DEFAULT 0,
        quantity_received REAL NOT NULL DEFAULT 0,
        line_order INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_order ON purchase_order_lines(order_id);
    CREATE TABLE IF NOT EXISTS entities(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        siret TEXT,
        vat_number TEXT,
        address TEXT,
        postal_code TEXT,
        iban TEXT,
        bic TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS analytical_axes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        entity_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS analytical_tags(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        axis_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        entity_id INTEGER,
        UNIQUE(axis_id,name)
    );
    CREATE INDEX IF NOT EXISTS idx_analytical_tags_axis ON analytical_tags(axis_id);
    CREATE TABLE IF NOT EXISTS cutoff_entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cutoff_type TEXT NOT NULL,
        label TEXT NOT NULL,
        amount REAL NOT NULL,
        counterpart_account_code TEXT NOT NULL,
        period_end_date TEXT NOT NULL,
        entry_id INTEGER NOT NULL,
        reversal_entry_id INTEGER,
        entity_id INTEGER,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE TABLE IF NOT EXISTS loans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        lender_name TEXT NOT NULL,
        principal_amount REAL NOT NULL,
        annual_rate REAL NOT NULL,
        start_date TEXT NOT NULL,
        duration_months INTEGER NOT NULL,
        monthly_payment REAL NOT NULL,
        status TEXT DEFAULT 'active',
        notes TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE TABLE IF NOT EXISTS loan_installments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loan_id INTEGER NOT NULL,
        installment_number INTEGER NOT NULL,
        due_date TEXT NOT NULL,
        capital_amount REAL NOT NULL,
        interest_amount REAL NOT NULL,
        remaining_balance REAL NOT NULL,
        paid INTEGER DEFAULT 0,
        entry_id INTEGER,
        paid_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_loan_installments_loan ON loan_installments(loan_id);
    CREATE TABLE IF NOT EXISTS finance_leases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        lessor_name TEXT NOT NULL,
        asset_description TEXT NOT NULL,
        asset_value REAL NOT NULL,
        redevance_amount REAL NOT NULL,
        periodicity TEXT DEFAULT 'monthly',
        start_date TEXT NOT NULL,
        duration_months INTEGER NOT NULL,
        purchase_option_amount REAL DEFAULT 0,
        status TEXT DEFAULT 'active',
        notes TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE TABLE IF NOT EXISTS finance_lease_payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lease_id INTEGER NOT NULL,
        payment_number INTEGER NOT NULL,
        due_date TEXT NOT NULL,
        amount REAL NOT NULL,
        paid INTEGER DEFAULT 0,
        entry_id INTEGER,
        paid_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_lease_payments_lease ON finance_lease_payments(lease_id);
    CREATE TABLE IF NOT EXISTS treasury_planned_flows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        label TEXT NOT NULL,
        label_key TEXT,
        category TEXT NOT NULL,
        direction TEXT NOT NULL,
        amount REAL NOT NULL,
        start_date TEXT NOT NULL,
        frequency TEXT NOT NULL DEFAULT 'monthly',
        end_date TEXT,
        source TEXT DEFAULT 'manual',
        active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE TABLE IF NOT EXISTS treasury_settings(
        entity_key INTEGER PRIMARY KEY,
        safety_threshold REAL DEFAULT 0,
        include_observed_burn INTEGER DEFAULT 1,
        dismissed_suggestions TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        review_type TEXT NOT NULL,
        period_label TEXT NOT NULL,
        status TEXT DEFAULT 'in_progress',
        created_at TEXT NOT NULL,
        created_by TEXT,
        completed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS review_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id INTEGER NOT NULL,
        item_order INTEGER NOT NULL,
        label TEXT NOT NULL,
        linked_route TEXT,
        checked INTEGER DEFAULT 0,
        note TEXT,
        checked_by TEXT,
        checked_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_review_items_review ON review_items(review_id);
    CREATE TABLE IF NOT EXISTS review_diagnostic_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id INTEGER NOT NULL,
        entity_id INTEGER,
        run_at TEXT NOT NULL,
        run_by TEXT,
        blocker_count INTEGER NOT NULL DEFAULT 0,
        warning_count INTEGER NOT NULL DEFAULT 0,
        entry_count INTEGER NOT NULL DEFAULT 0,
        snapshot_max_entry_id INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS review_diagnostic_issues(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        issue_code TEXT NOT NULL,
        severity TEXT NOT NULL,
        label TEXT NOT NULL,
        item_count INTEGER NOT NULL DEFAULT 0,
        amount REAL NOT NULL DEFAULT 0,
        details TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_review_diag_review ON review_diagnostic_runs(review_id,run_at);
    CREATE INDEX IF NOT EXISTS idx_review_diag_issues_run ON review_diagnostic_issues(run_id,severity);
    CREATE TABLE IF NOT EXISTS accountant_collaborations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        review_id INTEGER,
        accountant_email TEXT,
        firm_name TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        created_by TEXT,
        closed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_accountant_collab_entity ON accountant_collaborations(entity_id,status);
    CREATE TABLE IF NOT EXISTS accountant_invitations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collaboration_id INTEGER NOT NULL,
        entity_id INTEGER,
        email TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending',
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT,
        accepted_at TEXT,
        accepted_by_user_id INTEGER,
        revoked_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_accountant_invitation_entity
      ON accountant_invitations(entity_id,status,expires_at);

    CREATE TABLE IF NOT EXISTS accountant_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collaboration_id INTEGER NOT NULL,
        entity_id INTEGER,
        title TEXT NOT NULL,
        description TEXT,
        due_date TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        created_by TEXT,
        resolved_at TEXT,
        resolved_by TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_accountant_requests_collab ON accountant_requests(collaboration_id,status);
    CREATE TABLE IF NOT EXISTS accountant_request_comments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        entity_id INTEGER,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_accountant_comments_request ON accountant_request_comments(request_id,created_at);
    CREATE TABLE IF NOT EXISTS accountant_activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collaboration_id INTEGER NOT NULL,
        entity_id INTEGER,
        event_type TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_accountant_activity_collab ON accountant_activity(collaboration_id,created_at);
    CREATE TABLE IF NOT EXISTS delivery_notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        delivery_number TEXT UNIQUE NOT NULL,
        client_name TEXT NOT NULL,
        client_address TEXT,
        client_email TEXT,
        delivery_date TEXT NOT NULL,
        line_items TEXT NOT NULL,
        status TEXT DEFAULT 'draft',
        linked_invoice_id INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT
    );
    CREATE TABLE IF NOT EXISTS gocardless_mandates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        client_name TEXT NOT NULL,
        client_email TEXT,
        gc_customer_id TEXT,
        gc_mandate_id TEXT,
        status TEXT DEFAULT 'pending',
        redirect_flow_id TEXT,
        authorization_url TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gocardless_payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id INTEGER,
        mandate_row_id INTEGER NOT NULL,
        invoice_id INTEGER,
        gc_payment_id TEXT,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ecommerce_connections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL UNIQUE,
        shop_domain TEXT NOT NULL,
        credential_1 TEXT,
        credential_2 TEXT,
        connected_at TEXT,
        connected_by TEXT
    );
    CREATE TABLE IF NOT EXISTS ecommerce_imported_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        entity_id INTEGER,
        external_order_id TEXT NOT NULL,
        invoice_id INTEGER,
        imported_at TEXT NOT NULL,
        UNIQUE(platform,external_order_id)
    );

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
                       ('app_settings','price_index_name'),
                       ('company','siret'),('company','address'),('company','vat_number'),('company','postal_code'),
                       ('purchase_invoices','document_path'),('purchase_invoices','category'),
                       ('purchase_invoices','weinvoice_invoice_id'),('purchase_invoices','weinvoice_status'),
                       ('purchase_invoices','weinvoice_regulatory_code'),('purchase_invoices','weinvoice_last_sync_at'),
                       ('outgoing_invoices','client_siren'),('outgoing_invoices','operation_nature'),
                       ('outgoing_invoices','vat_on_debits'),('outgoing_invoices','delivery_address'),
                       ('outgoing_invoices','weinvoice_invoice_id'),('outgoing_invoices','weinvoice_status'),
                       ('outgoing_invoices','weinvoice_sent_at'),('outgoing_invoices','weinvoice_last_error'),
                       ('outgoing_invoices','weinvoice_idempotency_key'),
                       ('outgoing_invoices','weinvoice_regulatory_code'),('outgoing_invoices','weinvoice_last_sync_at'),
                       ('invoicing_clients','siren'),
                       ('app_settings','weinvoice_status'),('app_settings','weinvoice_last_check_at'),
                       ('app_settings','weinvoice_last_error'),
                       ('app_settings','weinvoice_company_id'),('app_settings','weinvoice_kyb_status'),
                       ('app_settings','weinvoice_onboarded_at'),
                       ('app_settings','require_purchase_validation'),
                       ('purchase_invoices','validation_status'),('purchase_invoices','validated_by'),
                       ('purchase_invoices','validated_at'),('purchase_invoices','rejection_reason'),
                       ('bank_transactions','category'),
                       ('bank_categorization_rules','entity_id'),
                       ('suppliers','iban'),('suppliers','bic'),
                       ('company','iban'),('company','bic'),
                       ('purchase_invoices','weinvoice_invoice_id'),
                       ('purchase_invoices','weinvoice_status'),
                       ('purchase_invoices','weinvoice_regulatory_code'),
                       ('purchase_invoices','weinvoice_last_sync_at'),
                       ('purchase_invoices','purchase_order_id'),
                       ('purchase_invoices','entity_id'),
                       ('outgoing_invoices','entity_id'),
                       ('outgoing_invoices','recurring_template_id'),('outgoing_invoices','recurring_run_date'),
                       ('outgoing_credit_notes','entity_id'),
                       ('invoice_reminders','entity_id'),
                       ('accounting_entries','entity_id'),
                       ('accounting_entry_lines','analytical_tag_id'),
                       ('accounting_chart_of_accounts','entity_id'),
                       ('invoices','entity_id'),
                       ('opportunities','entity_id'),
                       ('actions','entity_id'),
                       ('analytical_axes','entity_id'),
                       ('analytical_tags','entity_id'),
                       ('expenses','entity_id'),
                       ('bank_connections','entity_id'),
                       ('bank_accounts','entity_id'),
                       ('fixed_assets','entity_id'),
                       ('delivery_notes','entity_id'),
                       ('invoicing_clients','entity_id'),
                       ('suppliers','entity_id'),
                       ('purchase_orders','entity_id'),
                       ('outgoing_quotes','entity_id'),
                       ('gocardless_mandates','entity_id'),
                       ('gocardless_payments','entity_id'),
                       ('weinvoice_agreements','entity_id'),
                       ('webhook_subscriptions','entity_id'), ('expense_reports','entity_id')):
        try:
            cols=[r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()]
            if col not in cols:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col} TEXT'); c.commit()
        except Exception as e:
            _migration_warn(f"[ProfitOS] ATTENTION : migration colonne {table}.{col} ignorée ({e})")

    try:
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_weinvoice_remote ON purchase_invoices(entity_id,weinvoice_invoice_id)')
        c.commit()
    except Exception as e:
        _migration_warn(f"[ProfitOS] ATTENTION : index WeInvoice achats ignoré ({e})")

    # Lot F final — suivi/preuve des transmissions e-reporting existantes
    for col, decl in (
        ('proof_json','TEXT'),('rejection_motifs_json','TEXT'),
        ('flux_status','TEXT'),('last_checked_at','TEXT')
    ):
        try:
            cols=[r['name'] for r in c.execute('PRAGMA table_info(ereporting_transmissions)').fetchall()]
            if col not in cols: c.execute(f'ALTER TABLE ereporting_transmissions ADD COLUMN {col} {decl}')
        except Exception as e: _migration_warn(f"[ProfitOS] ATTENTION : migration e-reporting final {col} ignorée ({e})")
    try: c.commit()
    except Exception: pass

    # Lot F4B — qualification explicite du périmètre e-reporting
    for col, decl in (('ereporting_scope','TEXT'),('ereporting_category','TEXT'),
                      ('counterparty_country','TEXT'),('client_vat_number','TEXT')):
        try:
            cols=[r['name'] for r in c.execute('PRAGMA table_info(outgoing_invoices)').fetchall()]
            if col not in cols: c.execute(f'ALTER TABLE outgoing_invoices ADD COLUMN {col} {decl}')
        except Exception as e: _migration_warn(f"[ProfitOS] ATTENTION : migration e-reporting {col} ignorée ({e})")
    try: c.commit()
    except Exception: pass

    # Lot F3 — avoirs électroniques sortants WeInvoice
    for col, decl in (
        ('weinvoice_invoice_id','TEXT'),
        ('weinvoice_status','TEXT'),
        ('weinvoice_regulatory_code','TEXT'),
        ('weinvoice_last_sync_at','TEXT'),
        ('weinvoice_last_error','TEXT'),
    ):
        try:
            cols=[r['name'] for r in c.execute('PRAGMA table_info(outgoing_credit_notes)').fetchall()]
            if col not in cols:
                c.execute(f'ALTER TABLE outgoing_credit_notes ADD COLUMN {col} {decl}')
        except Exception as e:
            _migration_warn(f"[ProfitOS] ATTENTION : migration avoir électronique {col} ignorée ({e})")
    try:
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_weinvoice_remote ON outgoing_credit_notes(entity_id,weinvoice_invoice_id)')
        c.commit()
    except Exception as e:
        _migration_warn(f"[ProfitOS] ATTENTION : index WeInvoice avoirs ignoré ({e})")

    # V220 — rattache les actions historiques à l'entité de leur source quand elle est connue.
    try:
        c.execute("""UPDATE actions SET entity_id=(
            CASE WHEN kind='RECOVER' THEN (SELECT i.entity_id FROM invoices i WHERE i.id=actions.opportunity_id)
                 ELSE (SELECT o.entity_id FROM opportunities o WHERE o.id=actions.opportunity_id) END
        ) WHERE entity_id IS NULL""")
        c.execute('CREATE INDEX IF NOT EXISTS idx_actions_entity_status ON actions(entity_id,status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_analytical_axes_entity ON analytical_axes(entity_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_analytical_tags_entity ON analytical_tags(entity_id)')
        c.commit()
    except Exception as e:
        _migration_warn(f"[ProfitOS] ATTENTION : migration multi-entités V220 ignorée ({e})")

    # Pass 24 — migration des anciens budgets achats globaux vers la société
    # principale, puis clé composite (catégorie, entité). SQLite ne permet pas
    # de modifier une PK en place : reconstruction transactionnelle si nécessaire.
    try:
        bcols=[r['name'] for r in c.execute('PRAGMA table_info(purchase_budgets)').fetchall()]
        if bcols and ('entity_key' not in bcols or 'entity_id' not in bcols):
            c.execute("""CREATE TABLE IF NOT EXISTS purchase_budgets_v24(
                category TEXT NOT NULL, entity_key INTEGER NOT NULL DEFAULT 0,
                entity_id INTEGER, monthly_amount REAL NOT NULL DEFAULT 0,
                updated_at TEXT, PRIMARY KEY(category,entity_key))""")
            c.execute("""INSERT INTO purchase_budgets_v24
                         (category,entity_key,entity_id,monthly_amount,updated_at)
                         SELECT category,0,NULL,monthly_amount,updated_at FROM purchase_budgets
                         ON CONFLICT(category,entity_key) DO UPDATE SET
                           entity_id=excluded.entity_id,
                           monthly_amount=excluded.monthly_amount,
                           updated_at=excluded.updated_at""")
            c.execute("DROP TABLE purchase_budgets")
            c.execute("ALTER TABLE purchase_budgets_v24 RENAME TO purchase_budgets")
            c.commit()
    except Exception as e:
        c.rollback()
        _migration_warn(f"[ProfitOS] ATTENTION : migration purchase_budgets v24 ignorée ({e})")

    # Pass 14 — métadonnées typées des factures d'acompte/finales.
    for col, decl in (('invoice_kind', "TEXT DEFAULT 'standard'"),
                      ('source_quote_id', 'INTEGER'),
                      ('deposit_percent', 'REAL'),
                      ('deposit_applied_subtotal', 'REAL DEFAULT 0'),
                      ('deposit_applied_vat', 'REAL DEFAULT 0')):
        try:
            cols=[r['name'] for r in c.execute('PRAGMA table_info(outgoing_invoices)').fetchall()]
            if col not in cols:
                c.execute(f'ALTER TABLE outgoing_invoices ADD COLUMN {col} {decl}'); c.commit()
        except Exception as e:
            _migration_warn(f"[ProfitOS] ATTENTION : migration colonne outgoing_invoices.{col} ignorée ({e})")
    # WeInvoice multi-entités : entity_key=0 représente la société mère.
    # L'ancienne configuration globale est reprise seulement pour la société mère.
    try:
        if not c.execute('SELECT 1 FROM weinvoice_entity_settings LIMIT 1').fetchone():
            legacy=c.execute('SELECT weinvoice_status,weinvoice_last_check_at,weinvoice_last_error,weinvoice_company_id,weinvoice_kyb_status,weinvoice_onboarded_at FROM app_settings WHERE id=1').fetchone()
            if legacy and any(legacy[k] for k in ('weinvoice_company_id','weinvoice_last_check_at','weinvoice_last_error','weinvoice_onboarded_at')):
                c.execute("INSERT OR IGNORE INTO weinvoice_entity_settings(entity_key,weinvoice_status,weinvoice_last_check_at,weinvoice_last_error,weinvoice_company_id,weinvoice_kyb_status,weinvoice_onboarded_at,created_at,updated_at) VALUES(0,?,?,?,?,?,?,?,?)",
                    (legacy['weinvoice_status'] or 'disconnected',legacy['weinvoice_last_check_at'],legacy['weinvoice_last_error'],legacy['weinvoice_company_id'],legacy['weinvoice_kyb_status'] or 'not_started',legacy['weinvoice_onboarded_at'],now(),now()))
                c.commit()
    except Exception as e:
        _migration_warn(f"[ProfitOS] ATTENTION : migration WeInvoice multi-entités ignorée ({e})")

    # v1.8.9 — clôture comptable isolée par entité. L'ancienne clôture globale est reprise
    # uniquement pour la société mère (entity_key=0). Ce bloc se trouvait par erreur dans
    # sync_buyer_signals (base auth, où la table n'existe pas) : la reprise n'avait jamais lieu
    # et une période clôturée avant la v1.8.9 redevenait modifiable.
    try:
        legacy_closure = c.execute('SELECT closed_until,closed_at,closed_by FROM accounting_closure WHERE id=1').fetchone()
        if legacy_closure and legacy_closure['closed_until']:
            c.execute("""INSERT OR IGNORE INTO accounting_entity_closure(entity_key,entity_id,closed_until,closed_at,closed_by)
                         VALUES(0,NULL,?,?,?)""",
                      (legacy_closure['closed_until'], legacy_closure['closed_at'], legacy_closure['closed_by']))
            c.commit()
    except Exception as e:
        _migration_warn(f"reprise de la clôture comptable globale ignorée ({e})")

    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_outgoing_invoice_recurring_run ON outgoing_invoices(recurring_template_id, recurring_run_date) WHERE recurring_template_id IS NOT NULL")
        c.execute("CREATE INDEX IF NOT EXISTS ix_recurring_templates_due ON recurring_invoice_templates(entity_id,status,next_run_date)")
        c.commit()
    except Exception as e:
        _migration_warn(f"[ProfitOS] ATTENTION : index facturation récurrente ignoré ({e})")

    # Lot F production — l'index dépend de colonnes absentes des anciens tenants.
    # Il doit donc être créé APRES la migration douce ci-dessus.
    try:
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_weinvoice_remote ON purchase_invoices(entity_id,weinvoice_invoice_id)')
        c.commit()
    except Exception as e:
        _migration_warn(f"[ProfitOS] ATTENTION : index WeInvoice achats non créé ({e})")
    # v383 — idempotence forte des frais bancaires : une seule écriture
    # bank_fee par mouvement et par entité, y compris sous requêtes concurrentes.
    try:
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_accounting_entries_bank_fee_source
                     ON accounting_entries(entity_id, source_type, source_id)
                     WHERE source_type='bank_fee'""")
        c.commit()
    except Exception as e:
        c.rollback()
        _migration_warn(f"[ProfitOS] ATTENTION : index unique bank_fee non créé ({e})")
    # La société mère a entity_id NULL : un index (entity_id, …) laisse passer les doublons
    # (NULL ≠ NULL en SQL). Index sur COALESCE pour garantir une seule écriture par
    # mouvement bancaire, catégorisation comme frais, même sous double clic simultané.
    try:
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_accounting_entries_bank_source_all
                     ON accounting_entries(COALESCE(CAST(entity_id AS TEXT),''), source_type, source_id)
                     WHERE source_type IN ('bank_fee','bank_categorization')""")
        c.commit()
    except Exception as e:
        c.rollback()
        _migration_warn(f"index unique des écritures bancaires non créé ({e})")

    # v4xx — mentions légales des factures, numérotation à l'émission, TVA par taux,
    # TVA sur encaissements, FEC (référence de pièce, lettrage daté, auxiliaires).
    _v4_columns = (
        [('company', col, 'TEXT') for col in ('legal_form', 'share_capital', 'rcs_city', 'rm_number', 'vat_regime',
                                              'vat_on_debits', 'late_penalty_rate', 'escompte_text', 'payment_terms', 'country')]
        + [('entities', col, 'TEXT') for col in ('city', 'legal_form', 'share_capital', 'rcs_city', 'rm_number', 'vat_regime',
                                                 'vat_on_debits', 'late_penalty_rate', 'escompte_text', 'payment_terms', 'country')]
        + [('outgoing_invoices', col, 'TEXT') for col in ('vat_pending_json', 'service_date', 'vat_treatment',
                                                          'vat_exemption_reason', 'client_is_public', 'payment_terms')]
        + [('outgoing_quotes', col, 'TEXT') for col in ('client_siren', 'operation_nature', 'delivery_address',
                                                        'client_vat_number', 'counterparty_country')]
        + [('delivery_notes', col, 'TEXT') for col in ('client_siren', 'operation_nature', 'billing_address', 'due_date')]
        + [('finance_leases', 'vat_rate', 'REAL'), ('purchase_invoices', 'duplicate_ack', 'TEXT')]
        + [('invoicing_clients', 'country', 'TEXT'), ('invoicing_clients', 'is_public', 'TEXT'),
           ('accounting_entries', 'piece_ref', 'TEXT'), ('accounting_entry_lines', 'lettrage_date', 'TEXT'),
           ('accounting_entry_lines', 'vat_rate', 'REAL'), ('accounting_entry_lines', 'vat_base', 'REAL')]
        # Banque : compte 512 propre à chaque compte bancaire, date du solde de relevé importé.
        + [('bank_accounts', 'accounting_code', 'TEXT'), ('bank_accounts', 'statement_balance_date', 'TEXT')]
    )
    for table, col, decl in _v4_columns:
        try:
            cols=[r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()]
            if cols and col not in cols:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}'); c.commit()
        except Exception as e:
            c.rollback()
            _migration_warn(f"[ProfitOS] ATTENTION : migration colonne {table}.{col} ignorée ({e})")
    try:
        # fiscal_year porte la série : '<type>:<année>' (invoice:2026, credit:2026, delivery:2026).
        c.execute("""CREATE TABLE IF NOT EXISTS invoice_number_sequences(
            entity_key INTEGER NOT NULL DEFAULT 0,
            fiscal_year TEXT NOT NULL,
            last_sequence INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(entity_key,fiscal_year))""")
        c.execute("""CREATE TABLE IF NOT EXISTS accounting_auxiliary_codes(
            entity_key INTEGER NOT NULL DEFAULT 0,
            root TEXT NOT NULL,
            name_key TEXT NOT NULL,
            aux_code TEXT NOT NULL,
            label TEXT,
            created_at TEXT,
            PRIMARY KEY(entity_key,root,name_key))""")
        c.commit()
    except Exception as e:
        c.rollback()
        _migration_warn(f"[ProfitOS] ATTENTION : tables de numérotation/auxiliaires non créées ({e})")
    # Un numéro de facture définitif est unique par entité. Les brouillons n'ont plus de
    # numéro (NULL) : index partiel. Si des doublons historiques existent, l'index n'est
    # pas créé (message en log) — à corriger manuellement avant de relancer.
    try:
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_outgoing_invoices_entity_number
                     ON outgoing_invoices(COALESCE(CAST(entity_id AS TEXT),''), invoice_number)
                     WHERE invoice_number IS NOT NULL""")
        c.commit()
    except Exception as e:
        c.rollback()
        _migration_warn(f"[ProfitOS] ATTENTION : index unique des numéros de facture non créé ({e})")

    try:
        dbmod.upgrade_real_columns(c)
    except Exception as e:
        c.rollback()
        _migration_warn(f"conversion des colonnes REAL en DOUBLE PRECISION non faite ({e})")

    from profitos.accounting import seed_accounting_defaults
    seed_accounting_defaults(c)
    from profitos.expenses import seed_mileage_rate_table
    seed_mileage_rate_table(c)
    c.close()

def norm(x):
    s=unicodedata.normalize('NFKD',str(x or '')).encode('ascii','ignore').decode('ascii')
    return re.sub(r'\s+',' ',s.lower()).strip()

_FR_DATE_RE=re.compile(r'^\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(\s.*)?$')

def parse_date(v):
    if v is None or (isinstance(v,float) and pd.isna(v)): return None
    if isinstance(v,datetime): return v.date()
    if isinstance(v,date): return v
    try:
        # Format français jour/mois/année (« 05/03/2026 » = 5 mars) : sans dayfirst,
        # pandas le lisait comme le 3 mai.
        if isinstance(v,str) and _FR_DATE_RE.match(v):
            return pd.to_datetime(v.strip(),dayfirst=True).date()
        return pd.to_datetime(v).date()
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
    months=['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
    raw=str(v)
    # Date seule (ou minuit UTC, heure inconnue) : pas d'heure « 01:00 » due au fuseau.
    if len(raw.strip())<=10 or (d.astimezone(timezone.utc).hour==0 and d.astimezone(timezone.utc).minute==0):
        u=d.astimezone(timezone.utc)
        return f'{u.day} {months[u.month-1]} {u.year}'
    d=d.astimezone(PARIS)
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

def parse_amount(v):
    """Montant numérique ou None. Accepte les saisies à la française (« 1 234,56 € »,
    « 1.234,56 », « (45,00) ») que float() refusait."""
    if v is None or isinstance(v,bool): return None
    if isinstance(v,(int,float)):
        return None if (isinstance(v,float) and math.isnan(v)) else float(v)
    t=re.sub(r'[\s\u00a0\u202f€$£]|EUR','',str(v).strip(),flags=re.I)
    if not t: return None
    if t.startswith('(') and t.endswith(')'): t='-'+t[1:-1]
    if ',' in t and '.' in t:
        t=t.replace('.','').replace(',','.') if t.rfind(',')>t.rfind('.') else t.replace(',','')
    elif ',' in t:
        # « 1,234 » / « 12,500,000 » : séparateur de milliers anglo-saxon (export
        # QuickBooks, Stripe…). Sinon virgule décimale française (« 99,90 », « 0,125 »).
        if re.fullmatch(r'-?[1-9]\d{0,2}(,\d{3})+',t):
            t=t.replace(',','')
        else:
            t=t.replace(',','.') if t.count(',')==1 else t.replace(',','')
    try:
        f=float(t)
    except ValueError:
        return None
    return None if math.isnan(f) else f

def form_float(value, default=0.0):
    """float() d'une saisie de formulaire, au format français accepté (« 1 234,56 »,
    « 12,5 % », « 1 500 € »). Valeur vide : défaut. Illisible : ValueError, comme float()."""
    if value is None or (isinstance(value,str) and value.strip()==''):
        return float(default)
    if isinstance(value,(int,float)):
        return float(value)
    v=parse_amount(str(value).replace('%',''))
    if v is None:
        raise ValueError(f"Nombre illisible : {value!r}")
    return v

def money(v):
    try:return float(v or 0)
    except (TypeError,ValueError):
        pass
    a=parse_amount(v)
    return 0.0 if a is None else a

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

def sparkline_svg(values,width=260,height=56,color='#5fe0ac',stretch=False):
    """Petite courbe SVG en ligne, générée côté serveur (pas de lib JS de chart).
    values : liste de nombres (chronologique, le plus ancien en premier).
    stretch=True : la courbe occupe toute la largeur disponible (sinon elle reste
    à sa taille, centrée, et paraît minuscule dans un grand panneau)."""
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
    if stretch:
        return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" preserveAspectRatio="none" '
                f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Évolution sur la période">'
                f'<line x1="0" y1="{height-1}" x2="{width}" y2="{height-1}" stroke="#262626" stroke-width="1" vector-effect="non-scaling-stroke"/>'
                f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" '
                f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg"><path d="{path}" fill="none" stroke="{color}" '
            f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="3.5" fill="{color}"/></svg>')

FR_MONTHS_SHORT=['janv.','févr.','mars','avr.','mai','juin','juil.','août','sept.','oct.','nov.','déc.']

def bars_svg(labels_values,width=420,height=140,color='#5fe0ac',negative_color='#ff6e78'):
    """Barres verticales SVG simples (pas de lib JS), pour la prévision de trésorerie.
    labels_values : liste de tuples (label, valeur). Les valeurs négatives descendent sous une
    ligne de base (signe moins typographique « − » dans les étiquettes)."""
    if not labels_values: return None
    vals=[float(v or 0) for _,v in labels_values]
    hi=max(max(vals),0.0); lo=min(min(vals),0.0)
    span=(hi-lo) or 1.0
    n=len(labels_values); pad=10; gap=14
    top=18; bottom=height-30
    usable=bottom-top
    bar_w=(width-2*pad-gap*(n-1))/n
    zero_y=top+usable*(hi/span)
    bars=[f'<line x1="{pad}" x2="{width-pad}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" stroke="#8fa9d3" stroke-opacity=".35"/>']; labels=[]
    for i,(label,v) in enumerate(labels_values):
        v=float(v or 0)
        x=pad+i*(bar_w+gap)
        h=abs(v)/span*usable
        y=zero_y-h if v>=0 else zero_y
        fill=color if v>=0 else negative_color
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="4" fill="{fill}"/>')
        ty=(y-6) if v>=0 else min(bottom-2,y+h+13)
        bars.append(f'<text x="{x+bar_w/2:.1f}" y="{ty:.1f}" font-size="11" fill="currentColor" text-anchor="middle">{fr_signed(v)}</text>')
        # Libellés échappés : un nom de fournisseur venu d'un PDF reçu par e-mail ne doit
        # jamais injecter de balises dans la page (le SVG est affiché avec |safe).
        label=str(label)
        if len(label)==7 and label[:4].isdigit() and label[4]=='-' and label[5:].isdigit() and 1<=int(label[5:])<=12:
            label=f"{FR_MONTHS_SHORT[int(label[5:])-1]} {label[2:4]}"  # « 2026-10 » -> « oct. 26 »
        safe_label=label.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')
        labels.append(f'<text x="{x+bar_w/2:.1f}" y="{height-10:.1f}" font-size="11" fill="#8fa9d3" text-anchor="middle">{safe_label}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img">'+''.join(bars)+''.join(labels)+'</svg>')

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

def fetch_boamp_records():
    """Derniers avis BOAMP (une requête, partagée par toutes les organisations
    lors du job planifié boamp_refresh)."""
    r=requests.get(BOAMP,params={'limit':100,'order_by':'dateparution DESC'},timeout=20); r.raise_for_status()
    return r.json().get('results',[])


def _grow_key(source_url, title, buyer):
    return ('url:'+source_url.strip()) if source_url else ('tb:'+norm(title)+'|'+norm(buyer))


def sync_grow(rows=None):
    """Met à jour les opportunités GROW depuis BOAMP.

    v400 : upsert par avis (URL BOAMP) au lieu de tout supprimer/recréer —
    created_at et status sont conservés, donc un appel d'offres n'est
    « nouveau » qu'une fois et les actions liées gardent leur opportunité.
    Les avis disparus du flux sont retirés seulement s'ils sont encore OPEN et
    échus (ou sans date limite et vus il y a plus de 30 jours)."""
    p=profile()
    if not p:return 0
    if rows is None:
        rows=fetch_boamp_records()
    c=cx(); n=0
    existing={}
    for row in c.execute("SELECT id,title,buyer,source_url,status,deadline,created_at FROM opportunities WHERE type='GROW'").fetchall():
        existing.setdefault(_grow_key(row['source_url'],row['title'],row['buyer']),row)
    seen=set()
    for rec in rows:
        score,deps,reasons,warns=score_market(rec,p)
        if score<55:continue
        title=first(rec,'objet','title','titre') or 'Marché public BOAMP'; buyer=first(rec,'nomacheteur','acheteur') or 'Acheteur public'
        url=first(rec,'url_avis','url')
        key=_grow_key(url,title,buyer)
        if key in seen: continue
        seen.add(key)
        values=(title,0,score,f'Acheteur : {buyer}','BOAMP',url,buyer,json.dumps(deps,ensure_ascii=False),first(rec,'datelimitereponse','date_limite_reponse'),json.dumps(reasons,ensure_ascii=False),json.dumps(warns,ensure_ascii=False),json.dumps(rec,ensure_ascii=False,default=str))
        if key in existing:
            c.execute("""UPDATE opportunities SET title=?,value=?,score=?,details=?,source=?,source_url=?,buyer=?,departments=?,deadline=?,reasons=?,warnings=?,raw_json=?
                         WHERE id=?""",values+(existing[key]['id'],))
        else:
            c.execute("""INSERT INTO opportunities(type,title,value,score,details,source,source_url,buyer,departments,deadline,reasons,warnings,raw_json,status,created_at)
                         VALUES('GROW',?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)""",values+(now(),))
        n+=1
    today_iso=paris_today().isoformat()
    stale_before=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    for key,row in existing.items():
        if key in seen or (row['status'] or 'OPEN')!='OPEN':
            continue
        deadline=str(row['deadline'] or '')[:10]
        if (deadline and deadline<today_iso) or (not deadline and str(row['created_at'] or '')<stale_before):
            c.execute('DELETE FROM opportunities WHERE id=?',(row['id'],))
    c.execute('INSERT INTO audit_runs(run_type,rows_processed,signals_found,created_at) VALUES(?,?,?,?)',('GROW',len(rows),n,now())); c.commit(); c.close(); return n



def tenant_cx_direct(org_id):
    """Connexion tenant sans dépendre de la session (utilisée par le digest hebdomadaire, hors requête HTTP)."""
    return dbmod.connect_tenant(org_id, tenant_db(org_id))

def _digest_seen_table(c):
    c.execute('CREATE TABLE IF NOT EXISTS weekly_digest_seen(item_key TEXT PRIMARY KEY,first_reported_at TEXT)')


def _digest_key(kind, row):
    if kind=='RECOVER':
        return f"inv:{norm(row['customer'])}:{norm(row['invoice_number'])}"
    if kind=='GROW':
        return 'grow:'+_grow_key(row['source_url'],row['title'],row['buyer'])
    return f"save:{norm(row['title'])}"


def compute_weekly_digest(org_id, days=7):
    """Nouveautés RECOVER/SAVE/GROW pour le rapport hebdomadaire.

    v400 : « nouveau » signifie « jamais présenté dans un rapport envoyé »
    (table tenant weekly_digest_seen, alimentée après un envoi réussi) et non
    plus « created_at < 7 jours » : un import de factures ou une actualisation
    BOAMP recrée les lignes, ce qui faisait réapparaître chaque semaine les
    mêmes factures en retard et appels d'offres. Avant le tout premier rapport,
    on retombe sur la fenêtre de `days` jours pour ne pas envoyer tout l'historique.
    Retourne un dict prêt à être injecté dans le template email + la preview HTML."""
    since=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    c=tenant_cx_direct(org_id)
    try:
        # Créances échues : factures émises ET importées (vue unique, profitos/receivables.py).
        from profitos.receivables import open_receivables
        recover=sorted((r for r in open_receivables(c) if r['overdue']),key=lambda r:r['score'],reverse=True)
        save=c.execute("SELECT * FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC").fetchall()
        grow=c.execute("SELECT * FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC").fetchall()
        try:
            _digest_seen_table(c); c.commit()
            seen={r['item_key'] for r in c.execute('SELECT item_key FROM weekly_digest_seen').fetchall()}
        except Exception:
            seen=set()
    finally:
        c.close()
    keys=[]
    def fresh(kind, rows):
        out=[]
        for r in rows:
            k=_digest_key(kind,r); keys.append(k)
            if (k not in seen) if seen else (str(r['created_at'] or '')>=since):
                out.append(r)
        return out
    new_recover=fresh('RECOVER',recover); new_save=fresh('SAVE',save); new_grow=fresh('GROW',grow)
    recover_total=sum(r['outstanding'] for r in new_recover)
    save_total=sum(r['value'] or 0 for r in new_save)
    return {
        'period_days':days,
        'recover_total':recover_total,'recover_count':len(new_recover),'recover_top':list(new_recover[:3]),
        'save_total':save_total,'save_count':len(new_save),'save_top':list(new_save[:3]),
        'grow_count':len(new_grow),'grow_top':list(new_grow[:3]),
        'has_signal':bool(new_recover or new_save or new_grow),
        'seen_keys':keys,
    }

OUTBOUND_EMAIL_DAILY_QUOTA={'TRIAL':int(os.environ.get('EMAIL_QUOTA_TRIAL','30')),
                            'PAID':int(os.environ.get('EMAIL_QUOTA_PAID','500'))}
RECIPIENT_HOURLY_LIMIT=int(os.environ.get('EMAIL_RECIPIENT_HOURLY_LIMIT','3'))


def _email_hash(email):
    return hashlib.sha256(('profitos-rcpt:'+(email or '').strip().lower()).encode('utf-8')).hexdigest()


def check_outbound_email_quota(kind, recipient=None, record=False):
    """Contrôle avant tout e-mail EXTERNE envoyé au nom d'une organisation (factures,
    devis, relances, invitations, actions, export comptable...).

    Renvoie None si l'envoi est autorisé, sinon un message français expliquant le refus :
    - l'adresse e-mail de l'utilisateur qui agit doit être vérifiée ;
    - quota quotidien par organisation (essai : 30/jour, payant : 500/jour, ajustables
      par EMAIL_QUOTA_TRIAL / EMAIL_QUOTA_PAID).
    Avec record=True, l'envoi est comptabilisé lorsqu'il est autorisé."""
    uid=session.get('user_id'); oid=session.get('org_id')
    if not uid or not oid:
        return None
    c=auth_cx()
    try:
        u=c.execute('SELECT email_verified FROM users WHERE id=?',(uid,)).fetchone()
        if not u or not u['email_verified']:
            return ("Confirmez d'abord votre adresse e-mail pour envoyer des e-mails depuis ProfitOS "
                    "(lien « Renvoyer l'e-mail de vérification » dans Paramètres).")
        org=c.execute('SELECT plan,status FROM organizations WHERE id=?',(oid,)).fetchone()
        paid=bool(org and (org['status'] in ('ACTIVE_PAID','PAST_DUE')))
        limit=OUTBOUND_EMAIL_DAILY_QUOTA['PAID' if paid else 'TRIAL']
        since=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        sent=c.execute('SELECT COUNT(*) n FROM outbound_email_log WHERE organization_id=? AND created_at>=?',
                       (oid,since)).fetchone()['n']
        if sent>=limit:
            log_security_event('EMAIL_QUOTA','BLOCKED',target=str(kind)[:40])
            return (f"Limite d'envoi atteinte : {limit} e-mails par 24 heures pour votre formule. "
                    "Réessayez plus tard ou contactez le support.")
        if record:
            c.execute('INSERT INTO outbound_email_log(organization_id,user_id,kind,recipient_hash,created_at) VALUES(?,?,?,?,?)',
                      (oid,uid,str(kind)[:40],_email_hash(recipient) if recipient else None,now()))
            c.commit()
        return None
    finally:
        c.close()


def check_recipient_rate(kind, recipient, limit=None, window_seconds=3600):
    """Limite le nombre d'e-mails d'un même type vers une même adresse (mot de passe
    oublié, accusé de demande de démo...) : 3 par heure par défaut. Renvoie True si
    l'envoi est autorisé (et le comptabilise)."""
    limit=RECIPIENT_HOURLY_LIMIT if limit is None else limit
    if not recipient:
        return False
    digest=_email_hash(recipient)
    since=(datetime.now(timezone.utc)-timedelta(seconds=window_seconds)).isoformat()
    c=auth_cx()
    try:
        n=c.execute('SELECT COUNT(*) n FROM outbound_email_log WHERE recipient_hash=? AND kind=? AND created_at>=?',
                    (digest,str(kind)[:40],since)).fetchone()['n']
        if n>=limit:
            return False
        c.execute('INSERT INTO outbound_email_log(organization_id,user_id,kind,recipient_hash,created_at) VALUES(?,?,?,?,?)',
                  (None,None,str(kind)[:40],digest,now()))
        c.commit()
        return True
    finally:
        c.close()



def mark_weekly_digest_reported(org_id, digest):
    """Après un envoi réussi : les éléments présentés ne seront plus « nouveaux »."""
    keys=(digest or {}).get('seen_keys') or []
    if not keys:
        return 0
    c=tenant_cx_direct(org_id)
    try:
        _digest_seen_table(c)
        stamp=now()
        for k in keys:
            c.execute('INSERT OR IGNORE INTO weekly_digest_seen(item_key,first_reported_at) VALUES(?,?)',(k,stamp))
        c.commit()
    finally:
        c.close()
    return len(keys)


EMAIL_SEND_FAILED_MESSAGE=("Le service d'email n'a pas pu envoyer le message. Réessayez dans quelques minutes ; "
                           "si le problème persiste, contactez le support.")


def mask_email(value):
    """a***@domaine.fr — pour les logs (jamais d'adresse complète)."""
    value=str(value or '')
    if '@' not in value:
        return '***'
    local,domain=value.rsplit('@',1)
    return f"{local[:1]}***@{domain}"


def _sender_address(from_value):
    """Extrait l'adresse d'un en-tête « Nom <adresse> »."""
    m=re.search(r'<([^>]+)>',from_value or '')
    return (m.group(1) if m else (from_value or '')).strip()


def _org_sender_context(org):
    """Nom affiché, reply-to et pied de page pour un email envoyé au nom d'une
    organisation (factures, devis, relances). Best-effort : ne bloque jamais l'envoi."""
    from markupsafe import escape
    org_id=None; org_name=''
    try:
        if isinstance(org,int):
            org_id=org
        else:
            org_id=org['id']; org_name=org['name'] or ''
    except Exception:
        pass
    reply_to=None; company=None
    try:
        ac=auth_cx()
        try:
            if org_id and not org_name:
                row=ac.execute('SELECT name FROM organizations WHERE id=?',(org_id,)).fetchone()
                org_name=(row['name'] if row else '') or ''
            if org_id:
                owner=ac.execute(
                    "SELECT users.email FROM memberships JOIN users ON users.id=memberships.user_id "
                    "WHERE memberships.organization_id=? AND memberships.role='OWNER' ORDER BY memberships.id LIMIT 1",
                    (org_id,)).fetchone()
                reply_to=owner['email'] if owner else None
        finally:
            ac.close()
    except Exception:
        pass
    if org_id:
        try:
            tc=tenant_cx_direct(org_id)
            try:
                company=tc.execute('SELECT * FROM company WHERE id=1').fetchone()
            finally:
                tc.close()
        except Exception:
            company=None
    clean_name=re.sub(r'[\r\n"<>,;:@\\]',' ',org_name or '').strip()[:60] or 'Votre fournisseur'
    legal=(company['name'] if company and company['name'] else org_name) or ''
    parts=[legal]
    if company:
        address=' '.join(x for x in (company['address'],company['postal_code'],company['city']) if x)
        if address: parts.append(address)
        if company['siret']: parts.append(f"SIRET {company['siret']}")
    footer=(
        '<div style="max-width:640px;margin:8px auto 24px;padding:0 16px;font-family:-apple-system,Segoe UI,Roboto,'
        'Helvetica,Arial,sans-serif;font-size:12px;color:#6b7280;">'
        + ' — '.join(str(escape(x)) for x in parts if x)
        + '<br>Message envoyé via ProfitOS. Pour toute question, répondez directement à cet email.</div>'
    )
    return clean_name,reply_to,footer


def _append_footer(html, footer):
    if not footer:
        return html
    idx=html.lower().rfind('</body>')
    return html[:idx]+footer+html[idx:] if idx!=-1 else html+footer


def send_email(to_email, subject, html, dry_run=None, reply_to=None, kind=None, on_behalf_of_org=None, headers=None):
    """Envoie un email transactionnel.

    ``kind='system'`` : e-mail de compte (vérification, réinitialisation, sécurité),
    jamais soumis au quota. Tout autre envoi déclenché depuis une session
    d'organisation est un e-mail EXTERNE : adresse de l'expéditeur vérifiée et quota
    quotidien de l'organisation exigés (voir check_outbound_email_quota).

    Ordre de préférence en production :
    1. Resend API si RESEND_API_KEY est configurée ;
    2. SMTP historique si SMTP_HOST est configuré ;
    3. dry-run local sinon (journalisé en avertissement OPS EMAIL_SIMULATED).

    on_behalf_of_org : organisation (dict/Row ou id) au nom de laquelle on écrit
    à SON client final -> expéditeur « <Organisation> via ProfitOS », reply-to
    vers l'organisation (et non noreply), pied de page avec ses mentions.
    En cas d'échec, 'error' contient un message générique en français ; le
    détail technique reste dans les logs (adresse masquée).

    La clé Resend reste uniquement dans les variables d'environnement Render.
    """
    resend_key=os.environ.get('RESEND_API_KEY','').strip()
    smtp_host=os.environ.get('SMTP_HOST','').strip()
    from_email=os.environ.get('RESEND_FROM_EMAIL','ProfitOS <noreply@profitos.fr>').strip()
    smtp_from=os.environ.get('SMTP_FROM',from_email)
    headers=dict(headers or {})

    if on_behalf_of_org is not None:
        display,org_reply_to,footer=_org_sender_context(on_behalf_of_org)
        from_email=f"{display} via ProfitOS <{_sender_address(from_email)}>"
        smtp_from=f"{display} via ProfitOS <{_sender_address(smtp_from)}>"
        reply_to=reply_to or org_reply_to
        html=_append_footer(html,footer)

    simulated_by_default = dry_run is None and not (resend_key or smtp_host)
    if dry_run is None:
        dry_run = not (resend_key or smtp_host)
    if dry_run:
        if simulated_by_default:
            log_ops_event('EMAIL_SIMULATED','WARNING',detail=f'to={mask_email(to_email)} subject={str(subject)[:80]}')
        return {'sent':False,'dry_run':True,'provider':'dry-run','to':to_email,'subject':subject,'html':html}

    if kind!='system':
        try:
            blocked=check_outbound_email_quota(kind or 'external', to_email, record=True)
        except RuntimeError:
            blocked=None  # hors requête (tâches planifiées) : pas de session à contrôler
        if blocked:
            return {'sent':False,'blocked':True,'provider':'quota','error':blocked,'to':to_email,'subject':subject}

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
            if headers:
                params['headers'] = headers
            result=resend.Emails.send(params)
            email_id = result.get('id') if isinstance(result,dict) else getattr(result,'id',None)
            return {'sent':True,'provider':'resend','id':email_id,'to':to_email,'subject':subject}
        except Exception as exc:
            _email_log_failure('Resend email failed for %s (%s)', to_email, exc)
            return {'sent':False,'provider':'resend','error':EMAIL_SEND_FAILED_MESSAGE,'to':to_email,'subject':subject}

    # Compatibilité SMTP conservée pour le développement ou un fournisseur secondaire.
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.utils import formataddr, parseaddr
        msg=MIMEMultipart('alternative')
        msg['Subject']=subject
        name,addr=parseaddr(smtp_from)
        msg['From']=formataddr((name,addr)) if name else addr
        msg['To']=to_email
        if reply_to:
            msg['Reply-To']=reply_to
        for key,value in headers.items():
            msg[key]=value
        msg.attach(MIMEText(html,'html','utf-8'))
        with smtplib.SMTP(smtp_host,int(os.environ.get('SMTP_PORT',587)),timeout=30) as server:
            server.starttls()
            server.login(os.environ.get('SMTP_USER',''),os.environ.get('SMTP_PASSWORD',''))
            server.sendmail(addr,[to_email],msg.as_string())
        return {'sent':True,'provider':'smtp','to':to_email,'subject':subject}
    except Exception as exc:
        _email_log_failure('SMTP email failed for %s (%s)', to_email, exc)
        return {'sent':False,'provider':'smtp','error':EMAIL_SEND_FAILED_MESSAGE,'to':to_email,'subject':subject}


def _email_log_failure(message, to_email, exc):
    import logging
    try:
        logger=current_app.logger
    except Exception:
        logger=logging.getLogger('profitos.email')
    # Jamais l'adresse complète ni le message d'erreur brut (peut contenir l'adresse).
    logger.error(message, mask_email(to_email), type(exc).__name__)
    log_ops_event('EMAIL_SEND_FAILED','ERROR',detail=f'to={mask_email(to_email)} error={type(exc).__name__}')

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

def _api_scope_set(value):
    raw=(value or 'read').replace('read_write','read,write')
    scopes={x.strip() for x in raw.split(',') if x.strip()}
    if 'write' in scopes:
        scopes.add('read')
    return scopes or {'read'}

def api_key_required(fn):
    """Authentification Bearer : clé hachée/révocable, organisation et entité
    sont fixées par la clé elle-même, jamais par un paramètre fourni par le client."""
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        auth_header=request.headers.get('Authorization','')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error':'missing_api_key','message':'En-tête Authorization: Bearer <clé> requis.'}),401
        raw_key=auth_header[7:].strip()
        c=auth_cx()
        row=c.execute('SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL',
                      (hash_api_key(raw_key),)).fetchone()
        if not row or (row.get('expires_at') and str(row['expires_at'])<=now()):
            c.close()
            return jsonify({'error':'invalid_api_key','message':'Clé API invalide, expirée ou révoquée.'}),401
        # Une clé n'ouvre pas plus de droits que l'abonnement : organisation résiliée ou essai
        # expiré -> refus, comme pour l'interface web.
        org=c.execute('SELECT * FROM organizations WHERE id=?',(row['organization_id'],)).fetchone()
        if not org or not org_has_access(org):
            c.close()
            return jsonify({'error':'subscription_inactive','message':"L'abonnement de cette organisation n'est pas actif."}),403
        # Une clé créée par un membre retiré de l'équipe n'est plus valable.
        creator=(row['created_by'] or '').strip().lower()
        if creator:
            still_member=c.execute('SELECT 1 FROM memberships JOIN users ON users.id=memberships.user_id '
                                   'WHERE LOWER(users.email)=? AND memberships.organization_id=?',
                                   (creator,row['organization_id'])).fetchone()
            if not still_member:
                c.execute('UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL',(now(),row['id'])); c.commit(); c.close()
                return jsonify({'error':'invalid_api_key','message':'Clé API invalide ou révoquée.'}),401
        c.execute('UPDATE api_keys SET last_used_at=? WHERE id=?',(now(),row['id'])); c.commit(); c.close()
        g.api_org_id=row['organization_id']
        g.api_key_id=row['id']
        g.api_entity_id=row['entity_id'] if 'entity_id' in row.keys() else None
        raw_scopes=(row['scopes'] if 'scopes' in row.keys() else None) or row['scope'] or 'read'
        g.api_scopes=_api_scope_set(raw_scopes)
        g.api_key_scope='read_write' if 'write' in g.api_scopes else 'read'
        return fn(*args,**kwargs)
    # Limite de débit par clé (et non par IP) : 600 requêtes par minute et par clé.
    return limiter.limit('600 per minute', key_func=_api_rate_key, methods=None, override_defaults=True)(wrapped)

def _api_rate_key():
    header=request.headers.get('Authorization','')
    return 'api:'+hashlib.sha256(header.encode('utf-8')).hexdigest()[:32] if header else get_remote_address()

def api_scope_required(*required):
    """Exige tous les scopes indiqués. 'write' implique toujours 'read'."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args,**kwargs):
            granted=getattr(g,'api_scopes',{'read'})
            missing=[scope for scope in required if scope not in granted]
            if missing:
                return jsonify({'error':'insufficient_scope','required':list(required),
                                'message':'Portée API insuffisante.'}),403
            return fn(*args,**kwargs)
        return wrapped
    return decorator

def api_write_required(fn):
    # Compatibilité des routes existantes, désormais adossée au scope fin 'write'.
    return api_scope_required('write')(fn)

def api_entity_clause(column='entity_id'):
    eid=getattr(g,'api_entity_id',None)
    return (f"{column}= ?",(eid,)) if eid is not None else (f"{column} IS NULL",())

def api_require_idempotency():
    key=(request.headers.get('Idempotency-Key') or '').strip()
    if (not key or len(key)>128
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in key)):
        return None, (jsonify({'error':'idempotency_key_required',
                              'message':'Idempotency-Key (1 à 128 caractères imprimables) requis pour cette écriture.'}),400)
    return key,None


def sync_buyer_signals(org_id, invoices_rows):
    """Met à jour les signaux de risque acheteur partagés entre organisations, à partir
    des factures en retard de CETTE organisation. Agrégation anonymisée : les autres
    organisations ne voient jamais QUI a signalé quoi, seulement un compte et une moyenne."""
    # RGPD : la mutualisation des retards de paiement entre clients ProfitOS fait de ProfitOS
    # un responsable de traitement distinct (art. 28.10) et peut viser des personnes physiques.
    # Désactivée tant qu'une base contractuelle (opt-in + clause CGV) n'est pas en place.
    if os.environ.get('BUYER_SIGNALS_ENABLED','0').strip()!='1':
        return
    from collections import defaultdict
    by_customer=defaultdict(list)
    for r in invoices_rows:
        if r.get('days_overdue',0)>0 and norm(r.get('status','') or '')!='paid':
            by_customer[r['customer']].append(r['days_overdue'])
    if not by_customer: return
    c=auth_cx()
    try:
        _upsert_buyer_signals(c, org_id, by_customer)
        c.commit()
    finally:
        c.close()

def _upsert_buyer_signals(c, org_id, by_customer):
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

def buyer_risk_lookup(customer_name, exclude_org_id):
    """Cherche si d'autres organisations ProfitOS (que la sienne) ont aussi signalé
    ce même acheteur en retard de paiement. Retourne None si aucun autre signal."""
    key=norm(customer_name)
    if not key: return None
    c=auth_cx()
    rows=c.execute('SELECT * FROM buyer_signals WHERE buyer_name_norm=? AND organization_id!=?',(key,exclude_org_id)).fetchall()
    c.close()
    # Seuil d'anonymat : jamais d'information issue de moins de 5 autres organisations.
    if not rows or len(rows) < 5 or os.environ.get('BUYER_SIGNALS_ENABLED','0').strip()!='1': return None
    org_count=len(rows)
    total_invoices=sum(r['invoice_count'] for r in rows)
    avg_days=sum(r['avg_days_overdue']*r['invoice_count'] for r in rows)/total_invoices if total_invoices else 0
    return {'org_count':org_count,'total_invoices':total_invoices,'avg_days':round(avg_days)}

def benchmark_opted_in(org_id, c=None):
    """Participation volontaire de l'organisation au benchmark anonymisé (défaut : non)."""
    own=c is None
    c=c or auth_cx()
    try:
        row=c.execute('SELECT benchmark_opt_in FROM organizations WHERE id=?',(org_id,)).fetchone()
        return bool(row and row['benchmark_opt_in'])
    except Exception:
        return False
    finally:
        if own: c.close()

def sync_sector_dso(org_id, avg_days_overdue):
    """Alimente le benchmark anonymisé (moyenne DSO inter-organisations), UNIQUEMENT pour une
    organisation qui a choisi d'y participer dans Paramètres."""
    c=auth_cx()
    if not benchmark_opted_in(org_id, c):
        c.execute('DELETE FROM sector_dso_signals WHERE organization_id=?',(org_id,))
        c.commit(); c.close()
        return
    c.execute('''INSERT INTO sector_dso_signals(organization_id,snapshot_date,avg_days_overdue,updated_at)
                 VALUES(?,?,?,?)
                 ON CONFLICT(organization_id,snapshot_date) DO UPDATE SET
                   avg_days_overdue=excluded.avg_days_overdue,updated_at=excluded.updated_at''',
        (org_id,paris_today().isoformat(),avg_days_overdue,now()))
    c.commit(); c.close()

def sector_dso_benchmark(exclude_org_id):
    """Moyenne DSO des autres organisations (leur dernier relevé chacune), anonymisée.
    Réservée aux organisations participantes, et calculée uniquement sur les organisations
    qui ont elles-mêmes accepté de participer (opt-in dans Paramètres)."""
    c=auth_cx()
    if not benchmark_opted_in(exclude_org_id, c):
        c.close(); return None
    rows=c.execute('SELECT s.organization_id,MAX(s.snapshot_date) d FROM sector_dso_signals s JOIN organizations o ON o.id=s.organization_id '
                   'WHERE s.organization_id!=? AND COALESCE(o.benchmark_opt_in,0)=1 GROUP BY s.organization_id',(exclude_org_id,)).fetchall()
    if not rows: c.close(); return None
    values=[]
    for r in rows:
        v=c.execute('SELECT avg_days_overdue FROM sector_dso_signals WHERE organization_id=? AND snapshot_date=?',(r['organization_id'],r['d'])).fetchone()
        if v and v['avg_days_overdue'] is not None: values.append(v['avg_days_overdue'])
    c.close()
    # Seuil d'anonymat : en dessous de 5 organisations, la moyenne révélerait le
    # comportement de paiement d'un client identifiable.
    if len(values) < 5: return None
    return {'org_count':len(values),'avg_days':round(sum(values)/len(values))}

_FORMULA_PREFIXES=('=','+','-','@','\t','\r')

def spreadsheet_safe(value):
    """Neutralise l'injection de formules (CSV/XLSX) : un texte commençant par = + - @
    (ex. un nom de client « =HYPERLINK(...) » venu d'un import ou d'un e-mail fournisseur)
    est préfixé d'une apostrophe pour être affiché comme du texte. Les nombres sont intacts."""
    if isinstance(value,str) and value.startswith(_FORMULA_PREFIXES):
        if re.fullmatch(r'-?\d+(?:[.,]\d+)?',value.strip()):
            return value
        return "'"+value
    return value

def export_response(rows, filename_base, french_csv=True):
    """Exporte une liste de dicts en CSV ou XLSX selon ?format=csv|xlsx (défaut xlsx).
    Partagé par tous les blueprints (RECOVER/SAVE/GROW, audit, etc.).

    CSV « Excel français » par défaut : séparateur « ; » et virgule décimale (1234,56),
    ouvert tel quel par Excel en français. french_csv=False : séparateur « , » et point
    décimal (échange avec un outil anglo-saxon)."""
    fmt=request.args.get('format','xlsx').lower()
    rows=[{k:spreadsheet_safe(v) for k,v in dict(r).items()} for r in rows]
    df=pd.DataFrame(rows)
    if fmt=='csv':
        if french_csv:
            csv_bytes=df.to_csv(index=False,sep=';',decimal=',').encode('utf-8-sig')
        else:
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
    token=secrets.token_urlsafe(32)
    c=auth_cx()
    # Seule l'empreinte du jeton est stockée ; le lien expire après 7 jours.
    c.execute('INSERT INTO export_tokens(token,organization_id,export_type,created_at) VALUES(?,?,?,?)',
        (token_digest(token),org['id'],'recover',now())); c.commit(); c.close()
    base=app_base_url()
    link=f"{base}{url_for('export_download',token=token)}"
    html=render_template('email_transactional.html',title=f"Export ProfitOS — {org['name']}",
        intro=f"Voici le lien pour télécharger l'export des créances de {org['name']}, toujours à jour au moment du clic.",
        cta_label='Télécharger l\'export',cta_url=link,footer='Ce lien est valable 7 jours. Si vous n\'êtes pas concerné(e), ignorez ce message.')
    return send_email(email,f"Export ProfitOS — {org['name']}",html,dry_run=dry_run)

def _grant_stripe_month_credit(org, months=1):
    """Crédite le solde client Stripe de 'months' mois de la formule actuelle
    (montant HT négatif = crédit imputé sur les prochaines factures). Retourne
    True si le crédit a été enregistré chez Stripe."""
    stripe=get_stripe()
    if not stripe or not org or not _row_get(org,'stripe_customer_id'): return False
    plan=STRIPE_PLANS.get((_row_get(org,'plan') or '').upper())
    if not plan: return False
    stripe.Customer.create_balance_transaction(
        org['stripe_customer_id'],
        amount=-int(plan['price_eur'])*100*int(months),
        currency='eur',
        description=f"Parrainage ProfitOS : {int(months)} mois offert{'s' if int(months) > 1 else ''}",
        idempotency_key=f"profitos-referral-credit-{org['id']}-{_row_get(org,'referral_credit_months',0)}-{months}-{datetime.now(timezone.utc):%Y%m%d}",
    )
    return True

def apply_pending_referral_credit(acx, org_id):
    """Impute chez Stripe les mois de parrainage en attente d'une organisation
    devenue (ou redevenue) payante. Best-effort, appelé depuis le webhook."""
    try:
        org=acx.execute('SELECT * FROM organizations WHERE id=?',(org_id,)).fetchone()
        months=int(_row_get(org,'referral_credit_months',0) or 0)
        if not org or months<=0 or org['status']!='ACTIVE_PAID': return
        if _grant_stripe_month_credit(org, months):
            acx.execute('UPDATE organizations SET referral_credit_months=0,updated_at=? WHERE id=?',(now(),org_id))
    except Exception:
        current_app.logger.exception('Pending referral credit failed org_id=%s', org_id)

def reward_referrer_if_any(acx, referred_org_id):
    """Accorde 1 mois offert au parrain quand l'organisation parrainée devient payante.
    'acx' est une connexion auth déjà ouverte (appelé depuis le webhook Stripe, qui gère
    lui-même le commit global) — on ne ferme pas la connexion ici, on ne fait qu'écrire.
    Best-effort : ne doit jamais faire échouer le traitement du webhook Stripe.

    - parrain abonné (Stripe configuré) : crédit d'un mois sur le solde client
      Stripe, déduit automatiquement de sa prochaine facture ;
    - parrain en essai en cours : l'essai est prolongé de 30 jours ;
    - sinon (essai expiré, abonnement résilié, Stripe indisponible) : le mois est
      mis en attente et imputé dès le prochain paiement (jamais de réactivation
      gratuite d'un essai expiré)."""
    try:
        ref=acx.execute("SELECT * FROM referrals WHERE referred_org_id=? AND status='PENDING'",(referred_org_id,)).fetchone()
        if not ref: return
        acx.execute("UPDATE referrals SET status='REWARDED',rewarded_at=? WHERE id=?",(now(),ref['id']))
        referrer=acx.execute('SELECT * FROM organizations WHERE id=?',(ref['referrer_org_id'],)).fetchone()
        if not referrer: return
        credited=False
        if referrer['status'] in ('ACTIVE_PAID','PAST_DUE'):
            try:
                credited=_grant_stripe_month_credit(referrer, 1)
            except Exception:
                current_app.logger.exception('Stripe referral credit failed org_id=%s', referrer['id'])
                credited=False
        elif trial_is_active(referrer):
            base=parse_dt(referrer['trial_ends_at']) or datetime.now(timezone.utc)
            new_end=max(base,datetime.now(timezone.utc))+timedelta(days=30)
            acx.execute('UPDATE organizations SET trial_ends_at=?,updated_at=? WHERE id=?',(new_end.isoformat(),now(),referrer['id']))
            credited=True
        if not credited:
            acx.execute('UPDATE organizations SET referral_credit_months=COALESCE(referral_credit_months,0)+1,updated_at=? WHERE id=?',(now(),referrer['id']))
        current_app.logger.info('Referral rewarded referrer=%s referred=%s credited=%s',referrer['id'],referred_org_id,credited)
    except Exception:
        current_app.logger.exception('Referral reward failed for referred_org_id=%s', referred_org_id)

CHANGELOG_ENTRIES=[
    {'date':'2026-10-15','title':"Guide utilisateur intégré (menu Aide) et abonnement annuel : 2 mois offerts"},
    {'date':'2026-10-14','title':'Trésorerie : plan mensuel, flux prévus, TVA et emprunts intégrés ; simulateur de décision enrichi'},
    {'date':'2026-10-14','title':'Comptabilité complète : saisie, grand livre, balances, CA3 case par case, tableaux 2033'},
    {'date':'2026-08-22','title':'Segmentation clients, performance équipe, export comptable automatisé'},
    {'date':'2026-08-22','title':'Score de risque acheteur croisé, benchmark sectoriel DSO, calendrier unifié'},
    {'date':'2026-08-22','title':'Programme de parrainage, portail client public, rapprochement bancaire'},
    {'date':'2026-08-20','title':'Prévision de trésorerie, simulateur de caution, radar de partenaires'},
    {'date':'2026-08-18','title':'Rôles équipe, application installable sur mobile, export CSV/Excel'},
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


def app_base_url():
    """URL publique de l'application pour les liens envoyés par e-mail.

    En production, APP_BASE_URL est obligatoire (vérifié au démarrage) : l'en-tête Host
    de la requête n'est JAMAIS utilisé, sinon un attaquant pourrait faire pointer un lien
    de réinitialisation de mot de passe vers son propre domaine. Le repli sur l'hôte de
    la requête est réservé au développement local."""
    from profitos.config import is_production
    configured=(os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if configured:
        return configured
    if is_production():
        raise RuntimeError('APP_BASE_URL doit être défini en production.')
    try:
        return request.host_url.rstrip('/')
    except RuntimeError:
        return 'http://localhost:5050'

WEEKLY_UNSUB_SALT='weekly-report-unsubscribe'


def weekly_unsubscribe_token(user_id):
    from itsdangerous import URLSafeSerializer
    return URLSafeSerializer(current_app.config['SECRET_KEY'],salt=WEEKLY_UNSUB_SALT).dumps({'u':int(user_id)})


def weekly_unsubscribe_user_id(token):
    from itsdangerous import URLSafeSerializer, BadSignature
    try:
        data=URLSafeSerializer(current_app.config['SECRET_KEY'],salt=WEEKLY_UNSUB_SALT).loads(token)
        return int(data['u'])
    except (BadSignature,KeyError,TypeError,ValueError):
        return None


def _weekly_recipients(org_id):
    c=auth_cx()
    try:
        base=("SELECT users.id,users.email{extra} FROM memberships JOIN users ON users.id=memberships.user_id "
              "WHERE memberships.organization_id=? AND memberships.role='OWNER' AND COALESCE(users.is_active,1)=1 ORDER BY memberships.id")
        try:
            rows=c.execute(base.format(extra=',COALESCE(users.weekly_report_enabled,1) AS enabled'),(org_id,)).fetchall()
        except Exception:
            c.rollback()
            rows=[dict(r,enabled=1) for r in c.execute(base.format(extra=''),(org_id,)).fetchall()]
    finally:
        c.close()
    return rows


def send_weekly_email(org, digest, dry_run=None):
    """Envoie (ou simule) le rapport hebdomadaire aux propriétaires abonnés.

    Chaque destinataire reçoit son lien de désinscription (pied de page +
    en-têtes List-Unsubscribe / List-Unsubscribe-Post, RFC 8058). Les éléments
    présentés sont marqués comme « déjà signalés » après un envoi réussi."""
    owners=_weekly_recipients(org['id'])
    if not owners: return {'sent':False,'reason':'no_owner_email'}
    recipients=[r for r in owners if int(r['enabled'] or 0)==1]
    if not recipients: return {'sent':False,'reason':'unsubscribed','to':''}
    app_url=app_base_url()
    subject=f"Votre rapport financier de la semaine — {org['name']}"
    results=[]
    for r in recipients:
        token=weekly_unsubscribe_token(r['id'])
        unsubscribe_url=f"{app_url}/email/desinscription/{token}"
        one_click_url=f"{app_url}/api/v1/email/unsubscribe/{token}"
        html=render_template('email_weekly.html',org=org,digest=digest,app_url=app_url,unsubscribe_url=unsubscribe_url)
        headers={'List-Unsubscribe':f'<{one_click_url}>','List-Unsubscribe-Post':'List-Unsubscribe=One-Click'}
        results.append(send_email(r['email'],subject,html,dry_run=dry_run,headers=headers))
    sent=any(x.get('sent') for x in results)
    if sent:
        try: mark_weekly_digest_reported(org['id'],digest)
        except Exception: current_app.logger.exception('weekly digest marking failed org=%s',org['id'])
    return {'sent':sent,'dry_run':all(x.get('dry_run') for x in results),
            'to':', '.join(r['email'] for r in recipients),'results':results,
            'error':next((x.get('error') for x in results if x.get('error')),None)}

def gen_token():
    # Le token brut n'est jamais stocké en base à partir de V1.3.3.
    return secrets.token_urlsafe(32)

def token_digest(token):
    if not token or not isinstance(token,str):
        return ''
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def _token_user(kind, raw_token):
    """Retourne (user, stored_digest) pour un token stocké uniquement sous SHA-256.

    Les anciens tokens historiquement stockés en clair ne sont plus acceptés :
    les liens de vérification expirent en 24 h et les liens de réinitialisation
    en 1 h, donc conserver indéfiniment ce fallback affaiblirait inutilement la
    protection des secrets présents dans la base d'authentification.
    """
    if kind not in ('verification','reset'):
        raise ValueError('Unknown token kind')
    column='verification_token' if kind=='verification' else 'reset_token'
    digest=token_digest(raw_token)
    c=auth_cx()
    u=c.execute(f'SELECT * FROM users WHERE {column}=?',(digest,)).fetchone()
    c.close()
    return u,digest

def send_verification_email(user, dry_run=None):
    token=gen_token()
    digest=token_digest(token)
    c=auth_cx()
    c.execute('UPDATE users SET verification_token=?,verification_sent_at=? WHERE id=?',(digest,now(),user['id']))
    c.commit(); c.close()
    base=app_base_url()
    link=f"{base}{url_for('verify_email',token=token)}"
    html=render_template('email_transactional.html',title='Confirmez votre adresse e-mail',
        intro='Cliquez sur le bouton ci-dessous pour confirmer l’adresse e-mail de votre compte ProfitOS. Ce lien est valable 24 heures.',
        cta_label='Confirmer mon adresse',cta_url=link,footer='Si vous n’avez pas créé de compte ProfitOS, vous pouvez ignorer ce message.')
    return send_email(user['email'],'Confirmez votre compte ProfitOS',html,dry_run=dry_run,kind='system')

def send_reset_email(user, dry_run=None):
    token=gen_token()
    digest=token_digest(token)
    expires=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    c=auth_cx()
    c.execute('UPDATE users SET reset_token=?,reset_token_expires=? WHERE id=?',(digest,expires,user['id']))
    c.commit(); c.close()
    base=app_base_url()
    link=f"{base}{url_for('reset_password',token=token)}"
    html=render_template('email_transactional.html',title='Réinitialisez votre mot de passe',
        intro='Cliquez sur le bouton ci-dessous pour choisir un nouveau mot de passe. Ce lien est valable 1 heure.',
        cta_label='Choisir un nouveau mot de passe',cta_url=link,footer='Si vous n’êtes pas à l’origine de cette demande, ignorez ce message : votre mot de passe reste inchangé.')
    return send_email(user['email'],'Réinitialisation de votre mot de passe ProfitOS',html,dry_run=dry_run,kind='system')

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
    raise ValueError('Format non pris en charge. Utilisez un fichier PDF, DOCX ou TXT.')

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
        reasons.append(f"{pluralize(len(certs), 'exigence ou certification détectée', 'exigences ou certifications détectées')}")
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
            notifs.append({'icon':'🔴','text':f"Facture #{r['invoice_number']} — {r['customer']} — {fr_number(r['outstanding'])} €",'url':url_for('recover')})
        soon=(paris_today()+timedelta(days=7)).isoformat(); today_iso=paris_today().isoformat()
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
    active_entity=None
    active_entities=[]
    if session.get('org_id'):
        try:
            from profitos.entities import current_entity, accessible_entities, user_can_access_entity, current_entity_id
            tc=tenant_cx_direct(session['org_id'])
            active_entities=accessible_entities(tc, session.get('user_id'))
            if not user_can_access_entity(tc, session.get('user_id'), current_entity_id()):
                # L'accès à l'entité sélectionnée a été révoqué en cours de session
                # (ou n'a jamais été accordé) — retombe sur la société mère plutôt
                # que d'exposer silencieusement des données non autorisées.
                session.pop('current_entity_id', None)
            active_entity=current_entity(tc)
            tc.close()
        except Exception:
            active_entity=None
            active_entities=[]
    return {'auth_user':u,'auth_org':current_org(),'phase2_enabled':PHASE2_ENABLED,'user_orgs':user_organizations(),'app_version':current_app.config.get('APP_VERSION',''),'notifications':live_notifications(),'branding':org_branding(),'changelog_entries':CHANGELOG_ENTRIES,'unseen_changelog':unseen_changelog,'active_entity':active_entity,'active_entities':active_entities}




def security_session_context():
    if session.get('user_id'):
        session.permanent=True
        from profitos.account_security import session_expired
        if request.endpoint!='static' and session_expired():
            # Déconnexion après inactivité (SESSION_IDLE_MINUTES, 2 h par défaut) ou au-delà
            # de la durée absolue (SESSION_HOURS, 12 h) : la session est révoquée côté serveur.
            from profitos.account_security import revoke_current_session
            try:
                revoke_current_session()
            except Exception:
                pass
            session.clear()
            if request.method=='GET' and not request.path.startswith('/api/'):
                flash('Votre session a expiré. Veuillez vous reconnecter.')
                return redirect(url_for('login',next=request.path if request.path.startswith('/') else None))
            abort(401)

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

# Libellés affichés pour les valeurs internes (types de signaux, sources d'écritures).
# Les valeurs elles-mêmes (RECOVER, SAVE, GROW...) restent inchangées dans la logique.
DISPLAY_LABELS = {
    'RECOVER': 'Relance client', 'SAVE': 'Économie', 'GROW': 'Marché public', 'RETENTION': 'Retenue',
    'outgoing_invoice': 'Facture client', 'outgoing_credit_note': 'Avoir client',
    'customer_deposit_invoice': "Facture d'acompte", 'customer_final_invoice': 'Facture de solde',
    'outgoing_invoice_payment': 'Règlement client', 'outgoing_invoice_payment_v2': 'Règlement client',
    'purchase_invoice': 'Facture fournisseur', 'purchase_credit_note': 'Avoir fournisseur',
    'purchase_invoice_payment': 'Règlement fournisseur', 'purchase_invoice_payment_v2': 'Règlement fournisseur',
    'bank_categorization': 'Opération bancaire', 'bank_fee': 'Frais bancaires',
    'expense_report': 'Note de frais', 'expense_report_reimbursement': 'Remboursement de note de frais',
    'cutoff': 'Cut-off', 'cutoff_reversal': 'Extourne de cut-off',
    'loan_drawdown': "Déblocage d'emprunt", 'loan_installment': "Échéance d'emprunt",
    'finance_lease_payment': 'Loyer de crédit-bail', 'payroll_import': 'Import de paie',
}

def display_label(value):
    """Libellé français d'une valeur interne ; la valeur brute si elle est inconnue."""
    if value is None:
        return ''
    return DISPLAY_LABELS.get(str(value), DISPLAY_LABELS.get(str(value).upper(), value))


def fr_number(value, decimals=0):
    """Formate un nombre à la française : espace insécable pour les milliers,
    virgule pour la décimale (ex. 4800.5 -> "4 800,5"). Remplace les usages
    historiques de "{:,.Nf}".format(x) dans les templates, qui produisaient un
    séparateur de milliers en virgule — lisible à l'anglaise, pas au format
    attendu en France."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(',', '\u00a0').replace('.', ',')


def fr_date(value, with_time=False):
    """Formate une date ISO (AAAA-MM-JJ ou AAAA-MM-JJTHH:MM:SS) au format français
    JJ/MM/AAAA — ou JJ/MM/AAAA HH:MM si with_time. Retourne la valeur telle quelle
    si elle ne correspond pas au format attendu, plutôt que de planter.

    ATTENTION : ne jamais appliquer ce filtre à la valeur d'un <input type="date">
    ou type="datetime-local"> — ces champs exigent le format ISO brut pour que le
    sélecteur de date du navigateur fonctionne. Réservé à l'affichage seul."""
    if not value:
        return value
    s = str(value)
    if len(s) < 10:
        return value
    year, month, day = s[0:4], s[5:7], s[8:10]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return value
    result = f"{day}/{month}/{year}"
    if with_time and len(s) >= 16 and s[10] in ('T', ' ') and s[13] == ':':
        result += f" {s[11:16]}"
    return result


def pluralize(n, singular, plural=None, with_number=True):
    """Accord en nombre : pluralize(1, 'facture') -> « 1 facture », pluralize(3, 'signal', 'signaux')
    -> « 3 signaux ». Remplace les « (s) » / « (aux) » des libellés. 0 et 1 sont au singulier
    (règle française)."""
    try:
        count = float(n or 0)
    except (TypeError, ValueError):
        count = 0
    word = singular if abs(count) < 2 else (plural or singular + 's')
    if not with_number:
        return word
    shown = fr_number(count, 0) if float(count).is_integer() else fr_number(count, 1)
    return f"{shown}\u00a0{word}"


FR_MONTHS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
             'septembre', 'octobre', 'novembre', 'décembre']


def fr_month(value):
    """« 2026-10 » ou « 2026-10-08 » -> « octobre 2026 »."""
    s = str(value or '')
    if len(s) >= 7 and s[:4].isdigit() and s[4] == '-' and s[5:7].isdigit() and 1 <= int(s[5:7]) <= 12:
        return f"{FR_MONTHS[int(s[5:7]) - 1]} {s[:4]}"
    return value


def fr_signed(value, decimals=0, plus=False):
    """Montant signé avec le vrai signe moins (−) ; « 0 » jamais affiché « −0 »."""
    try:
        v = round(float(value), decimals)
    except (TypeError, ValueError):
        return value
    if v == 0:
        return fr_number(0, decimals)
    return ('\u2212' if v < 0 else ('+' if plus else '')) + fr_number(abs(v), decimals)


def fr_datetime_paris(value):
    """Horodatage ISO (UTC) -> « jj/mm/aaaa HH:MM » à l'heure de Paris."""
    if not value:
        return ''
    try:
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return fr_date(value, with_time=True)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(PARIS)
    return d.strftime('%d/%m/%Y %H:%M')


INVOICE_STATUS_LABELS = {
    'draft': 'Brouillon', 'sent': 'Émise', 'partially_paid': 'Partiellement payée',
    'paid': 'Payée', 'overdue': 'En retard', 'cancelled': 'Annulée', 'issued': 'Émis',
}


# Libellés français des codes techniques affichés à l'écran (statuts, fréquences,
# types). Un code inconnu est affiché tel quel.
FR_LABELS = {
    'open': 'Ouvert', 'OPEN': 'Ouvert', 'resolved': 'Résolu', 'closed': 'Clôturé', 'active': 'Actif', 'ACTIVE': 'Actif',
    'paused': 'En pause', 'completed': 'Terminé', 'pending': 'En attente', 'PENDING': 'En attente', 'APPROVED': 'Validée',
    'approved': 'Validé', 'REJECTED': 'Refusée', 'rejected': 'Rejeté', 'SENT': 'Envoyée', 'sent': 'Envoyé', 'DONE': 'Terminée',
    'draft': 'Brouillon', 'prepared': 'Préparée', 'locked': 'Verrouillée', 'filed': 'Déposée', 'validated': 'Validé',
    'submitted': 'Soumis', 'revoked': 'Révoqué', 'expired': 'Expiré', 'accepted': 'Accepté', 'refused': 'Refusé',
    'in_progress': 'En cours', 'error': 'Erreur', 'cancelled': 'Annulé', 'CANCELLED': 'Annulé', 'posted': 'Comptabilisé',
    'CONNECTED': 'Connectée', 'connected': 'Connectée', 'SYNC_ERROR': 'Erreur de synchronisation', 'PENDING_CONSENT': 'Consentement en attente',
    'DISCONNECTED': 'Déconnectée', 'reimbursed': 'Remboursée', 'paid': 'Payée', 'unpaid': 'À payer',
    'weekly': 'Hebdomadaire', 'monthly': 'Mensuelle', 'quarterly': 'Trimestrielle', 'yearly': 'Annuelle', 'annual': 'Annuelle',
    'VENTES': 'Ventes', 'ACHATS': 'Achats', 'BANQUE': 'Banque', 'CAISSE': 'Caisse', 'OD': 'Opérations diverses', 'AN': 'À-nouveaux',
    'LIASSE': 'Liasse fiscale', 'IS': 'Impôt sur les sociétés', 'CVAE': 'CVAE', 'DAS2': 'DAS2 (honoraires versés)', 'CA3': 'TVA mensuelle ou trimestrielle (CA3)', 'CA12': 'TVA annuelle (CA12)',
    'silae': 'Silae', 'payfit': 'PayFit', 'generic': 'Autre logiciel',
}


def fr_label(value):
    return FR_LABELS.get(str(value if value is not None else ''), value if value is not None else '')


def invoice_status_label(status):
    """Libellé français d'un statut de facture (jamais la valeur brute « cancelled »)."""
    return INVOICE_STATUS_LABELS.get(str(status or ''), str(status or ''))


def invoice_label(inv):
    """Numéro d'une facture émise, ou « Brouillon n°<id> » (numéro attribué à l'émission)."""
    try:
        number = inv['invoice_number']
    except (KeyError, IndexError, TypeError):
        number = None
    if number:
        return number
    try:
        return f"Brouillon n°{inv['id']}"
    except (KeyError, IndexError, TypeError):
        return 'Brouillon'


def init_runtime(app):
    """Attach shared request hooks and Jinja globals to a Flask app instance."""
    app.jinja_env.globals['can_access'] = can_access
    app.jinja_env.globals['plan_feature_enabled'] = plan_feature_enabled
    def _min_plan_for(feature):
        from .plan_limits import minimum_plan_for
        code=minimum_plan_for(feature)
        return {'code':code,'name':STRIPE_PLANS[code]['name']} if code in STRIPE_PLANS else None
    app.jinja_env.globals['min_plan_for'] = _min_plan_for
    app.jinja_env.globals['ROLE_LABELS'] = ROLE_LABELS
    app.jinja_env.globals['ROLES'] = ROLES
    app.jinja_env.globals['csrf_token'] = csrf_token
    app.jinja_env.globals['trial_days_left'] = trial_days_left
    app.jinja_env.globals['subscription_state'] = subscription_state
    app.jinja_env.globals['plan_overage'] = plan_overage
    from .plan_limits import FEATURE_LABELS
    app.jinja_env.globals['FEATURE_LABELS'] = FEATURE_LABELS
    app.jinja_env.globals['current_role'] = current_role
    app.jinja_env.globals['asset_url'] = asset_url
    from profitos import ui_labels as _ui_labels
    _ui_labels.register(app)
    from profitos.receivables import detail_url as _receivable_url
    app.jinja_env.globals['receivable_url'] = _receivable_url
    app.jinja_env.globals['pluralize'] = pluralize
    app.jinja_env.filters['pluralize'] = pluralize
    app.jinja_env.filters['fr_month'] = fr_month
    app.jinja_env.filters['fr_signed'] = fr_signed
    app.jinja_env.filters['fr_datetime_paris'] = fr_datetime_paris
    app.jinja_env.filters['fr_number'] = fr_number
    app.jinja_env.filters['fr_date'] = fr_date
    app.jinja_env.filters['label'] = display_label
    from profitos import invoice_amounts as _amounts
    app.jinja_env.filters['fr_qty'] = _amounts.fr_qty
    app.jinja_env.filters['fr_price'] = _amounts.fr_price
    app.jinja_env.filters['fr_rate'] = _amounts.fr_rate
    app.jinja_env.filters['fr_rate_plain'] = lambda v: _amounts.fr_rate(v)[:-2] if v not in (None, '') else ''
    app.jinja_env.filters['invoice_label'] = invoice_label
    app.jinja_env.filters['invoice_status_label'] = invoice_status_label
    app.jinja_env.filters['fr_label'] = fr_label
    from profitos.sepa import format_iban
    app.jinja_env.filters['format_iban'] = format_iban
    app.before_request(csrf_protect)
    app.before_request(security_session_context)
    app.before_request(ensure_tenant_schema)
    app.before_request(refresh_invoice_aging_hook)
    app.before_request(enforce_entity_access)
    app.before_request(enforce_role_areas)
    app.context_processor(commercial_context)
