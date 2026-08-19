from flask import Flask, render_template, request, redirect, url_for, flash, abort, session, g
import sqlite3, json, re, math, unicodedata, secrets, functools, os
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
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,full_name TEXT,is_active INTEGER DEFAULT 1,email_verified INTEGER DEFAULT 0,verification_token TEXT,verification_sent_at TEXT,reset_token TEXT,reset_token_expires TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS memberships(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,organization_id INTEGER NOT NULL,role TEXT DEFAULT 'OWNER',created_at TEXT,UNIQUE(user_id,organization_id));
    CREATE TABLE IF NOT EXISTS activity_log(id INTEGER PRIMARY KEY AUTOINCREMENT,organization_id INTEGER,user_id INTEGER,event_type TEXT,description TEXT,created_at TEXT);
    '''); c.commit()
    # Migration douce pour les bases auth créées avant l'ajout des colonnes de vérification/reset.
    cols=[r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()]
    for col,ddl in (
        ('email_verified',"ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0"),
        ('verification_token',"ALTER TABLE users ADD COLUMN verification_token TEXT"),
        ('verification_sent_at',"ALTER TABLE users ADD COLUMN verification_sent_at TEXT"),
        ('reset_token',"ALTER TABLE users ADD COLUMN reset_token TEXT"),
        ('reset_token_expires',"ALTER TABLE users ADD COLUMN reset_token_expires TEXT"),
    ):
        if col not in cols: c.execute(ddl)
    org_cols=[r['name'] for r in c.execute('PRAGMA table_info(organizations)').fetchall()]
    for col,ddl in (
        ('stripe_customer_id',"ALTER TABLE organizations ADD COLUMN stripe_customer_id TEXT"),
        ('stripe_subscription_id',"ALTER TABLE organizations ADD COLUMN stripe_subscription_id TEXT"),
    ):
        if col not in org_cols: c.execute(ddl)
    c.commit(); c.close()

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

def log_activity(kind, desc):
    try:
        c=auth_cx(); c.execute('INSERT INTO activity_log(organization_id,user_id,event_type,description,created_at) VALUES(?,?,?,?,?)',(session.get('org_id'),session.get('user_id'),kind,desc,now())); c.commit(); c.close()
    except: pass

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
    if not uid:return None
    c=auth_cx(); r=c.execute('SELECT * FROM users WHERE id=? AND is_active=1',(uid,)).fetchone(); c.close(); return r

def current_org():
    oid=session.get('org_id')
    if not oid:return None
    c=auth_cx(); r=c.execute('SELECT * FROM organizations WHERE id=?',(oid,)).fetchone(); c.close(); return r

def login_required(fn):
    @functools.wraps(fn)
    def wrapped(*a,**kw):
        if not session.get('user_id'): return redirect(url_for('login',next=request.path))
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
STRIPE_PRICE_ID=os.environ.get('STRIPE_PRICE_ID')
BILLING_ENABLED=bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)

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
    return area in AREA_ACCESS.get(session.get('role','MEMBER'),set())

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
# Rate limiting — implémentation en mémoire (par process), suffisante pour un
# pilote sur une seule instance. En production multi-worker/multi-instance,
# remplacer par Flask-Limiter + backend Redis (le stockage en mémoire locale
# n'est pas partagé entre workers/processus).
# ---------------------------------------------------------------------------
_rate_buckets=defaultdict(list)

def rate_limit(max_calls, per_seconds):
    """Limite uniquement les requêtes POST (tentatives réelles) ; les GET (affichage de page) ne sont jamais limités."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*a,**kw):
            if request.method!='POST':
                return fn(*a,**kw)
            key=f"{request.endpoint}:{request.remote_addr}"
            t=datetime.now(timezone.utc).timestamp()
            bucket=_rate_buckets[key]
            bucket[:]=[x for x in bucket if t-x<per_seconds]
            if len(bucket)>=max_calls:
                flash("Trop de tentatives. Merci de réessayer dans quelques minutes.")
                return redirect(request.referrer or url_for('login')), 429
            bucket.append(t)
            return fn(*a,**kw)
        return wrapped
    return deco

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
    CREATE TABLE IF NOT EXISTS app_settings(id INTEGER PRIMARY KEY CHECK(id=1),onboarding_complete INTEGER DEFAULT 0,currency TEXT DEFAULT 'EUR',locale TEXT DEFAULT 'fr-FR',notifications_enabled INTEGER DEFAULT 1,slack_webhook_url TEXT,teams_webhook_url TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS dso_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,snapshot_date TEXT UNIQUE,avg_days_overdue REAL,total_outstanding REAL,invoice_count INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS company(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT,city TEXT,department TEXT,allowed_departments TEXT,activities TEXT,certifications TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_number TEXT,customer TEXT,amount REAL,paid_amount REAL DEFAULT 0,issue_date TEXT,due_date TEXT,status TEXT,days_overdue INTEGER,score INTEGER,created_at TEXT,kind TEXT DEFAULT 'STANDARD',retention_release_date TEXT,retention_pct REAL,customer_email TEXT);
    CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,vendor TEXT,description TEXT,amount REAL,expense_date TEXT,category TEXT);
    CREATE TABLE IF NOT EXISTS opportunities(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,title TEXT,value REAL DEFAULT 0,score INTEGER,details TEXT,source TEXT,source_url TEXT,buyer TEXT,departments TEXT,deadline TEXT,reasons TEXT,warnings TEXT,raw_json TEXT,status TEXT DEFAULT 'OPEN',created_at TEXT);
    CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT,opportunity_id INTEGER,kind TEXT,title TEXT,draft TEXT,status TEXT DEFAULT 'PENDING',expected_value REAL DEFAULT 0,created_at TEXT,sent_at TEXT,sent_to TEXT);
    CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,action_id INTEGER,outcome_type TEXT,amount REAL,verified INTEGER DEFAULT 0,note TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,run_type TEXT,rows_processed INTEGER,signals_found INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS dce_documents(id INTEGER PRIMARY KEY AUTOINCREMENT,opportunity_id INTEGER,filename TEXT,filetype TEXT,text_content TEXT,analysis_json TEXT,go_score INTEGER,recommendation TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS weekly_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,period_start TEXT,period_end TEXT,payload_json TEXT,sent_at TEXT,recipient TEXT,status TEXT DEFAULT 'PENDING');
    CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT,entity_id INTEGER,kind TEXT,old_status TEXT,new_status TEXT,changed_by TEXT,note TEXT,created_at TEXT);
    '''); c.commit()
    # Migration douce pour les bases tenant créées avant l'ajout de created_at / retenues de garantie.
    for table,col in (('invoices','created_at'),('opportunities','created_at'),
                       ('invoices','kind'),('invoices','retention_release_date'),('invoices','retention_pct'),
                       ('invoices','customer_email'),('actions','sent_at'),('actions','sent_to'),
                       ('app_settings','slack_webhook_url'),('app_settings','teams_webhook_url')):
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

