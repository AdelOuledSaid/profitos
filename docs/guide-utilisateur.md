# ProfitOS — Guide utilisateur

Oct 14, 2026 · @adel

## 1. Présentation

ProfitOS est un logiciel de gestion financière en ligne pour les TPE, PME, indépendants et cabinets. Il réunit facturation, facture électronique, achats, banque, trésorerie et comptabilité dans un seul outil, en français et conforme aux règles fiscales françaises.

Ce guide explique, module par module, comment utiliser ProfitOS au quotidien. Les menus cités sont ceux de la barre latérale à gauche de l'écran.

**Les grands modules**

| Module | À quoi il sert | Formule minimale |
| --- | --- | --- |
| Facturation | Devis, factures, acomptes, avoirs, abonnements, relances clients | Starter |
| Facture électronique | Factures Factur-X, envoi par plateforme agréée, suivi des statuts | Starter |
| Achats et dépenses | Factures fournisseurs, notes de frais, budgets | Starter |
| Banque | Connexion ou import des relevés, rapprochement, catégorisation | Pro |
| Comptabilité | Écritures, grand livre, balance, TVA, clôture, FEC, liasse 2033 | Pro |
| Trésorerie et scénarios | Prévision à 90 jours, plan mensuel, simulateur de décision | Business |
| Multi-sociétés | Plusieurs entités, consolidation, portefeuille cabinet | Multi-sociétés |

Un module non inclus dans votre formule reste visible dans le menu avec un cadenas et le nom de la formule qui l'inclut.

## 2. Premiers pas

En cinq minutes, votre compte est créé et votre société paramétrée pour émettre des factures conformes.

### Créer son compte

1. Sur la page d'inscription, saisissez votre prénom, votre nom, votre adresse e-mail, le nom de votre société et un mot de passe.
2. Acceptez les conditions générales, puis validez.
3. Ouvrez l'e-mail de confirmation et cliquez sur le lien. Tant que l'adresse n'est pas vérifiée, un bandeau jaune le rappelle en haut de chaque page.

Le mot de passe doit être long et peu courant : les mots de passe trop simples, répétitifs ou contenant votre e-mail sont refusés.

### Renseigner le profil entreprise

Menu **Entreprise & compte → Profil entreprise**. Ces informations apparaissent sur vos factures et sont obligatoires pour qu'elles soient légales.

| Information | Pourquoi |
| --- | --- |
| Dénomination, forme juridique, capital social | Mentions légales des factures |
| SIRET et ville du greffe (RCS) | Identification de l'entreprise ; le SIRET est contrôlé (clé de validité) |
| Numéro de TVA intracommunautaire | Obligatoire sauf en franchise en base |
| Régime de TVA | Normal, ou franchise en base (art. 293 B du CGI) : ProfitOS ajoute alors la mention légale |
| TVA sur les débits | À cocher si vous avez opté pour les débits (prestations de services) |
| Adresse, code postal, ville | Adresse du siège |
| Conditions de paiement, pénalités de retard, escompte | Mentions obligatoires de bas de facture |
| IBAN et BIC | Affichés sur la facture pour être payé par virement |

Tant qu'une mention obligatoire manque, ProfitOS vous le signale avant l'émission d'une facture.

### Se repérer dans l'interface

- **Menu simplifié ou complet** : par défaut, le menu ne montre que l'essentiel. Le bouton **Afficher toutes les fonctionnalités**, en bas du menu, ouvre le menu complet ; **Revenir au menu simplifié** le referme.
- **Thème clair ou sombre** : bouton **Thème clair** / **Thème sombre** dans l'encadré de votre compte, en bas du menu.
- **Nouveautés** : le bouton en haut à droite liste les dernières améliorations.
- **Sur téléphone** : le menu s'ouvre avec le bouton en haut de l'écran.
- **Premiers pas** : le tableau de bord affiche une liste de démarrage (profil complété, premières factures et dépenses importées, première action, collègue invité) qui se coche au fur et à mesure.

## 3. Formules et abonnement

Quatre formules de 9 à 199 € HT par mois, sans engagement en mensuel, avec 2 mois offerts en paiement annuel. Menu **Entreprise & compte → Abonnement**.

|  | Starter | Pro | Business | Multi-sociétés |
| --- | --- | --- | --- | --- |
| Prix mensuel (HT) | 9 € | 29 € | 59 € | 199 € |
| Prix annuel (HT) | 90 € | 290 € | 590 € | 1 990 € |
| Utilisateurs | 2 | 5 | 20 | 100 |
| Organisations | 1 | 1 | 1 | 25 |
| Imports par mois | 20 | 200 | Illimités | Illimités |
| Rapports par mois | 10 | 100 | Illimités | Illimités |
| Lectures automatiques de documents par mois | 30 | 200 | 1 000 | 3 000 |
| Facturation, achats, relances, imports | Oui | Oui | Oui | Oui |
| Comptabilité, TVA, FEC, immobilisations, emprunts | — | Oui | Oui | Oui |
| Banque et rapprochement | — | Oui | Oui | Oui |
| API, webhooks, connecteurs e-commerce | — | Oui | Oui | Oui |
| Trésorerie et scénarios, simulateur, diagnostic | — | — | Oui | Oui |
| Multi-sociétés, consolidation, portefeuille cabinet | — | — | — | Oui |

