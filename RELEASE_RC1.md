# ProfitOS v1.0 RC1

Release Candidate destinée aux premiers pilotes.

Cette archive de déploiement exclut volontairement les environnements virtuels, dépôts Git, caches, bases locales, sauvegardes, uploads utilisateurs et fichiers `.env` contenant potentiellement des secrets.

Avant ouverture pilote : exécuter la suite complète `pytest -v` sur le projet de travail, déployer sur Render, puis effectuer les tests de fumée listés dans `RENDER_RC1_CHECKLIST.md`.
