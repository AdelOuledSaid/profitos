# ProfitOS V1.3.2 — Rate Limiting + Render Key Value

## Objectif

Remplacer le compteur local en mémoire par **Flask-Limiter** avec un stockage partagé **Render Key Value** (Valkey, compatible Redis).

## Pourquoi

L'ancien compteur était propre à chaque processus et disparaissait à chaque redémarrage. Avec `REDIS_URL`, tous les workers/instances partagent le même état de limitation.

## Render

Le `render.yaml` crée une ressource :

- `profitos-rate-limit`
- type `keyvalue`
- plan `free` pour le pilote
- région `frankfurt`
- accès public bloqué (`ipAllowList: []`)
- persistance désactivée, ce qui convient à un cache de rate limits

`REDIS_URL` est injectée automatiquement depuis le `connectionString` privé du Key Value.

## Limites conservées

Les limites historiques ProfitOS restent identiques et ne concernent que les POST, notamment login, signup, forgot/reset password, DCE et synchronisations coûteuses.

## Fallback

`RATELIMIT_IN_MEMORY_FALLBACK_ENABLED=True` permet de conserver une protection locale si Key Value est temporairement indisponible.

## Vérifications après déploiement

1. `/readyz` doit rester HTTP 200.
2. Les logs doivent contenir `Rate limiter initialized backend=render-key-value`.
3. Une route limitée doit renvoyer HTTP 429 après dépassement.
4. La réponse 429 doit contenir `Retry-After` et les headers `X-RateLimit-*`.
5. Render doit afficher la ressource `profitos-rate-limit` en état Available/Live.

## Note production

Le Key Value gratuit de Render ne persiste pas ses données et est destiné au test/pilote. Pour le rate limiting, la non-persistance n'est pas un problème fonctionnel majeur, mais avant une commercialisation à grande échelle il est recommandé de passer à une instance payante.