Les prix sont hors taxes ; la TVA à 20 % s'ajoute sur la facture d'abonnement.

### Essai gratuit

L'essai dure 14 jours avec toutes les fonctions de la formule Pro, sans carte bancaire. À son terme, l'accès est suspendu jusqu'à la souscription d'une formule. Vos données sont conservées et vos exports (FEC, factures, export RGPD) restent disponibles.

### Changer de formule

- **Formule supérieure** : active immédiatement ; la différence est facturée au prorata des jours restants.
- **Formule inférieure** : appliquée immédiatement ; le montant déjà payé pour le reste du mois est déduit de votre prochaine facture. ProfitOS vérifie d'abord que vos données tiennent dans la nouvelle formule (nombre d'utilisateurs, d'organisations).
- **Paiement annuel** : vous pouvez monter de formule en cours d'année ; le passage à une formule inférieure ou au mensuel se fait à l'échéance annuelle.

Quand vous cliquez sur une fonction non incluse, ProfitOS vous amène directement à la formule qui la contient.

### Paiement, factures et résiliation

Le paiement est traité par Stripe ; ProfitOS ne conserve aucune donnée de carte. Le bouton **Gérer mon abonnement** ouvre l'espace sécurisé Stripe : moyen de paiement, factures, résiliation. Après une résiliation, l'accès continue jusqu'à la fin de la période payée. En cas d'échec de paiement, un délai de grâce de 7 jours s'applique avant suspension.

## 4. Tableau de bord

Le tableau de bord répond en un coup d'œil à trois questions : combien j'ai, combien on me doit, que dois-je faire aujourd'hui. Menu **Tableau de bord**.

| Bloc | Ce qu'il montre |
| --- | --- |
| Message du jour | Les factures clients en retard aujourd'hui, ou l'absence de retard |
| Premiers pas | La liste de démarrage, tant qu'elle n'est pas terminée |
| Trésorerie | Solde déclaré et sa date, état (stable, vigilance, alerte), mini-courbe sur 90 jours, solde prévu à 30 et 90 jours, point bas |
| À encaisser | Total des factures clients échues non payées (émises et importées) |
| Économies potentielles | Dépenses à revoir repérées par ProfitOS |
| Gains vérifiés | Résultats confirmés des actions menées (relance payée, économie réalisée) |
| À traiter en priorité | Les actions les plus utiles du moment, avec un lien vers le Centre d'actions |

Le lien **à mettre à jour** apparaît sous le solde quand il date de plus de 7 jours : la prévision part de ce montant, il faut donc le tenir à jour (voir Trésorerie).

## 5. Facturation clients

Du devis à l'encaissement, chaque document est numéroté, conforme et comptabilisé automatiquement. Menu **Ventes & clients → Facturation**.

&#91;embedded content: cycle d'une facture · 6 étapes, 1 correction\]

Tout se joue à l'émission : avant, la facture se modifie librement ; après, elle est figée et seule un avoir la corrige.

### Clients

**Facturation → Clients → Nouveau client** : nom, adresse, e-mail, SIREN, numéro de TVA, pays, client public ou privé. Une fiche client remplie une fois préremplit tous les devis et factures suivants. Le SIREN est obligatoire pour un client professionnel français en facture électronique.

### Devis

1. **Devis → Nouveau devis** : client, lignes (description, quantité, prix unitaire HT, taux de TVA), date de validité.
2. **Envoyer** : le client reçoit un lien pour consulter le devis, le télécharger en PDF, l'**accepter** ou le **refuser** en ligne. Vous pouvez aussi marquer vous-même un devis accepté ou refusé.
3. Un devis accepté devient en un clic une **facture d'acompte** (pourcentage au choix), une **facture** complète, ou une **facture finale** qui déduit automatiquement les acomptes déjà facturés.

### Factures

1. **Nouvelle facture** : client, lignes, date d'émission, échéance, nature de l'opération (biens, services ou mixte), date de livraison ou de prestation.
2. La facture reste un **brouillon** modifiable, sans numéro, tant qu'elle n'est pas émise.
3. **Envoyer** (ou **Marquer émise**) : ProfitOS attribue le numéro légal, séquentiel et sans trou sur l'année (par exemple FA-2026-014), fige la facture et passe l'écriture de vente en comptabilité.
4. Le client reçoit un lien pour consulter et télécharger sa facture ; le PDF est au format Factur-X (voir section 6).

Une facture émise ne peut plus être modifiée ni supprimée : c'est la loi. Pour la corriger ou l'annuler, émettez un **avoir** depuis la facture (total ou partiel). Une **facture proforma** peut être générée à tout moment ; elle n'a pas de valeur comptable.

### Bons de livraison

**Facturation → Livraisons** : créez un bon de livraison, marquez-le livré, puis **Facturer** pour générer la facture correspondante.

### Factures récurrentes

