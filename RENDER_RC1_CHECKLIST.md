# ProfitOS v1.0 RC1 — Checklist Render

## Obligatoire
- `PROFITOS_ENV=production`
- `PROFITOS_SECRET_KEY` : secret long et aléatoire
- `DATABASE_URL` : PostgreSQL de production
- `APP_BASE_URL=https://app.profitos.fr`

## Infrastructure recommandée
- `REDIS_URL` si Redis est activé pour le rate limiting / services associés
- `PROFITOS_UPLOAD_DIR` vers un stockage persistant si des uploads locaux sont utilisés

## Intégrations — configurer uniquement si utilisées
- Powens : `POWENS_DOMAIN`, `POWENS_CLIENT_ID`, `POWENS_CLIENT_SECRET`, `POWENS_REDIRECT_URI`
- Stripe : `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` et les `STRIPE_PRICE_*`
- WeInvoice : variables `WEINVOICE_*`
- Resend/SMTP : `RESEND_*` ou `SMTP_*`
- GoCardless : `GOCARDLESS_*`
- Swan : `SWAN_*`
- Google Drive / Dropbox : identifiants OAuth correspondants
- Twilio : `TWILIO_*`

## Boîte fournisseurs
Elle reste désactivée par défaut. Ne mettre `SUPPLIER_INBOX_WEBHOOK_ENABLED` à vrai que lorsque le webhook est réellement configuré et protégé. Définir alors `SUPPLIER_INBOX_WEBHOOK_SECRET` avec un secret serveur robuste (au moins 32 caractères). La validation native de signature du fournisseur d'e-mail doit être ajoutée/configurée selon le prestataire retenu.

## Vérifications après déploiement
- connexion / déconnexion
- tableau de bord
- changement d'entité
- factures clients et fournisseurs
- comptabilité / TVA
- banque / Powens si activé
- absence d'erreur 500 dans les logs Render
