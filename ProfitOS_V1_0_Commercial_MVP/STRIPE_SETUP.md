# Configurer la facturation Stripe

## Comportement de l'application

- **En production, Stripe est obligatoire.** `scripts/cloud_preflight.py` (lancé au
  démarrage sur Render) échoue si `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` ou
  au moins un tarif `STRIPE_PRICE_*_ID` manque. Sans Stripe, personne ne peut
  souscrire et chaque organisation perd l'accès à la fin de son essai.
- **Si Stripe n'est pas configuré** (environnement de développement ou incident) :
  l'essai en cours reste utilisable ; la page `/billing` affiche toujours la formule,
  le statut et la date de fin d'essai, avec le message « Paiement en ligne bientôt
  disponible, contactez-nous ». Seul un environnement explicitement non production
  (`PROFITOS_ENV=development` ou `test`) garde l'accès ouvert sans Stripe ;
  `PROFITOS_ENV` absent ou inconnu est traité comme la production.
- **Essai** : 14 jours avec exactement les fonctions de la formule Pro. À la fin de
  l'essai, toute l'application redirige vers la page « Abonnement inactif »
  (`/abonnement-inactif`, accessible à tous les rôles). Le propriétaire y trouve le
  lien vers la facturation, les autres rôles le nom et l'e-mail du propriétaire.
  Les exports (FEC, factures, export RGPD) restent accessibles en lecture seule.
- **Impayé** : statut `PAST_DUE` uniquement depuis un abonnement actif, avec un
  délai de grâce de `PROFITOS_PAST_DUE_GRACE_DAYS` jours (7 par défaut), puis
  blocage (exports toujours disponibles).
- Contrôle d'accès centralisé : un garde `before_request`
  (`profitos/feature_access.py`, `enforce_subscription_access`) vérifie le statut
  d'abonnement et la formule pour chaque route connectée et pour l'API `/api/v1`.

## Formules et tarifs (HT)

