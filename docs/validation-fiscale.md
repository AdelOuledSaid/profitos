# ProfitOS — Validation des calculs fiscaux

Ce document décrit comment les calculs fiscaux de ProfitOS sont validés avant d'ouvrir la
préparation des déclarations aux clients, et sert de **grille de validation** pour
l'expert-comptable.

## 1. Règle d'ouverture

En production, la préparation des déclarations, l'archivage des accusés et la confirmation
des dépôts sont **fermés par défaut**. Les montants restent consultables, signalés comme
indicatifs. On ouvre :

- à des organisations pilotes : `FISCAL_DECLARATIONS_PILOT_ORGS=12,34` (identifiants) ;
- à tous les clients : `FISCAL_DECLARATIONS_ENABLED=1`, **seulement** une fois la grille du
  paragraphe 6 signée par un expert-comptable.

## 2. Statuts : aucune acceptation sans accusé EDI

Tests `tests/test_v457_statuts_non_acceptes.py` :

- un dépôt fait par l'entreprise ne peut prendre que les statuts « préparée », « dépôt
  déclaré », « justificatif à vérifier » ou « dépôt confirmé », quel que soit le résultat
  du contrôle de l'accusé et même après confirmation manuelle ;
- ni l'actualisation du statut ni la tâche horaire ne font avancer un dépôt manuel ;
- aucune route ni tâche ne modifie directement la table des déclarations, et aucun
  formulaire ne peut imposer un statut ;
- les libellés de ces statuts ne mentionnent jamais une acceptation, et les pages
  rappellent que seul un accusé EDI la constate ;
- un dépôt **simulé** est toujours affiché « (simulation) — rien n'a été transmis », ne
  compte jamais comme un dépôt réel (échéancier, relances, doublons) et n'empêche pas de
  préparer la vraie déclaration ;
- seul un connecteur de partenaire EDI réel (mode production) peut produire le statut
  « Acceptée par l'administration (accusé EDI) ».

## 3. Sécurité du module (v459)