def send_email(to_email, subject, html, dry_run=None):
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
        msg.attach(MIMEText(html,'html'))
        with smtplib.SMTP(smtp_host,int(os.environ.get('SMTP_PORT',587))) as server:
            server.starttls()
            server.login(os.environ.get('SMTP_USER',''),os.environ.get('SMTP_PASSWORD',''))
            server.sendmail(msg['From'],[to_email],msg.as_string())
        return {'sent':True,'provider':'smtp','to':to_email,'subject':subject}
    except Exception as exc:
        current_app.logger.exception('SMTP email failed for %s', to_email)
        return {'sent':False,'provider':'smtp','error':str(exc),'to':to_email,'subject':subject}

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
    return secrets.token_urlsafe(32)

def send_verification_email(user, dry_run=None):
    token=gen_token()
    c=auth_cx(); c.execute('UPDATE users SET verification_token=?,verification_sent_at=? WHERE id=?',(token,now(),user['id'])); c.commit(); c.close()
    base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050')
    link=f"{base}{url_for('verify_email',token=token)}"
    html=render_template('email_transactional.html',title='Confirm your email',
        intro='Click below to confirm your ProfitOS account email address.',
        cta_label='Verify email',cta_url=link,footer='If you did not create a ProfitOS account, you can ignore this email.')
    return send_email(user['email'],'Confirm your ProfitOS account',html,dry_run=dry_run)

def send_reset_email(user, dry_run=None):
    token=gen_token()
    expires=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    c=auth_cx(); c.execute('UPDATE users SET reset_token=?,reset_token_expires=? WHERE id=?',(token,expires,user['id'])); c.commit(); c.close()
    base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050')
    link=f"{base}{url_for('reset_password',token=token)}"
    html=render_template('email_transactional.html',title='Reset your password',
        intro='Click below to choose a new password. This link expires in 2 hours.',
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

def commercial_context():
    return {'auth_user':current_user(),'auth_org':current_org(),'phase2_enabled':PHASE2_ENABLED,'user_orgs':user_organizations()}




def security_session_context():
    if session.get('user_id'):
        session.permanent=True

def init_runtime(app):
    """Attach shared request hooks and Jinja globals to a Flask app instance."""
    app.jinja_env.globals['can_access'] = can_access
    app.jinja_env.globals['ROLE_LABELS'] = ROLE_LABELS
    app.jinja_env.globals['ROLES'] = ROLES
    app.jinja_env.globals['csrf_token'] = csrf_token
    app.jinja_env.globals['trial_days_left'] = trial_days_left
    app.before_request(csrf_protect)
    app.before_request(security_session_context)
    app.before_request(ensure_tenant_schema)
    app.context_processor(commercial_context)
