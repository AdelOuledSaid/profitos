# Veqalio — Resend

Variables Render nécessaires :

- `RESEND_API_KEY` : secret défini manuellement dans Render (service web ET cron
  `profitos-jobs`), jamais dans Git.
- `RESEND_FROM_EMAIL=Veqalio <noreply@profitos.fr>` (groupe `profitos-shared`)
- `APP_BASE_URL=https://app.profitos.fr`

Sans `RESEND_API_KEY` ni `SMTP_HOST`, le préflight de déploiement échoue en production :
aucun email (vérification, mot de passe oublié, relances) ne partirait.

## DNS du domaine `profitos.fr`

Dans Resend → Domains, ajouter `profitos.fr` et créer chez le registrar les enregistrements
indiqués :

1. **SPF** (TXT) : inclut l'expéditeur Resend (`include:amazonses.com` sur le sous-domaine
   indiqué par Resend).
2. **DKIM** (TXT/CNAME `resend._domainkey`) : clé fournie par Resend.
3. **DMARC** (TXT `_dmarc.profitos.fr`) : commencer par
   `v=DMARC1; p=none; rua=mailto:dmarc@profitos.fr; adkim=s; aspf=s`, surveiller les rapports
   deux à quatre semaines, puis passer à `p=quarantine` et enfin `p=reject`.

Gmail et Yahoo exigent SPF + DKIM + DMARC alignés pour les envois en volume ; sans DMARC,
les factures et relances envoyées aux clients finaux risquent le dossier spam.

## Webhook de distribution (v492)

Sans ce webhook, un e-mail envoyé passe pour un e-mail reçu : une relance partie vers une
adresse morte compterait comme une relance faite.

1. Resend → **Webhooks** → *Add endpoint* : URL `https://app.profitos.fr/webhooks/resend`,
   événements `email.sent`, `email.delivered`, `email.delivery_delayed`, `email.bounced`,
   `email.complained` (et `email.failed` s'il est proposé).
2. Copier le *signing secret* (`whsec_…`) dans la variable Render `RESEND_WEBHOOK_SECRET`
   du **service web** (pas besoin sur le cron), puis redéployer.
3. Depuis Resend, envoyer un événement de test : la réponse doit être `200`.

Effets : chaque facture, relance ou devis envoyé est suivi (remis, retardé, rejeté,
signalé). Une adresse rejetée définitivement ou qui signale un spam est bloquée pour les
envois suivants ; la fiche facture l'indique, Mission Control crée une mission « ne reçoit
plus vos e-mails » et l'écran **Facturation → E-mails non distribués** permet de corriger
l'adresse sur les factures non réglées et la fiche client. Les e-mails de compte
(vérification, mot de passe) restent tentés. Sans secret, la route répond `503` et rien
n'est traité ; une signature invalide ou trop ancienne (plus de 5 minutes) est refusée.

`python scripts/security_check.py` signale un envoi par Resend sans secret de webhook.

## Emails envoyés au nom d'une organisation

Factures, devis et relances partent avec l'expéditeur « <Organisation> via Veqalio
<noreply@profitos.fr> », une adresse de réponse vers le propriétaire de l'organisation et un
pied de page avec sa raison sociale, son adresse et son SIRET. Le rapport hebdomadaire
contient un lien de désinscription et les en-têtes `List-Unsubscribe` /
`List-Unsubscribe-Post` (désinscription en un clic).

## Parcours à tester après déploiement

1. Créer un nouveau compte avec une adresse email accessible.
2. Vérifier que l'email de confirmation de compte arrive (pas en spam).
3. Cliquer sur le bouton de vérification.
4. Tester « Mot de passe oublié ».
5. Envoyer une facture de test à votre propre adresse : vérifier l'expéditeur, le
   « Répondre à » et le pied de page.
6. En cas d'échec : logs Resend, et logs Render (`EMAIL_SEND_FAILED`, adresses masquées).
