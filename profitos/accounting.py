"""Fondations comptables de ProfitOS : plan comptable, journaux et moteur
d'écritures en partie double.

Ce module est le SEUL point d'entrée autorisé pour créer des écritures
comptables (accounting_entries / accounting_entry_lines). Toute autre partie
du code qui a besoin de générer des écritures doit appeler create_entry() —
jamais insérer directement dans ces tables — pour que l'équilibre
débit = crédit soit garanti à chaque écriture, sans exception.

Ce fichier ne connaît aucune donnée entreprise (factures, dépenses...) :
il ne fait que fournir la structure et le moteur. La génération automatique
d'écritures à partir des factures/dépenses/relevés bancaires est un chantier
séparé, construit au-dessus de ce module.
"""
from datetime import datetime, date


# --- Plan comptable par défaut (PCG français, sous-ensemble courant) -------
# Format : (code, libellé, classe, compte_collectif).
# compte_collectif=True signale un compte utilisé avec un tiers auxiliaire
# (ex. 411000 Clients, 401000 Fournisseurs) plutôt qu'en direct.
DEFAULT_CHART_OF_ACCOUNTS = [
    # Classe 1 — Capitaux
    ('101000', 'Capital social', 1, False),
    ('106800', 'Autres réserves', 1, False),
    ('120000', "Résultat de l'exercice (bénéfice)", 1, False),
    ('129000', "Résultat de l'exercice (perte)", 1, False),
    ('164000', "Emprunts auprès des établissements de crédit", 1, False),
    ('455000', 'Associés - comptes courants', 1, False),
    # Classe 2 — Immobilisations
    ('218300', 'Matériel de bureau et informatique', 2, False),
    ('281830', 'Amortissements du matériel de bureau et informatique', 2, False),
    # Classe 4 — Tiers
    ('401000', 'Fournisseurs', 4, True),
    ('408000', 'Fournisseurs - Factures non parvenues', 4, False),
    ('411000', 'Clients', 4, True),
    ('418000', 'Clients - Produits non encore facturés', 4, False),
    ('421000', 'Personnel - rémunérations dues', 4, False),
    ('431000', 'Sécurité sociale', 4, False),
    ('445510', 'TVA à décaisser', 4, False),
    ('445660', 'TVA déductible sur autres biens et services', 4, False),
    ('445710', 'TVA collectée', 4, False),
    ('447000', 'Autres impôts, taxes et versements assimilés', 4, False),
    ('467000', 'Autres comptes débiteurs ou créditeurs', 4, True),
    ('486000', "Charges constatées d'avance", 4, False),
    # Classe 5 — Financiers
    ('512000', 'Banque', 5, False),
    ('530000', 'Caisse', 5, False),
    # Classe 6 — Charges
    ('606100', 'Fournitures non stockables (eau, énergie)', 6, False),
    ('606400', 'Fournitures administratives', 6, False),
    ('606800', 'Autres matières et fournitures', 6, False),
    ('611000', 'Sous-traitance générale', 6, False),
    ('612000', 'Redevances de crédit-bail', 6, False),
    ('613000', 'Locations', 6, False),
    ('615000', 'Entretien et réparations', 6, False),
    ('616000', "Primes d'assurance", 6, False),
    ('618100', 'Documentation générale', 6, False),
    ('622600', 'Honoraires', 6, False),
    ('623000', 'Publicité, publications, relations publiques', 6, False),
    ('625100', 'Voyages et déplacements', 6, False),
    ('625600', 'Missions', 6, False),
    ('625700', 'Réceptions', 6, False),
    ('626000', 'Frais postaux et de télécommunications', 6, False),
    ('627000', 'Services bancaires', 6, False),
    ('628100', 'Cotisations diverses', 6, False),
    ('641000', 'Rémunérations du personnel', 6, False),
    ('645000', 'Charges de sécurité sociale et de prévoyance', 6, False),
    ('661000', "Charges d'intérêts", 6, False),
    ('671000', 'Charges exceptionnelles', 6, False),
    ('681000', "Dotations aux amortissements", 6, False),
    # Classe 7 — Produits
    ('706000', 'Prestations de services', 7, False),
    ('707000', 'Ventes de marchandises', 7, False),
    ('708000', 'Produits des activités annexes', 7, False),
    ('758000', 'Produits divers de gestion courante', 7, False),
    ('791000', 'Transferts de charges', 7, False),
]

# --- Journaux par défaut ----------------------------------------------------
# Format : (code, libellé, type). Les types suivent la convention FEC usuelle :
# ACHATS, VENTES, BANQUE, CAISSE, OD (opérations diverses), AN (à-nouveaux).
DEFAULT_JOURNALS = [
    ('AC', 'Achats', 'ACHATS'),
    ('VE', 'Ventes', 'VENTES'),
    ('BQ', 'Banque', 'BANQUE'),
    ('CA', 'Caisse', 'CAISSE'),
    ('OD', 'Opérations diverses', 'OD'),
    ('AN', 'À-nouveaux', 'AN'),
]

