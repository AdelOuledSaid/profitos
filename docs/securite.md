# Dossier sécurité ProfitOS

Ce document décrit les mesures de sécurité indispensables avant d'accueillir des clients réels. Pour chaque contrôle, il indique où il est mis en œuvre et quel test automatisé le prouve. Il est destiné à l'exploitant, à un auditeur ou à un client qui demande des garanties. Les points marqués **Externe** ne peuvent pas être prouvés par le code seul.

## 1. Contrôle avant ouverture

Dans le shell du service web Render, lancer dans cet ordre :

```
python scripts/cloud_preflight.py   # configuration bloquante
python scripts/go_live_check.py     # services tiers, mentions légales, sauvegardes
python scripts/security_check.py    # sécurité (ce dossier)
```

`security_check.py` fonctionne en lecture seule et n'affiche aucun secret ni aucune adresse. Il sort avec le code 1 s'il trouve un point bloquant. Il vérifie :

- les secrets et le chiffrement : la clé de session et la clé de chiffrement des données sont distinctes, le mode production, HTTPS et PostgreSQL ;
- les en-têtes HTTP (CSP, HSTS, X-Frame-Options, nosniff, Referrer-Policy) et le cookie de session (`__Host-`, Secure, HttpOnly, SameSite=Lax) ;
- l'absence de mise en cache (`no-store`) des liens secrets : factures, devis et missions d'experts ;
- le fait que les documents téléversés et les fichiers internes ne sont pas servis publiquement ;
- le limiteur de requêtes, qui doit être partagé entre instances (`REDIS_URL`) ;
- la double authentification des propriétaires et des administrateurs. Elle est obligatoire pour les comptes d'administration plateforme (`PLATFORM_ADMIN_EMAILS`) ;
- le journal de sécurité, qui doit être alimenté ;
- les sauvegardes : phrase de passe, copie hors site S3, dernière sauvegarde réussie de moins de 26 h.

## 2. Matrice des contrôles

| Domaine | Contrôle | Mise en œuvre | Preuve |
|---|---|---|---|
| Isolation | Un schéma PostgreSQL par entreprise. La connexion est choisie par l'organisation de la session, jamais par un paramètre envoyé par le navigateur. | `profitos/db.py`, `runtime.tenant_cx` | `test_v467_isolation_audit.py` |
| Isolation | Cloisonnement par entité (multi-sociétés) : `entity_id IS ?` sur chaque requête | modules métier | `test_v274_lotA_dso_entity_isolation.py`, `test_v291_lotD_ecommerce_entity_isolation.py`, `test_v292_lotD_cloud_purchase_entity_isolation.py` |
| Permissions | Rôles OWNER / ADMIN / comptable / lecture, avec des zones fonctionnelles par rôle | `runtime.MODULE_AREAS` | `test_v456_role_areas_fiscal.py` |
| Permissions | Les validations d'achats sont réservées aux rôles habilités | `purchase_pipeline.VALIDATOR_ROLES` | `test_v486_chaine_achats.py` |
| Authentification | Mots de passe hachés, double authentification TOTP, verrouillage temporaire après 10 échecs, invalidation des sessions (`auth_version`) | `account_security.py` | `test_v414_account_security.py` |
| Authentification | Limitation des requêtes sur la connexion, l'inscription et la réinitialisation | limiteur (Redis en production) | `test_v420_security_finishing.py` |
| Requêtes | Jeton CSRF et contrôle d'origine sur toute requête qui modifie des données | `runtime.csrf_protect` | `test_v489_securite.py`, `test_v224_mobile_android_ios_bridge.py` |
| Liens secrets | Les jetons publics sont stockés uniquement sous forme hachée et ne sont jamais mis en cache | `_PUBLIC_TOKEN_PREFIXES` | `test_secure_tokens.py`, `test_v304_lotE_hashed_token_only.py`, `test_v489_securite.py` |
| Navigateur | CSP stricte (aucun script en ligne), HSTS, anti-iframe | `security_headers` | `test_smoke.py::test_security_headers`, `scripts/security_check.py` |
| Données sensibles | Jetons bancaires, cloud et e-commerce, et secrets 2FA chiffrés avec `PROFITOS_DATA_KEY` | `profitos/crypto.py` | `test_v417_secrets_encryption.py` |
| Traçabilité | Journal de sécurité : connexions, échecs, changements de rôle, exports. Les adresses IP sont hachées. | table `security_events` | `test_security_audit.py`, `scripts/check_security_events.py` |
| Traçabilité | Journal des actions métier : validations, rapprochements, missions | `purchase_pipeline_log`, `verification_items` | `test_v484_resolution.py`, `test_v486_chaine_achats.py` |
| Documents | Les pièces téléversées sont servies uniquement après contrôle d'accès, jamais en statique | routes de téléchargement | `test_v489_securite.py` |
| Sauvegarde | Sauvegarde quotidienne chiffrée, copie S3, rotation | `scripts/backup.py`, `backup_postgres.py` | `test_v406_backup_restore.py` |
| Restauration | La restauration refuse une mauvaise phrase de passe, une archive altérée ou une cible non vide | `scripts/restore.py` | `test_v406_backup_restore.py`, `test_v467_restauration_postgresql.py` |
| RGPD | Export et suppression des données d'un compte | `gdpr_export.py` | `test_v305_lotE_gdpr_deletion_consistency.py` |

