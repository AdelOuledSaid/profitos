# Application mobile ProfitOS — guide de finalisation

Ce dossier `mobile/` contient le point de départ d'une vraie application
native (iOS + Android), construite avec Capacitor autour de l'application
web ProfitOS déjà en ligne. **Rien de tout cela n'a pu être exécuté ni
testé dans mon environnement** — pas d'accès réseau pour installer les
paquets npm, pas de Xcode, pas d'Android Studio. Ce qui suit est la marche
à suivre précise pour terminer chez toi.

## Ce que fait cette approche

Capacitor ne réécrit pas ProfitOS en une nouvelle application séparée. Il
enveloppe l'application web déjà hébergée (`https://app.profitos.fr`) dans
une coquille native légère — un `WebView` plein écran, sans barre
d'adresse ni interface de navigateur, qui a accès aux API natives du
téléphone (caméra, notifications, etc.) via des plugins. C'est l'approche
la plus rapide pour obtenir une présence App Store / Play Store sans
réécrire l'application en React Native ou Flutter.

**Conséquence importante** : l'app mobile chargera toujours la version
*en ligne* de ProfitOS. Une mise à jour du site web se répercute
immédiatement dans l'app, sans repasser par une validation Apple/Google —
sauf si tu changes du code natif lui-même (icônes, plugins, permissions).

## Prérequis, une seule fois

- **Node.js** (déjà présent chez toi si tu utilises `npm` ailleurs)
- **Pour iOS** : un Mac avec Xcode installé (obligatoire — Apple ne permet
  pas de compiler une app iOS ailleurs que sur macOS), et un compte
  développeur Apple (99 $/an) pour publier sur l'App Store
- **Pour Android** : Android Studio (disponible sur Windows/Mac/Linux), et
  un compte développeur Google Play (25 $, paiement unique)

## Étapes

```bash
cd mobile
npm install
```

Ça installe Capacitor et les plugins listés dans `package.json`
(notifications push, status bar, splash screen).

### Générer les icônes et écrans de démarrage

```bash
npm install -g @capacitor/assets
npx capacitor-assets generate --iconBackgroundColor '#081020' --splashBackgroundColor '#081020'
```

Cette commande lit `static/icons/icon-512.png` (déjà présent dans le
projet ProfitOS) et génère automatiquement toutes les tailles nécessaires
pour iOS et Android — pas besoin de les créer à la main.

### Ajouter les plateformes natives

```bash
npx cap add ios
npx cap add android
```

Ça crée deux nouveaux dossiers, `ios/` et `android/`, contenant de vrais
projets Xcode et Android Studio — la partie que je ne peux pas générer
depuis mon environnement, faute d'accès réseau pour télécharger les
templates de plateforme.

### Ouvrir et lancer

```bash
npx cap open ios       # ouvre le projet dans Xcode
npx cap open android   # ouvre le projet dans Android Studio
```

Depuis Xcode, branche un iPhone ou lance le simulateur, clique sur
▶ Run. Depuis Android Studio, pareil avec un appareil ou un émulateur.
**C'est le tout premier moment où cette application aura réellement été
exécutée** — teste soigneusement avant toute publication.

### Après chaque modification de `capacitor.config.json` ou des plugins

```bash
npx cap sync
```

## Ce qui marche déjà sans rien faire de plus

- Le formulaire de note de frais utilise déjà `capture="environment"` sur
  le champ photo (construit plus tôt cette session) — dans le WebView
  Capacitor, ça devrait ouvrir directement l'appareil photo comme sur le
  web mobile. **À confirmer une fois l'app compilée**, le comportement
  exact d'un attribut HTML standard dans un WebView natif peut varier
  légèrement d'une plateforme à l'autre.
- Le mode hors-ligne du service worker existant continue de fonctionner
  dans le WebView.

## Ce qui reste à construire si tu veux aller plus loin

- **Notifications push réelles** : le paquet `@capacitor/push-notifications`
  est déjà dans `package.json`, mais le câblage complet demande un projet
  Firebase (Android) et un certificat APNs (iOS), plus une route côté
  serveur ProfitOS pour enregistrer les jetons d'appareil et déclencher les
  envois — rien de tout ça n'est construit pour l'instant.
- **Revue avant publication** : Apple et Google examinent chaque
  soumission manuellement. Prévois quelques jours de délai, et assure-toi
  que les mentions légales/CGU sont accessibles depuis l'app.

## Fichiers de ce dossier

- `package.json` — dépendances Capacitor
- `capacitor.config.json` — pointe vers `https://app.profitos.fr`, à
  changer si ton domaine de production est différent
- `www/index.html` — fichier requis par Capacitor pour le tout premier
  scaffold, jamais réellement affiché (le `server.url` prend le dessus)
