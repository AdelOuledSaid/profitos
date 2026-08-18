from flask import Flask, jsonify
from .config import get_config, APP_VERSION
from .runtime import init_auth_db, init_runtime, database_readiness


def create_app(config_object=None):
    app = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
        static_url_path='/static',
    )
    app.config.from_object(config_object or get_config())

    init_auth_db()
    init_runtime(app)

    from .routes import account, dce, main, actions, reports, imports
    account.register(app)
    dce.register(app)
    main.register(app)
    actions.register(app)
    reports.register(app)
    imports.register(app)

    @app.get('/healthz')
    def healthz():
        # Liveness : le process Flask/Gunicorn répond.
        return jsonify(status='ok', service='profitos', version=APP_VERSION)

    @app.get('/readyz')
    def readyz():
        # Readiness : le process répond ET la base auth est joignable.
        ok, backend, error = database_readiness()
        payload = dict(status='ready' if ok else 'not_ready', service='profitos',
                       version=APP_VERSION, database=backend)
        if error:
            payload['error'] = error[:300]
        return jsonify(**payload), (200 if ok else 503)

    return app
