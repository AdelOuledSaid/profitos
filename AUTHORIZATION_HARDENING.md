# ProfitOS V1.3.1 — Authorization & Session Hardening

## Changements

- La base `memberships` est désormais la source de vérité des rôles à chaque requête protégée.
- Une rétrogradation ou suppression d’un membre prend effet dès la requête suivante, sans attendre une reconnexion.
- Si l’accès à l’organisation active est supprimé mais que l’utilisateur appartient encore à une autre organisation, ProfitOS bascule vers une organisation encore autorisée.
- Les actions Stripe Checkout / Customer Portal exigent explicitement la permission `billing`.
- La modification du profil entreprise exige OWNER ou ADMIN.
- La synchronisation BOAMP passe de GET à POST avec CSRF et rate limiting.
- La déconnexion passe de GET à POST avec CSRF.

## Tests manuels recommandés

1. Connecter deux comptes à la même organisation.
2. Avec OWNER, passer l’autre compte de ADMIN à COMMERCIAL.
3. Sans reconnecter le second compte, actualiser Settings : l’accès doit être refusé immédiatement.
4. Retirer le second compte de l’organisation et vérifier que l’accès cesse à la requête suivante.
5. Vérifier `Actualiser BOAMP`, Logout et Stripe depuis les rôles autorisés/non autorisés.
