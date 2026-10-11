# ProfitOS — Déploiement en production (Render)

Document de référence unique. Les anciens fichiers (`CLOUD_DEPLOYMENT.md`,
`RENDER_RC1_CHECKLIST.md`, `PRODUCTION_CHECKLIST.md`) renvoient ici.

## 1. Architecture

| Service Render | Rôle | Plan (à confirmer dans le tableau de bord) |
|---|---|---|
| `profitos` (web) | Flask + gunicorn (2 workers × 4 threads), disque `profitos-data` monté sur `/var/data` | `starter` (payant : requis pour le disque) |
| `profitos-jobs` (cron, `5 * * * *`) | `python scripts/run_job.py --due` : tâches planifiées | `starter` |
| `profitos-rate-limit` (Key Value) | compteurs de rate limiting | `starter` |
| `profitos-db` (PostgreSQL) | base auth (`public`) + un schéma `org_N` par client | `basic-1gb` |

Le disque persistant (`/var/data/uploads` : factures d'achat, justificatifs, documents
cabinet ; `/var/data/backups` : sauvegardes locales) a deux conséquences Render :
**une seule instance** du service web, et **pas de déploiement sans interruption**
(quelques secondes de coupure à chaque déploiement). Évolution prévue : stockage objet S3
(le code passe déjà par `profitos/storage.py`, point d'extension unique), qui lèvera ces
deux limites.

Le cron n'a pas accès au disque : les tâches qui écrivent des documents
(`einvoice_inbound_sync`, `cloud_folder_sync`, `backup`, `retention_purge`) sont
exécutées par le service web, déclenchées par le cron via
`POST /api/v1/internal/jobs/<nom>` (en-tête `Authorization: Bearer $JOBS_TRIGGER_TOKEN`).

## 2. Mise en place (première fois)

1. **Blueprint** : Render → New → Blueprint → dépôt GitHub. Vérifier les plans proposés
   (noms et prix) avant de valider.
2. **Secrets** (`sync: false`) à saisir :
   - web : `RESEND_API_KEY`, `BACKUP_PASSPHRASE`, `BACKUP_S3_*`, `SENTRY_DSN`, Stripe
     (voir `STRIPE_SETUP.md`), WeInvoice, Powens, Anthropic, Google Drive/Dropbox,
     GoCardless, Swan, Twilio selon les intégrations utilisées ;
   - cron `profitos-jobs` : `RESEND_API_KEY`, `POWENS_*`, `SENTRY_DSN`.
3. **Domaine** : Settings → Custom Domains → `app.profitos.fr` (CNAME vers
   `profitos.onrender.com`), certificat TLS automatique. Tant que le domaine n'est pas
   actif, définir `JOBS_TRIGGER_URL=https://profitos.onrender.com` sur le cron.
4. **Resend** : vérifier le domaine `profitos.fr` (SPF, DKIM, DMARC — voir `RESEND_SETUP.md`).
5. **PostgreSQL** : vérifier que le PITR est actif (base → Recovery), noter la rétention.
6. **Sauvegardes hors site** : créer un bucket S3 compatible (autre compte, versionnage,
   cycle de vie 35 jours) et renseigner `BACKUP_S3_*`. Conserver `BACKUP_PASSPHRASE` dans
   le gestionnaire de mots de passe de l'entreprise. Voir `RESTORE.md`.
7. **CI** : le dépôt GitHub doit être connecté à Render ; `autoDeployTrigger: checksPass`
   ne déploie que si le workflow `Tests` (`.github/workflows/tests.yml`) est vert.
   Si votre compte Render ne propose pas cette option, mettre `commit` dans `render.yaml`.
8. **Supervision externe** : sonde HTTP toutes les minutes sur `https://app.profitos.fr/readyz`
   (UptimeRobot, Better Stack…) avec alerte email/SMS ; Render fait déjà son propre health
   check sur `/readyz`. Activer les notifications Render d'échec de déploiement et d'échec du
   cron (Settings → Notifications).

## 3. À chaque déploiement

`preDeployCommand: python scripts/cloud_preflight.py --init-tenants` s'exécute avant la
bascule. Il échoue (et l'ancienne version reste en ligne) si :
`DATABASE_URL` absente ; `PROFITOS_UPLOAD_DIR` absente, sous `/tmp` ou non inscriptible ;
aucun email configuré (`RESEND_API_KEY` ou `SMTP_HOST`) ; `APP_BASE_URL` non HTTPS ;
`PROFITOS_SECRET_KEY` < 32 caractères ; migration d'au moins une organisation en échec
(les autres sont migrées quand même, détail dans les logs).

Les migrations tenant ne sont rejouées que si l'empreinte du schéma a changé
(table `schema_version` dans chaque schéma `org_N`) : un déploiement sans changement de
schéma ne coûte qu'une requête par organisation. `--force-migrate` rejoue tout.

## 4. Tâches planifiées (heure de Paris)

| Tâche | Quand | Effet |
|---|---|---|
| `einvoice_inbound_sync` | toutes les heures | réception des factures fournisseurs WeInvoice (obligatoire depuis le 1er septembre 2026) |
| `cloud_folder_sync` | 1 h | import des factures du dossier Google Drive / Dropbox |
| `backup` | 2 h | sauvegarde chiffrée base + documents |
| `dso_snapshots` | 3 h | ancienneté des impayés + historique DSO |
| `bank_sync` | 5 h | synchronisation Powens |
| `recurring_invoices` | 6 h | factures récurrentes générées **en brouillon** + email au propriétaire |
| `boamp_refresh` | 6 h | appels d'offres BOAMP (GROW) |
| `weekly_reports` | lundi 7 h | rapport hebdomadaire (désactivable par utilisateur) |
| `weekly_accountant_export` | lundi 8 h | lien d'export au comptable si activé |
| `trial_reminders` | 9 h | fin d'essai J-3 / J0, échec de paiement |
| `retention_purge` | dimanche 4 h | purges RGPD (si les fonctions de purge sont présentes) |

Chaque tâche est isolée par organisation et idempotente (table `job_runs` : une période
réussie n'est jamais rejouée, 3 tentatives maximum en cas d'échec). Commandes utiles
(Shell Render) : `python scripts/run_job.py --list`, `python scripts/run_job.py weekly_reports --force`,
`python scripts/run_job.py backup --local` (depuis le Shell du service web).

## 5. Variables d'environnement (noms uniquement)

**Groupe `profitos-shared`** : `PROFITOS_ENV`, `PROFITOS_SECRET_KEY`, `APP_BASE_URL`,
`RESEND_FROM_EMAIL`, `JOBS_TRIGGER_TOKEN`, `PG_POOL_MAX`, `PROFITOS_DATA_KEY`, et l'identité légale ci-dessous.

**Identité légale de l'éditeur** (obligatoire — le preflight refuse le déploiement sans les
champs marqués *) : `LEGAL_COMPANY_NAME`*, `LEGAL_FORM`*, `LEGAL_SIREN`*, `LEGAL_ADDRESS`*,
`LEGAL_PUBLICATION_DIRECTOR`*, `LEGAL_CONTACT_EMAIL`*, `LEGAL_SHARE_CAPITAL`, `LEGAL_RCS_CITY`,
`LEGAL_VAT_NUMBER`, `LEGAL_PHONE`, `LEGAL_PRIVACY_EMAIL`, `LEGAL_COURT_CITY`, `LEGAL_HOST_NAME`,
`LEGAL_HOST_ADDRESS`, `LEGAL_HOST_CONTACT` (par défaut : Render Services, Inc., 525 Brannan Street,
Suite 300, San Francisco — à vérifier dans votre contrat Render).

**Secrets obligatoires** (preflight) : `PROFITOS_DATA_KEY` (≥ 32 caractères, chiffrement des
identifiants d'intégration et de la 2FA — ne jamais la changer sans migration) et
`BACKUP_PASSPHRASE` (≥ 16 caractères, à conserver aussi hors de Render).

**Plateforme** : `DATABASE_URL`, `REDIS_URL`, `PG_CONNECT_TIMEOUT`, `PG_APPLICATION_NAME`,
`PG_POOL_MIN`, `PG_POOL_MAX`, `PG_SEARCH_PATH_EACH_QUERY`, `MAX_UPLOAD_MB`, `SESSION_HOURS`.

**Stockage / sauvegardes** : `PROFITOS_UPLOAD_DIR`, `PROFITOS_STORAGE_BACKEND`, `BACKUP_DIR`,
`BACKUP_PASSPHRASE`, `BACKUP_KEEP`, `BACKUP_KEEP_LOCAL_WITH_S3`, `BACKUP_S3_BUCKET`,
`BACKUP_S3_ENDPOINT`, `BACKUP_S3_REGION`, `BACKUP_S3_ACCESS_KEY_ID`,
`BACKUP_S3_SECRET_ACCESS_KEY`, `BACKUP_S3_PREFIX`.

**Tâches** : `JOBS_TRIGGER_TOKEN`, `JOBS_TRIGGER_URL`.

**Email / SMS** : `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `DEMO_NOTIFY_EMAIL`, `DEMO_REPLY_EMAIL`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.

**Supervision** : `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`.

**Facturation** (voir `STRIPE_SETUP.md`, obligatoire en production — le preflight échoue
sans clé secrète, secret de webhook et au moins un tarif) : `STRIPE_SECRET_KEY`,
`STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER_ID`,
`STRIPE_PRICE_PRO_ID`, `STRIPE_PRICE_BUSINESS_ID`, `STRIPE_PRICE_MULTI_ID`,
`STRIPE_AUTOMATIC_TAX` (1 par défaut), `PROFITOS_PAST_DUE_GRACE_DAYS` (7 par défaut) ;
`STRIPE_PRICE_ID` = ancien nom du tarif Pro, encore accepté.

**Facturation électronique** : `WEINVOICE_ENV`, `WEINVOICE_CLIENT_ID`, `WEINVOICE_CLIENT_SECRET`,
`WEINVOICE_INVOICE_CLIENT_ID`, `WEINVOICE_INVOICE_CLIENT_SECRET`, `WEINVOICE_WEBHOOK_SECRET`,
`WEINVOICE_INVOICE_WEBHOOK_SECRET`.

**Banque / paiements** : `POWENS_DOMAIN`, `POWENS_CLIENT_ID`, `POWENS_CLIENT_SECRET`,
`POWENS_REDIRECT_URI`, `GOCARDLESS_ACCESS_TOKEN`, `GOCARDLESS_ENVIRONMENT`, `SWAN_ENV`,
`SWAN_ENVIRONMENT`, `SWAN_CLIENT_ID`, `SWAN_CLIENT_SECRET`, `SWAN_GRAPHQL_URL`,
`SWAN_OAUTH_REDIRECT_URI`, `SWAN_WEBHOOK_SECRET`, `SWAN_ENABLE_ACCOUNT_CARD_REQUESTS`.

**Autres intégrations** : `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `GOOGLE_DRIVE_CLIENT_ID`,
`GOOGLE_DRIVE_CLIENT_SECRET`, `DROPBOX_CLIENT_ID`, `DROPBOX_CLIENT_SECRET`,
`SUPPLIER_INBOX_DOMAIN`, `SUPPLIER_INBOX_WEBHOOK_ENABLED`, `SUPPLIER_INBOX_WEBHOOK_SECRET`.

## 6. Vérifications après déploiement

- `GET /healthz` = 200, `GET /readyz` = 200 avec `database=postgresql`, `GET /ops/health`
- inscription, vérification d'email reçue (Resend), connexion, mot de passe oublié reçu
- deux organisations : aucune fuite de données
- import d'une facture d'achat PDF, puis redéploiement : le PDF est toujours téléchargeable
- `python scripts/run_job.py --list` puis `python scripts/run_job.py weekly_reports --org <id> --force`
- logs Render : pas de 5xx, pas de `JOB_ORG_FAILED`, pas d'`EMAIL_SIMULATED`
- sauvegarde de la nuit présente (bucket ou `/var/data/backups`), exercice de restauration
  trimestriel (`RESTORE.md`)
