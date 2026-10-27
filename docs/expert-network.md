# Expert Network — fonctionnement et cadre (v485)

## Principe

Mission Control détecte et vérifie un problème ; quand une intervention humaine est utile,
l'entreprise demande une mission à un expert de l'annuaire privé de ProfitOS. L'expert
reçoit un **dossier factuel figé** (seulement les éléments cochés par l'entreprise, avec son
consentement explicite), propose un devis, échange et remet son livrable depuis un **lien
personnel** ; le résultat est ensuite **contrôlé dans les données** : le constat d'origine
doit disparaître de Mission Control.

| Étape | Où | Garde-fous |
| --- | --- | --- |
| Détection | Mission Control, Contract Guardian, Dossier, Radar | Constat vérifié, avec preuves |
| Demande | `/experts/demande` (lien « Faire appel à un expert » sur les constats) | Consentement obligatoire ; éléments transmis choisis (identité, constat) ; pas de coordonnées bancaires, pas d'accès au compte |
| Devis, échanges, livrable | `/expert/mission/<lien>` côté expert, `/experts/missions/<id>` côté entreprise | Lien secret (seule son empreinte SHA-256 est conservée), valable 90 jours, désactivé dès la clôture ou l'annulation ; liens de documents en https uniquement |
| Résultat | Suivi Mission Control | À l'acceptation du devis, le constat passe « en contrôle » ; il n'est soldé que si les données le confirment |

## Annuaire

Tenu par l'opérateur de ProfitOS sur `/admin/experts` (adresses autorisées dans
`PLATFORM_ADMIN_EMAILS`, adresse vérifiée exigée). Un expert n'est proposé aux entreprises
que s'il est **actif** et, pour l'expertise comptable et le conseil juridique, si son **numéro
d'inscription** (Ordre des experts-comptables, barreau) a été vérifié manuellement dans
l'annuaire officiel ; modifier le numéro annule la vérification. Les évaluations des
entreprises (1 à 5) sont visibles dans l'annuaire.

## Modèle économique : points à valider avant tout paiement

Cette version **ne prend aucune commission et ne gère aucun paiement** : les honoraires sont
convenus et réglés directement entre l'entreprise et l'expert.

- **Avocats** : le règlement intérieur national interdit la rémunération de l'apport
  d'affaires (art. 11.3) et le partage d'honoraires. Selon le guide du CNB sur les plateformes
  détenues par des tiers, un avocat peut contribuer **forfaitairement aux frais de
  fonctionnement** d'une plateforme (art. 19.4.2), mais pas par une somme calculée sur ses
  honoraires, ni par un forfait par dossier, ni par l'achat de demandes de devis ; une
  convention distincte avec la plateforme doit être transmise au bâtonnier. Le CNB a ouvert en
  septembre 2026 une consultation sur la rémunération de l'apport d'affaires **entre
  avocats**, sans effet à ce jour pour une plateforme.
- **Experts-comptables** : règles déontologiques propres (ordonnance de 1945, code de
  déontologie) ; à faire confirmer par le Conseil de l'Ordre avant toute rémunération liée aux
  missions.
- **Autres domaines** (trésorerie, RH, marketing, informatique) : professions non
  réglementées ; une commission est envisageable contractuellement, avec transparence vis-à-vis
  du client.
- Un abonnement fixe des experts aux outils, ou une offre « accompagnement coordonné » payée
  par l'entreprise, sont les pistes les plus compatibles avec ces règles ; à valider avec un
  avocat et l'Ordre avant lancement.

Sources : [Guide pratique du CNB — participation des avocats aux plateformes détenues par des
tiers](https://www.cnb.avocat.fr/sites/default/files/cnb_guide_pratique_participation_des_avocats_aux_plateformes_detenues_par_des_tiers.pdf) ;
[CNB — rémunération de l'apport d'affaires entre avocats, consultation de septembre 2026](https://cnb.avocat.fr/actualite/remuneration-de-l-apport-d-affaires-entre-avocats-le-cnb-ouvre-une-consultation).

## Suite possible

Dépôt de fichiers par l'expert, rapprochement automatique besoin → expert, équipes
pluridisciplinaires, signature électronique de la lettre de mission, paiement par un
établissement agréé.
