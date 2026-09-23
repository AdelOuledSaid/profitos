"""Multi-entités : gestion de filiales avec leur propre numérotation de
factures/achats, leur propre TVA, leur propre IBAN, et consolidation au
niveau groupe.

Principe de conception, volontairement prudent : la table `company`
existante reste l'entité principale (« société mère »), totalement
inchangée dans son fonctionnement — aucun code existant qui la lit
directement ne casse. Les filiales sont des lignes de la table `entities`,
et chaque document (facture, achat, écriture comptable) porte un
`entity_id` optionnel : NULL signifie « société mère », un entier renvoie
vers une ligne de `entities`. resolve_entity() est le point d'entrée unique
pour obtenir l'identité (nom, SIRET, TVA, IBAN...) à utiliser sur un
document donné, que ce soit la société mère ou une filiale — tout le reste
du code peut rester ignorant de cette distinction.
"""


def resolve_entity(conn, entity_id):
    """Retourne un dict avec l'identité à utiliser pour un document donné :
    nom, SIRET, TVA, adresse, IBAN, BIC. entity_id=None (ou falsy) renvoie
    la société mère (table company) ; un entier renvoie la filiale
    correspondante. Lève ValueError si l'entity_id pointe vers une filiale
    inexistante — jamais un document émis avec une identité silencieusement
    vide."""
    if not entity_id:
        row = conn.execute('SELECT * FROM company WHERE id=1').fetchone()
        if not row:
            return {'id': None, 'name': '', 'siret': '', 'vat_number': '',
                    'address': '', 'postal_code': '', 'iban': '', 'bic': ''}
        return {
            'id': None, 'name': row['name'] or '', 'siret': row['siret'] or '',
            'vat_number': row['vat_number'] or '', 'address': row['address'] or '',
            'postal_code': row['postal_code'] or '', 'iban': row['iban'] or '', 'bic': row['bic'] or '',
        }
    row = conn.execute('SELECT * FROM entities WHERE id=?', (entity_id,)).fetchone()
    if not row:
        raise ValueError(f"Entité inconnue : {entity_id!r}.")
    return {
        'id': row['id'], 'name': row['name'] or '', 'siret': row['siret'] or '',
        'vat_number': row['vat_number'] or '', 'address': row['address'] or '',
        'postal_code': row['postal_code'] or '', 'iban': row['iban'] or '', 'bic': row['bic'] or '',
    }


def list_all_entities(conn):
    """Retourne la liste complète des identités disponibles pour cette
    organisation : la société mère en premier (id=None), puis chaque
    filiale — pour peupler un sélecteur d'entité dans un formulaire."""
    entities = [resolve_entity(conn, None)]
    for row in conn.execute('SELECT id FROM entities ORDER BY name').fetchall():
        entities.append(resolve_entity(conn, row['id']))
    return entities
