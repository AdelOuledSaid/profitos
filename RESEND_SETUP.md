# ProfitOS — Resend

Variables Render nécessaires :

- `RESEND_API_KEY` : secret défini manuellement dans Render, jamais dans Git.
- `RESEND_FROM_EMAIL=ProfitOS <noreply@profitos.fr>`
- `APP_BASE_URL=https://app.profitos.fr`

Le domaine `profitos.fr` doit être vérifié dans Resend (DKIM + SPF).

Parcours à tester après déploiement :

1. Créer un nouveau compte avec une adresse email accessible.
2. Vérifier que l'email « Confirm your ProfitOS account » arrive.
3. Cliquer sur le bouton de vérification.
4. Tester « Mot de passe oublié ».
5. Vérifier les logs Resend en cas d'échec.