**Facturation → Récurrentes** pour les abonnements et contrats : client, lignes, fréquence (semaine, mois, trimestre, année), délai de paiement, première date et date de fin facultative. ProfitOS prépare chaque facture en brouillon à la date prévue ; vous la vérifiez puis l'envoyez. Un modèle peut être mis en pause à tout moment. Les prochaines échéances sont reprises dans la prévision de trésorerie.

### Paiements

Sur une facture, **Règlement** enregistre un paiement total ou partiel (date, montant, moyen) ; **Marquer payée** solde la facture. Chaque règlement passe son écriture en banque. Avec le module Banque, ProfitOS propose de rapprocher chaque virement reçu de la facture correspondante (voir section 8).

### Relances clients et échéancier

- **Relances clients** liste les factures en retard, classées par un score de risque (montant, retard, historique) pour savoir qui relancer en premier. Les retenues de garantie y sont suivies jusqu'à leur date de libération.
- **Relancer** sur une facture prépare l'e-mail de relance ; rien n'est envoyé sans votre validation.
- **Échéancier** affiche sur un calendrier les échéances clients et fournisseurs.
- **Facturation → Créances** donne l'état de toutes les sommes dues par client.

## 6. Facture électronique

ProfitOS produit des factures électroniques au format Factur-X et les transmet par une plateforme agréée, comme l'exige la réforme française. Toutes les entreprises doivent pouvoir recevoir des factures électroniques à partir du 1er septembre 2026 ; l'émission devient obligatoire en septembre 2026 pour les grandes entreprises et ETI, en septembre 2027 pour les PME et micro-entreprises.

### Le format Factur-X

Chaque facture émise est un PDF lisible par une personne, qui contient aussi les données structurées de la facture (fichier XML, profil EN 16931). ProfitOS contrôle ces données avec les règles européennes et françaises avant l'envoi : SIREN du vendeur et de l'acheteur, mentions de paiement, cadre de facturation, taux de TVA, acomptes. Le bouton **Factur-X** d'une facture télécharge le fichier.

### Les nouvelles mentions obligatoires

- SIREN du client professionnel.
- Adresse de livraison quand elle diffère de l'adresse de facturation.
- Nature de l'opération : livraison de biens, prestation de services, ou les deux.
- Option pour le paiement de la TVA d'après les débits, le cas échéant.

ProfitOS demande ces informations au bon moment et bloque l'envoi s'il en manque une.

### Envoyer une facture par la plateforme

1. Émettez la facture (section 5).
2. Sur la facture, cliquez **Envoyer par la plateforme** : la facture part vers la plateforme de votre client.
3. Le statut se met à jour seul ; **Synchroniser** force la mise à jour.

| Statut | Signification |
| --- | --- |
| Déposée | Reçue par la plateforme, en cours de contrôle |
| Émise / Reçue | Transmise à la plateforme du client |
| Mise à disposition | Disponible pour le client |
| Prise en charge / Approuvée | Le client l'a acceptée |
| Refusée | Le client la rejette ; elle sort de la prévision de trésorerie |
| En litige / Suspendue | À régulariser avec le client ; exclue de la prévision |
| Rejetée | Refusée par la plateforme (données invalides) : corrigez puis renvoyez par un avoir et une nouvelle facture |

Le statut « Encaissée » n'est pas encore transmis automatiquement à la plateforme.

### Factures fournisseurs reçues

Les factures de vos fournisseurs arrivent dans **Facturation → Achats** via la plateforme (bouton **Synchroniser**). Pour chacune, vous pouvez l'approuver, la refuser ou la mettre en litige ; votre réponse est renvoyée au fournisseur.

### E-reporting

Les ventes à des particuliers et les opérations internationales ne passent pas par une facture électronique mais doivent être déclarées (e-reporting). **Facturation → E-reporting → Transmettre** prépare et envoie la déclaration de la période ; une preuve de dépôt est conservée.

### Diagnostic

**Facturation → Diagnostic facture électronique** vérifie votre paramétrage : profil entreprise complet, SIREN, connexion à la plateforme, clients sans SIREN.

## 7. Achats et dépenses

Toutes les factures fournisseurs passent par un circuit simple : saisie ou lecture automatique, validation, paiement, comptabilisation. Menu **Facturation → Achats**.

### Enregistrer une facture fournisseur

Trois façons de faire entrer une facture :

- **Importer un PDF** : déposez le PDF (même scanné) ; ProfitOS lit le fournisseur, le numéro, les dates, les montants HT, TVA et TTC, et propose une catégorie. Vous vérifiez avant d'enregistrer.
- **Boîte mail fournisseurs** : une adresse e-mail propre à votre compte ; transférez-y les factures reçues, elles arrivent dans vos achats.
- **Nouvelle facture** : saisie manuelle.

Les factures électroniques reçues par la plateforme arrivent aussi ici (section 6). ProfitOS signale une facture qui ressemble à une facture déjà enregistrée (même fournisseur, numéro ou montant) pour éviter de la payer deux fois.

### Valider et payer

1. **Valider** confirme la facture et passe l'écriture d'achat (charge, TVA déductible, fournisseur). **Rejeter** l'écarte avec un motif.
2. **Payer** enregistre un paiement total ou partiel. Le justificatif (PDF) reste attaché à la facture.
3. **Virements** prépare un lot de virements SEPA pour les factures à payer, à importer dans votre banque.

