# ProfitOS — Production réglementaire

Référence technique de la production fiscale de ProfitOS : ce qui est calculé, à partir de
quelles données et selon quelles règles, comment les déclarations sont déposées et suivies,
et quelles sont les limites connues. Destinée à l'équipe et aux experts-comptables qui
valident les calculs. Le mode d'emploi pour les clients est dans `guide-utilisateur.md`.

Règles en vigueur pour les exercices 2026 (loi de finances 2026, LFSS 2026), à revoir
chaque année.

## 1. Principe

1. **ProfitOS calcule** à partir de la comptabilité (balance de l'exercice, écritures,
   immobilisations, emprunts) et du **profil fiscal** (régimes, associés, filiales, locaux,
   mandat de prélèvement).
2. **ProfitOS prépare** : contrôles bloquants et avertissements, contenu figé (empreinte
   SHA-256), récapitulatif PDF case par case.
3. **Le dépôt** est fait par l'entreprise sur impots.gouv.fr (EFI) ou, pour les
   déclarations qui l'exigent, par EDI (expert-comptable ou partenaire EDI).
4. **Le suivi** : accusé de réception archivé et contrôlé automatiquement, échéancier,
   relances.

Les montants sont des aides à la déclaration : ils doivent être relus, notamment par
l'expert-comptable.

## 2. Déclarations couvertes

| Déclaration | Formulaire | Calcul | Dépôt |
| --- | --- | --- | --- |
| TVA mensuelle ou trimestrielle | 3310-CA3 | `vat_return.ca3_form` | impots.gouv.fr (EFI) |
| TVA annuelle, régime simplifié | 3517-S CA12 | `vat_return.ca12_form` | EFI |
| Acomptes de TVA, régime simplifié | 3514 | 55 % en juillet, 40 % en décembre de la ligne 57 ; aucun si la base est sous 1 000 € | EFI |
| Liasse, régime simplifié | 2065 + 2033-A à 2033-G | `liasse`, `liasse_2033_annexes` | EFI dans les cas éligibles |
| Liasse, régime normal | 2065 + 2050 à 2059-G | `liasse_normal`, `liasse_annexes` | **EDI obligatoire** |
| Impôt sur les sociétés | 2571 (acomptes), 2572 (solde) | `corporate_tax.compute_is` | EFI |
| Valeur ajoutée | 1330-CVAE | `corporate_tax.compute_cvae` | **EDI obligatoire** |
| Honoraires versés | DAS2 | `tiers_declarations.das2` | EFI, ou avec la liasse |
| Prélèvements sur dividendes | 2777 | `tiers_declarations.rcm_2777` | en ligne (préparation seule) |
| Imprimé fiscal unique | IFU 2561 | `tiers_declarations.ifu_2561` | en ligne (préparation seule) |
| Cotisation foncière | CFE, 1447-C / 1447-M | `local_taxes.cfe_schedule` (montant de l'avis saisi) | suivi et rappels |
| Loyers professionnels | DECLOYER | `local_taxes.decloyer` (locaux du profil fiscal) | avec la liasse en EDI |

## 3. Règles de calcul

### TVA
- CA3 : base et taxe par taux, TVA déductible sur immobilisations et autres biens et
  services, crédit antérieur, TVA nette ou crédit ; écriture de liquidation une fois par
  période. Le crédit d'une période (ligne 25 / 27) n'est reporté en ligne 22 de la suivante
  que si la période a été liquidée : sinon, la préparation le signale.
- CA12 : lignes par taux (20 % 5A/0207, 10 % 6C/0151, 5,5 % 06/0105, 2,1 % 09/0950,
  8,5 % 07/0201), déductions, crédit antérieur, acomptes payés (compte 44581 ou montant
  saisi), solde ou excédent ; date limite : deuxième jour ouvré suivant le 1er mai pour une
  clôture au 31 décembre.

### Impôt sur les sociétés
- Taux normal 25 % ; taux réduit 15 % jusqu'à 42 500 € de bénéfice pour les PME éligibles
  (chiffre d'affaires ≤ 10 M€, capital libéré, détenu à 75 % au moins par des personnes
  physiques).
- Contribution sociale de 3,3 % sur l'IS au-delà de 763 000 € (exonération des PME dont le chiffre d'affaires est inférieur à 7,63 M€).
- Exercice de plus ou de moins de 12 mois (premier exercice notamment) : plafond de 42 500 €
  et abattement de 763 000 € ajustés à la durée, comptée en mois entiers plus les jours
  résiduels en trentièmes (1er janvier – 15 septembre = 8,5 mois) ; seuils de chiffre
  d'affaires appréciés sur 12 mois.
