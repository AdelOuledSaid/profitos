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
  paragraphe 7 signée par un expert-comptable.

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

Huit dossiers sont passés en écritures dans `tests/test_v458_dossiers_reference.py`
(A à C) et `tests/test_v460_dossiers_reference_2.py` (D à H), et les résultats de ProfitOS
comparés aux montants calculés à la main ci-dessous (tolérance 1 €), à partir des notices
officielles (3310-CA3, 2571, 2572, 1329-AC et 1329-DEF 2026) et du BOFiP.

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
| CVAE 2025 | barème 2025 à 0,19 % : 0,063 % × 400 000 / 2 500 000 = 0,0101 %, arrondi à 0,01 % ; 520 000 × 0,01 % = 52 ; dégrèvement de 125 € (CA < 2 M€) | 0 |
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

### Dossier D — CA3 mensuelles avec crédit de TVA, premier trimestre 2026

| Mois | Opérations (HT) | Calcul | Attendu |
| --- | --- | --- | --- |
| Janvier | ventes 10 000 à 20 % ; machine 50 000 ; achats 5 000 | 2 000 − (10 000 + 1 000) | crédit 9 000 (lignes 25 et 27) |
| Février | ventes 20 000 ; achats 10 000 | 4 000 − (2 000 + crédit reporté 9 000 en ligne 22) | crédit 7 000 |
| Mars | ventes 50 000 à 20 % et 20 000 à 10 % ; achats 15 000 | 12 000 − (3 000 + 7 000) | 2 000 à payer (lignes 28 et 32) |
| Fin mars | après les trois liquidations | 44567 soldé, 4457 / 4456 soldés | 44551 créditeur de 2 000 |

Sans liquidation de janvier, le crédit ne peut pas être reporté : la préparation de février
le signale (« La TVA d'une période précédente n'a pas été liquidée »).

### Dossier E — acomptes et solde d'IS

