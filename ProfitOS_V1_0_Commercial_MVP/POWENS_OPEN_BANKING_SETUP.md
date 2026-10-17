# ProfitOS — Open Banking Powens V1

Variables Render à ajouter :
- `POWENS_DOMAIN` : sous-domaine Powens, sans `.biapi.pro`
- `POWENS_CLIENT_ID`
- `POWENS_CLIENT_SECRET`

Dans la console Powens, autoriser comme callback :
`https://app.profitos.fr/banking/callback`

Le module ne stocke pas le token bancaire permanent dans la base ProfitOS.
Il conserve uniquement l'identifiant utilisateur Powens et génère un jeton côté serveur lorsque nécessaire.

Par sécurité, le solde bancaire synchronisé n'écrase pas automatiquement le solde utilisé par les moteurs financiers.
L'utilisateur doit cliquer sur « Utiliser ces soldes dans le pilotage ».
