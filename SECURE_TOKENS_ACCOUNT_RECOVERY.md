# ProfitOS V1.3.3 — Secure Tokens & Account Recovery

## Changements

### Tokens non stockés en clair
Les liens d'email contiennent toujours un token aléatoire, mais PostgreSQL/SQLite
ne stocke désormais que son empreinte SHA-256.

Une fuite de base de données ne suffit donc plus à utiliser directement un lien
de vérification ou de réinitialisation encore actif.

Les colonnes historiques `verification_token` et `reset_token` sont conservées
pour éviter une migration de schéma risquée ; leur contenu est désormais un hash.

### Compatibilité V1.3.2
Pendant cette version, ProfitOS accepte aussi les anciens tokens plaintext déjà
émis avant le déploiement. Dès qu'un nouveau token est généré, il remplace
l'ancien par un hash.

### Expiration
- Vérification email : 24 heures
- Reset mot de passe : 1 heure
- Invitation équipe : 7 jours

### Usage unique
Vérification et reset consomment le token atomiquement.
Un second clic après consommation échoue.

### Invalidation des anciennes sessions
La table `users` possède désormais `auth_version`.
Après un changement de mot de passe :
- `auth_version` est incrémenté ;
- toutes les anciennes sessions deviennent invalides ;
- l'utilisateur doit se reconnecter.

Les sessions ouvertes avant le déploiement V1.3.3 sont migrées doucement :
leur version est initialisée lors de la première requête.

## Tests de production recommandés

1. Mot de passe oublié
2. Réception de l'email Resend
3. Utiliser le lien une première fois : succès
4. Réutiliser exactement le même lien : échec
5. Vérifier qu'un autre navigateur déjà connecté est déconnecté à sa prochaine requête
6. Vérification email : nouveau lien valable, réutilisation impossible
