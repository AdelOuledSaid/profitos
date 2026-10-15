import hashlib
import re
import hmac
import logging
import os
import secrets
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, g, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import get_config, APP_VERSION, is_production as _env_is_production, runtime_env

# Clé de session de secours propre au processus, uniquement en développement/test
# explicites : jamais de valeur fixe connue publiquement (sessions forgeables).
_DEV_PROCESS_SECRET = secrets.token_urlsafe(48)
from .runtime import init_auth_db, init_runtime, database_readiness, production_dependency_status, log_ops_event, init_rate_limiter, limiter


_EMAIL_RE = None


def _scrub_pii(value):
    """Masque les adresses email (a***@domaine) dans une structure Sentry."""
    global _EMAIL_RE
    import re
    if _EMAIL_RE is None:
        _EMAIL_RE = re.compile(r'([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
    if isinstance(value, str):
        return _EMAIL_RE.sub(r'\1***@\2', value)
    if isinstance(value, dict):
        return {k: _scrub_pii(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_pii(v) for v in value)
    return value


def _sentry_before_send(event, hint=None):
    request = event.get('request') or {}
    request.pop('cookies', None)
    request.pop('data', None)            # corps de formulaire : jamais envoyé
    if 'query_string' in request:
        request['query_string'] = '[filtré]'
    headers = request.get('headers') or {}
    for h in list(headers):
        if h.lower() in ('authorization', 'cookie', 'x-csrf-token'):
            headers[h] = '[filtré]'
    event.pop('user', None)
    return _scrub_pii(event)


def _init_sentry(app):
    """Sentry optionnel : actif seulement si SENTRY_DSN est défini ET sentry-sdk installé."""
    dsn = (os.environ.get('SENTRY_DSN') or '').strip()
    if not dsn or app.testing:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        app.logger.warning('SENTRY_DSN défini mais sentry-sdk non installé : suivi des erreurs désactivé.')
        return False
    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        environment=os.environ.get('PROFITOS_ENV', 'development'),
        release=f'profitos@{APP_VERSION}',
        send_default_pii=False,
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0') or 0),
        before_send=_sentry_before_send,
    )
    return True


_STATIC_GZIP_TYPES = ('text/css', 'application/javascript', 'text/javascript', 'image/svg+xml', 'application/json',
                      'application/manifest+json')
_static_gzip_cache = {}


def _configure_static_cache(app):
    """Assets versionnés (?v=<mtime> via asset_url) : cache navigateur d'un an
    (immutable) ; compression gzip des assets texte, mise en cache en mémoire."""
    import gzip

    @app.after_request
    def static_cache_headers(response):
        if not request.path.startswith('/static/') or response.status_code != 200:
            return response
        version = request.args.get('v', '')
        if version and version != '0':
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            response.headers.pop('Pragma', None)
        mimetype = (response.mimetype or '').lower()
        if (mimetype in _STATIC_GZIP_TYPES and 'gzip' in request.headers.get('Accept-Encoding', '').lower()
                and not request.headers.get('Range') and 'Content-Encoding' not in response.headers):
            try:
                rel = request.path[len('/static/'):]
                path = (Path(app.static_folder) / rel).resolve()
                if not str(path).startswith(str(Path(app.static_folder).resolve())) or path.stat().st_size > 1_000_000:
                    return response
                key = (str(path), path.stat().st_mtime)
                body = _static_gzip_cache.get(key)
                if body is None:
                    body = gzip.compress(path.read_bytes(), compresslevel=6)
                    _static_gzip_cache[key] = body
                response.direct_passthrough = False
                response.set_data(body)
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Content-Length'] = str(len(body))
                response.headers.add('Vary', 'Accept-Encoding')
                if response.headers.get('ETag'):
                    response.set_etag(response.get_etag()[0] + '-gz')
            except Exception:
                return response
        return response


def _configure_logging(app):
    if app.testing:
        return
    if not logging.getLogger().handlers:
        # Logs JSON OPS / jobs / migrations visibles dans les logs Render (stderr).
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    app.logger.setLevel(logging.INFO)
    if not _env_is_production():
        log_dir = Path(app.root_path).parent / 'logs'
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(log_dir / 'profitos.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        app.logger.addHandler(handler)


def create_app(config_object=None):
    app = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
        static_url_path='/static',
    )
    app.config.from_object(config_object or get_config())

    is_production = _env_is_production()
    if not app.config.get('SECRET_KEY'):
        if is_production:
            raise RuntimeError('PROFITOS_SECRET_KEY est obligatoire. Hors production, définissez explicitement '
                               'PROFITOS_ENV=development (ou test) pour utiliser une clé temporaire.')
        app.config['SECRET_KEY'] = _DEV_PROCESS_SECRET
    if is_production:
        base_url = (os.environ.get('APP_BASE_URL') or '').strip()
        if not base_url.startswith('https://') or len(base_url) <= len('https://'):
            raise RuntimeError('APP_BASE_URL doit être défini en https:// en production '
                               '(liens des e-mails : jamais déduits de l\'en-tête Host).')
        if not (os.environ.get('PROFITOS_DATA_KEY') or '').strip():
            logging.getLogger(__name__).warning(
                'PROFITOS_DATA_KEY absent : la connexion des intégrations tierces (Shopify, WooCommerce, '
                'Dropbox, Google Drive) sera refusée tant que la clé de chiffrement n\'est pas définie.')
        secret = app.config.get('SECRET_KEY') or ''
        weak = (secret == 'dev-only-change-me' or len(secret) < 32
                or re.search(r'change|remplac|example|exemple|secret[-_]?key|xxxx', secret, re.I)
                or len(set(secret)) < 10
                # empreinte de la valeur d'exemple publiée dans .env.production.example
                or hashlib.sha256(secret.encode()).hexdigest() == '1e7aa31aa660d8ee12406a65a035b9cce18c34c2ef2069a7e6360f4f02f13ed3')
        if weak:
            raise RuntimeError('PROFITOS_SECRET_KEY doit être une valeur aléatoire d\'au moins 32 caractères en production '
                               '(pas la valeur d\'exemple). Générez-la avec : python -c "import secrets;print(secrets.token_urlsafe(48))"')
        # Render termine TLS en amont et fournit X-Forwarded-Proto / Host.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _configure_logging(app)
    _init_sentry(app)
    _configure_static_cache(app)
    from . import db as _dbmod
    _dbmod.init_app(app)  # rend les connexions PostgreSQL au pool en fin de requête
    init_rate_limiter(app)
    init_auth_db()
    from .account_security import ensure_account_security_schema
    ensure_account_security_schema()
    init_runtime(app)
    from . import jobs as _jobs
    _jobs.register(app)  # tâches planifiées : déclenchement interne + désinscription rapport hebdo

    from .routes import account, dce, main, actions, reports, imports, api, money_hunter, financial_brain, cash_intelligence, decision_simulator, invoicing, bank_sync, accounting, expenses, cloud_storage, entities, analytics, swan_baas, loans, finance_leases, reviews, cabinet, gocardless_baas, ecommerce, push_notifications, payroll
    from .routes import ledger as ledger_routes
    account.register(app)
    from .routes import security as security_routes
    security_routes.register(app)
    dce.register(app)
    main.register(app)
    actions.register(app)
    reports.register(app)
    imports.register(app)
    api.register(app)
    money_hunter.register(app)
    financial_brain.register(app)
    cash_intelligence.register(app)
    decision_simulator.register(app)
    invoicing.register(app)
    bank_sync.register(app)
    accounting.register(app)
    ledger_routes.register(app)
    from .routes import teledeclarations as teledeclaration_routes
    teledeclaration_routes.register(app)
    from .routes import automations as automation_routes
    automation_routes.register(app)
    expenses.register(app)
    cloud_storage.register(app)
    entities.register(app)
    analytics.register(app)
    swan_baas.register(app)
    loans.register(app)
    finance_leases.register(app)
    reviews.register(app)
    cabinet.register(app)
    gocardless_baas.register(app)
    ecommerce.register(app)
    push_notifications.register(app)
    payroll.register(app)

    @app.before_request
    def request_context():
        incoming = request.headers.get('X-Request-ID', '')
        g.request_id = incoming[:80] if incoming and re_safe_request_id(incoming) else secrets.token_hex(12)
        g.request_started_at = time.monotonic()

    @app.after_request
    def security_headers(response):
        response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
        started = getattr(g, 'request_started_at', None)
        if started is not None:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            response.headers['X-Response-Time-Ms'] = str(elapsed_ms)
            if elapsed_ms >= 1500:
                log_ops_event('SLOW_REQUEST','WARNING',detail=f'{elapsed_ms}ms')
        if response.status_code >= 500:
            log_ops_event('HTTP_5XX','ERROR',detail=f'status={response.status_code}')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=()'
        response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "img-src 'self' data:; font-src 'self' data:; "
            "style-src-elem 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; script-src-attr 'none'; "
            "connect-src 'self'; form-action 'self' https://checkout.stripe.com https://billing.stripe.com"
        )
        if is_production:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        if request.path.startswith(('/login','/signup','/forgot-password','/reset-password','/verify')) or request.cookies:
            response.headers.setdefault('Cache-Control', 'no-store, private')
            response.headers.setdefault('Pragma', 'no-cache')
        if request.path.startswith(_PUBLIC_TOKEN_PREFIXES):
            # Liens publics à jeton (factures, devis, exports) : jamais en cache partagé
            # ni dans le cache du navigateur, même sans cookie.
            response.headers['Cache-Control'] = 'no-store, private'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Referrer-Policy'] = 'no-referrer'
        _rfc5987_content_disposition(response)
        return response

    @app.errorhandler(400)
    def bad_request(error):
        return render_template('error.html', code=400, title='Requête invalide', message='La requête a été refusée pour des raisons de sécurité ou de validation.'), 400

    @app.errorhandler(403)
    def forbidden(error):
        return render_template('error.html', code=403, title='Accès refusé', message="Vous n'avez pas l'autorisation d'accéder à cette ressource."), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template('error.html', code=404, title='Page introuvable', message="La ressource demandée n'existe pas ou n'est plus disponible."), 404

    @app.errorhandler(413)
    def too_large(error):
        return render_template('error.html', code=413, title='Fichier trop volumineux', message='Le fichier dépasse la taille maximale autorisée.'), 413

    @app.errorhandler(429)
    def too_many(error):
        return render_template('error.html', code=429, title='Trop de requêtes', message='Trop de tentatives ont été effectuées. Réessayez plus tard.'), 429

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.exception('Erreur interne request_id=%s path=%s', getattr(g, 'request_id', '-'), request.path)
        return render_template('error.html', code=500, title='Erreur interne', message="Une erreur est survenue. L'identifiant de requête peut être communiqué au support.", request_id=getattr(g, 'request_id', None)), 500

    @app.get('/ops/health')
    @limiter.exempt
    def ops_health():
        deps = production_dependency_status()
        healthy = bool(deps.get('database',{}).get('ok'))
        payload = {
            'status': 'ok' if healthy else 'degraded',
            'service': 'profitos',
            'version': APP_VERSION,
        }
        # Le détail des dépendances (base, e-mail, cache configurés ou non) aide un
        # attaquant à cibler l'infrastructure : il n'est renvoyé qu'avec le jeton
        # d'exploitation OPS_HEALTH_TOKEN, envoyé dans l'en-tête X-Ops-Token.
        expected = os.environ.get('OPS_HEALTH_TOKEN', '')
        provided = request.headers.get('X-Ops-Token', '')
        if expected and provided and hmac.compare_digest(provided, expected):
            payload['dependencies'] = deps
        return jsonify(**payload), (200 if healthy else 503)

    @app.get('/healthz')
    @limiter.exempt
    def healthz():
        return jsonify(status='ok', service='profitos', version=APP_VERSION)

    @app.get('/readyz')
    @limiter.exempt
    def readyz():
        ok, backend, error = database_readiness()
        if ok and app.config.get('PROFITOS_IS_PRODUCTION', os.environ.get('PROFITOS_ENV', '').lower() == 'production'):
            # Disque persistant absent ou en lecture seule : l'instance n'est pas prête
            # (Render garde alors la version précédente en ligne au lieu de perdre des fichiers).
            from profitos.storage import upload_dir_writable
            disk_ok, disk_error = upload_dir_writable()
            if not disk_ok:
                ok, error = False, f'stockage des documents : {disk_error}'
        # Réponse publique minimale : ni type ni version de base de données, ni détail
        # d'erreur (ces informations restent dans les journaux serveur).
        payload = dict(status='ready' if ok else 'not_ready')
        if error:
            app.logger.error('Readiness DB failure backend=%s: %s', backend, error)
        return jsonify(**payload), (200 if ok else 503)

    # Pages et PDF publics à jeton : limite de débit explicite (sinon aucune limite
    # globale ne s'applique à ces routes non authentifiées).
    for endpoint in _PUBLIC_TOKEN_ENDPOINTS:
        view = app.view_functions.get(endpoint)
        if view is not None:
            app.view_functions[endpoint] = limiter.limit('30 per minute', override_defaults=True)(view)

    return app


