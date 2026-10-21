# ProfitOS

Logiciel de gestion financière en ligne pour les TPE, PME, indépendants et cabinets
d'expertise comptable : facturation et facture électronique, achats, banque, trésorerie,
comptabilité complète et production fiscale (TVA, liasses, impôt sur les sociétés,
déclarations annuelles), en français et selon les règles fiscales françaises.

Application Flask (Python 3.12), SQLite en développement, PostgreSQL en production
(un schéma par organisation), déployée sur Render (`app.profitos.fr`).

## Modules

| Module | Contenu |
| --- | --- |
| Facturation | Clients, devis, factures, acomptes, avoirs, bons de livraison, factures récurrentes, paiements, QR code de virement et lien de paiement, relances automatiques par étapes |
| Facture électronique | Factur-X, envoi et réception par plateforme agréée (WeInvoice), statuts, e-reporting |
| Achats et dépenses | Factures fournisseurs (dépôt, boîte mail, dossiers cloud), notes de frais, budgets |
| Banque | Connexion bancaire ou import de relevés, rapprochement, catégorisation, état de rapprochement |
| Trésorerie | Prévision à 90 jours, scénarios, plan mensuel, simulateur de décision, diagnostic |
| Veille de secteur | Indicateurs officiels (INSEE, BCE) choisis selon l'activité de l'entreprise, alertes, révision de prix, actualités |
| Comptabilité | Saisie, journaux, grand livre, balance, immobilisations (dotations, cessions), emprunts, crédit-bail, paie importée, cut-off, analytique, clôture, plaquette, FEC |
| Fiscalité | TVA CA3 et CA12 (acomptes 3514), liasses 2033-A à G et 2050 à 2059-G avec 2065, IS et CVAE, DAS2, prélèvements sur dividendes (2777) et IFU, CFE, DECLOYER, profil fiscal, échéancier et relances |
| Déclarations fiscales | Préparation contrôlée, récapitulatif PDF, dépôt par l'entreprise sur impots.gouv.fr, archivage et contrôle automatique de l'accusé ; transmission EDI dès qu'un partenaire est connecté |
| Multi-sociétés et cabinets | Entités, consolidation, portefeuille clients, révision comptable, accès expert-comptable |

Fonctionnement des déclarations, règles fiscales appliquées et limites :
[`docs/production-reglementaire.md`](docs/production-reglementaire.md). Validation des calculs
sur dossiers de référence et grille pour l'expert-comptable :
[`docs/validation-fiscale.md`](docs/validation-fiscale.md) (la préparation des déclarations
reste fermée aux clients tant qu'elle n'est pas signée).
Mode d'emploi pour les clients : [`docs/guide-utilisateur.md`](docs/guide-utilisateur.md)
(publié dans l'application, menu Aide). Avant la mise en vente :
[`docs/mise-en-vente.md`](docs/mise-en-vente.md) (liste de contrôle et preuves) et
[`docs/pilote.md`](docs/pilote.md) (programme pilote).

## Démarrage local

```bash
python -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # puis renseigner au minimum PROFITOS_SECRET_KEY
python app.py                     # http://127.0.0.1:5050
```

Sous Windows : `LANCER_PROFITOS.bat` (développement) ou `RUN_PRODUCTION_WINDOWS.bat`
(serveur Waitress).

## Tests

```bash
pytest -q
```

Environ 1 550 tests (sécurité, isolation des organisations, comptabilité, fiscalité,
déclarations, tâches planifiées). La CI GitHub (`.github/workflows/tests.yml`) les exécute à
chaque push ; Render ne déploie que si elle est verte. Les parcours dans un vrai
navigateur et l'exercice de restauration PostgreSQL se lancent à part (voir
`docs/validation-fiscale.md`, § 6). L'isolation entre entreprises est auditée route par
route par `tests/test_v467_isolation_audit.py`.

Le dépôt GitHub doit contenir le projet **à sa racine** (dossiers `profitos/`, `templates/`,
`tests/`… directement sous la racine). Un second exemplaire dans un sous-dossier fait échouer
la CI (« import file mismatch »).

## Production

- Déploiement Render, variables, tâches planifiées, vérifications :
  [`DEPLOIEMENT_PRODUCTION.md`](DEPLOIEMENT_PRODUCTION.md)
- Liste de contrôle avant mise en ligne : [`PRODUCTION_CHECKLIST.md`](PRODUCTION_CHECKLIST.md)
- Sauvegardes et restauration : [`RESTORE.md`](RESTORE.md),
  [`PRODUCTION_MONITORING_BACKUP.md`](PRODUCTION_MONITORING_BACKUP.md)
- PostgreSQL : [`MIGRATION_POSTGRES.md`](MIGRATION_POSTGRES.md)
- Paiements et e-mails : [`STRIPE_SETUP.md`](STRIPE_SETUP.md), [`RESEND_SETUP.md`](RESEND_SETUP.md)
- Banque : [`POWENS_OPEN_BANKING_SETUP.md`](POWENS_OPEN_BANKING_SETUP.md)

Points de santé : `GET /healthz` (processus), `GET /readyz` (base, disque, e-mail).

## Sécurité

[`SECURITY.md`](SECURITY.md) (vue d'ensemble), [`AUTHORIZATION_HARDENING.md`](AUTHORIZATION_HARDENING.md),
[`CSP_XSS_HARDENING.md`](CSP_XSS_HARDENING.md), [`RATE_LIMITING_REDIS.md`](RATE_LIMITING_REDIS.md),
[`SECURE_TOKENS_ACCOUNT_RECOVERY.md`](SECURE_TOKENS_ACCOUNT_RECOVERY.md),
[`SECURITY_AUDIT_LOGGING.md`](SECURITY_AUDIT_LOGGING.md),
[`SECURITY_EVENTS_DASHBOARD.md`](SECURITY_EVENTS_DASHBOARD.md).

## Architecture

[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Historique

Les fichiers `V1_*.md`, `RELEASE_RC1.md` et `RENDER_RC1_CHECKLIST.md` décrivent des versions
antérieures et sont conservés pour mémoire. Les nouveautés récentes sont publiées dans
l'application (menu Nouveautés).
