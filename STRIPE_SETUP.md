# Configurer la facturation Stripe

## Ce qui a été construit

- **Sans configuration Stripe** : l'app fonctionne normalement, sans aucune limite
  (mode démo/dev — utile pour toi et tes pilotes tant que tu ne factures pas).
- **Avec Stripe configuré** : après les 14 jours d'essai, l'accès à RECOVER/SAVE/GROW,
  au dashboard et aux uploads est bloqué et redirige vers `/billing` tant qu'aucun
  abonnement actif n'existe. Une bannière affiche le nombre de jours d'essai restants.
- Bouton "S'abonner" → crée une session Stripe Checkout (paiement carte hébergé par Stripe).
- Bouton "Gérer mon abonnement" → ouvre le Stripe Billing Portal (facture, moyen de
  paiement, annulation — géré entièrement par Stripe, rien à coder côté ProfitOS).
- Webhook `/billing/webhook` : Stripe informe ProfitOS quand un paiement réussit,
  qu'un abonnement est annulé, ou qu'un paiement échoue — met à jour automatiquement
  `plan`/`status` de l'organisation en base.

⚠️ **Non testé en conditions réelles** dans cet environnement de développement (pas
d'accès réseau pour appeler l'API Stripe). Le comportement "billing désactivé" (sans
clés) est testé et fonctionne. Le parcours de paiement réel doit être testé en
**mode test Stripe** avant toute mise en production — voir ci-dessous.

## Étapes pour configurer (mode test, gratuit, aucune carte réelle débitée)

1. **Crée un compte Stripe** sur stripe.com si tu n'en as pas.
2. Reste en **mode Test** (interrupteur en haut à droite du dashboard Stripe).
3. **Crée un produit** : Produits → Ajouter un produit → nom "ProfitOS Pro",
   prix récurrent (ex. 149 €/mois). Stripe te donne un `Price ID` du type `price_1AbC...`.
4. **Récupère tes clés API** : Développeurs → Clés API →
   `Clé secrète` (`sk_test_...`) et `Clé publiable` (`pk_test_...`).
5. **Configure le webhook** : Développeurs → Webhooks → Ajouter un endpoint.
   - En local, utilise le [Stripe CLI](https://stripe.com/docs/stripe-cli) :
     `stripe listen --forward-to localhost:5050/billing/webhook`
     — il t'affiche un secret `whsec_...` à copier dans `.env`.
   - En production, l'URL sera `https://tondomaine.com/billing/webhook`, et Stripe
     te donnera le `whsec_...` correspondant directement dans le dashboard.
6. **Remplis `.env`** :
   ```
   STRIPE_SECRET_KEY=sk_test_xxxx
   STRIPE_PUBLISHABLE_KEY=pk_test_xxxx
   STRIPE_PRICE_ID=price_xxxx
   STRIPE_WEBHOOK_SECRET=whsec_xxxx
   ```
7. `pip install -r requirements.txt` (installe la lib `stripe`).
8. Relance `python app.py`, va sur `/billing`, clique "S'abonner".
9. Utilise une [carte de test Stripe](https://stripe.com/docs/testing) comme
   `4242 4242 4242 4242`, n'importe quelle date future, n'importe quel CVC.
10. Vérifie que tu es bien redirigé, que le statut passe à "Plan PRO — actif" sur
    `/billing`, et que la table `organizations` a bien `status='ACTIVE_PAID'`.

## Passage en production (argent réel)

- Bascule le dashboard Stripe en mode **Live**, recrée le produit et récupère les
  clés `sk_live_...` / `pk_live_...` / le webhook `whsec_...` en live.
- Remplace les valeurs dans `.env` de production (jamais les clés live en local).
- Active Stripe Tax ou la TVA manuelle selon ta situation — non géré par ce code,
  à configurer côté Stripe si nécessaire.

## Limites connues

- Un seul plan/prix géré (`STRIPE_PRICE_ID` unique). Pour plusieurs plans
  (Starter/Pro/Enterprise), il faudra étendre `/billing/checkout` pour accepter
  un `price_id` en paramètre et adapter `billing.html`.
- Pas de gestion des essais Stripe natifs (`trial_period_days` sur Checkout) —
  le trial est géré côté ProfitOS (`trial_ends_at`), indépendamment de Stripe,
  ce qui est volontaire pour ne pas dépendre de Stripe pendant l'essai gratuit.
