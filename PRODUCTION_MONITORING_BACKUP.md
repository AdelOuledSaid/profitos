# ProfitOS V1.3.6 — Production Monitoring & Backup Readiness

## Monitoring
Nouveau endpoint :
`GET /ops/health`

Il expose uniquement des états non sensibles :
- version ;
- PostgreSQL configuré / disponible ;
- Redis configuré ;
- Email configuré.

Aucune URL de connexion, clé API ou credential n'est retourné.

Chaque réponse contient :
- `X-Request-ID`
- `X-Response-Time-Ms`

Les requêtes >= 1500 ms et les réponses HTTP 5xx génèrent un log structuré `OPS`
dans Render, corrélable avec le `request_id`.

## Sauvegarde PostgreSQL
Script :
`python scripts/backup_postgres.py`

Il utilise `pg_dump` en format custom et crée :
`backups/profitos_YYYYMMDDTHHMMSSZ.dump`

Le dossier `backups/` et les fichiers `.dump` sont exclus de Git.

## Vérification d'une sauvegarde
`python scripts/check_backup.py backups/<fichier.dump>`

Le script utilise `pg_restore --list` pour vérifier que l'archive est lisible.

## Recommandation production
Une sauvegarde n'est réellement validée qu'après un test de restauration sur une
base séparée. Ne jamais restaurer un test sur la base de production active.

## Tests après déploiement
1. `curl https://app.profitos.fr/healthz`
2. `curl https://app.profitos.fr/ops/health`
3. `curl -I https://app.profitos.fr/login` et vérifier `X-Response-Time-Ms`
4. vérifier l'absence de 5xx dans les logs Render
5. créer une sauvegarde locale et la vérifier avec `check_backup.py`
