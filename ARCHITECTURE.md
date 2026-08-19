# ProfitOS V1.1 — Production Architecture

## Goal

V1.1 is a structural release: it keeps the V1.0 behavior while splitting the 1,455-line monolith into a Flask application factory, shared runtime/services, and domain route modules.

## Layout

```text
ProfitOS_V1_1_Production_Architecture/
├── app.py                     # local development launcher
├── wsgi.py                    # production WSGI entry point
├── profitos/
│   ├── __init__.py            # create_app()
│   ├── config.py              # dev / production config
│   ├── db.py                  # SQLite/PostgreSQL abstraction
│   ├── runtime.py             # shared domain services & security hooks
│   └── routes/
│       ├── account.py         # auth, organizations, team, billing
│       ├── dce.py             # DCE upload + service worker
│       ├── main.py            # dashboard, company, recover/save/grow
│       ├── actions.py         # action center / sending
│       ├── reports.py         # weekly/monthly/impact
│       └── imports.py         # invoice/expense import + SAVE engine
├── templates/
├── static/
├── tests/
├── Procfile
└── requirements.txt
```

## Production changes

- Application factory (`create_app`) removes the global Flask singleton from business modules.
- Production WSGI entry point (`wsgi.py`).
- Gunicorn config via `Procfile` for Linux cloud hosts.
- Waitress launcher for Windows.
- Central configuration with secure cookie flags in production.
- `/healthz` endpoint for Railway/Render health checks.
- Smoke tests with pytest.
- Existing SQLite/PostgreSQL abstraction is preserved.
- Existing endpoints are preserved because route registration uses `app.route()` inside modular registrars, avoiding template URL breakage.

## Deliberate compatibility decision

V1.1 does **not** rewrite every SQL statement or every domain helper. Shared logic lives in `runtime.py` so the refactor remains behavior-compatible. Future V1.2+ can move individual services (`recover`, `grow`, `email`, `billing`) out of runtime without another big-bang rewrite.
