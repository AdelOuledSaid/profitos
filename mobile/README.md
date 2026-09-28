# Application mobile ProfitOS — guide de finalisation

Ce dossier `mobile/` contient le point de départ d'une vraie application
native (iOS + Android), construite avec Capacitor autour de l'application
web ProfitOS déjà en ligne. Pour Android, le chemin recommandé est désormais
le workflow GitHub Actions `.github/workflows/android-capacitor.yml` : il
génère le projet natif dans le cloud et produit un APK Debug sans imposer
Node.js ni Android Studio sur le PC de l'utilisateur. La publication Play
Store reste une étape séparée, après test réel de l'APK.

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

## Prérequis

- **Android via GitHub Actions (recommandé)** : aucun Node.js ni Android Studio
  requis sur le PC. Il faut seulement déposer le projet sur GitHub et lancer
  l'action `Build ProfitOS Android`.
- **Développement Android local (facultatif)** : Node.js + Android Studio.
- **Pour iOS** : un Mac avec Xcode installé (obligatoire — Apple ne permet
  pas de compiler une app iOS ailleurs que sur macOS), et un compte
  développeur Apple (99 $/an) pour publier sur l'App Store
- **Pour Android** : Android Studio (disponible sur Windows/Mac/Linux), et
  un compte développeur Google Play (25 $, paiement unique)


## Build Android dans GitHub (recommandé)

1. Pousser le projet sur la branche `main`.
2. Ouvrir **Actions > Build ProfitOS Android > Run workflow**.
3. Sans secret de signature, le workflow produit `ProfitOS-Android-Debug-APK`,
   à installer sur un vrai téléphone Android pour les tests.
4. Pour produire l'AAB Release, configurer ensemble les quatre secrets GitHub :
   `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`,
   `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`.
5. Le workflow refuse une configuration partielle, vérifie le keystore et
   l'alias avec `keytool`, puis produit `ProfitOS-Android-Release-AAB`.

La génération d'un AAB ne signifie pas que l'application est publiée. Avant
Google Play, tester au minimum l'authentification, les écrans principaux, la
photo de justificatif, Powens, les téléchargements PDF et les liens externes.

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

## Ce qui est déjà réglé dans la configuration

- **Icône, splash screen, nom de l'app** — `capacitor.config.json` a déjà
  `appName: "ProfitOS"` et les couleurs du splash screen configurées.
  Lance `npx capacitor-assets generate` (voir plus haut) pour générer
  toutes les tailles d'icônes à partir de `static/icons/icon-512.png`.
- **Navigation vers les domaines externes** — Powens (connexion bancaire),
  WeInvoice, Swan, GoCardless, Dropbox, Google sont explicitement
  autorisés dans `server.allowNavigation`. Sans ça, la WebView bloque
  silencieusement toute redirection vers un domaine non listé — un clic
  sur "Connecter ma banque" n'aurait rien fait, sans message d'erreur.
- **Stockage sécurisé** — rien de spécifique n'a été ajouté : l'app ne
  stocke aujourd'hui aucune donnée sensible en local (la session
  d'authentification est gérée par les cookies sécurisés du WebView
  lui-même, comme sur le web). Si tu ajoutes plus tard un cache local de
  données financières, revois ce point avec `@capacitor/preferences` ou
  un plugin de stockage chiffré dédié.

## Ce qui reste à construire si tu veux aller plus loin

- **Notifications push réelles** : côté serveur, l'enregistrement du jeton
  est fait et testé (`POST /api/mobile/register-push-token`, protégé par
  CSRF via un en-tête `X-CSRF-Token` — récupère-le d'abord via
  `GET /api/mobile/csrf-token`). Il manque encore : le code client dans
  l'app (appeler `PushNotifications.register()` du plugin
  `@capacitor/push-notifications`, envoyer le jeton reçu à cette route), un
  projet Firebase (Android) et un certificat APNs (iOS), et le code serveur
  qui déclenche réellement l'envoi d'une notification (aucune notification
  n'est envoyée pour l'instant, seulement enregistrée).
- **Revue avant publication** : Apple et Google examinent chaque
  soumission manuellement. Prévois quelques jours de délai, et assure-toi
  que les mentions légales/CGU sont accessibles depuis l'app.

## Fichiers de ce dossier

- `package.json` — dépendances Capacitor
- `capacitor.config.json` — pointe vers `https://app.profitos.fr`, à
  changer si ton domaine de production est différent
- `www/index.html` — fichier requis par Capacitor pour le tout premier
  scaffold, jamais réellement affiché (le `server.url` prend le dessus)
