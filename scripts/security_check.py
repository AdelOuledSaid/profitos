"""Contrôle de sécurité avant d'accueillir des clients réels (v489), à lancer dans le shell
du service web Render :

    python scripts/security_check.py

Lecture seule, aucun secret affiché. Complète scripts/cloud_preflight.py (configuration
bloquante) et scripts/go_live_check.py (services tiers) sur les points propres aux données
financières :

- secrets et chiffrement (clé de session, clé de chiffrement des données distincte) ;
- en-têtes HTTP de sécurité et cookie de session (Secure, HttpOnly, SameSite, préfixe __Host-) ;
- liens secrets (factures, devis, exports, missions d'experts) jamais mis en cache ;
- documents téléversés jamais servis publiquement ;
- limiteur de requêtes partagé entre instances (Redis) ;
- double authentification des propriétaires, administrateurs et administrateurs plateforme ;
- journal de sécurité alimenté ;
- sauvegardes : phrase de passe, copie hors site, dernière sauvegarde réussie.

Code de sortie : 0 si aucun point bloquant, 1 sinon.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, FAIL, INFO = 'ok', 'warn', 'fail', 'info'
SYMBOL = {OK: '✓', WARN: '⚠', FAIL: '✗', INFO: '•'}
REQUIRED_HEADERS = {
    'Content-Security-Policy': "frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Strict-Transport-Security': 'max-age=',
}
TOKEN_PATHS = ('/facture/controle-securite', '/devis/controle-securite', '/expert/mission/controle-securite')


def _item(section, level, label, detail=''):
    return {'section': section, 'level': level, 'label': label, 'detail': detail}


def check_secrets(env):
    s = 'Secrets et chiffrement'
    secret = (env.get('PROFITOS_SECRET_KEY') or '').strip()
    data = (env.get('PROFITOS_DATA_KEY') or '').strip()
    out = [_item(s, OK if len(secret) >= 32 else FAIL, 'Clé de session (PROFITOS_SECRET_KEY) de 32 caractères au moins'),
           _item(s, OK if len(data) >= 32 else FAIL, 'Clé de chiffrement des données (PROFITOS_DATA_KEY)',
                 '' if len(data) >= 32 else 'Chiffre les jetons bancaires, cloud, e-commerce et les secrets 2FA.')]
    if secret and data and secret == data:
        out.append(_item(s, FAIL, 'Clé de chiffrement identique à la clé de session',
                         'Utilisez deux valeurs différentes : la fuite de l\'une ne doit pas livrer l\'autre.'))
    env_name = (env.get('PROFITOS_ENV') or '').strip().lower()
    out.append(_item(s, OK if env_name == 'production' else FAIL, f"Mode production (PROFITOS_ENV={env_name or 'absent'})"))
    url = (env.get('APP_BASE_URL') or '').strip()
    out.append(_item(s, OK if url.startswith('https://') else FAIL, 'Adresse publique en HTTPS (APP_BASE_URL)'))
    db = (env.get('DATABASE_URL') or '').strip()
    out.append(_item(s, OK if db.startswith(('postgres://', 'postgresql://')) else FAIL,
                     'Base PostgreSQL (un schéma par entreprise)' if db else 'DATABASE_URL absente'))
    return out


def check_http(app):
    """En-têtes, cookie de session, liens secrets et documents, sur l'application réelle."""
    s = 'Protection HTTP'
    out = []
    client = app.test_client()
    r = client.get('/login', base_url='https://controle.local')
    for name, expected in REQUIRED_HEADERS.items():
        value = r.headers.get(name, '')
        out.append(_item(s, OK if expected in value else FAIL, f'En-tête {name}', '' if expected in value else 'absent ou incomplet'))
    cookies = [v for k, v in r.headers.items() if k.lower() == 'set-cookie']
    session_cookie = next((c for c in cookies if 'profitos_session' in c), None)
    if session_cookie:
        flags = session_cookie.lower()
        good = 'secure' in flags and 'httponly' in flags and 'samesite=lax' in flags and session_cookie.startswith('__Host-')
        out.append(_item(s, OK if good else FAIL, 'Cookie de session Secure, HttpOnly, SameSite=Lax, préfixe __Host-',
                         '' if good else session_cookie.split(';', 1)[1][:120]))
    else:
        out.append(_item(s, INFO, 'Cookie de session non émis sur la page de connexion', 'Contrôle à refaire après connexion.'))
    for path in TOKEN_PATHS:
        rr = client.get(path, base_url='https://controle.local')
        cc = rr.headers.get('Cache-Control', '')
        out.append(_item(s, OK if 'no-store' in cc else FAIL, f'Lien secret jamais mis en cache ({path.split("/")[1]})',
                         '' if 'no-store' in cc else f'Cache-Control : {cc or "absent"}'))
    for path in ('/uploads/purchase_pdfs/x.pdf', '/tenant_data/1.db', '/static/../profitos_auth.db'):
        rr = client.get(path, base_url='https://controle.local')
        out.append(_item(s, OK if rr.status_code in (404, 403, 400, 302) else FAIL, f'Fichier interne non servi ({path})',
                         '' if rr.status_code in (404, 403, 400, 302) else f'HTTP {rr.status_code}'))
    return out