## 3. Exercice de restauration (trimestriel)

Il faut le faire au moins une fois **avant le premier client payant**, puis chaque trimestre. Une sauvegarde jamais restaurée n'est pas une sauvegarde.

1. Créer une base PostgreSQL de test vide sur Render. Ne jamais utiliser la production.
2. Dans le shell, définir `RESTORE_DATABASE_URL` vers cette base. Définir `BACKUP_PASSPHRASE` si elle n'est pas déjà présente.
3. Lancer `python scripts/restore.py --from-s3 <horodatage> --work-dir /tmp/restore --target-db "$RESTORE_DATABASE_URL" --uploads-dir /tmp/restore/uploads`.
4. Vérifier sur la base restaurée : le nombre d'organisations, le nombre de factures de deux entreprises prises au hasard, et la présence de pièces jointes dans `/tmp/restore/uploads`.
5. Noter la date, l'horodatage restauré, la durée et les écarts dans le registre d'exploitation, puis supprimer la base de test.

L'objectif de reprise est le suivant : perte maximale de 24 h de données, remise en service en moins de 4 h.

## 4. Incident de sécurité

1. **Contenir.** Révoquer la session suspecte (l'incrément de `auth_version` déconnecte toutes les sessions de l'utilisateur). En cas de fuite d'un secret, le changer sur Render : `PROFITOS_SECRET_KEY` déconnecte tout le monde, et les jetons tiers se révoquent chez le fournisseur.
2. **Établir les faits.** Lancer `python scripts/check_security_events.py`, puis consulter les journaux Render et le journal des actions métier.
3. **Notifier.** Une violation de données personnelles se notifie à la CNIL dans les 72 h. Il faut prévenir les clients concernés si le risque est élevé, et informer les sous-traitants touchés.
4. **Corriger et tracer.** Ajouter un test qui reproduit la faille, livrer le correctif et compléter ce dossier.

## 5. Registre RGPD et sous-traitants

ProfitOS agit comme sous-traitant de ses clients pour leurs données comptables et commerciales. Il est responsable de traitement pour les comptes utilisateurs et la facturation.

### Sous-traitants

| Sous-traitant | Usage | Données | Localisation |
|---|---|---|---|
| Render | Hébergement de l'application, PostgreSQL, disque | toutes les données applicatives | région choisie à la création du service, à vérifier dans le tableau de bord |
| Stripe | Abonnements | identité de facturation | UE / États-Unis (clauses contractuelles types) |
| Resend | E-mails transactionnels | adresse e-mail, contenu des messages | à vérifier dans le DPA |
| WeInvoice | Plateforme agréée de facturation électronique | factures émises et reçues | France |
| Fournisseur bancaire (Swan ou Powens) | Lecture des comptes et des transactions | données bancaires | UE |
| Anthropic | Copilote et extraction de factures, sur demande de l'utilisateur uniquement | faits calculés du dossier, pièces envoyées à l'extraction | États-Unis (clauses contractuelles types) |
| AWS S3 | Copie hors site des sauvegardes chiffrées | sauvegardes chiffrées (illisibles sans la phrase de passe) | région du bucket |

Durées de conservation : 10 ans pour les pièces comptables (Code de commerce, art. L123-22). Les comptes supprimés sont effacés, à l'exception des obligations légales.

## 6. Points externes à faire valider

Ces points ne relèvent pas du code. Ils doivent être réalisés ou signés avant d'annoncer la conformité.

- **Test d'intrusion** par un prestataire indépendant (idéalement qualifié PASSI), sur l'environnement de production.
- **Restauration réelle sur Render** (section 3), avec un procès-verbal daté.
- **Accords de sous-traitance (DPA)** signés ou acceptés avec chaque sous-traitant ci-dessus. Les clauses contractuelles types doivent être vérifiées pour les transferts hors UE.
- **Registre des traitements** et **mentions d'information** relus par un juriste.
- **Revue des accès** : la liste des comptes d'administration plateforme et des secrets partagés doit être revue chaque trimestre.