### Suivre ses fournisseurs

| Page | Usage |
| --- | --- |
| Fournisseurs | Fiche par fournisseur, historique des factures |
| Commandes | Bons de commande fournisseurs, puis réception de la marchandise |
| Dettes fournisseurs | Ce qui reste à payer, par fournisseur et par échéance |
| Prévisions de paiements | Les décaissements fournisseurs à venir |
| Récurrents | Les achats qui reviennent (loyer, abonnements) |
| Budgets et pilotage | Un budget par catégorie, comparé aux dépenses réelles |
| Export | Liste des achats en Excel ou CSV |

### Notes de frais

Menu **Notes de frais**. Un collaborateur crée une note, ajoute ses lignes (ticket, montant, catégorie ; frais kilométriques calculés avec le barème fiscal) puis la **soumet**. Un responsable la **valide** ou la **rejette**, puis la marque **remboursée**. Chaque étape passe ses écritures.

### Importer des dépenses

**Données & connexions → Importer des dépenses** accepte un fichier Excel, CSV ou des PDF (section 12). Les dépenses servent à repérer les économies possibles et à calculer la dépense courante de la prévision de trésorerie.

## 8. Banque

Vos opérations bancaires arrivent dans ProfitOS, soit par connexion à la banque, soit par import de relevés, puis se rapprochent de vos factures en quelques clics. Menu **Données & connexions → Banques connectées** (formule Pro et plus).

### Faire entrer ses opérations

- **Connecter ma banque** : connexion sécurisée en lecture seule à votre banque ; **Synchroniser** récupère les soldes et toutes les opérations passées. Les opérations encore « à venir » (carte à débit différé, virement en cours) sont importées une fois définitives. Le consentement bancaire est à renouveler périodiquement : ProfitOS l'indique quand c'est le cas.
- **Importer un relevé** (CSV, Excel ou PDF) : choisissez le nom du compte (par exemple « Qonto »), déposez le fichier. Les relevés avec une colonne Montant signée ou deux colonnes Débit et Crédit sont acceptés. Réimporter un relevé déjà importé, ou un relevé qui chevauche le précédent, n'ajoute aucun doublon. Si le relevé contient une colonne Solde, le dernier solde est enregistré.

### Rapprocher les opérations

Pour chaque encaissement, ProfitOS propose la ou les factures clients correspondantes (montant restant dû, numéro de facture, nom du client), avec un niveau de confiance ; pour chaque sortie, les factures fournisseurs.

1. Vérifiez la proposition et cliquez **Rapprocher**. Vous pouvez saisir un montant partiel, au format français (« 1 234,56 »).
2. Le paiement est enregistré sur la facture, son écriture passée en banque, et le statut de la facture mis à jour (payée ou partiellement payée).
3. Un virement peut régler plusieurs factures, et une facture recevoir plusieurs virements.

Aucun rapprochement n'est fait sans votre validation.

### Catégoriser les autres opérations

Une opération sans facture (abonnement, frais, impôt) se comptabilise directement depuis la liste des transactions : choisissez une catégorie, un compte comptable et un taux de TVA, puis **Valider**. ProfitOS sépare le HT et la TVA (TVA déductible sur immobilisations pour un compte de classe 2) et retient votre choix : la fois suivante, la même opération est préremplie et se valide en un clic.

- **Règles de catégorisation** : « tout libellé contenant URSSAF → Charges sociales », appliqué aux opérations existantes et futures.
- **Frais bancaires** : un petit écart restant après un rapprochement (5 € maximum) se comptabilise en frais bancaires d'un clic.
- **Ignorer** : pour un virement interne entre vos comptes, par exemple.

### Un compte comptable par compte bancaire

Dans le tableau des comptes bancaires, la colonne **Compte comptable** associe chaque banque à son compte 512 (par exemple 512100 pour Qonto, 512200 pour le Crédit Agricole). Toutes les écritures de ce compte bancaire utilisent alors ce compte. Créez d'abord le compte dans le plan comptable s'il n'existe pas.

### État de rapprochement bancaire

**Banques → État de rapprochement** compare, pour un compte et une date, le solde de la banque au solde du compte 512 :

1. Solde du relevé (le dernier solde connu, ou celui que vous saisissez).
2. Solde comptable du compte 512.
3. Plus les opérations bancaires pas encore comptabilisées.
4. Écart restant, expliqué par les écritures 512 sans opération bancaire (chèque émis pas encore débité, règlement saisi à la main).

Quand l'écart correspond au solde de la banque avant le premier mouvement importé, ProfitOS indique qu'il manque le solde d'ouverture en comptabilité. La page s'imprime pour votre dossier de clôture.

### Utiliser le solde bancaire

**Utiliser ces soldes dans le pilotage** reprend le total de vos comptes comme solde de départ de la trésorerie. Cette action n'est jamais faite automatiquement.

## 9. Trésorerie & scénarios

ProfitOS prévoit votre solde jour par jour sur 90 jours, à partir de tout ce qu'il sait déjà, et vous prévient avant une tension. Menu **Trésorerie → Trésorerie & scénarios** (formule Business et plus).

### 1. Donner le solde de départ

