# Google Play — éléments de préparation

## État technique Android

Le workflow GitHub Actions produit systématiquement un **APK Debug**. Un
**AAB Release signé** est produit uniquement lorsque les quatre secrets de
signature Android sont présents et que le keystore/alias passent la
vérification `keytool`. L'AAB doit encore être testé puis téléversé dans
Google Play Console : sa génération ne vaut ni publication ni validation par
Google.

Brouillon à adapter et faire relire par un juriste avant publication —
je ne suis pas juriste, et une politique de confidentialité qui gère des
données bancaires (via Powens) et comptables a de vrais enjeux légaux
(RGPD notamment). Ce document pose une base sérieuse, pas un texte final.

## Politique de confidentialité — brouillon

*À héberger sur une page publique (ex: https://profitos.fr/confidentialite)
— Google Play exige un lien vers une politique de confidentialité
accessible sans connexion, pas un fichier joint.*

---

**Politique de confidentialité — ProfitOS**
*Dernière mise à jour : [DATE]*

ProfitOS (ci-après « nous ») propose une application de gestion
financière et comptable pour les petites et moyennes entreprises.

**Données que nous collectons**
- Informations de compte : nom, email, mot de passe (chiffré), organisation
- Données financières que vous saisissez ou importez : factures, achats,
  notes de frais, écritures comptables
- Données bancaires, si vous connectez un compte bancaire via notre
  partenaire Powens (agréé DSP2) : soldes, transactions. Nous ne stockons
  jamais vos identifiants bancaires — Powens gère la connexion directement
  avec votre banque
- Données techniques : adresse IP, type d'appareil, journaux d'activité à
  des fins de sécurité et de support

**Pourquoi nous les utilisons**
- Fournir le service (tenue de comptabilité, facturation, suivi de trésorerie)
- Sécurité du compte et prévention de la fraude
- Support client
- Obligations légales et comptables (conservation des données comptables
  selon les durées légales françaises)

**Partage avec des tiers**
Nous ne vendons aucune donnée. Nous partageons certaines données
uniquement avec les prestataires nécessaires au fonctionnement du
service, chacun agissant comme sous-traitant au sens RGPD :
- Powens (connexion bancaire), si vous l'activez
- [Ajouter : hébergeur, prestataire d'emailing, etc.]
- [Si applicable : Swan, GoCardless, Shopify/WooCommerce — uniquement les
  intégrations que vous connectez vous-même]

**Vos droits (RGPD)**
Accès, rectification, suppression, portabilité, opposition — contactez
[EMAIL DPO/CONTACT] pour exercer ces droits.

**Conservation**
Les données comptables sont conservées conformément aux obligations
légales françaises (10 ans pour les pièces comptables). Les autres
données sont supprimées [DÉLAI] après clôture du compte.

**Sécurité**
Chiffrement en transit (HTTPS) et au repos, contrôle d'accès par rôle,
[détailler vos mesures réelles].

**Contact**
[EMAIL], [ADRESSE POSTALE si personne morale identifiée]

---

## Google Play — Data Safety (formulaire officiel)

Google demande de déclarer précisément chaque catégorie de données. Base
réaliste selon ce que l'app collecte effectivement :

| Catégorie | Collectée ? | Partagée ? | Motif |
|---|---|---|---|
| Nom, email | Oui | Non | Fonctionnement du compte |
| Informations financières (factures, montants) | Oui | Non | Fonctionnalité principale de l'app |
| Informations bancaires (si Powens connecté) | Oui | Oui (avec Powens) | Fonctionnalité principale de l'app |
| Identifiants (mot de passe) | Oui, chiffré | Non | Authentification |
| Journaux d'app / diagnostics | Oui | Non | Sécurité, débogage |

*Remplis le vrai formulaire directement dans Play Console — ce tableau
est un point de départ, pas une soumission automatique.*

## Fiche Google Play — brouillon

**Nom de l'app** : ProfitOS

**Description courte** (80 caractères max) :
> Facturation, comptabilité et trésorerie pour les PME françaises.

**Description longue** (brouillon, à retravailler) :
> ProfitOS centralise la gestion financière de votre entreprise :
> facturation client, suivi des achats fournisseurs, notes de frais,
> comptabilité complète (plan comptable, lettrage, FEC), et pilotage de
> trésorerie.
>
> Fonctionnalités principales :
> • Facturation et devis, envoi et suivi des paiements
> • Suivi des dépenses et notes de frais avec photo du justificatif
> • Comptabilité générale conforme au plan comptable français
> • Connexion bancaire sécurisée (via Powens, agréé DSP2)
> • Multi-entités pour les groupes et cabinets comptables
>
> [Ajouter votre proposition de valeur différenciante]

**Captures d'écran requises** : minimum 2, format téléphone (recommandé :
1080×1920 ou proche). À produire une fois l'app compilée et testée —
capture les écrans les plus parlants : tableau de bord, facturation,
notes de frais avec photo.

**Catégorie** : Finance ou Entreprise (à choisir dans Play Console)

**Classification de contenu** : questionnaire Play Console standard,
normalement "Tout public" pour une app de gestion sans contenu sensible.
