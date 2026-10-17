# ProfitOS V1.3.5 — Audit Logging & Security Events

V1.3.5 ajoute un journal de sécurité séparé de `activity_log`.

Événements couverts :
- connexion réussie et échouée ;
- déconnexion ;
- demande de réinitialisation ;
- reset réussi, invalide ou expiré ;
- vérification email réussie, invalide ou expirée ;
- invitation, changement de rôle et retrait d'un membre.

Données enregistrées :
- type d'événement et résultat ;
- user_id / organization_id quand connus ;
- cible technique non sensible ;
- empreinte SHA-256 pseudonymisée de l'IP avec `PROFITOS_SECRET_KEY` comme pepper ;
- User-Agent tronqué ;
- horodatage UTC.

Ne sont jamais journalisés :
- mots de passe ;
- tokens de reset/vérification ;
- clés API ;
- secrets ;
- IP brute.

Le journal de sécurité ne doit jamais empêcher une action métier : une panne du logging est absorbée.