_PUBLIC_TOKEN_PREFIXES = ('/facture/', '/devis/', '/export-download/', '/comptabilite/export-fec/telecharger/',
                          '/invitation/', '/collaboration-comptable/accepter/')
_PUBLIC_TOKEN_ENDPOINTS = ('public_invoice_view', 'public_invoice_pdf', 'public_quote_view', 'public_quote_pdf',
                           'export_download', 'accounting_fec_download')


def _rfc5987_content_disposition(response):
    """Un nom de fichier accentué (« Facture-Société.pdf ») dans Content-Disposition
    n'est pas encodable en latin-1 et provoquait une erreur 500. Réécrit l'en-tête au
    format RFC 6266/5987 : filename ASCII de repli + filename*=UTF-8''…"""
    value = response.headers.get('Content-Disposition')
    if not value:
        return
    try:
        value.encode('latin-1')
        if all(ord(ch) < 128 for ch in value):
            return
    except UnicodeEncodeError:
        pass
    import unicodedata
    from urllib.parse import quote
    m = re.match(r'\s*(inline|attachment)\s*;\s*filename="?([^";]*)"?', value, re.I)
    if not m:
        response.headers['Content-Disposition'] = 'attachment'
        return
    kind, name = m.group(1).lower(), m.group(2)
    ascii_name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', '_', ascii_name).strip() or 'document'
    response.headers['Content-Disposition'] = f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def re_safe_request_id(value):
    return all(c.isalnum() or c in '-_.' for c in value)
