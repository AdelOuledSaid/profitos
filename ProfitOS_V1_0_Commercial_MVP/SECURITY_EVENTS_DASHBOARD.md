# ProfitOS V1.3.5 — Security Events Dashboard

URL:
`/settings/security-events`

Sécurité:
- accessible uniquement après authentification ;
- rôle OWNER obligatoire ;
- requête limitée à l'organisation active ;
- maximum 200 événements affichés ;
- aucune IP brute, aucun hash d'IP, aucun User-Agent, token, mot de passe ou secret affiché.

Migration:
`ensure_security_events_table()` crée la table de manière idempotente dans la
base AUTH réellement utilisée par l'instance. Elle est appelée au démarrage,
avant chaque écriture d'événement, et avant l'affichage du dashboard.

Test de production:
1. ouvrir Settings > Journal de sécurité ;
2. faire une tentative de connexion avec un mauvais mot de passe ;
3. faire une connexion correcte ;
4. revenir au journal ;
5. vérifier LOGIN_FAILED / FAILURE et LOGIN_SUCCESS / SUCCESS.
