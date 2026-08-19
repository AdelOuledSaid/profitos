# ProfitOS V1.1 — Production Architecture

This release refactors the cleaned V1.0 into a modular production-oriented Flask structure while preserving the existing product behavior.

## Local start (same simple workflow)

```cmd
cd Z:\ProfitOS_V1_1_Production_Architecture
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open:

`http://127.0.0.1:5050`

## Production-style local start on Windows

```cmd
RUN_PRODUCTION_WINDOWS.bat
```

This runs Waitress instead of Flask's development server.

## Cloud WSGI

Linux / Railway / Render style command:

```bash
gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT wsgi:app
```

## Health check

`GET /healthz`

returns:

```json
{"status":"ok","service":"profitos","version":"1.1"}
```

## Tests

```cmd
pytest -q
```

## Architecture

See `ARCHITECTURE.md`.

## Important

For a public launch, configure at minimum:

- `PROFITOS_ENV=production`
- `PROFITOS_SECRET_KEY`
- `DATABASE_URL` (PostgreSQL recommended)
- Stripe variables if billing is enabled
- SMTP variables for transactional mail
- HTTPS at the hosting/reverse-proxy layer

The existing PostgreSQL compatibility layer is preserved, but it still needs integration testing against the exact managed PostgreSQL target before a public production launch.


## V1.3 — Production Hardening

Sécurité des sessions, headers HTTP, CSRF same-origin, uploads isolés et validés, anti open-redirect, validation des webhooks, request IDs, pages d’erreur sûres et tests de sécurité de base. Voir `SECURITY.md` et `PRODUCTION_CHECKLIST.md`.
