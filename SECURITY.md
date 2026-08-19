# ProfitOS V1.3 — Security baseline

## Protections actives
- Cookies de session HttpOnly, SameSite=Lax et Secure en production.
- Durée de session limitée (12 h par défaut).
- CSRF sur toutes les mutations hors webhook Stripe signé.
- Vérification same-origin supplémentaire en production.
- Headers : CSP, HSTS, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy.
- Rate limiting pilote sur login/signup/reset/invitations/DCE.
- Anti open-redirect sur `next`.
- Uploads : extensions allowlistées, nom aléatoire, isolation par organisation, validation de signature, suppression après traitement.
- Webhooks Slack/Teams allowlistés en HTTPS pour réduire le risque SSRF.
- `/readyz` n'expose plus les détails d'erreur de connexion.
- Request ID sur chaque réponse et erreurs 500.

## À faire avant clients payants
- Passer PostgreSQL Render à un plan avec sauvegardes.
- Stockage objet privé (S3/R2) si conservation des fichiers bruts nécessaire.
- Redis + Flask-Limiter pour rate limiting partagé si plusieurs workers/instances.
- SMTP transactionnel et SPF/DKIM/DMARC.
- Monitoring/Sentry et alertes uptime.
- Revue de sécurité externe, CGU, politique de confidentialité et DPA/RGPD.
