# ProfitOS — Liste de contrôle avant la mise en vente

État au 20 octobre 2026. Chaque ligne indique la preuve disponible et ce qui reste à faire.
Une case n'est cochée que lorsque la preuve existe.

| # | Condition | État | Preuve | Reste à faire |
| --- | --- | --- | --- | --- |
| 1 | Tous les tests critiques passent | ☑ | Suite complète verte sur SQLite et sur PostgreSQL réel (`PROFITOS_TEST_DATABASE_URL`, base vide à chaque test ; 42 tests propres à SQLite ignorés, sans erreur de l'application) ; parcours navigateur v461 (fiscalité) et v462 (facturation, banque, clôture, FEC) verts sur PostgreSQL | Relancer les parcours navigateur avant chaque mise en production importante (commande au § 6 de `validation-fiscale.md`) |
| 2 | Calculs fiscaux validés par un professionnel | ☐ | 8 dossiers de référence calculés à la main (`validation-fiscale.md`, § 4) | Grille du § 7 remplie et signée par un expert-comptable sur 3 dossiers réels ; ensuite seulement `FISCAL_DECLARATIONS_ENABLED=1` |
| 3 | Aucune fuite de données entre entreprises | ☑ (technique) | `test_v467_isolation_audit` : toutes les routes GET et POST parcourues avec les identifiants d'une autre entreprise, sur SQLite et PostgreSQL ; test capable de détecter une fuite (vérifié en en introduisant une) | Un test d'intrusion externe reste recommandé avant d'ouvrir à un grand nombre de clients |
| 4 | Sauvegardes et restauration testées | ☑ (local) / ☐ (Render) | `test_v406` (SQLite) et `test_v467_restauration_postgresql` : pg_dump chiffré restauré dans une base neuve, toutes les tables identiques ligne pour ligne, documents identiques à l'octet | Faire le même exercice sur Render : restaurer la dernière sauvegarde dans une base PostgreSQL de test (`RESTORE.md`) et noter la date et la durée |
| 5 | Facturation et paiements clients fonctionnels | ◐ | Factures, émission, PDF, règlement par rapprochement bancaire testés de bout en bout (v462) ; abonnements Stripe testés en mode test | Passer Stripe en mode réel et faire un vrai paiement de 1 € remboursé ; passer WeInvoice en production (accès de production nécessaires) |
| 6 | CGU, confidentialité et contrats validés | ◐ | Mentions légales, confidentialité, CGU, CGV avec accord de traitement (RGPD art. 28) et clause sur les déclarations fiscales publiés | Relecture par un avocat ; compléter les mentions de l'éditeur (SIREN, siège, directeur de la publication) dès l'immatriculation |
| 7 | Un client pilote a terminé un cycle réel | ☐ | Guide du pilote : `docs/pilote.md` | Recruter un pilote et suivre un cycle complet (au moins un mois de TVA et une clôture mensuelle) |

## Contrôle sur Render : `scripts/go_live_check.py`

Dans le shell du service web Render :

```bash
python scripts/go_live_check.py            # configuration
python scripts/go_live_check.py --network  # + connexions réelles, en lecture seule
```

Le script vérifie, sans rien créer ni afficher de secret, que chaque service est en mode
production et répond :

| Service | Vérifié |
| --- | --- |
| WeInvoice (facture électronique, v465) | `WEINVOICE_ENV=production`, deux paires de clés, secrets des notifications ; jeton obtenu pour chaque paire |
| Swan / Powens (banque, v466) | environnement live, secret des notifications, adresse de retour OAuth sous `APP_BASE_URL` ; jeton Swan obtenu |
| Stripe (abonnements) | clé `sk_live_`, secret du webhook, 4 tarifs ; compte autorisé à encaisser, tarifs actifs et du même mode |
| Resend (e-mails) | fournisseur configuré, domaine d'expédition réel |
| Sauvegardes (v467) | phrase de passe, copie S3, dernière sauvegarde réussie de moins de 26 h |
| Mise en vente (v468) | mentions légales, mode pré-lancement, ouverture du module fiscal, simulation EDI absente |

Code de sortie 1 tant qu'il reste un point bloquant. Ce contrôle ne remplace pas un essai
réel : une facture électronique envoyée à un destinataire de test en production, une
synchronisation bancaire sur un vrai compte, un paiement Stripe de 1 € remboursé.

## Ordre recommandé

1. Points 2, 6 et 7 en parallèle : ils dépendent de tiers et prennent du temps.
2. Point 4 sur Render dès maintenant (une heure).
3. Point 5 : Stripe réel et WeInvoice production quand l'entreprise est immatriculée.
4. Ouverture progressive : pilote(s) via `FISCAL_DECLARATIONS_PILOT_ORGS`, puis tous les clients.