- Résultat fiscal (2058-A / 2033-B) : résultat comptable, IS et amendes réintégrés
  automatiquement, réintégrations et déductions saisies, imputation des déficits antérieurs
  dans la limite d'1 M€ majoré de 50 % du bénéfice excédant.
- Acomptes les 15 mars, juin, septembre et décembre compris dans l'exercice, chacun égal au
  quart de l'IS du dernier exercice clos **ramené à 12 mois**, arrondi à l'euro ; dispense
  si cet IS ne dépasse pas 3 000 € et pendant le premier exercice ; solde (2572) le 15 du
  4e mois suivant la clôture (15 mai pour une clôture au 31 décembre). Chaque acompte se
  dépose pour son échéance.
- Non calculés : acomptes de contribution sociale, majoration du dernier acompte des
  entreprises dont le CA dépasse 250 M€, découpage d'un exercice de plus de 12 mois ne
  couvrant pas une création (période d'imposition au 31 décembre).
- Écriture de l'impôt : 695 / 444.

### CVAE
- Taux maximal : 0,28 % en 2024, 0,19 % en 2025 (plus une contribution complémentaire de
  47,4 % de la CVAE due), 0,28 % en 2026 et 2027 (barème 0,094 % / +0,169 % / +0,019 %),
  0,19 % en 2028, 0,09 % en 2029, supprimée en 2030. Le taux, calculé sur le chiffre
  d'affaires ramené à 12 mois, est arrondi au centième de pourcent (0,0564 % → 0,06 %).
- Valeur ajoutée : variation des stocks en « variation positive » (OD) ou « négative »
  (OQ) ; non ramenée à 12 mois ; plafonnée à 80 % du chiffre d'affaires réel de la période
  (85 % au-delà de 7,6 M€) ; dégrèvement sous 2 M€ de chiffre d'affaires : 188 € (2024,
  2026, 2027), 125 € (2025, 2028), 63 € (2029) ; non due si 63 € ou moins ; taxe
  additionnelle CCI 9,23 % (13,84 % en 2025). Déclaration obligatoire au-delà de 152 500 €
  de chiffre d'affaires (ramené à 12 mois) ; acomptes de 50 % les 15 juin et 15 septembre
  quand la CVAE dépasse 1 500 €.

### Liasses
- Bilan, compte de résultat et annexes établis depuis la balance (préfixe de compte le plus
  long), à-nouveaux du premier jour comptés en ouverture.
- 2054 à 2056 : codes de cases officiels quand ils sont établis avec certitude ; sinon,
  présentation par rubrique.
- 2059-A / 2033-C : cessions d'immobilisations enregistrées dans ProfitOS (court terme ;
  long terme pour les titres de participation détenus depuis deux ans au moins).
- 2059-B et 2059-D présentés à néant (cas normal d'une société à l'IS) ; 2059-C depuis le
  stock de moins-values à long terme saisi.
- 2059-F / 2033-F, 2059-G / 2033-G, 2065 : profil fiscal.
- 2033-D : déficits (cases 982, 983, 984, 860, 870).

### Cessions d'immobilisations
Dotation complémentaire au prorata jusqu'à la date de sortie (en tenant compte des dotations
mensuelles ou trimestrielles déjà passées), sortie de l'actif (valeur nette en 675), prix en
775 et TVA collectée ; alerte si des années d'amortissement n'ont jamais été passées.

### DAS2
Comptes 6226, 6227 (honoraires), 6221-6222 (commissions), 6516 (droits d'auteur), 6511
(redevances), 653 (jetons de présence), TVA comprise, par bénéficiaire ; seuil : plus de
1 200 € sur l'année civile. Les montants comptabilisés sont modifiables, la DAS2 portant
sur les sommes versées. SIRET et adresse exigés (repris de la fiche fournisseur).

### Dividendes : 2777 et IFU
- Dividendes payés : débits du compte 457 ; part des personnes physiques selon la
  composition du capital (ou saisie).
- Prélèvement forfaitaire non libératoire 12,8 % (hors associés dispensés) et
  prélèvements sociaux 18,6 % depuis le 1er janvier 2026 (17,2 % auparavant) ; comparaison
  avec les retenues comptabilisées au compte 442 ; échéance le 15 du mois suivant.
- IFU : par associé, cases 2DC, 2CK, 2BH ; échéance le 15 février.

### CFE et DECLOYER
- CFE : acompte de 50 % au 15 juin si la CFE de l'année précédente atteint 3 000 € (hors
  mensualisation), solde au 15 décembre ; exonération l'année de création (1447-C avant le
  31 décembre), base réduite de moitié l'année suivante ; 1447-M en cas de changements.
  Écriture 635110 / 447000, une fois par an.
