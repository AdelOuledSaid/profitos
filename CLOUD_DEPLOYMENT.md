# ProfitOS — Cloud Deployment

La cible de production est **Render** : procédure complète, variables et tâches planifiées
dans `DEPLOIEMENT_PRODUCTION.md`. Sauvegardes et restauration : `RESTORE.md`.

## Render (résumé)

1. New → Blueprint → dépôt GitHub : `render.yaml` crée le service web `profitos`
   (plan payant + disque persistant `/var/data`), le cron `profitos-jobs`, le Key Value et
   la base `profitos-db`.
2. Renseigner les variables `sync: false`.
3. Les migrations s'exécutent en `preDeployCommand` (`scripts/cloud_preflight.py --init-tenants`).
4. Health check : `/readyz`.

## Documents clients

Les documents (factures d'achat, justificatifs, documents cabinet) sont écrits dans
`PROFITOS_UPLOAD_DIR`, qui doit être un **disque persistant** (`/var/data/uploads` sur Render).
Ne jamais utiliser `/tmp` : les fichiers disparaîtraient à chaque redéploiement, et le
préflight refuse cette configuration en production. Un disque Render limite le service à une
instance et désactive les déploiements sans interruption ; un backend S3 est l'évolution
prévue (`profitos/storage.py`).

## Railway (alternative)

`railway.json` exécute le préflight en pre-deploy. Il faut ajouter un **volume** Railway et
pointer `PROFITOS_UPLOAD_DIR` dessus, puis créer un cron Railway lançant
`python scripts/run_job.py --due` toutes les heures.

## Vérifications après déploiement

Voir la section 6 de `DEPLOIEMENT_PRODUCTION.md`.