| Calcul | Détail | Attendu |
| --- | --- | --- |
| IS 2025 (référence) | bénéfice 100 000 : 15 % × 42 500 + 25 % × 57 500 | 20 750 |
| Acomptes 2026 | 6,25 % × 57 500 + 3,75 % × 42 500 = 5 187,50 | 4 × 5 188 (15/03, 15/06, 15/09, 15/12) |
| Solde 2026 (2572) | IS 2026 sur 135 500 = 6 375 + 23 250 = 29 625 ; − 20 752 | 8 873 (7 373 avec 1 500 € de crédits d'impôt) |
| Excédent | IS 2026 sur 60 000 = 10 750 ; − 20 752 | 10 002 à restituer |
| Seuil | IS de référence 3 000 € (bénéfice 20 000) | dispense, aucune 2571 ; 3 001 € → 4 × 750 |

### Dossier F — CVAE aux seuils (taux arrondi au centième de pourcent)

| Cas (exercice civil 2026 sauf mention) | Calcul | Attendu |
| --- | --- | --- |
| CA 152 500 / 152 501 | seuil de déclaration | 1330-CVAE non obligatoire / obligatoire |
| CA 1 M€, VA 600 000 | 0,0188 % → 0,02 % : 120 € − dégrèvement 188 € | 0 |
| CA 1 999 999, VA 1 M€ | 0,0564 % → 0,06 % : 600 − 188 | 412 |
| CA 2 M€, VA 1 M€ | 600, sans dégrèvement ; taxe additionnelle 9,23 % | 600 + 55, pas d'acompte |
| CA 3 M€, VA 2 M€ | 0,094 % → 0,09 % : 1 800 > 1 500 € | 1 800 ; acomptes 900 / 900 en 2027 ; TA 166 |
| CA 2,5 M€, VA 78 750 / 80 000 | 0,0752 % → 0,08 % : 63 € / 64 € | non due / 64 |
| CA 7,6 M€ / 7 600 001 €, VA 7 M€ | 0,21 % sur 80 % du CA / sur 85 % | 12 768 / 13 566 |
| 2025 : CA 2,7 M€, VA 1 M€ | barème 0,19 % : 0,0554 % → 0,06 % (exemple de la notice) ; contribution complémentaire 47,4 % ; TA 13,84 % | 600 + 284 + 83 |
| 2025 : CA 1,5 M€, VA 1 M€ | 0,03 % : 300 − dégrèvement 125 € | 175 + 83 de contribution complémentaire |
| Exercice de 16 mois, CA 4,2 M€ | CA ramené à 12 mois 3,15 M€ (exemple de la notice) : 0,0976 % → 0,10 % ; VA 3 M€ non ramenée | 3 000 |
| Exercice de 16 mois, CA 8 M€ | plafond à 85 % (CA réel > 7,6 M€) ; taux sur 6 M€ : 0,17 % ; 6,8 M€ × 0,17 % | 11 560 |

### Dossier G — liasse 2050 avec emprunt et provisions, exercice 2025

À-nouveaux : banque 50 000, capital 42 000, provision pour litige 8 000. Emprunt de 120 000 €
sans intérêts sur 60 mois (échéances de 2 000 € du 1er février 2025 au 1er janvier 2030),
11 échéances payées ; ventes 100 000 (70 000 encaissés) ; achats 40 000 non réglés ;
provision pour un nouveau litige 3 000 ; reprise d'une provision devenue sans objet 5 000 ;
dépréciation d'un client douteux 2 000.

| Calcul | Détail | Attendu |
| --- | --- | --- |
| Résultat | 100 000 − 40 000 − 3 000 + 5 000 − 2 000 | 60 000 |
| Bilan | banque 218 000, clients 30 000 − 2 000 ; capital 42 000, résultat 60 000, provision 6 000, emprunt 98 000, fournisseurs 40 000 | actif = passif = 246 000 |
| 2057 (VH) | capital restant dû 98 000 : échéances 2026 / 2027 à janvier 2030 | 24 000 à un an, 74 000 de 1 à 5 ans |
| 2056 litiges (4A à 4D) | 8 000 + 3 000 − 5 000 | 8 000 / 3 000 / 5 000 / 6 000 |
| 2056 clients (6T à 6W) | dotation 2 000 | 0 / 2 000 / 0 / 2 000 |
| 2056 total (7C à UD), dont exploitation (UE / UF) | | 8 000 / 5 000 / 5 000 / 8 000 ; 5 000 / 5 000 |

### Dossier H — premier exercice de 18 mois (1er juillet 2025 – 31 décembre 2026)

| Calcul | Détail | Attendu |
| --- | --- | --- |
| Durée | mois entiers + jours résiduels / 30 | 18 mois (2050, 2065) |
| PME | CA 600 000 ramené à 12 mois = 400 000 | éligible |
| Plafond du taux réduit | 42 500 × 18 / 12 | 63 750 |
| IS | 15 % × 63 750 + 25 % × 136 250 | 43 625 |
| Acomptes pendant l'exercice | premier exercice | aucun |
| Acomptes 2027 | IS ramené à 12 mois 29 083,33 / 4 | 4 × 7 271 |
| Solde | clôture au 31 décembre | 15 mai 2027 |
| CVAE 2026 | CA ramené à 12 mois 400 000 < 500 000 | 0, déclaration obligatoire |
| Exercice de 8,5 mois (1er janvier – 15 septembre) | 42 500 × 8,5 / 12 (exemple du BOFiP) | plafond 30 104 |
| Contribution sociale sur 18 mois | abattement 763 000 × 18 / 12 = 1 144 500 | 3,3 % × (IS − 1 144 500) |

### Dossier I — cycle de clôture sur deux exercices (2024 bénéficiaire, 2025 déficitaire)

`tests/test_v464_cloture_bout_en_bout.py`, par les écrans de clôture et d'export FEC :

| Calcul | Détail | Attendu |
| --- | --- | --- |
| Résultat 2024 | 100 000 − 40 000 − 20 000 − 2 000 = 38 000 ; IS 15 % = 5 700 | 32 300 |
| À-nouveaux au 01/01/2025 | chaque compte de bilan repris pour son solde, tiers par tiers | identiques au bilan de clôture ; 120000 créditeur de 32 300 ; banque 40 800 |
| Après clôture | saisie ou contrepassation datée de 2024 | refusée ; à-nouveaux non régénérables |
| FEC 2024 | 18 colonnes, dates AAAAMMJJ, montants, équilibre, numérotation continue | conforme, 12 écritures |
| Affectation 2025 | 5 % en réserve légale, solde en report à nouveau | 1 615 / 30 685 |
| Résultat 2025 | 20 000 − 30 000 − 2 000 | − 12 000 (129000 débiteur dans les à-nouveaux 2026) |
| Liasse 2025 | colonne N-1 | égale à la liasse 2024 |

## 5. Écarts trouvés et corrigés grâce aux dossiers

| Écart | Effet | Correction |
| --- | --- | --- |
| Hausse de stock (603 créditeur) ignorée dans la valeur ajoutée | VA sous-évaluée (dossier B : 500 000 au lieu de 520 000) | variation des stocks répartie en « variation positive » (OD) ou « négative » (OQ) |
| Dégrèvement CVAE absent pour 2024-2025, barème réduit 2028-2029 absent | CVAE surévaluée pour les entreprises sous 2 M€ | dégrèvement selon l'année (v458, corrigé en v460) |
| v460 — taux de CVAE non arrondi au centième de pourcent | écart jusqu'à ± 0,005 point de taux (ex. 0,0564 % au lieu de 0,06 %) | arrondi au centième le plus proche, comme les notices |
| v460 — CVAE 2025 calculée au barème 0,28 % | 2025 : barème 0,19 %, dégrèvement 125 €, taxe additionnelle 13,84 % et contribution complémentaire de 47,4 % | barème, dégrèvement et taxes de l'année 2025 |
| v460 — plafonnement de la valeur ajoutée apprécié sur le CA ramené à 12 mois | plafond à 80 % au lieu de 85 % pour un exercice long dépassant 7,6 M€ de CA réel | CA réel de la période |
| v460 — dispense d'acomptes d'IS (≤ 3 000 €) ignorée par la 2571 et la liste des déclarations | acomptes proposés à des entreprises dispensées, et pendant le premier exercice | dispense appliquée partout |
| v460 — acomptes calculés sur l'IS d'un exercice long sans le ramener à 12 mois | après un premier exercice de 18 mois, acomptes surévalués de 50 % | IS de référence ramené à 12 mois |
| v460 — durée de l'exercice en jours / 365 | plafond de 42 500 € légèrement faux (8,48 mois au lieu de 8,5) | mois entiers + jours résiduels / 30 (BOFiP) |
| v460 — abattement de 763 000 € non proratisé | contribution sociale surévaluée sur un exercice long | abattement ajusté à la durée |
| v460 — crédit de TVA non reporté si la période précédente n'est pas liquidée, sans avertissement | ligne 22 vide sans explication | avertissement à la préparation |
| Dépôt simulé affiché « Acceptée » et compté comme déposé | échéance cochée sans dépôt réel | libellé « (simulation) », exclu des dépôts réels |
| v461 — CA3 en retard d'un jour non relancée | la recherche des retards (63 jours en arrière) ne voyait que les deux périodes voisines de son point de départ : relance seulement au bout d'un mois | toutes les périodes dont l'échéance tombe dans la fenêtre |

## 5 bis. Contrôles croisés déclaré ↔ comptabilisé (v490)

Écran **Comptabilité → Contrôles fiscaux** (`profitos/fiscal_controls.py`, tests
`tests/test_v490_controles_fiscaux.py`). Chaque contrôle est « conforme », « à vérifier » ou
« écart à corriger », cite sa source et indique l'action :

| Contrôle | Règle | Niveau en cas d'écart |
| --- | --- | --- |
| TVA déclarée ↔ écritures | lignes 16, 19 et 20 de la dernière version de chaque CA3 (hors simulation) comparées aux écritures actuelles de la période, tolérance 1 € | écart à corriger si déposée (régularisation ou rectificative) ; à vérifier si seulement préparée |
| Base × taux | TVA du 445710 par taux comparée à base × taux sur l'exercice, tolérance 1 % (au moins 1 €) | à vérifier |
| TVA non ventilée | TVA sans taux mémorisé | à vérifier |
| Liquidation | comptes 445710, 445620, 445660 soldés avant le mois en cours | écart à corriger |
| TVA à décaisser | solde du 445510 au-delà de la dernière liquidation | à vérifier |
| CA ↔ base taxée | comptes 70 comparés aux bases taxées, tolérance 2 % (au moins 1 000 €) | à vérifier (export, exonération, autoliquidation, encaissements) |
| Acomptes d'IS | acomptes exigibles depuis 5 jours (calculés sur l'IS de l'exercice clos) comparés au compte 444 ; pas de comparaison sur le seul premier acompte, calculé sur N-2 | à vérifier |
| Excédent d'acomptes | solde d'IS négatif sur l'exercice clos | à vérifier (remboursement avec le 2572) |
| Liasse | équilibre du bilan, comptes rattachés par défaut | écart / à vérifier |
| Clôture | pré-contrôles existants (IS non comptabilisé, comptes d'attente…) | à vérifier |

Le **dossier de contrôle fiscal** (`/comptabilite/controles-fiscaux/dossier`, imprimable,
jamais mis en cache) reprend ces contrôles avec une case de revue par ligne, la liste des
déclarations avec leur **mode de transmission** (préparée par ProfitOS, déposée par
l'entreprise sur impots.gouv.fr, transmise par le partenaire EDI, simulation) et un bloc de
validation signé par l'expert-comptable.

## 6. Parcours complet dans un navigateur, sur PostgreSQL (v461)

`tests/test_v461_parcours_complet.py` enchaîne tout le parcours d'un client dans Chromium
(Playwright), contre l'application lancée sur une base PostgreSQL réelle :

1. inscription, profil de l'entreprise (SIRET), profil fiscal (régime normal, CA3 mensuelle) ;
2. saisie des écritures de deux mois, CA3 et liquidation de la TVA de chaque mois ;
3. tâche planifiée au lendemain de l'échéance : e-mail « Déclarations en retard » ;
4. préparation de la CA3 du premier mois, récapitulatif PDF téléchargé et relu (SIREN,
   formulaire) ;
5. accusé avec un montant erroné : « justificatif à vérifier », relance « écarts » le
   lendemain, puis confirmation avec explication obligatoire ;
6. CA3 du mois suivant avec un accusé concordant : « dépôt confirmé » sans intervention ;
7. plus aucune relance pour les déclarations justifiées, rappel de l'échéance suivante à
   7 jours ; aucun statut « acceptée » ; justificatif téléchargeable ; aucune erreur
   serveur ni de console sur les pages fiscales.

Les dates des relances sont simulées en fixant la date du jour de la tâche ; seuls la
vérification de l'adresse e-mail et l'abonnement sont posés directement en base (lien de
l'e-mail et paiement Stripe hors périmètre). Test ignoré par défaut ; pour le lancer :

```bash
PROFITOS_E2E_DATABASE_URL="postgresql://utilisateur@/base_vide?host=/tmp&port=5432" pytest tests/test_v461_parcours_complet.py
```

Le parcours a révélé l'écart ci-dessus (relance tardive des CA3), corrigé et couvert par
`tests/test_v461_relances_ca3.py`, qui tourne en CI.

`tests/test_v462_parcours_modules.py` enchaîne de la même façon, dans le navigateur :
exercice précédent saisi, à-nouveaux, clôture (saisie tardive et recul de la clôture
refusés), export FEC contrôlé, facture client émise (mentions obligatoires, TVA sur les
encaissements), PDF, relevé bancaire importé et rapproché (TVA devenue exigible),
facture fournisseur, bilan de l'exercice en cours équilibré.

`tests/test_v467_restauration_postgresql.py` sauvegarde la base PostgreSQL de ces parcours
(pg_dump chiffré) et la restaure dans une base neuve : toutes les tables de tous les
schémas identiques ligne pour ligne, documents identiques à l'octet. Les trois tests se
lancent avec la même variable `PROFITOS_E2E_DATABASE_URL`.

Les tests ordinaires peuvent aussi tourner sur PostgreSQL, chaque test repartant d'une base
vide (base de test dédiée, jamais la production) :

```bash
PROFITOS_TEST_DATABASE_URL="postgresql://utilisateur@/base_de_test?host=/tmp&port=5432" pytest tests/test_v467_isolation_audit.py tests/test_v459_securite_fiscale.py
```

`test_v467_isolation_audit` parcourt toutes les routes GET et POST de l'application,
connecté à une entreprise B avec les identifiants d'une entreprise A : aucune donnée de A
ne doit apparaître et ses données doivent rester identiques. Sa capacité à détecter une
fuite a été vérifiée en en introduisant une volontairement (page facture lisant la base
d'une autre entreprise) : le test échoue sur cette page.

## 7. Grille de validation par l'expert-comptable

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