Tests `tests/test_v459_securite_fiscale.py` (chaque protection a été retirée une à une pour
vérifier qu'un test échoue) :

| Contrôle | Garantie |
| --- | --- |
| Module fermé | préparation, dépôt, archivage, confirmation et actualisation refusés en production sans `FISCAL_DECLARATIONS_ENABLED` (ou hors des organisations pilotes) ; aucun rappel fiscal envoyé |
| Routes et API | toutes les pages fiscales exigent une connexion et sont rattachées au contrôle des rôles ; aucune route d'API par jeton n'expose les déclarations ; jeton CSRF exigé sur chaque envoi |
| Isolation | une autre entreprise, ou une autre entité de la même organisation, obtient « introuvable » sur les déclarations, PDF, contenus et justificatifs, en lecture comme en écriture |
| Autorisations | profil fiscal, paramètres d'impôt et écriture d'IS, DAS2, IFU, 2777, CFE et déclarations réservés au propriétaire, à l'administrateur et au comptable ; mandat de prélèvement au propriétaire et à l'administrateur ; IBAN masqué dans l'export JSON pour les autres rôles |
| Montants et dates | montants infinis ou démesurés (au-delà de 1 000 milliards d'euros) refusés partout ; montants négatifs refusés (crédits, acomptes, déficits, réintégrations, parts, dispenses) ; période de 24 mois au plus ; échéance d'acompte dans les deux ans ; date de dépôt ni future ni antérieure à la fin de la période |
| Statuts | un statut inconnu renvoyé par un partenaire est ignoré ; aucun formulaire ne peut imposer un statut ; un dépôt manuel n'atteint jamais « Acceptée » |

## 4. Dossiers de référence

Trois dossiers complets sont passés en écritures dans `tests/test_v458_dossiers_reference.py`,
et les résultats de ProfitOS comparés aux montants calculés à la main ci-dessous
(tolérance 1 €).

### Dossier A — SARL de services, régime simplifié, TVA CA12, exercice 2025

Écritures (HT, TVA 20 %) : capital 10 000 ; prestations 120 000 (encaissées 132 000 TTC sur
144 000) ; fournitures 30 000 ; loyers 12 000 ; avocat 2 000 + frais d'actes 500 (même
bénéficiaire) ; conseil 900 ; salaires 24 000 ; charges sociales 10 000 ; véhicule
utilitaire 15 000 acquis le 1er avril ; amende 300 ; tout est réglé.

| Calcul | Détail | Attendu |
| --- | --- | --- |
| Dotation véhicule | 15 000 / 5 × 275 / 365 | 2 260,27 |
| Résultat comptable (2033-B, 310) | 120 000 − (30 000 + 12 000 + 2 000 + 500 + 900 + 24 000 + 10 000 + 2 260,27 + 300) | 38 039,73 |
| Résultat fiscal (370) | + amende 300 réintégrée | 38 339,73 |
| IS (PME, taux réduit) | 15 % × 38 340 | 5 751 |
| Disponibilités | 10 000 + 132 000 − 36 000 − 14 400 − 3 000 − 1 080 − 24 000 − 10 000 − 18 000 − 300 | 35 220 |
| Bilan 2033-A | TVA non liquidée présentée sans compensation : créance 12 080, dette 24 000 | actif = passif = 72 039,73 |
| Capitaux propres | 10 000 + 38 039,73 | 48 039,73 |
| CA12 | TVA brute 24 000 ; déductions biens et services 9 080 ; immobilisations 3 000 ; TVA due | 11 920 |
| Acomptes 2026 (ligne 57 = 24 000 − 9 080 = 14 920) | 55 % / 40 % | 8 206 / 5 968 |
| DAS2 | Cabinet Dupont 2 400 + 600 TTC (> 1 200) ; Petit Conseil 1 080 (sous le seuil) | 3 000 déclarés, 1 bénéficiaire |
| CVAE | chiffre d'affaires < 500 000 | 0, déclaration non obligatoire |

### Dossier B — SAS, régime réel normal, TVA CA3, exercice 2025

Écritures : capital 50 000 ; ventes 700 000 HT à 20 % et 200 000 HT à 5,5 % ; achats de
marchandises 300 000 HT ; stock final 20 000 ; services 100 000 HT ; salaires 200 000 ;
charges sociales 90 000 ; CFE 2 000 ; emprunt 100 000 (remboursé 18 000, intérêts 3 000) ;
machine 60 000 HT (dotation 6 000) ; provision pour litige 5 000 ; déficits antérieurs
30 000 ; capital libéré et détenu par des personnes physiques.

| Calcul | Détail | Attendu |
| --- | --- | --- |
| Résultat comptable | 900 000 − (300 000 − 20 000 + 100 000 + 200 000 + 90 000 + 2 000 + 6 000 + 5 000 + 3 000) | 214 000 |
| Résultat fiscal (2058-A) | 214 000 − déficits imputés 30 000 (≤ 1 M€) | 184 000 |
| IS | 15 % × 42 500 + 25 % × 141 500 = 6 375 + 35 375 ; contribution sociale nulle | 41 750 |
| Bilan 2050 / 2051 | immobilisations nettes 54 000, stock 20 000, clients 51 000, banque 285 000, créance de TVA 92 000 ; capital 50 000, résultat 214 000, provision 5 000, emprunt 82 000, TVA collectée 151 000 | actif = passif = 502 000 |
| Valeur ajoutée (2059-E) | 900 000 + variation positive des stocks 20 000 − (achats 300 000 + services 100 000) | 520 000 |
| CVAE 2025 | 520 000 × 0,094 % × (900 000 − 500 000) / 2 500 000 = 78,21 ; dégrèvement de 188 € (CA < 2 M€) | 0 |
| TVA de l'année | 140 000 + 11 000 − (60 000 + 20 000) − 12 000 | 59 000 |

### Dossier C — exercice déficitaire avec cession, régime simplifié, exercice 2025

À-nouveaux : machine 10 000 acquise le 1er janvier 2023, amortie 2 000 en 2023 et 2024 ;
banque 14 000 ; capital 20 000. Ventes 50 000 HT, achats 70 000. Machine cédée le 30 juin
2025 pour 7 000 HT. Déficits antérieurs 5 000.

| Calcul | Détail | Attendu |
| --- | --- | --- |
| Dotation complémentaire | 2 000 × 181 / 365 | 991,78 |
| Valeur nette comptable | 10 000 − 4 000 − 991,78 | 5 008,22 |
| Plus-value à court terme (2033-C) | 7 000 − 5 008,22 | 1 991,78 |
| Résultat | 50 000 − 70 000 − 991,78 − 5 008,22 + 7 000 | − 19 000 |
| Bilan 2033-A | clients 60 000 + créance de cession 8 400 + banque 14 000 ; capital 20 000 − 19 000, fournisseurs 70 000, TVA 11 400 | actif = passif = 82 400 |
| Déficits (2033-D) | 982 : 5 000 ; 983 : 0 ; 984 : 5 000 ; 860 : 19 000 ; 870 : 24 000 | conformes |
| IS | exercice déficitaire | 0 |

## 5. Écarts trouvés et corrigés grâce aux dossiers

| Écart | Effet | Correction |
| --- | --- | --- |
| Hausse de stock (603 créditeur) ignorée dans la valeur ajoutée | VA sous-évaluée (dossier B : 500 000 au lieu de 520 000) | variation des stocks répartie en « variation positive » (OD) ou « négative » (OQ) |
| Dégrèvement CVAE de 188 € absent pour 2024-2025, barème réduit 2028-2029 absent | CVAE 2025 surévaluée pour les entreprises sous 2 M€ | 188 € (2024-2027), 125 € (2028), 63 € (2029) ; taxe additionnelle 2024-2025 |
| Dépôt simulé affiché « Acceptée » et compté comme déposé | échéance cochée sans dépôt réel | libellé « (simulation) », exclu des dépôts réels |

## 6. Grille de validation par l'expert-comptable

À remplir sur **au moins trois dossiers réels** (un par régime : simplifié, normal, et un
exercice déficitaire ou avec cession), en comparant ProfitOS à la liasse et aux
déclarations établies par le cabinet.

| Contrôle | Dossier 1 | Dossier 2 | Dossier 3 |
| --- | --- | --- | --- |
| Bilan (2033-A ou 2050-2051) identique à ± 1 € | ☐ | ☐ | ☐ |
| Compte de résultat (2033-B ou 2052-2053) | ☐ | ☐ | ☐ |
| Résultat fiscal, réintégrations, déficits | ☐ | ☐ | ☐ |
| Immobilisations, amortissements, plus-values | ☐ | ☐ | ☐ |
| IS (taux réduit, acomptes, solde) | ☐ | ☐ | ☐ |
| TVA (CA3 ou CA12, acomptes) | ☐ | ☐ | ☐ |
| Valeur ajoutée et CVAE | ☐ | ☐ | ☐ |
| DAS2 | ☐ | ☐ | ☐ |
| 2065, 2059-F/G ou 2033-F/G | ☐ | ☐ | ☐ |

Validé par : ____________________ (cabinet, nom, date). Une fois la grille signée et les
écarts éventuels corrigés : `FISCAL_DECLARATIONS_ENABLED=1`.
