# ProfitOS — Sauvegardes et restauration

## Ce qui est sauvegardé

| Élément | Mécanisme | Fréquence | Rétention |
|---|---|---|---|
| Base PostgreSQL (auth + un schéma `org_N` par client) | PITR Render (plans payants) | continu | selon plan (à activer, voir plus bas) |
| Base PostgreSQL | `scripts/backup.py` → `pg_dump --format=custom`, chiffré | quotidien (tâche `backup`, 2 h Paris) | `BACKUP_KEEP` local (7) ou bucket S3 (cycle de vie du bucket) |
| Documents clients (`PROFITOS_UPLOAD_DIR`, disque persistant) | `scripts/backup.py` → `tar.gz`, chiffré | quotidien | idem |
| Disque persistant Render | instantanés automatiques Render | quotidien | 7 jours (restauration depuis le tableau de bord) |

Chiffrement : `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256`, phrase secrète
`BACKUP_PASSPHRASE` (16 caractères minimum). **Conservez cette phrase hors de Render**
(gestionnaire de mots de passe de l'entreprise) : sans elle, aucune sauvegarde n'est lisible.

Chaque sauvegarde produit :
`profitos_<horodatage>.pgdump.enc` (ou `.sqlite.tar.gz.enc` en dev), `profitos_<horodatage>.uploads.tar.gz.enc`
et `profitos_<horodatage>.manifest.json` (empreintes SHA-256, vérifiées avant toute restauration).

> Sans `BACKUP_S3_*`, les sauvegardes restent sur le **même disque** que les documents
> (`/var/data/backups`) : cela protège contre une erreur applicative, **pas** contre la perte
> du disque ou du compte. Configurez un bucket S3 compatible (AWS S3 Paris, Scaleway,
> OVHcloud, Cloudflare R2…) dans une autre organisation/compte, avec versionnage et une règle
> de cycle de vie (ex. 35 jours).

## Activer le PITR PostgreSQL (Render)

Render Postgres sur un plan payant propose la restauration à un instant donné (Point-in-Time
Recovery). Tableau de bord Render → base `profitos-db` → **Recovery** : vérifier que le PITR
est actif et noter la fenêtre de rétention du plan. Une restauration PITR crée une **nouvelle**
base : il faut ensuite repointer `DATABASE_URL` (groupe `profitos-shared`) et redéployer.

## Restaurer

Prérequis : `BACKUP_PASSPHRASE` dans l'environnement, `openssl`, `pg_restore` de même version
majeure que le serveur.

1. **Choisir la sauvegarde** : `ls /var/data/backups` (Shell du service web) ou le bucket.
2. **Restaurer d'abord dans une base vide de test**, jamais directement en production :
   ```bash
   python scripts/restore.py --from-s3 20261009T000000Z --work-dir /var/data/restore \
       --target-db "$RESTORE_DATABASE_URL" --uploads-dir /var/data/restore/uploads
   # ou, sauvegarde locale :
   python scripts/restore.py --manifest /var/data/backups/profitos_20261009T000000Z.manifest.json \
       --target-db "$RESTORE_DATABASE_URL" --uploads-dir /var/data/restore/uploads
   ```
   Le script vérifie les empreintes SHA-256, déchiffre dans un répertoire temporaire, lance
   `pg_restore --clean --if-exists --no-owner` et extrait les documents (extraction sûre, sans
   liens ni chemins sortants). Il refuse une cible non vide et la base `DATABASE_URL` sans `--yes`.
3. **Vérifier** : `python scripts/cloud_preflight.py --init-tenants` avec `DATABASE_URL` pointant
   sur la base restaurée, puis connexion à un compte de test, contrôle d'une facture et d'un justificatif.
4. **Basculer** : mettre l'application en maintenance (suspendre le service web), restaurer
   en production (`--yes`) ou repointer `DATABASE_URL`, remplacer `/var/data/uploads` par le
   répertoire restauré, relancer le service.

## Exercice de restauration (à faire chaque trimestre)

- Test automatisé : `tests/test_v406_backup_restore.py` fait un aller-retour complet
  sauvegarde → chiffrement → restauration (chemin SQLite), et vérifie le refus d'une phrase
  secrète erronée, d'une archive altérée et d'une cible non vide.
- Exercice réel : créer une base Render temporaire, restaurer la sauvegarde de la veille
  (étape 2), vérifier (étape 3), noter la durée totale (objectif RTO < 2 h) et l'âge de la
  sauvegarde (RPO ≤ 24 h, ou quelques minutes avec le PITR), puis supprimer la base temporaire.
