# ProfitOS — Architecture

## Vue d'ensemble

Application Flask monolithique et modulaire : une fabrique `create_app()` enregistre une
trentaine de modules de routes ; la logique métier vit dans des modules Python sans Flask
(calculs comptables et fiscaux testables isolément). Les pages sont rendues côté serveur
(Jinja), sans framework JavaScript.

```text
.
├── app.py                  # lanceur local (port 5050)
├── wsgi.py                 # point d'entrée gunicorn / Waitress
├── render.yaml             # services Render (web, cron, Key Value, PostgreSQL)
├── profitos/
│   ├── __init__.py         # create_app() : configuration, sécurité, enregistrement des routes
│   ├── config.py           # configurations développement / production
│   ├── db.py               # accès SQLite / PostgreSQL (schéma par organisation, pool)
│   ├── runtime.py          # services partagés : sessions, rôles, CSRF, migrations, e-mails, utilitaires
│   ├── storage.py          # stockage des documents (disque persistant, point d'extension S3)
│   ├── jobs.py             # tâches planifiées (décorateur @job, exécution par organisation)
│   ├── routes/             # pages et API, un module par domaine
│   └── *.py                # logique métier (voir ci-dessous)
├── templates/              # pages Jinja (≈ 170)
├── static/                 # CSS, JS, icônes, service worker
├── scripts/                # préflight, tâches, sauvegarde / restauration, migration PostgreSQL
├── docs/                   # guide utilisateur, production réglementaire
├── mobile/                 # application Capacitor (Android / iOS)
└── tests/                  # pytest (≈ 1 540 tests)
```

## Données

- **Base d'authentification** (`profitos_auth.db` en développement, schéma `public` en
  PostgreSQL) : utilisateurs, organisations, adhésions et rôles, abonnements, journaux de
  sécurité et d'activité, exécutions des tâches (`job_runs`).
- **Une base par organisation** : `tenant_data/org_<id>.db` en développement, schéma
  `org_<id>` en PostgreSQL (sélection par `SET search_path`, `profitos/db.py`). Toutes les
  données métier y vivent : facturation, achats, banque, écritures comptables, paramètres et
  déclarations fiscales. Une même organisation peut contenir plusieurs entités
  (`entity_id`), isolées dans chaque requête.
- **Migrations** : `runtime.migrate_tenant()` crée les tables (`CREATE TABLE IF NOT EXISTS`),
  ajoute les colonnes manquantes (`_v4_columns`) et reprend les données si nécessaire ; elles
  sont rejouées seulement si l'empreinte du schéma change (table `schema_version`), et
  lancées avant chaque déploiement par `scripts/cloud_preflight.py --init-tenants`.
- **Compatibilité SQL** : les requêtes utilisent `?`, traduit en `%s` pour PostgreSQL. Un
  motif `LIKE` contenant `%` se passe en paramètre (un test de garde l'impose).
- **Documents** (`PROFITOS_UPLOAD_DIR`, disque persistant en production) : catégories
  `purchase_documents`, `expense_receipts`, `cabinet_documents`, `declaration_receipts`
  (accusés de dépôt des déclarations), `accounting_attachments`, et les imports. Elles sont
  incluses dans l'export et la purge RGPD (`storage.org_directories`).

## Contrôle d'accès

1. **Session** : `login_required`, 2FA facultative, verrouillage après échecs, jetons à usage
   unique (`account_security.py`, `totp.py`).
2. **Formule** : `feature_access.MODULE_FEATURES` et les préfixes d'URL (`/comptabilite/`
   exige `accounting_core`) ; un module non inclus reste visible avec un cadenas.
3. **Rôle** (OWNER, ADMIN, COMPTABLE, MEMBER, COMMERCIAL, LECTURE) :
   `runtime.MODULE_AREAS` rattache chaque module de routes à un domaine (`accounting`,
   `banking`, `pilotage`, `cabinet`…), vérifié avant chaque requête
   (`enforce_role_areas`). Toute route sous `/comptabilite` doit y être rattachée (test
   `test_v456`). LECTURE ne peut rien modifier.
4. **Actions sensibles** : toute écriture fiscale (profil fiscal, paramètres d'impôt, DAS2,
   IFU, 2777, CFE, déclarations) est réservée à OWNER, ADMIN et COMPTABLE
   (`runtime.fiscal_write_allowed`) ; le mandat de prélèvement à OWNER et ADMIN. En
   production, la préparation des déclarations est fermée tant que
   `FISCAL_DECLARATIONS_ENABLED` n'est pas activé (`teledeclaration.declarations_open`).
   Les saisies de montants refusent les valeurs non finies ou démesurées (`form_float`).
5. **CSRF** sur tous les POST, en-têtes de sécurité et CSP stricte, limitation de débit
   (Flask-Limiter + Redis).

## Logique métier (modules principaux)

| Domaine | Modules |
| --- | --- |
| Facturation | `invoice_amounts`, `invoice_numbering`, `invoice_compliance`, `invoice_pdf`, `receivables`, `sepa` |
| Facture électronique | `weinvoice`, `einvoice_validation`, `ereporting` |
| Achats, banque | `expenses`, `document_extraction`, `bank_import`, `bank_reconciliation`, `cloud_storage` |
| Trésorerie | `treasury`, `loans`, `finance_leases` |
| Comptabilité | `accounting` (écritures, immobilisations, cessions), `ledger` (exercices, balance, clôture), `reviews`, `plaquette` |
| Liasses | `liasse` (2033-A/B), `liasse_2033_annexes` (2033-B fiscal à 2033-G), `liasse_normal` (2050 à 2059-E), `liasse_annexes` (2054 à 2056 codés, 2059-A à G, 2065) |
| Impôts | `vat_return` (CA3, CA12), `corporate_tax` (IS, CVAE, calendrier, profil fiscal, mandat), `tiers_declarations` (DAS2, 2777, IFU), `local_taxes` (CFE, DECLOYER) |
| Déclarations | `teledeclaration` (contenu canonique, contrôles, canaux EFI / EDI, dépôt manuel, contrôle de l'accusé, connecteurs EDI), `declaration_pdf` (récapitulatif), `automation` (échéancier, retards, écritures récurrentes, contrôles avant liasse) |
| Transverse | `entities`, `fr_identifiers` (SIREN, IBAN…), `gdpr_export`, `retention`, `crypto`, `safe_http`, `webhooks_outbound` |

Le fonctionnement fiscal est détaillé dans
[`docs/production-reglementaire.md`](docs/production-reglementaire.md).

## Tâches planifiées

`jobs.py` déclare les tâches (`@job(nom, cadence, heure)`), exécutées pour chaque
organisation payante concernée, de façon idempotente (`job_runs`). Le cron Render lance
`scripts/run_job.py --due` chaque heure ; les tâches qui écrivent sur le disque sont
déclenchées sur le service web. Liste et horaires : `DEPLOIEMENT_PRODUCTION.md`, section 4.

## Dates et montants

- La date du jour se prend avec `accounting.paris_today()` (fuseau Europe/Paris), jamais
  `date.today()` (test de garde).
- Les saisies françaises (« 1 234,56 ») passent par `runtime.form_float` ; les montants des
  déclarations sont arrondis à l'euro au moment de la préparation.

## Tests

`pytest -q`. Les tests créent des bases temporaires (fixtures `tmp_path`). Les vérifications
PostgreSQL se font contre une base réelle (`DATABASE_URL`), voir `MIGRATION_POSTGRES.md`.