def check_rate_limit(env):
    return [_item('Limitation des requêtes', OK if (env.get('REDIS_URL') or '').strip() else WARN,
                  'Compteurs partagés entre instances (REDIS_URL)',
                  '' if (env.get('REDIS_URL') or '').strip() else
                  'Sans Redis, chaque instance compte séparément : la protection contre les essais de mots de passe '
                  'est affaiblie si plusieurs instances tournent.')]


def check_accounts(ac, env):
    s = 'Comptes et accès'
    out = []
    try:
        rows = ac.execute("""SELECT u.email, COALESCE(u.totp_enabled,0) t, m.role FROM users u JOIN memberships m ON m.user_id=u.id
                             WHERE m.role IN ('OWNER','ADMIN')""").fetchall()
    except Exception as exc:
        return [_item(s, WARN, 'Comptes illisibles', type(exc).__name__)]
    total = len({r['email'] for r in rows})
    without = sorted({r['email'] for r in rows if not int(r['t'] or 0)})
    out.append(_item(s, OK if not without else WARN, f"Double authentification des propriétaires et administrateurs : "
                     f"{total - len(without)}/{total} activée(s)",
                     '' if not without else f"{len(without)} compte(s) sans 2FA : à encourager, en priorité pour les cabinets."))
    admins = {x.strip().lower() for x in (env.get('PLATFORM_ADMIN_EMAILS', '') + ',' + env.get('DEMO_NOTIFY_EMAIL', '')).split(',')
              if x.strip()}
    for email in sorted(admins):
        r = ac.execute('SELECT email_verified, COALESCE(totp_enabled,0) t FROM users WHERE LOWER(email)=?', (email,)).fetchone()
        if not r:
            out.append(_item(s, WARN, 'Adresse d\'administration sans compte', 'Créez le compte et vérifiez l\'adresse.'))
        else:
            ok = bool(r['email_verified']) and bool(int(r['t'] or 0))
            out.append(_item(s, OK if ok else FAIL, 'Compte d\'administration plateforme vérifié et protégé par 2FA',
                             '' if ok else 'Activez la double authentification sur ce compte (accès à l\'annuaire et aux demandes).'))
    return out


def check_security_log(ac):
    try:
        n = ac.execute('SELECT COUNT(*) n FROM security_events').fetchone()['n']
    except Exception:
        return [_item('Traçabilité', FAIL, 'Journal de sécurité absent', 'Lancez : python scripts/init_security_events.py')]
    return [_item('Traçabilité', OK if n else WARN, f'Journal de sécurité : {n} événement(s) enregistré(s)',
                  '' if n else 'Aucun événement : vérifiez que les connexions sont bien journalisées.')]


def run(env=None, app=None, ac=None):
    env = dict(os.environ if env is None else env)
    items = check_secrets(env) + check_rate_limit(env)
    if app is None:
        from profitos import create_app
        from profitos.config import ProductionConfig
        try:
            app = create_app(ProductionConfig)
        except Exception as exc:  # l'application refuse elle-même de démarrer : c'est bloquant
            items.append(_item('Protection HTTP', FAIL, 'L\'application refuse de démarrer en production', str(exc)[:200]))
    if app is not None:
        items += check_http(app)
    if ac is None:
        from profitos.runtime import auth_cx
        ac = auth_cx()
        own = True
    else:
        own = False
    try:
        items += check_accounts(ac, env) + check_security_log(ac)
        import importlib.util
        spec = importlib.util.spec_from_file_location('profitos_go_live', ROOT / 'scripts' / 'go_live_check.py')
        gl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gl)
        items += [_item(i['section'], i['level'], i['label'], i.get('detail', '')) for i in gl.check_backups(env)]
        items.append(gl.last_backup(ac))
    finally:
        if own:
            ac.close()
    return items


def main():
    items = run()
    section = None
    for i in items:
        if i['section'] != section:
            section = i['section']
            print(f'\n{section}')
        print(f"  {SYMBOL[i['level']]} {i['label']}" + (f" — {i['detail']}" if i['detail'] else ''))
    fails = [i for i in items if i['level'] == FAIL]
    warns = [i for i in items if i['level'] == WARN]
    print(f"\n{len(fails)} point(s) bloquant(s), {len(warns)} avertissement(s). Voir docs/securite.md.")
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