# --- Correspondance catégories de dépenses -> compte de charge par défaut --
# Deux systèmes de catégories coexistent dans ProfitOS et sont tous deux
# couverts ici : les catégories textuelles de l'import en masse
# (_classify_expense_document, profitos/routes/imports.py) et les clés courtes
# du module Achats/fournisseurs (PURCHASE_CATEGORIES, profitos/routes/invoicing.py).
DEFAULT_CATEGORY_MAPPING = {
    # Import en masse (imports.py)
    'Assurance': '616000',
    'Banque / Frais financiers': '627000',
    'Cotisations sociales - URSSAF': '645000',
    'Facture fournisseur': '606800',
    'Impôts et taxes': '447000',
    'Logiciels / Abonnements': '606800',
    'Loyer / Immobilier': '613000',
    'Salaires et paie': '641000',
    'TVA': '445660',
    'Télécom / Internet': '626000',
    'Énergie': '606100',
    'autre': '606800',
    # Module Achats / fournisseurs (invoicing.py, PURCHASE_CATEGORIES)
    'carburant': '606100',
    'logiciel_saas': '606800',
    'assurance': '616000',
    'telephone': '626000',
    'sous_traitance': '611000',
    'materiel': '606800',
    'loyer': '613000',
}


def _entry_already_exists(conn, source_type, source_id):
    """Vérifie si une écriture a déjà été générée pour cette source, pour ne
    jamais dupliquer une écriture si la route déclenchante est appelée deux
    fois (ex. double clic, nouvelle tentative après erreur réseau)."""
    row = conn.execute(
        'SELECT id FROM accounting_entries WHERE source_type=? AND source_id=? LIMIT 1',
        (source_type, source_id),
    ).fetchone()
    return row is not None


def _category_account(conn, category):
    """Retourne le compte de charge associé à une catégorie de dépense, ou le
    compte générique 606800 si la catégorie n'est pas connue — jamais
    d'échec silencieux, mais jamais de blocage non plus sur une catégorie
    inhabituelle."""
    row = conn.execute(
        'SELECT account_code FROM accounting_category_mapping WHERE category=?', (category,)
    ).fetchone()
    return row['account_code'] if row else '606800'


def generate_sale_entry(conn, invoice):
    """Génère l'écriture de vente (journal VE) pour une facture client émise :
    Clients (411) au débit du TTC, Prestations de services (706) et TVA
    collectée (445710) au crédit. invoice : ligne de outgoing_invoices.
    Idempotent — ne crée rien si déjà généré pour cette facture."""
    if _entry_already_exists(conn, 'outgoing_invoice', invoice['id']):
        return None
    lines = [
        {'account_code': '411000', 'debit': invoice['total'], 'auxiliary_name': invoice['client_name']},
        {'account_code': '706000', 'credit': invoice['subtotal']},
    ]
    if invoice['vat_amount']:
        lines.append({'account_code': '445710', 'credit': invoice['vat_amount']})
    return create_entry(
        conn, 'VE', invoice['issue_date'] or date.today(),
        f"Facture {invoice['invoice_number']} — {invoice['client_name']}",
        lines, source_type='outgoing_invoice', source_id=invoice['id'],
        entity_id=invoice['entity_id'] if 'entity_id' in invoice.keys() else None,
    )