Saisissez votre trésorerie disponible (format « 12 345,67 » accepté) et cliquez **Mettre à jour**. Si vos comptes bancaires sont connectés ou vos relevés importés, un bandeau propose de reprendre leur solde en un clic. Au-delà de 7 jours, le solde est signalé comme ancien : toute la prévision part de ce montant.

### 2. Ce que la prévision prend en compte

| Source | Comment elle est datée |
| --- | --- |
| Factures clients restant dues (émises et importées) | À l'échéance, décalée selon l'habitude de paiement du client ; une facture déjà en retard est attendue sous 7 à 45 jours selon son retard |
| Factures récurrentes à venir | Date d'émission prévue + délai de paiement du modèle |
| Factures fournisseurs à payer | À leur échéance ; déjà échues : dès demain |
| Échéances d'emprunts et de crédit-bail | À leur date |
| TVA à payer | Estimée d'après la comptabilité, payée le 15 du mois suivant |
| Flux prévus que vous saisissez | À leurs dates (une fois ou répétés) |
| Dépense courante observée | Moyenne des 90 derniers jours de dépenses, répartie chaque jour |

Les factures refusées, en litige ou suspendues sont exclues et listées à part. Les flux attendus après 90 jours ne sont pas comptés.

**Habitude de paiement** : quand un client a au moins deux factures entièrement réglées, ProfitOS calcule son retard moyen (par exemple « paie en moyenne 12 jours après l'échéance ») et date ses prochains paiements en conséquence.

### 3. Lire les trois scénarios

- **Probable** : chaque encaissement à sa date attendue, compté à 100 %. C'est la référence des montants à 30, 60 et 90 jours.
- **Prudent** : encaissements 15 jours plus tard et seulement 55 % du restant dû compté.
- **Optimiste** : encaissements 10 jours plus tôt.

Les décaissements sont les mêmes dans les trois scénarios. Chaque scénario affiche son point bas et son solde à 90 jours.

### 4. Seuil de sécurité, alertes et autonomie

- **Seuil de sécurité** (réglage en bas de la page) : le montant sous lequel vous ne voulez pas descendre. Il s'affiche en pointillé sur le graphique.
- **Alerte** : tension si le scénario probable devient négatif ; vigilance s'il passe sous le seuil, si la marge à 30 jours est faible, ou si le scénario prudent crée une tension. La date prévue est toujours indiquée.
- **Autonomie de trésorerie** : au rythme net prévu, le nombre de mois avant d'épuiser la trésorerie, ou « En hausse » si elle progresse.

### 5. Comprendre d'où vient le résultat

**Du solde actuel à J+90** décompose les 90 prochains jours : solde de départ, chaque catégorie d'encaissements et de décaissements, solde d'arrivée. Le **plan de trésorerie mensuel** donne la même chose mois par mois (solde d'ouverture, flux par catégorie, solde de clôture, point bas du mois), exportable en Excel ou CSV. En dessous, le **réalisé bancaire** des 3 derniers mois sert de point de comparaison.

### 6. Ajouter les flux que les factures ne disent pas

Section **Flux prévus** : libellé, catégorie (salaires et charges sociales, loyer, impôts, abonnements, remboursement de financement, vente signée, subvention, apport…), montant, fréquence (une fois, semaine, mois, trimestre, année), première échéance et fin facultative.

Si vos relevés couvrent au moins 3 mois, ProfitOS repère les opérations qui reviennent chaque mois avec un montant stable (loyer, URSSAF, salaires…) et les propose : **Ajouter** les intègre à la prévision, **Masquer** les écarte. Les virements d'un client déjà facturé ne sont jamais proposés, puisque ses factures sont déjà comptées.

**Éviter le double comptage** : si vos dépenses importées contiennent déjà les salaires ou le loyer que vous saisissez en flux prévus, décochez « Compter la dépense courante observée » dans les réglages.

### 7. Tester l'encaissement de votre plus grosse facture

**Simulation d'encaissement** : choisissez quand votre plus gros client paiera (aujourd'hui, sous 7, 30 ou 60 jours, ou pas avant 90 jours) et voyez l'effet sur le point bas et le solde à 90 jours, sans modifier la facture.

### Flux projetés 90 jours

Menu **Trésorerie → Flux projetés 90 jours** : les mêmes données par tranches de 30 jours (encaissements, fournisseurs, autres sorties, dépenses courantes, flux net). Les montants sont identiques à ceux de Trésorerie & scénarios.

La prévision est une aide à la décision, pas une garantie : elle n'invente aucune dépense que vous n'avez pas enregistrée.

## 10. Aide à la décision

Trois pages s'appuient sur la prévision de trésorerie pour vous aider à décider. Elles sont visibles dans le menu complet (formule Business et plus).

### Simulateur de décision

Menu **Trésorerie → Simulateur de décision**. Il répond à la question : « puis-je me le permettre, et comment ? »

1. Choisissez le type de décision : investissement ou machine, embauche, dépense exceptionnelle, nouveau marché.
2. Renseignez le décaissement initial HT et son jour, le taux de TVA (payée avec le décaissement, récupérée le 15 du mois suivant), le coût mensuel supplémentaire (pour une embauche : salaire brut × 1,45 environ), la recette mensuelle que la décision rapporte, l'encaissement ponctuel attendu et son jour, la réserve minimale à préserver, le financement maximum accepté et la date limite de décision. Les montants acceptent le format « 12 500,50 ».
3. Dans **Contraintes réelles**, cochez les leviers que vous acceptez : report négociable (et de combien de jours), paiement fractionnable (en combien d'échéances), financement externe.
4. Dans **Rentabilité et financement**, choisissez l'horizon de rentabilité (36 mois par défaut), le taux et la durée du prêt envisagé.
5. **Simuler la décision**.

Le résultat se lit en cinq blocs :

| Bloc | Ce qu'il dit |
| --- | --- |
| Scénario initial | Verdict de la décision telle que saisie (soutenable, vigilance ou risqué) et besoin de financement pour éviter le négatif |
| Impact sur votre trésorerie | Graphique sur 90 jours : sans la décision, avec la décision, avec le plan optimisé, et votre réserve |
| Test de résistance | Le point bas si vos clients paient en retard (scénario prudent) |
| Rentabilité | Gain net mensuel, délai de retour sur investissement, résultat cumulé sur l'horizon choisi, rendement, et coût du prêt recommandé (mensualité, intérêts) |
| Plan optimisé | La meilleure date, le fractionnement et le financement, en n'utilisant que les leviers cochés, avec les trois alternatives les plus proches |

Un report ou un fractionnement reste à confirmer avec le fournisseur ou le partenaire ; le prêt, avec votre banque.

### Diagnostic financier

Menu **Trésorerie → Diagnostic financier**. Un score de maîtrise financière sur 100, la décision qui protège le plus votre argent en ce moment, la liste des décisions classées par impact, et une réponse à « peut-on financièrement prendre ce marché ? » pour vos opportunités. La page dit aussi ce que ProfitOS ne sait pas encore, pour que vous complétiez les données manquantes.

### Gisements de cash

Menu **Trésorerie → Gisements de cash**. Le montant que vous pourriez récupérer (factures clients en retard à relancer, économies repérées sur vos dépenses, marchés publics ouverts) et vos décisions prioritaires. Chaque piste se transforme en action dans le **Centre d'actions** ; une fois menée, son résultat réel s'enregistre dans **Gains vérifiés**. Les économies potentielles ne sont jamais ajoutées à la trésorerie prévue.

## 11. Comptabilité

La comptabilité se tient en grande partie toute seule : chaque facture, paiement et opération bancaire validée passe son écriture en partie double. Vous contrôlez, complétez et clôturez. Menu **Comptabilité** (formule Pro et plus).

### Ce qui est comptabilisé automatiquement

| Événement | Journal | Écriture |
| --- | --- | --- |
| Facture client émise | VE (ventes) | 411 Client / 706 ou 707 Ventes / 44571 TVA collectée |
| Avoir client | VE | Écriture inverse de la facture |
| Règlement client | BQ (banque) | 512 Banque / 411 Client, lettré avec la facture |
| Facture fournisseur validée | AC (achats) | Charge 6xx / 44566 TVA déductible / 401 Fournisseur |
| Paiement fournisseur | BQ | 401 Fournisseur / 512 Banque |
| Opération bancaire catégorisée | BQ | Compte choisi / TVA / 512 |
| Note de frais, paie importée, emprunt, crédit-bail, amortissement | OD ou journal dédié | Selon l'opération |

La TVA sur encaissements (prestations de services) est gérée : elle devient exigible au paiement, au prorata de chaque règlement.

### Consulter

- **Journaux** : les écritures par journal, numérotées en continu par journal et par année.
- **Grand livre** : toutes les lignes par compte, filtrables par période, compte ou tiers ; export Excel ou CSV.
- **Balance** : générale, clients ou fournisseurs, avec soldes d'ouverture, mouvements et soldes ; ProfitOS confirme qu'elle est équilibrée.
- **Lettrage** : rapproche les factures et leurs règlements sur un compte de tiers.
- **Plan comptable** : le plan comptable général français est préinstallé ; vous pouvez ajouter des comptes (par exemple un 512100 pour une seconde banque) ou en désactiver.

### Saisir et corriger

- **Nouvelle écriture** : journal, date, libellé, référence de pièce et lignes au débit et au crédit ; l'écriture n'est enregistrée que si elle est équilibrée.
- **Pièces jointes** : attachez le justificatif (PDF, image, tableur) à une écriture.
- **Contrepasser** : une écriture enregistrée ne se modifie pas ; on passe une écriture inverse datée, puis la bonne. C'est l'exigence d'inaltérabilité de la comptabilité.
- **Analytique** : étiquetez les lignes par projet, chantier ou activité pour suivre la rentabilité de chacun.

### TVA

- **TVA** : récapitulatif de la TVA collectée par taux et de la TVA déductible sur une période.
- **Déclaration CA3** : les cases du formulaire 3310-CA3 préremplies à partir des écritures (base HT et TVA par taux, TVA déductible sur immobilisations et sur autres biens et services, crédit antérieur, TVA nette due ou crédit à reporter). Vous reportez les montants sur impots.gouv.fr ou les transmettez à votre expert-comptable ; ProfitOS ne télédéclare pas.
- **Liquider** passe l'écriture de liquidation de la période (TVA à décaisser ou crédit à reporter), une seule fois par période.

### Immobilisations, emprunts, crédit-bail, paie

| Page | Ce qu'elle fait |
| --- | --- |
| Immobilisations | Registre des biens ; amortissement linéaire ou dégressif, dotations calculées et passées en écriture ; cession |
| Emprunts | Tableau d'amortissement calculé (capital, intérêts) ; chaque échéance payée passe son écriture ; échéances reprises dans la trésorerie |
| Crédit-bail | Contrat, loyers à payer, levée d'option d'achat |
| Paie | Import du journal de paie de votre logiciel de paie, contrôle puis validation en écriture |
| Cut-off | Charges à payer, produits à recevoir, charges constatées d'avance ; extourne à l'ouverture de l'exercice suivant |

### Clôturer l'exercice

1. **Exercices** : déclarez vos exercices comptables (dates de début et de fin, y compris un premier exercice de plus ou moins de 12 mois).
2. **Révision** : une liste de contrôles de fin d'exercice à cocher, avec un diagnostic automatique ; vous pouvez y inviter votre expert-comptable.
3. **Clôture** : verrouille la période (plus aucune écriture possible avant la date de clôture) et génère les à-nouveaux de l'exercice suivant, le résultat étant reporté en 120 (bénéfice) ou 129 (perte).
4. **Plaquette** : bilan et compte de résultat de l'exercice en PDF.
5. **Liasse 2033** : les tableaux 2033-A (bilan simplifié) et 2033-B (compte de résultat simplifié) préremplis depuis la balance, pour le régime réel simplifié.

### Export FEC

**Export FEC** génère le fichier des écritures comptables au format exigé par l'administration fiscale (article A47 A-1 du LPF) pour une période. Vous pouvez le télécharger ou l'envoyer directement à votre expert-comptable par un lien sécurisé à durée limitée.

## 12. Importer ses données

Vous pouvez démarrer avec l'historique de votre ancien outil : factures, dépenses, relevés et comptabilité s'importent depuis un fichier. Menu **Données & connexions**.

### Formats acceptés

- Fichiers **Excel** (.xlsx, .xls), **CSV** et, pour les factures, dépenses et relevés, **PDF** (même scannés).
- CSV au format Excel français : séparateur « ; » ou « , », montants « 1 234,56 », dates « jj/mm/aaaa », accents en UTF-8 ou Windows.
- Les noms de colonnes courants en français et en anglais sont reconnus (« Montant TTC », « amount », « Échéance »…).
- Sur chaque page d'import, **Télécharger le modèle CSV** donne un fichier prêt à remplir avec les bonnes colonnes.

### Que peut-on importer ?

| Import | Colonnes minimales | Remarques |
| --- | --- | --- |
| Factures clients | Numéro, client, montant TTC, échéance | Plusieurs PDF à la fois possibles ; l'import remplace les factures importées précédemment pour la même entité |
| Dépenses | Fournisseur, montant, date | Catégorie attribuée automatiquement si absente |
| Relevé bancaire | Date, montant (ou Débit et Crédit) | Voir section 8 ; sans doublon à la réimportation |
| Comptabilité (FEC) | Fichier FEC complet | Voir ci-dessous |

Le nombre d'imports par mois dépend de la formule (20 en Starter, 200 en Pro, illimité au-delà).

### Reprendre une comptabilité existante

**Comptabilité → Reprise** importe le FEC exporté de votre ancien logiciel comptable, au choix :

- **toutes les écritures**, pour retrouver l'historique complet (grand livre, balance) ;
- **les soldes seulement**, pour démarrer avec les à-nouveaux de l'exercice.

Les comptes absents du plan comptable sont créés, et l'import est refusé s'il est déséquilibré ou s'il touche une période clôturée. Tout ou rien : la moindre erreur annule l'import, et un même fichier ne peut être importé qu'une fois.

## 13. Équipe, multi-sociétés et sécurité

Chaque collaborateur a son propre compte et un rôle qui limite ce qu'il voit et fait ; votre compte est protégé par mot de passe, double authentification et contrôle des appareils.

### Inviter son équipe

Menu **Entreprise & compte → Équipe** : saisissez l'e-mail du collaborateur et choisissez son rôle. Le nombre d'utilisateurs dépend de la formule (section 3).

| Rôle | Peut |
| --- | --- |
| Propriétaire | Tout, y compris l'abonnement et la suppression du compte |
| Administrateur | Tout sauf l'abonnement |
| Comptable | Facturation, comptabilité, banque, trésorerie, imports, relances, espace cabinet |
| Commercial | Marchés publics et actions |
| Membre | Les modules métier, sans les paramètres, l'équipe ni l'abonnement |
| Lecture seule | Consulter les modules, sans rien modifier |

Retirer un membre, créer une clé d'API ou supprimer le compte demande de ressaisir votre mot de passe.

### Travailler avec son expert-comptable

Depuis la **Révision** des comptes, invitez votre expert-comptable : il accède au dossier, échange avec vous par demandes et commentaires, et reçoit le FEC par lien sécurisé.

### Plusieurs sociétés

- **Plusieurs entités** dans une même organisation (filiales, établissements) : le sélecteur en haut du menu change l'entité active ; factures, banque, trésorerie et comptabilité sont séparées par entité, et un compte bancaire se rattache à une entité.
- **Plusieurs organisations** (formule Multi-sociétés, jusqu'à 25) : **+ Nouvelle organisation** en bas du menu ; elles sont couvertes par l'abonnement de l'organisation principale.
- **Consolidation et portefeuille cabinet** : vue d'ensemble des sociétés suivies.

### Sécuriser son compte

Menu **Entreprise & compte → Sécurité du compte**.

- **Double authentification (2FA)** : activez-la avec une application d'authentification (Google Authenticator, Microsoft Authenticator…). Conservez les 10 codes de secours affichés une seule fois ; chacun ne sert qu'une fois. Le propriétaire peut rendre la 2FA obligatoire pour tous les membres.
- **Sessions** : la liste de vos appareils connectés ; déconnectez-en un à distance. Une connexion depuis un nouvel appareil vous est signalée par e-mail.
- **Mot de passe et e-mail** : changer de mot de passe déconnecte vos autres appareils ; un changement d'e-mail se confirme depuis la nouvelle adresse.
- **Protection automatique** : après 10 mots de passe erronés, le compte est verrouillé temporairement ; une session inactive se ferme d'elle-même.

## 14. Données personnelles et export RGPD

Vos données vous appartiennent : vous pouvez les télécharger à tout moment et supprimer votre compte vous-même.

- **Mes données** (Sécurité du compte) : télécharge une archive ZIP de vos données personnelles d'utilisateur.
- **Export RGPD de l'organisation** (Paramètres, réservé au propriétaire et aux administrateurs) : une archive ZIP de toutes les données de l'organisation, une table par fichier CSV lisible dans Excel. Les secrets (mots de passe, jetons) n'y figurent jamais.
- **Exports métier** : factures en PDF et Factur-X, FEC, grand livre, balance, plan de trésorerie, achats en Excel ou CSV.
- **Supprimer le compte** (Paramètres) : saisissez le nom exact de l'organisation et votre mot de passe. Si vous êtes le dernier membre, la suppression de l'organisation est programmée avec un délai de grâce de 14 jours, pendant lequel elle est inaccessible mais restaurable sur demande ; elle est ensuite définitive. Sinon, vous quittez simplement l'organisation. Un propriétaire unique doit d'abord nommer un autre propriétaire.

Les identifiants de connexion à des services tiers (stockage, boutique en ligne) et les secrets de double authentification sont chiffrés, et les données sont sauvegardées chaque jour.

Avant de supprimer votre compte, pensez à télécharger vos factures et votre FEC : la loi impose de conserver les pièces comptables 10 ans.

## 15. Questions fréquentes et dépannage

| Problème | Solution |
| --- | --- |
| Je ne peux pas émettre ma facture | Une mention obligatoire manque : complétez le profil entreprise (SIRET, TVA, adresse, conditions de paiement) ou la fiche client (SIREN, adresse). Le message indique le champ. |
| J'ai fait une erreur sur une facture émise | Une facture émise ne se modifie pas : émettez un avoir depuis la facture, puis une nouvelle facture correcte. |
| Un module affiche un cadenas | Il n'est pas inclus dans votre formule ; cliquez dessus pour voir la formule qui l'inclut et en changer. |
| « Connexion bancaire indisponible » | La connexion automatique n'est pas activée pour votre compte : importez vos relevés (CSV, Excel, PDF), ils fonctionnent de la même façon. |
| Mon import est refusé (« colonnes manquantes ») | Téléchargez le modèle CSV de la page d'import et reprenez ses en-têtes de colonnes. |
| Le relevé importé n'ajoute rien | Ses opérations étaient déjà importées : ProfitOS écarte les doublons. |
| La prévision de trésorerie me semble trop optimiste | Ajoutez les salaires, le loyer et les impôts en flux prévus, ou validez ceux repérés sur vos relevés ; vérifiez que le solde de départ est récent. |
| Une charge semble comptée deux fois dans la prévision | Décochez « Compter la dépense courante observée » si vos charges récurrentes sont saisies en flux prévus. |
| L'état de rapprochement montre un écart | Traitez les opérations bancaires non comptabilisées, puis vérifiez les écritures 512 sans mouvement bancaire et le solde d'ouverture. |
| Je ne peux plus saisir d'écriture à une date | La période est clôturée ; passez l'écriture sur la période ouverte. |
| Je ne reçois pas l'e-mail de confirmation | Vérifiez les indésirables, puis cliquez « Renvoyer l'e-mail de vérification » dans le bandeau jaune. |
| Mon compte est verrouillé | Trop de mots de passe erronés : patientez quelques minutes ou réinitialisez votre mot de passe depuis la page de connexion. |
| J'ai perdu mon téléphone (2FA) | Connectez-vous avec un de vos codes de secours, puis réactivez la double authentification sur le nouveau téléphone. |

Pour toute autre question, écrivez au support à l'adresse indiquée dans les mentions légales du site.
