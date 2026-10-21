# ProfitOS — Production checklist

Procédure détaillée : `DEPLOIEMENT_PRODUCTION.md`.

- [ ] `python scripts/go_live_check.py --network` : 0 point bloquant (services en mode production, voir `docs/mise-en-vente.md`)
- [ ] `/healthz` = 200
- [ ] `/readyz` = 200 et database=postgresql
- [ ] `APP_BASE_URL=https://app.profitos.fr` (domaine personnalisé actif, TLS OK)
- [ ] `PROFITOS_SECRET_KEY` >= 32 caractères (groupe `profitos-shared`)
- [ ] disque `profitos-data` monté, `PROFITOS_UPLOAD_DIR=/var/data/uploads`
- [ ] `RESEND_API_KEY` défini, domaine vérifié (SPF, DKIM, DMARC)
- [ ] test signup/login/logout, email de vérification reçu
- [ ] test mot de passe oublié (email reçu)
- [ ] test 2 organisations : aucune fuite de données
- [ ] test upload CSV/XLSX valide, extension interdite refusée
- [ ] facture d'achat PDF toujours présente après un redéploiement
- [ ] cron `profitos-jobs` : dernière exécution réussie
- [ ] `BACKUP_PASSPHRASE` + `BACKUP_S3_*` configurés, première sauvegarde présente
- [ ] PITR PostgreSQL actif ; exercice de restauration fait (`RESTORE.md`)
- [ ] sonde externe sur `/readyz`, `SENTRY_DSN` configuré
- [ ] vérifier CSP/HSTS/Secure cookie dans le navigateur
- [ ] déclarations fiscales : préparation, récapitulatif PDF, accusé archivé conservé après redéploiement
- [ ] `EDI_PARTNER_ENABLED=0` tant qu'aucun partenaire EDI n'est contractualisé ; `EDI_SIMULATION_ENABLED` absent
- [ ] calculs fiscaux validés par un expert-comptable sur trois dossiers réels (grille de `docs/validation-fiscale.md`) avant `FISCAL_DECLARATIONS_ENABLED=1`
- [ ] dépôt GitHub : projet à la racine, CI verte avant déploiement
