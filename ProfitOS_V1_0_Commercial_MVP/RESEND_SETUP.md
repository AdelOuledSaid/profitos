# ProfitOS — Resend

Variables Render nécessaires :

- `RESEND_API_KEY` : secret défini manuellement dans Render (service web ET cron
  `profitos-jobs`), jamais dans Git.
- `RESEND_FROM_EMAIL=ProfitOS <noreply@profitos.fr>` (groupe `profitos-shared`)
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

## Emails envoyés au nom d'une organisation

Factures, devis et relances partent avec l'expéditeur « <Organisation> via ProfitOS
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
