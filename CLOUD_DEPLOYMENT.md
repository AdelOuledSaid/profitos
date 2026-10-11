# ProfitOS V1.2 — Cloud Deployment

## Railway

Le projet fournit `railway.json`.

1. Poussez le dossier sur GitHub.
2. Créez un projet Railway depuis le repo.
3. Ajoutez un service PostgreSQL au même projet.
4. Dans le service web, créez `DATABASE_URL` en référence à l'URL du service Postgres.
5. Définissez `PROFITOS_ENV=production`, `PROFITOS_SECRET_KEY` et `APP_BASE_URL`.
6. Railway exécutera le pre-deploy `python scripts/cloud_preflight.py --init-tenants`.
7. Le healthcheck est `/readyz`.

Commande de démarrage :

```bash
gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT wsgi:app
```

## Render

Le projet fournit `render.yaml` avec un Web Service et un Render Postgres.

1. Poussez le repo sur GitHub/GitLab/Bitbucket.
2. Dans Render : New → Blueprint.
3. Sélectionnez le repo.
4. Render crée le service `profitos` et la base `profitos-db`.
5. Renseignez les variables marquées `sync: false`.
6. Le healthcheck est `/readyz`.

## Uploads en V1.2

Les documents sont encore écrits sur le système de fichiers local. Sur les plateformes à disque éphémère, configurez :

```bash
PROFITOS_UPLOAD_DIR=/tmp/profitos-uploads
```

Ces fichiers peuvent disparaître lors d'un redéploiement. Le stockage objet persistant est prévu pour V1.3.

## Vérifications après déploiement

- `GET /healthz` → liveness
- `GET /readyz` → base joignable
- signup/login/logout
- création organisation
- import test factures
- récupération GROW / BOAMP
- webhook Stripe en mode test si activé
