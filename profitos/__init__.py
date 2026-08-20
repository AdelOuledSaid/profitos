import logging
import os
import secrets
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, g, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import get_config, APP_VERSION
from .runtime import init_auth_db, init_runtime, database_readiness, production_dependency_status, log_ops_event, init_rate_limiter, limiter


def _configure_logging(app):
    if app.testing:
        return
    app.logger.setLevel(logging.INFO)
    if os.environ.get('PROFITOS_ENV', 'development').lower() != 'production':
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

    is_production = os.environ.get('PROFITOS_ENV', 'development').lower() == 'production'
    if is_production:
        secret = app.config.get('SECRET_KEY') or ''
        if secret == 'dev-only-change-me' or len(secret) < 32:
            raise RuntimeError('PROFITOS_SECRET_KEY doit être défini avec au moins 32 caractères en production.')
        # Render termine TLS en amont et fournit X-Forwarded-Proto / Host.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _configure_logging(app)
    init_rate_limiter(app)
    init_auth_db()
    init_runtime(app)

    from .routes import account, dce, main, actions, reports, imports
    account.register(app)
    dce.register(app)
    main.register(app)
    actions.register(app)
    reports.register(app)
    imports.register(app)

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
            "connect-src 'self'; form-action 'self'"
        )
        if is_production:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        if request.path.startswith(('/login','/signup','/forgot-password','/reset-password','/verify')) or request.cookies:
            response.headers.setdefault('Cache-Control', 'no-store, private')
            response.headers.setdefault('Pragma', 'no-cache')
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
            'dependencies': deps,
        }
        return jsonify(**payload), (200 if healthy else 503)

    @app.get('/healthz')
    @limiter.exempt
    def healthz():
        return jsonify(status='ok', service='profitos', version=APP_VERSION)

    @app.get('/readyz')
    @limiter.exempt
    def readyz():
        ok, backend, error = database_readiness()
        payload = dict(status='ready' if ok else 'not_ready', service='profitos', version=APP_VERSION, database=backend)
        if error:
            # Ne pas exposer les credentials / détails de connexion publiquement.
            payload['error'] = 'database_unavailable'
            app.logger.error('Readiness DB failure: %s', error)
        return jsonify(**payload), (200 if ok else 503)

    return app


def re_safe_request_id(value):
    return all(c.isalnum() or c in '-_.' for c in value)
