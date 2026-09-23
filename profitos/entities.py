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


def current_entity_id():
    """Entité actuellement sélectionnée dans la session (sélecteur permanent
    en barre latérale) — None signifie société mère. Ne touche jamais la
    base : lecture pure de la session Flask."""
    from flask import session
    return session.get('current_entity_id')


def current_entity(conn):
    """Résout l'identité complète de l'entité actuellement sélectionnée en
    session. Si l'entité stockée en session a été supprimée entre-temps,
    retombe silencieusement sur la société mère plutôt que de faire planter
    la page — la session est nettoyée en conséquence."""
    entity_id = current_entity_id()
    try:
        return resolve_entity(conn, entity_id)
    except ValueError:
        from flask import session
        session.pop('current_entity_id', None)
        return resolve_entity(conn, None)


def user_has_entity_restrictions(conn, user_id):
    """True si cet utilisateur a des restrictions d'accès explicites — absence
    totale de ligne signifie accès complet à toutes les entités (comportement
    par défaut, rétrocompatible : aucun compte existant n'est restreint tant
    que personne n'a explicitement configuré de restriction)."""
    row = conn.execute('SELECT 1 FROM user_entity_access WHERE user_id=? LIMIT 1', (user_id,)).fetchone()
    return row is not None


def accessible_entities(conn, user_id):
    """Retourne la liste des entités que cet utilisateur peut voir/utiliser —
    société mère en premier si autorisée. Sans restriction configurée, renvoie
    tout (comme list_all_entities)."""
    if not user_id or not user_has_entity_restrictions(conn, user_id):
        return list_all_entities(conn)
    allowed_ids = {r['entity_id'] for r in conn.execute('SELECT entity_id FROM user_entity_access WHERE user_id=?', (user_id,)).fetchall()}
    result = []
    if None in allowed_ids:
        result.append(resolve_entity(conn, None))
    for row in conn.execute('SELECT id FROM entities ORDER BY name').fetchall():
        if row['id'] in allowed_ids:
            result.append(resolve_entity(conn, row['id']))
    return result


def user_can_access_entity(conn, user_id, entity_id):
    """Vérification stricte pour la défense en profondeur — jamais basée
    uniquement sur ce que montre l'interface. entity_id=None teste l'accès à
    la société mère."""
    if not user_id or not user_has_entity_restrictions(conn, user_id):
        return True
    row = conn.execute('SELECT 1 FROM user_entity_access WHERE user_id=? AND entity_id IS ?', (user_id, entity_id)).fetchone()
    return row is not None
    """Lit le solde de trésorerie de l'entité donnée (None = société mère,
    table financial_settings inchangée ; un entier = filiale, table
    entity_financial_settings séparée puisque financial_settings a une
    contrainte CHECK(id=1) que SQLite ne permet pas de modifier après coup).
    Retourne un dict {cash_balance, cash_as_of} — jamais None, avec des
    valeurs à zéro/vide si rien n'a encore été saisi pour cette entité."""
    if not entity_id:
        row = conn.execute('SELECT cash_balance,cash_as_of FROM financial_settings WHERE id=1').fetchone()
    else:
        row = conn.execute(
            'SELECT cash_balance,cash_as_of FROM entity_financial_settings WHERE entity_id=?', (entity_id,)
        ).fetchone()
    if not row:
        return {'cash_balance': None, 'cash_as_of': None}
    return {'cash_balance': row['cash_balance'], 'cash_as_of': row['cash_as_of']}


def set_cash_balance(conn, entity_id, cash_balance, cash_as_of, updated_at):
    """Écrit le solde de trésorerie de l'entité donnée, sans jamais toucher
    au solde d'une autre entité (contrairement à un UPDATE global sur une
    table à une seule ligne)."""
    if not entity_id:
        conn.execute(
            'INSERT INTO financial_settings(id,cash_balance,cash_as_of,updated_at) VALUES(1,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET cash_balance=excluded.cash_balance,'
            'cash_as_of=excluded.cash_as_of,updated_at=excluded.updated_at',
            (cash_balance, cash_as_of, updated_at),
        )
    else:
        conn.execute(
            'INSERT INTO entity_financial_settings(entity_id,cash_balance,cash_as_of,updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(entity_id) DO UPDATE SET cash_balance=excluded.cash_balance,'
            'cash_as_of=excluded.cash_as_of,updated_at=excluded.updated_at',
            (entity_id, cash_balance, cash_as_of, updated_at),
        )
    conn.commit()
