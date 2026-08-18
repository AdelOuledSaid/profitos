# ProfitOS V1.2 — Migration PostgreSQL

## 1. Mode local

Sans `DATABASE_URL`, ProfitOS continue d'utiliser SQLite. Rien ne change pour votre lancement local sur `127.0.0.1:5050`.

## 2. Mode PostgreSQL

Définissez :

```bash
PROFITOS_ENV=production
PROFITOS_SECRET_KEY=<secret long>
DATABASE_URL=postgresql://user:password@host:5432/profitos
```

Puis validez :

```bash
python scripts/cloud_preflight.py --init-tenants
python scripts/postgres_smoke.py
```

Architecture multi-tenant PostgreSQL :

- `public` : users, organizations, memberships, activity_log
- `org_1`, `org_2`, ... : données métier isolées par organisation

Le `search_path` est réappliqué avant les requêtes tenant. Cette précaution est importante avec les poolers en mode transaction.

## 3. Migrer les données SQLite existantes

Sauvegardez d'abord le dossier. Ensuite :

```bash
set DATABASE_URL=postgresql://...
python scripts/migrate_sqlite_to_postgres.py
```

Le script copie la base auth puis les fichiers `tenant_data/org_<id>.db` vers les schémas PostgreSQL correspondants. Après migration, contrôlez :

- `/readyz`
- connexion utilisateur
- RECOVER / SAVE / GROW
- organisations / rôles
- Stripe en mode test

## 4. Ne supprimez pas SQLite immédiatement

Conservez une sauvegarde en lecture seule jusqu'à validation fonctionnelle de PostgreSQL.