| Formule | Mensuel (sans engagement) | Prix HT / mois | Annuel (2 mois offerts, facultatif) | Prix HT / an |
|---|---|---|---|---|
| Starter | `STRIPE_PRICE_STARTER_ID` | 9 € | `STRIPE_PRICE_STARTER_YEARLY_ID` | 90 € |
| Pro | `STRIPE_PRICE_PRO_ID` (ou l'ancien `STRIPE_PRICE_ID`) | 29 € | `STRIPE_PRICE_PRO_YEARLY_ID` | 290 € |
| Business | `STRIPE_PRICE_BUSINESS_ID` | 59 € | `STRIPE_PRICE_BUSINESS_YEARLY_ID` | 590 € |
| Multi-sociétés (25 organisations) | `STRIPE_PRICE_MULTI_ID` | 199 € | `STRIPE_PRICE_MULTI_YEARLY_ID` | 1 990 € |

L'option « Annuel » n'apparaît sur les pages Tarifs et Abonnement que si au moins
un tarif annuel est configuré. Les CGV prévoient un rappel avant chaque
renouvellement annuel : activez dans Stripe **Paramètres → Facturation →
Abonnements et e-mails → « Envoyer un rappel avant le renouvellement »**.

Les prix Stripe doivent être créés **hors taxes** (« Tax behavior : exclusive »).
TVA 20 % en sus, calculée par Stripe Tax (`automatic_tax`), avec collecte de
l'adresse de facturation et du numéro de TVA intracommunautaire. Si Stripe Tax
n'est pas encore activé sur le compte, mettre `STRIPE_AUTOMATIC_TAX=0`
temporairement (sinon Checkout refuse la session).

## Ce qui est construit

- Bouton « Choisir … » → session Stripe Checkout (`locale=fr`, codes promo
  acceptés). Si l'organisation est encore en essai, la fin d'essai est reportée
  dans Stripe (`subscription_data.trial_end`) : payer en avance ne fait perdre
  aucun jour d'essai.
- Anti double abonnement : si Stripe connaît déjà un abonnement actif / en essai /
  impayé pour le client, l'utilisateur est envoyé vers le portail ; les sessions
  Checkout encore ouvertes sont expirées avant d'en créer une nouvelle.
- Changement de formule depuis la page Abonnement (`/billing/changer-formule`,
  `Subscription.modify`) : formule supérieure immédiate, différence facturée au
  prorata (`always_invoice` + `pending_if_incomplete` : rien ne change si le
  paiement échoue) ; formule inférieure en mensuel immédiate avec avoir au prorata
  (`create_prorations`), refusée tant que l'équipe ou les organisations dépassent
  la nouvelle limite ; en annuel, descente et passage au mensuel à l'échéance.
- Bouton « Gérer mon abonnement » → Stripe Billing Portal (moyen de paiement,
  factures, résiliation).
- Webhook `/billing/webhook` (signé) : seul l'abonnement enregistré
  (`organizations.stripe_subscription_id`) peut modifier le statut, sauf la toute
  première association. L'abonnement est relu via l'API Stripe pour éviter les
  événements livrés dans le désordre. Un `price_id` inconnu ne change jamais la
  formule (journalisé).
- Multi-sociétés : les organisations créées depuis une organisation abonnée
  Multi-sociétés y sont rattachées (`organizations.parent_org_id`) et partagent son
  plan, son statut et ses limites.
- Parrainage : abonné → crédit d'un mois sur le solde client Stripe ; en essai →
  essai prolongé de 30 jours ; sinon → mois mis en attente et imputé au premier
  paiement.

## Étapes de configuration (mode test)

1. Créez un compte Stripe et restez en **mode Test**.
2. Créez un produit par formule (Starter, Pro, Business, Multi-sociétés) avec un
   prix récurrent mensuel HT (9 €, 29 €, 59 €, 199 €) et, si vous proposez
   l'annuel, un second prix récurrent annuel (90 €, 290 €, 590 €, 1 990 €).
   Notez chaque `price_…`.
3. Activez **Stripe Tax** (Paramètres → Taxes) et l'enregistrement TVA France.
4. Configurez le **Billing Portal** (Paramètres → Facturation → Portail client) :
   moyen de paiement, factures et résiliation (le changement de formule se fait
   dans ProfitOS).
5. Récupérez les clés API (`sk_test_…`, `pk_test_…`).
6. Webhook : Développeurs → Webhooks → endpoint `https://votre-domaine/billing/webhook`
   avec les événements `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.payment_succeeded`,
   `invoice.payment_failed`. En local :
   `stripe listen --forward-to localhost:5050/billing/webhook`.
7. Renseignez les variables (dans Render : Environment, elles sont déclarées dans
   `render.yaml` avec `sync: false`) :
   ```
   STRIPE_SECRET_KEY=sk_test_xxxx
   STRIPE_PUBLISHABLE_KEY=pk_test_xxxx
   STRIPE_WEBHOOK_SECRET=whsec_xxxx
   STRIPE_PRICE_STARTER_ID=price_xxxx
   STRIPE_PRICE_PRO_ID=price_xxxx
   STRIPE_PRICE_BUSINESS_ID=price_xxxx
   STRIPE_PRICE_MULTI_ID=price_xxxx
   # facultatif : tarifs annuels
   STRIPE_PRICE_STARTER_YEARLY_ID=price_xxxx
   STRIPE_PRICE_PRO_YEARLY_ID=price_xxxx
   STRIPE_PRICE_BUSINESS_YEARLY_ID=price_xxxx
   STRIPE_PRICE_MULTI_YEARLY_ID=price_xxxx
   STRIPE_AUTOMATIC_TAX=1
   ```
8. Testez avec la carte `4242 4242 4242 4242` et vérifiez que `/billing` affiche la
   formule active et que `organizations.status='ACTIVE_PAID'`.

## Passage en production

- Basculez Stripe en mode **Live**, recréez produits/prix, webhook et clés live, et
  remplacez les variables dans Render (jamais de clés live en local).
- Un webhook live et des clés test (ou l'inverse) sont rejetés (`mode mismatch`).