- DECLOYER : loyer annuel hors taxes et hors charges de chaque local loué au 1er janvier,
  joint à la liasse ; contrôle de cohérence avec le compte 6132.

## 4. Dépôt et suivi

### Canaux
`teledeclaration.filing_channel` : la liasse du régime normal et la 1330-CVAE sont en EDI
obligatoire ; les autres déclarations se déposent sur impots.gouv.fr. Avec un partenaire
EDI connecté (`EDI_PARTNER_ENABLED=1`), toutes peuvent être transmises directement et leur
statut est actualisé chaque heure.

### Statuts

| Statut | Signification |
| --- | --- |
| `prepared` | Préparée — à déposer sur impots.gouv.fr |
| `declared` | Dépôt déclaré — accusé archivé mais illisible automatiquement (image, scan) |
| `to_verify` | Justificatif à vérifier — l'accusé ne correspond pas à la déclaration |
| `confirmed` | Dépôt confirmé par l'accusé officiel (contrôle automatique concordant, ou vérification manuelle tracée) |
| `deposited`, `acknowledged`, `accepted`, `rejected` | Parcours EDI : déposée chez le partenaire, accusé de réception, acceptée ou rejetée par l'administration |

Seul `accepted` renvoyé par un partenaire EDI réel constate l'acceptation par
l'administration. Un dépôt simulé (`mode='simulation'`, développement uniquement) est
toujours libellé « (simulation) » et ne compte jamais comme un dépôt réel. Pour un dépôt sur
impots.gouv.fr, aucune interface ne permet de la vérifier : un rejet est signalé dans
l'espace professionnel de l'entreprise.

### Contrôle de l'accusé
`teledeclaration.analyze_receipt` lit le texte du PDF (10 pages au plus) et recherche le
SIREN, le formulaire, la période (ou l'échéance d'un acompte) et le montant. Résultat :
`concordant` → `confirmed`, `ecarts` → `to_verify`, `non_verifiable` → `declared`. La
confirmation manuelle exige une case « vérifié » et, en cas d'écarts, une explication ;
elle est tracée (historique, journal de sécurité).

### Échéancier et relances
`automation.upcoming_deadlines` (90 jours, selon le régime de TVA) ; rappels à 7 jours et la
veille ; `automation.overdue_declarations` (échéance passée sans dépôt) et
`automation.receipts_to_verify` : relance le lendemain puis chaque semaine pendant neuf
semaines. Pas de relance pour une déclaration sans montant ni préparation, ni pour une
liasse ou une CA12 d'un exercice sans écritures.

### Ouverture aux clients
En production, la préparation des déclarations est fermée tant que les calculs n'ont pas été
validés (`FISCAL_DECLARATIONS_ENABLED`, `FISCAL_DECLARATIONS_PILOT_ORGS`) : voir
`validation-fiscale.md`.

## 5. Limites connues

- Codes de cases non établis avec certitude laissés vides (partie des 2054 à 2056, 2033-C à
  2033-G, 2065 sans codes) : à compléter à partir des notices officielles de l'année.
- Pas de vérification automatique de l'acceptation par l'administration hors EDI.
- Pas de dépôt direct de la 2777 et de l'IFU (préparation et suivi seulement).
- CFE : montant de l'avis à saisir (calculée par l'administration).
- Chemins des menus d'impots.gouv.fr indiqués à titre indicatif.
- Connecteur EDI générique : à adapter à l'interface du partenaire retenu.

## 6. Tests

`tests/test_v445` à `test_v461` (dont `test_v457` : aucune acceptation sans accusé EDI ;
`test_v458` et `test_v460` : dossiers de référence calculés à la main, détaillés dans
`validation-fiscale.md` ; `test_v461` : parcours complet dans un navigateur sur PostgreSQL) : liasses, IS et CVAE, télédéclarations, automatisations,
régime simplifié et annexes, DAS2, 2777, IFU, CFE, DECLOYER, dépôt sur impots.gouv.fr,
contrôle des accusés, relances, contrôle des rôles.
