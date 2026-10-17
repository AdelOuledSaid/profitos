# Migration vers PostgreSQL

## Ce qui a été fait

`app.py` ne se connecte plus directement à SQLite. Toutes les connexions
passent par `db.py`, qui bascule automatiquement entre :

- **SQLite** (défaut, aucune configuration) — un fichier par organisation
  dans `tenant_data/org_<id>.db`, comportement identique à avant.
- **PostgreSQL** — dès que la variable d'environnement `DATABASE_URL` est
  définie. Une seule base, un **schéma Postgres par organisation**
  (`org_<id>`) pour préserver l'isolation logique des données entre clients,
  sélectionné via `SET search_path` à l'ouverture de chaque connexion tenant.
  Les tables d'authentification (organizations/users/memberships/activity_log)
  restent dans le schéma `public`, partagé.

`db.py` traduit à la volée les quelques constructions spécifiques à SQLite
utilisées dans `app.py` :

| SQLite | Traduit en PostgreSQL |
|---|---|
| `?` (placeholders) | `%s` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| `SELECT last_insert_rowid()` | `SELECT lastval()` |
| `PRAGMA table_info(table)` | requête sur `information_schema.columns` |
| `INSERT OR IGNORE INTO ...` | `INSERT INTO ... ON CONFLICT DO NOTHING` |

Aucune requête dans `app.py` n'a dû être modifiée — c'est le principe de
cette couche d'abstraction : le code métier reste identique quel que soit
le backend.

## ⚠️ Ce qui N'A PAS pu être testé ici

Cet environnement de développement n'a pas d'accès réseau : impossible
d'installer `psycopg2` ni de se connecter à une vraie instance PostgreSQL.
Le mode SQLite (par défaut) a été testé de bout en bout et fonctionne
(signup, onboarding, upload, RECOVER/SAVE/GROW, rapport hebdo). **Le mode
PostgreSQL doit être testé avant toute mise en production réelle.**

## Étapes pour migrer réellement

1. **Provisionner une base PostgreSQL** (Render, Railway, Supabase, RDS, etc.).
2. **Installer la dépendance** : `pip install psycopg2-binary` (déjà ajouté à `requirements.txt`).
3. **Définir `DATABASE_URL`** dans l'environnement, format :
   `postgresql://user:password@host:5432/profitos`
4. **Lancer l'app une première fois** — `init_auth_db()` et `init_tenant_db()`
   créent automatiquement les tables/schémas au premier accès (comme en SQLite).
5. **Tester chaque écran manuellement** : signup, vérification email, login,
   reset password, onboarding, upload factures/dépenses, RECOVER/SAVE/GROW,
   detail, actions, impact, rapport hebdomadaire. Porter une attention
   particulière aux upserts (`ON CONFLICT`) sur `company`/`app_settings` et à
   la génération d'ID (`lastval()`) sur signup.
6. **Migrer les données existantes** (si des clients pilotes ont déjà des
   données en SQLite) : écrire un script one-shot qui lit chaque
   `tenant_data/org_<id>.db` et réinsère les lignes dans le schéma Postgres
   correspondant. Non fourni dans ce lot — à faire uniquement si des
   données réelles existent déjà à migrer.

## Restant hors scope de cette migration

- **CSRF** : déjà en place (indépendant du backend DB), voir `app.py::csrf_protect`.
- **Rate limiting** : implémentation en mémoire actuelle ne fonctionne que sur
  une seule instance/process. En production avec plusieurs workers (Gunicorn
  `--workers N>1`) ou plusieurs instances, remplacer par **Flask-Limiter +
  Redis** pour un compteur partagé.
- **Pool de connexions** : `db.py` ouvre une connexion Postgres par requête
  HTTP (comme le faisait déjà le code SQLite). Correct pour un pilote à
  faible trafic ; pour scaler, ajouter un pool (`psycopg2.pool` ou passer à
  SQLAlchemy) avant une ouverture publique large.