def _next_lettrage_code(conn):
    """Prochain code de lettrage disponible : A, B, ... Z, AA, AB, ... (comme
    la numérotation des colonnes d'un tableur)."""
    row = conn.execute(
        "SELECT lettrage_code FROM accounting_entry_lines WHERE lettrage_code IS NOT NULL "
        "ORDER BY LENGTH(lettrage_code) DESC, lettrage_code DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'A'
    chars = list(row['lettrage_code'])
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != 'Z':
            chars[i] = chr(ord(chars[i]) + 1)
            return ''.join(chars)
        chars[i] = 'A'
        i -= 1
    return 'A' + ''.join(chars)


def _letter_pair(conn, account_code, original_source_type, original_source_id, new_entry_id):
    """Lettre automatiquement la ligne d'origine (facture) et la ligne de
    règlement qui vient d'être créée, sur le même compte collectif (411 ou
    401), si les deux existent, ne sont pas déjà lettrées et que leurs
    montants se compensent exactement. Ne lève jamais d'erreur : le lettrage
    automatique est une aide, pas une contrainte — s'il ne peut pas
    s'appliquer proprement, l'écriture reste valide, simplement non lettrée."""
    original_line = conn.execute(
        """SELECT l.id, l.debit, l.credit FROM accounting_entry_lines l
           JOIN accounting_entries e ON e.id = l.entry_id
           WHERE e.source_type=? AND e.source_id=? AND l.account_code=? AND l.lettrage_code IS NULL
           LIMIT 1""",
        (original_source_type, original_source_id, account_code),
    ).fetchone()
    new_line = conn.execute(
        """SELECT id, debit, credit FROM accounting_entry_lines
           WHERE entry_id=? AND account_code=? AND lettrage_code IS NULL LIMIT 1""",
        (new_entry_id, account_code),
    ).fetchone()
    if not original_line or not new_line:
        return
    original_amount = original_line['debit'] or original_line['credit']
    new_amount = new_line['debit'] or new_line['credit']
    if abs(original_amount - new_amount) > 0.01:
        return
    code = _next_lettrage_code(conn)
    conn.execute('UPDATE accounting_entry_lines SET lettrage_code=? WHERE id=?', (code, original_line['id']))
    conn.execute('UPDATE accounting_entry_lines SET lettrage_code=? WHERE id=?', (code, new_line['id']))
    conn.commit()


def generate_sale_payment_entry(conn, invoice):
    """Génère l'écriture de règlement (journal BQ) quand une facture client
    est marquée payée : Banque (512) au débit, Clients (411) au crédit.
    Lettre automatiquement cette écriture avec la facture d'origine sur le
    compte 411. Idempotent."""
    source_type = 'outgoing_invoice_payment'
    if _entry_already_exists(conn, source_type, invoice['id']):
        return None
    entry_id = create_entry(
        conn, 'BQ', date.today(),
        f"Règlement facture {invoice['invoice_number']} — {invoice['client_name']}",
        [
            {'account_code': '512000', 'debit': invoice['total']},
            {'account_code': '411000', 'credit': invoice['total'], 'auxiliary_name': invoice['client_name']},
        ],
        source_type=source_type, source_id=invoice['id'],
    )
    _letter_pair(conn, '411000', 'outgoing_invoice', invoice['id'], entry_id)
    return entry_id


def generate_purchase_entry(conn, purchase):
    """Génère l'écriture d'achat (journal AC) pour une facture fournisseur
    enregistrée : compte de charge (selon la catégorie) + TVA déductible
    (445660) au débit, Fournisseurs (401) au crédit. purchase : ligne de
    purchase_invoices. Idempotent."""
    if _entry_already_exists(conn, 'purchase_invoice', purchase['id']):
        return None
    charge_account = _category_account(conn, purchase['category'])
    lines = [{'account_code': charge_account, 'debit': purchase['subtotal']}]
    if purchase['vat_amount']:
        lines.append({'account_code': '445660', 'debit': purchase['vat_amount']})
    lines.append({
        'account_code': '401000', 'credit': purchase['total'],
        'auxiliary_name': purchase['supplier_name'],
    })
    return create_entry(
        conn, 'AC', purchase['issue_date'] or date.today(),
        f"Facture {purchase['invoice_number']} — {purchase['supplier_name']}",
        lines, source_type='purchase_invoice', source_id=purchase['id'],
        entity_id=purchase['entity_id'] if 'entity_id' in purchase.keys() else None,
    )


def generate_purchase_payment_entry(conn, purchase):
    """Génère l'écriture de règlement (journal BQ) quand une facture
    fournisseur est marquée payée : Fournisseurs (401) au débit, Banque (512)
    au crédit. Lettre automatiquement cette écriture avec la facture
    d'origine sur le compte 401. Idempotent."""
    source_type = 'purchase_invoice_payment'
    if _entry_already_exists(conn, source_type, purchase['id']):
        return None
    entry_id = create_entry(
        conn, 'BQ', date.today(),
        f"Règlement facture {purchase['invoice_number']} — {purchase['supplier_name']}",
        [
            {'account_code': '401000', 'debit': purchase['total'], 'auxiliary_name': purchase['supplier_name']},
            {'account_code': '512000', 'credit': purchase['total']},
        ],
        source_type=source_type, source_id=purchase['id'],
    )
    _letter_pair(conn, '401000', 'purchase_invoice', purchase['id'], entry_id)
    return entry_id


def seed_accounting_defaults(conn):
    """Insère le plan comptable, les journaux et la correspondance de
    catégories par défaut s'ils ne sont pas déjà présents. Idempotent : ne
    duplique jamais, n'écrase jamais une valeur déjà personnalisée par
    l'utilisateur (INSERT OR IGNORE)."""
    now = datetime.utcnow().isoformat()
    for code, label, klass, collective in DEFAULT_CHART_OF_ACCOUNTS:
        conn.execute(
            'INSERT OR IGNORE INTO accounting_chart_of_accounts'
            '(code,label,account_class,is_collective,is_active,is_default,created_at)'
            ' VALUES(?,?,?,?,1,1,?)',
            (code, label, klass, 1 if collective else 0, now),
        )
    for code, label, jtype in DEFAULT_JOURNALS:
        conn.execute(
            'INSERT OR IGNORE INTO accounting_journals(code,label,journal_type,is_default,created_at)'
            ' VALUES(?,?,?,1,?)',
            (code, label, jtype, now),
        )
    for category, account_code in DEFAULT_CATEGORY_MAPPING.items():
        conn.execute(
            'INSERT OR IGNORE INTO accounting_category_mapping(category,account_code,updated_at)'
            ' VALUES(?,?,?)',
            (category, account_code, now),
        )
    conn.commit()


class AccountingError(ValueError):
    """Levée quand une écriture ne respecte pas les règles de la partie
    double, ou référence un compte/journal inexistant. Ne jamais attraper
    cette exception pour forcer une écriture déséquilibrée : c'est
    précisément ce qu'elle empêche."""


def _next_piece_number(conn, journal_code, entry_date):
    """Numéro de pièce séquentiel par journal et par année civile, au format
    {JOURNAL}-{ANNÉE}-{SÉQUENCE sur 5 chiffres}, ex. VE-2026-00001."""
    year = str(entry_date)[:4]
    prefix = f'{journal_code}-{year}-'
    row = conn.execute(
        "SELECT piece_number FROM accounting_entries"
        " WHERE journal_code=? AND piece_number LIKE ? ORDER BY id DESC LIMIT 1",
        (journal_code, prefix + '%'),
    ).fetchone()
    if row:
        try:
            last_seq = int(row['piece_number'].rsplit('-', 1)[-1])
        except (ValueError, IndexError):
            last_seq = 0
    else:
        last_seq = 0
    return f'{prefix}{last_seq + 1:05d}'


def create_entry(conn, journal_code, entry_date, label, lines,
                  source_type=None, source_id=None, created_by=None, entity_id=None):
    """Crée une écriture comptable en partie double, avec ses lignes.

    lines : liste de dicts {account_code, debit=0, credit=0, label=None,
    auxiliary_name=None}. Chaque ligne doit avoir soit un débit soit un
    crédit (jamais les deux, jamais aucun des deux) et référencer un compte
    existant dans accounting_chart_of_accounts.

    Lève AccountingError et n'écrit rien en base si :
    - lines est vide ou a moins de 2 lignes (une écriture à une seule ligne
      ne peut pas être équilibrée par construction),
    - la somme des débits ne correspond pas à la somme des crédits (tolérance
      de 0,01 € pour les arrondis flottants),
    - une ligne a un débit ET un crédit, ou ni l'un ni l'autre,
    - un journal_code ou un account_code référencé n'existe pas.

    Retourne l'id de l'écriture créée si tout est valide.
    """
    if not lines or len(lines) < 2:
        raise AccountingError(
            "Une écriture doit avoir au moins deux lignes pour pouvoir être équilibrée."
        )

    entry_date_str = entry_date.isoformat() if isinstance(entry_date, date) else str(entry_date)
    closure = conn.execute('SELECT closed_until FROM accounting_closure WHERE id=1').fetchone()
    if closure and closure['closed_until'] and entry_date_str <= closure['closed_until']:
        raise AccountingError(
            f"La période est clôturée jusqu'au {closure['closed_until']} — "
            f"impossible d'ajouter une écriture au {entry_date_str}."
        )

    journal = conn.execute(
        'SELECT code FROM accounting_journals WHERE code=?', (journal_code,)
    ).fetchone()
    if not journal:
        raise AccountingError(f"Journal inconnu : {journal_code!r}.")

    total_debit = 0.0
    total_credit = 0.0
    clean_lines = []
    for i, raw in enumerate(lines):
        account_code = raw.get('account_code')
        debit = round(float(raw.get('debit') or 0), 2)
        credit = round(float(raw.get('credit') or 0), 2)
        if not account_code:
            raise AccountingError(f"Ligne {i + 1} : compte manquant.")
        if debit and credit:
            raise AccountingError(
                f"Ligne {i + 1} ({account_code}) : ne peut pas avoir un débit et un crédit à la fois."
            )
        if not debit and not credit:
            raise AccountingError(
                f"Ligne {i + 1} ({account_code}) : doit avoir un débit ou un crédit non nul."
            )
        account = conn.execute(
            'SELECT code FROM accounting_chart_of_accounts WHERE code=? AND (entity_id IS NULL OR entity_id=?)',
            (account_code, entity_id),
        ).fetchone()
        if not account:
            raise AccountingError(f"Compte inconnu : {account_code!r} (ligne {i + 1}).")
        total_debit += debit
        total_credit += credit
        clean_lines.append({
            'account_code': account_code,
            'auxiliary_name': raw.get('auxiliary_name'),
            'label': raw.get('label') or label,
            'debit': debit,
            'credit': credit,
        })

    if abs(total_debit - total_credit) > 0.01:
        raise AccountingError(
            f"Écriture déséquilibrée : total débit {total_debit:.2f} € "
            f"≠ total crédit {total_credit:.2f} €."
        )

    now = datetime.utcnow().isoformat()
    piece_number = _next_piece_number(conn, journal_code, entry_date_str)

    cur = conn.execute(
        'INSERT INTO accounting_entries'
        '(journal_code,piece_number,entry_date,label,source_type,source_id,is_locked,created_by,created_at,entity_id)'
        ' VALUES(?,?,?,?,?,?,0,?,?,?)',
        (journal_code, piece_number, entry_date_str, label, source_type, source_id, created_by, now, entity_id),
    )
    entry_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    for order, ln in enumerate(clean_lines):
        conn.execute(
            'INSERT INTO accounting_entry_lines'
            '(entry_id,account_code,auxiliary_name,label,debit,credit,line_order)'
            ' VALUES(?,?,?,?,?,?,?)',
            (entry_id, ln['account_code'], ln['auxiliary_name'], ln['label'],
             ln['debit'], ln['credit'], order),
        )
    conn.commit()
    return entry_id


# --- Export FEC (Fichier des Écritures Comptables) -------------------------
# Format réglementaire français (arrêté du 29 juillet 2013, art. L47 A du LPF) :
# 18 colonnes obligatoires, séparateur "|", encodage UTF-8, montants au format
# décimal avec point (jamais de séparateur de milliers, jamais de virgule —
# c'est un format de données réglementaire, pas un affichage humain : ne
# jamais y appliquer fr_number). Nom de fichier imposé : SIREN + FEC +
# date de clôture (AAAAMMJJ) + .txt.
#
# AVERTISSEMENT : cet export est construit du mieux possible à partir de la
# spécification connue, mais n'a pas pu être vérifié contre un validateur FEC
# officiel (pas d'accès réseau dans cet environnement). À faire valider par
# un expert-comptable ou un validateur FEC avant tout usage réel en cas de
# contrôle fiscal.
FEC_COLUMNS = [
    'JournalCode', 'JournalLib', 'EcritureNum', 'EcritureDate',
    'CompteNum', 'CompteLib', 'CompAuxNum', 'CompAuxLib',
    'PieceRef', 'PieceDate', 'EcritureLib', 'Debit', 'Credit',
    'EcritureLet', 'DateLet', 'ValidDate', 'Montantdevise', 'Idevise',
]


def _fec_date(value):
    """Convertit une date ISO (AAAA-MM-JJ) au format FEC (AAAAMMJJ), sans
    séparateur. Chaîne vide si la date est absente — jamais inventée."""
    if not value:
        return ''
    s = str(value)
    return s[0:4] + s[5:7] + s[8:10] if len(s) >= 10 else ''


def _fec_amount(value):
    """Formate un montant au format FEC : point décimal, deux décimales,
    aucun séparateur de milliers. Ne jamais utiliser fr_number ici — le FEC
    est un format de données, pas un affichage destiné à un humain."""
    return f"{float(value or 0):.2f}"


def generate_fec(conn, date_from, date_to, entity_id=None):
    """Génère le contenu FEC (liste de lignes, la première étant l'en-tête)
    pour toutes les écritures dont la date est comprise entre date_from et
    date_to (inclus). entity_id filtre sur une seule entité à la fois —
    None signifie explicitement « société mère uniquement » (entity_id NULL
    en base), jamais « toutes les entités confondues » : chaque entité est
    un sujet fiscal distinct et doit avoir son propre fichier FEC, jamais un
    mélange. Retourne une liste de listes de chaînes — une ligne par
    écriture comptable, dans l'ordre chronologique puis par numéro de pièce."""
    if entity_id:
        entries = conn.execute(
            """SELECT e.*, j.label AS journal_label FROM accounting_entries e
               JOIN accounting_journals j ON j.code = e.journal_code
               WHERE e.entry_date BETWEEN ? AND ? AND e.entity_id=?
               ORDER BY e.entry_date, e.journal_code, e.piece_number""",
            (str(date_from), str(date_to), entity_id),
        ).fetchall()
    else:
        entries = conn.execute(
            """SELECT e.*, j.label AS journal_label FROM accounting_entries e
               JOIN accounting_journals j ON j.code = e.journal_code
               WHERE e.entry_date BETWEEN ? AND ? AND e.entity_id IS NULL
               ORDER BY e.entry_date, e.journal_code, e.piece_number""",
            (str(date_from), str(date_to)),
        ).fetchall()

    rows = [FEC_COLUMNS]
    for e in entries:
        lines = conn.execute(
            """SELECT l.*, a.label AS account_label FROM accounting_entry_lines l
               JOIN accounting_chart_of_accounts a ON a.code = l.account_code
               WHERE l.entry_id=? ORDER BY l.line_order""",
            (e['id'],),
        ).fetchall()
        validation_date = _fec_date(e['created_at'][:10]) if e['created_at'] else _fec_date(e['entry_date'])
        for l in lines:
            rows.append([
                e['journal_code'],
                e['journal_label'],
                e['piece_number'],
                _fec_date(e['entry_date']),
                l['account_code'],
                l['account_label'],
                l['account_code'] if l['auxiliary_name'] else '',
                l['auxiliary_name'] or '',
                e['piece_number'],
                _fec_date(e['entry_date']),
                l['label'] or e['label'],
                _fec_amount(l['debit']),
                _fec_amount(l['credit']),
                l['lettrage_code'] or '',
                '',
                validation_date,
                '',
                '',
            ])
    return rows


def fec_filename(siret, date_to):
    """Nom de fichier réglementaire : SIREN (9 premiers chiffres du SIRET) +
    FEC + date de clôture au format AAAAMMJJ + .txt. Lève ValueError si le
    SIRET est absent ou trop court — un export FEC sans SIREN valide n'a
    aucune valeur légale, mieux vaut échouer clairement que produire un nom
    de fichier invalide."""
    digits = ''.join(ch for ch in str(siret or '') if ch.isdigit())
    if len(digits) < 9:
        raise ValueError(
            "SIRET manquant ou incomplet dans le profil entreprise — "
            "impossible de nommer le fichier FEC réglementairement."
        )
    siren = digits[:9]
    return f"{siren}FEC{_fec_date(date_to)}.txt"


# --- Immobilisations / amortissements ---------------------------------------
# Amortissement linéaire uniquement (le plus courant en TPE/PME). Prorata
# temporis sur l'année d'acquisition (nombre de jours entre la date d'achat
# et le 31 décembre de cette année, sur 365 — convention simple ; certains
# cabinets utilisent une base 360 jours, à ajuster si besoin).

def compute_depreciation_for_year(conn, asset, calendar_year):
    """Calcule la dotation aux amortissements pour une année civile donnée.
    Ne se fie jamais uniquement à la formule théorique : plafonne toujours
    le résultat pour que la somme des dotations déjà comptabilisées plus
    celle-ci ne dépasse jamais le coût d'achat — gère ainsi automatiquement
    le prorata de la dernière année sans avoir à le calculer à part.
    Retourne 0 si l'actif n'était pas encore acquis cette année-là, ou s'il
    est déjà totalement amorti."""
    purchase_date_obj = date.fromisoformat(str(asset['purchase_date'])[:10])
    if calendar_year < purchase_date_obj.year:
        return 0.0
    annual_rate = asset['purchase_amount'] / asset['useful_life_years']
    if calendar_year == purchase_date_obj.year:
        days_remaining = (date(calendar_year, 12, 31) - purchase_date_obj).days + 1
        theoretical = annual_rate * days_remaining / 365
    else:
        theoretical = annual_rate
    already_posted = conn.execute(
        'SELECT COALESCE(SUM(amount),0) t FROM fixed_asset_depreciation_runs WHERE asset_id=?',
        (asset['id'],),
    ).fetchone()['t']
    remaining = asset['purchase_amount'] - already_posted
    return round(min(theoretical, max(remaining, 0.0)), 2)


def generate_depreciation_entry(conn, asset, period_label, amount):
    """Enregistre une dotation aux amortissements pour une période donnée :
    débite le compte de charge (681xxx), crédite le compte d'amortissement
    de l'actif (compte contra-actif, ex. 281830). Idempotent via la
    contrainte UNIQUE(asset_id,period_label) de fixed_asset_depreciation_runs
    — une même période ne peut jamais être comptabilisée deux fois pour le
    même actif."""
    existing = conn.execute(
        'SELECT id FROM fixed_asset_depreciation_runs WHERE asset_id=? AND period_label=?',
        (asset['id'], period_label),
    ).fetchone()
    if existing:
        return None
    if amount <= 0:
        raise AccountingError("Aucune dotation à comptabiliser pour cette période (actif déjà totalement amorti).")
    entry_id = create_entry(
        conn, 'OD', date.today(),
        f"Dotation aux amortissements — {asset['label']} ({period_label})",
        [
            {'account_code': asset['expense_account'], 'debit': amount},
            {'account_code': asset['depreciation_account'], 'credit': amount},
        ],
        source_type='fixed_asset_depreciation', source_id=asset['id'],
    )
    conn.execute(
        'INSERT INTO fixed_asset_depreciation_runs(asset_id,period_label,amount,entry_id,created_at) VALUES(?,?,?,?,?)',
        (asset['id'], period_label, amount, entry_id, datetime.utcnow().isoformat()),
    )
    conn.commit()


CUTOFF_ACCOUNTS = {
    'CCA': '486000',   # Charges constatées d'avance — actif
    'FNP': '408000',   # Fournisseurs - Factures non parvenues — passif
    'FAE': '418000',   # Clients - Produits non encore facturés — actif
}
CUTOFF_LABELS = {
    'CCA': 'Charge constatée d\'avance',
    'FNP': 'Facture non parvenue',
    'FAE': 'Facture à établir',
}


def create_cutoff_entry(conn, cutoff_type, label, amount, counterpart_account_code,
                         period_end_date, entity_id=None, created_by=None):
    """Enregistre une écriture de cut-off (CCA, FNP ou FAE) à la clôture
    d'une période, journal OD.

    - CCA (charge constatée d'avance) : une charge déjà payée/comptabilisée
      cette période concerne en réalité une période future. Débite 486000,
      crédite le compte de charge (counterpart_account_code) — retire la
      part qui ne concerne pas encore cette période.
    - FNP (facture non parvenue) : un bien/service a été reçu cette période
      mais la facture fournisseur n'est pas encore arrivée. Débite le compte
      de charge (counterpart_account_code), crédite 408000 — rattache la
      charge à la bonne période malgré la facture manquante.
    - FAE (facture à établir) : une prestation a été livrée cette période
      mais la facture client n'est pas encore émise. Débite 418000, crédite
      le compte de produit (counterpart_account_code) — rattache le produit
      à la bonne période.

    Ne crée jamais la contre-passation elle-même : voir reverse_cutoff_entry,
    déclenchée explicitement au début de la période suivante, pour ne jamais
    dater une écriture dans le futur de façon silencieuse."""
    if cutoff_type not in CUTOFF_ACCOUNTS:
        raise AccountingError(f"Type de cut-off inconnu : {cutoff_type!r} (attendu CCA, FNP ou FAE).")
    if amount <= 0:
        raise AccountingError("Le montant du cut-off doit être positif.")
    cutoff_account = CUTOFF_ACCOUNTS[cutoff_type]
    full_label = f"{CUTOFF_LABELS[cutoff_type]} — {label}"

    if cutoff_type == 'CCA':
        lines = [{'account_code': cutoff_account, 'debit': amount},
                  {'account_code': counterpart_account_code, 'credit': amount}]
    elif cutoff_type == 'FNP':
        lines = [{'account_code': counterpart_account_code, 'debit': amount},
                  {'account_code': cutoff_account, 'credit': amount}]
    else:  # FAE
        lines = [{'account_code': cutoff_account, 'debit': amount},
                  {'account_code': counterpart_account_code, 'credit': amount}]

    entry_id = create_entry(
        conn, 'OD', period_end_date, full_label, lines,
        source_type='cutoff', created_by=created_by, entity_id=entity_id,
    )
    conn.execute(
        """INSERT INTO cutoff_entries
           (cutoff_type,label,amount,counterpart_account_code,period_end_date,entry_id,entity_id,created_at,created_by)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (cutoff_type, label, amount, counterpart_account_code, str(period_end_date), entry_id,
         entity_id, datetime.utcnow().isoformat(), created_by),
    )
    conn.commit()
    cutoff_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    # source_id de l'écriture comptable mis à jour après coup pour pointer
    # vers la ligne de suivi cutoff_entries (son id n'existe qu'une fois
    # l'écriture elle-même déjà créée).
    conn.execute('UPDATE accounting_entries SET source_id=? WHERE id=?', (cutoff_id, entry_id))
    conn.commit()
    return cutoff_id


def reverse_cutoff_entry(conn, cutoff_id, reversal_date, created_by=None):
    """Génère la contre-passation (extourne) d'un cut-off existant : mêmes
    comptes et montant, débit et crédit inversés, datée du jour choisi
    (typiquement le tout début de la période suivante). Refuse si ce
    cut-off a déjà été extourné — jamais une double contre-passation."""
    cutoff = conn.execute('SELECT * FROM cutoff_entries WHERE id=?', (cutoff_id,)).fetchone()
    if not cutoff:
        raise AccountingError(f"Cut-off introuvable : id={cutoff_id!r}.")
    if cutoff['reversal_entry_id']:
        raise AccountingError("Ce cut-off a déjà été extourné.")

    original_lines = conn.execute(
        'SELECT account_code,debit,credit FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order',
        (cutoff['entry_id'],),
    ).fetchall()
    reversed_lines = [
        {'account_code': l['account_code'], 'debit': l['credit'] or 0, 'credit': l['debit'] or 0}
        for l in original_lines
    ]
    reversal_id = create_entry(
        conn, 'OD', reversal_date,
        f"Extourne — {CUTOFF_LABELS[cutoff['cutoff_type']]} — {cutoff['label']}",
        reversed_lines,
        source_type='cutoff_reversal', source_id=cutoff_id,
        created_by=created_by, entity_id=cutoff['entity_id'],
    )
    conn.execute('UPDATE cutoff_entries SET reversal_entry_id=? WHERE id=?', (reversal_id, cutoff_id))
    conn.commit()
    return reversal_id


def compute_compte_resultat(conn, date_from, date_to, entity_id=None):
    """Produits et charges de la période, classés par compte. Le résultat
    (produits - charges) est aussi utilisé par compute_bilan() pour
    apparaître au passif — calculé une seule fois, jamais recalculé
    différemment à deux endroits."""
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    rows = conn.execute(
        f"""SELECT l.account_code, a.account_class, a.label,
               COALESCE(SUM(l.debit),0) total_debit, COALESCE(SUM(l.credit),0) total_credit
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            JOIN accounting_chart_of_accounts a ON a.code=l.account_code
            WHERE e.entry_date BETWEEN ? AND ? AND a.account_class IN (6,7) AND {ef}
            GROUP BY l.account_code, a.account_class, a.label
            ORDER BY l.account_code""",
        (str(date_from), str(date_to), *ep),
    ).fetchall()
    charges = [{'code': r['account_code'], 'label': r['label'], 'amount': round(r['total_debit'] - r['total_credit'], 2)}
               for r in rows if r['account_class'] == 6]
    produits = [{'code': r['account_code'], 'label': r['label'], 'amount': round(r['total_credit'] - r['total_debit'], 2)}
                for r in rows if r['account_class'] == 7]
    total_charges = round(sum(c['amount'] for c in charges), 2)
    total_produits = round(sum(p['amount'] for p in produits), 2)
    return {
        'charges': charges, 'produits': produits,
        'total_charges': total_charges, 'total_produits': total_produits,
        'resultat': round(total_produits - total_charges, 2),
    }


def compute_bilan(conn, as_of_date, exercice_start_date, entity_id=None):
    """Bilan (actif/passif) à une date donnée. exercice_start_date sert à
    calculer le résultat de l'exercice en cours à faire apparaître au
    passif — le même calcul que compute_compte_resultat, jamais dupliqué
    différemment.

    Classement par classe PCG (account_class déjà stocké par compte) :
    classe 2 = immobilisations (28xxxx = amortissements, en déduction) ;
    classe 5 = disponibilités ; classe 4 = tiers, ventilé actif/passif selon
    le sens du solde (créditeur = passif, débiteur = actif) plutôt que par
    une liste de codes à maintenir à la main — un compte non prévu se classe
    quand même correctement. Classe 1 hors 164000 (emprunts) = capitaux
    propres hors résultat."""
    resultat_data = compute_compte_resultat(conn, exercice_start_date, as_of_date, entity_id)
    resultat_exercice = resultat_data['resultat']

    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    rows = conn.execute(
        f"""SELECT l.account_code, a.account_class, a.label,
               COALESCE(SUM(l.debit),0) total_debit, COALESCE(SUM(l.credit),0) total_credit
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            JOIN accounting_chart_of_accounts a ON a.code=l.account_code
            WHERE e.entry_date<=? AND a.account_class IN (1,2,4,5) AND {ef}
            GROUP BY l.account_code, a.account_class, a.label
            ORDER BY l.account_code""",
        (str(as_of_date), *ep),
    ).fetchall()

    immobilisations_brutes = 0.0
    amortissements = 0.0
    creances = 0.0
    disponibilites = 0.0
    dettes = 0.0
    capitaux_propres_hors_resultat = 0.0
    emprunts = 0.0
    detail_actif_immo, detail_actif_tiers, detail_passif_tiers = [], [], []

    for r in rows:
        net = round(r['total_debit'] - r['total_credit'], 2)
        code, klass, label = r['account_code'], r['account_class'], r['label']
        if klass == 2:
            if code.startswith('28'):
                amortissements += -net
                if net:
                    detail_actif_immo.append({'code': code, 'label': label, 'amount': -net})
            else:
                immobilisations_brutes += net
                if net:
                    detail_actif_immo.append({'code': code, 'label': label, 'amount': net})
        elif klass == 5:
            disponibilites += net
        elif klass == 4:
            if net >= 0:
                creances += net
                if net:
                    detail_actif_tiers.append({'code': code, 'label': label, 'amount': net})
            else:
                dettes += -net
                if net:
                    detail_passif_tiers.append({'code': code, 'label': label, 'amount': -net})
        elif klass == 1:
            if code == '164000':
                emprunts += -net
            else:
                capitaux_propres_hors_resultat += -net

    immobilisations_nettes = round(immobilisations_brutes - amortissements, 2)
    total_actif = round(immobilisations_nettes + creances + disponibilites, 2)
    total_passif = round(capitaux_propres_hors_resultat + resultat_exercice + emprunts + dettes, 2)

    actif = {
        'immobilisations_brutes': round(immobilisations_brutes, 2), 'amortissements': round(amortissements, 2),
        'immobilisations_nettes': immobilisations_nettes, 'creances': round(creances, 2),
        'disponibilites': round(disponibilites, 2), 'total': total_actif,
        'detail_immo': detail_actif_immo, 'detail_tiers': detail_actif_tiers,
    }
    passif = {
        'capitaux_propres': round(capitaux_propres_hors_resultat, 2), 'resultat_exercice': resultat_exercice,
        'emprunts': round(emprunts, 2), 'dettes': round(dettes, 2), 'total': total_passif,
        'detail_tiers': detail_passif_tiers,
    }
    return actif, passif, resultat_data
