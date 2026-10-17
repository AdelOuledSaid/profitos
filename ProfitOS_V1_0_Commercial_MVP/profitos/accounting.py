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
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import json

from profitos import invoice_amounts as amounts

PARIS_TZ = ZoneInfo('Europe/Paris')


def paris_today():
    """Date du jour en France (Europe/Paris). Le serveur tourne en UTC : entre minuit
    et 1 h/2 h du matin, date.today()/UTC donnait encore la veille — une facture émise
    le 1er janvier à 0 h 30 était datée et numérotée sur l'année précédente."""
    return datetime.now(PARIS_TZ).date()


def paris_date_of(iso_timestamp):
    """Date (Europe/Paris) d'un horodatage ISO UTC stocké en base ; '' si illisible."""
    if not iso_timestamp:
        return ''
    try:
        dt = datetime.fromisoformat(str(iso_timestamp).replace('Z', '+00:00'))
    except ValueError:
        return str(iso_timestamp)[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PARIS_TZ).date().isoformat()


# Comptes de TVA et de produits utilisés par les écritures de vente.
VAT_COLLECTED_ACCOUNT = '445710'
VAT_PENDING_ACCOUNT = '445740'      # TVA collectée en attente (TVA sur encaissements)
GOODS_REVENUE_ACCOUNT = '707000'    # Ventes de marchandises
SERVICES_REVENUE_ACCOUNT = '706000'  # Prestations de services
REVENUE_ACCOUNT_BY_NATURE = {'biens': GOODS_REVENUE_ACCOUNT, 'services': SERVICES_REVENUE_ACCOUNT}


# --- Plan comptable par défaut (PCG français, sous-ensemble courant) -------
# Format : (code, libellé, classe, compte_collectif).
# compte_collectif=True signale un compte utilisé avec un tiers auxiliaire
# (ex. 411000 Clients, 401000 Fournisseurs) plutôt qu'en direct.
DEFAULT_CHART_OF_ACCOUNTS = [
    # Classe 1 — Capitaux
    ('101000', 'Capital social', 1, False),
    ('106800', 'Autres réserves', 1, False),
    ('110000', 'Report à nouveau (solde créditeur)', 1, False),
    ('119000', 'Report à nouveau (solde débiteur)', 1, False),
    ('120000', "Résultat de l'exercice (bénéfice)", 1, False),
    ('129000', "Résultat de l'exercice (perte)", 1, False),
    ('164000', "Emprunts auprès des établissements de crédit", 1, False),
    # Comptes courants d'associés : dettes envers les associés (classe 4), jamais des
    # capitaux propres — classés en classe 1, ils gonflaient les fonds propres du bilan.
    ('455000', 'Associés - comptes courants', 4, False),
    # Classe 2 — Immobilisations
    ('218300', 'Matériel de bureau et informatique', 2, False),
    ('281830', 'Amortissements du matériel de bureau et informatique', 2, False),
    # Classe 4 — Tiers
    ('401000', 'Fournisseurs', 4, True),
    ('408000', 'Fournisseurs - Factures non parvenues', 4, False),
    ('411000', 'Clients', 4, True),
    ('418000', 'Clients - Produits non encore facturés', 4, False),
    ('419100', 'Clients - Avances et acomptes reçus sur commandes', 4, False),
    ('421000', 'Personnel - rémunérations dues', 4, False),
    ('425000', 'Personnel - avances et acomptes', 4, False),
    ('427000', 'Personnel - oppositions', 4, False),
    ('428200', 'Dettes provisionnées pour congés à payer', 4, False),
    ('431000', 'Sécurité sociale', 4, False),
    ('437000', 'Autres organismes sociaux', 4, False),
    ('442100', "Prélèvement à la source (impôt sur le revenu)", 4, False),
    ('445510', 'TVA à décaisser', 4, False),
    ('445660', 'TVA déductible sur autres biens et services', 4, False),
    ('445710', 'TVA collectée', 4, False),
    ('445740', 'TVA collectée en attente (TVA sur encaissements)', 4, False),
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
    ('631100', 'Taxe sur les salaires', 6, False),
    ('633300', 'Participation des employeurs à la formation professionnelle continue', 6, False),
    ('641000', 'Rémunérations du personnel', 6, False),
    ('641100', 'Salaires, appointements', 6, False),
    ('641200', 'Congés payés', 6, False),
    ('641300', 'Primes et gratifications', 6, False),
    ('641400', 'Indemnités et avantages divers', 6, False),
    ('645000', 'Charges de sécurité sociale et de prévoyance', 6, False),
    ('645100', "Cotisations à l'URSSAF", 6, False),
    ('645200', 'Cotisations aux mutuelles', 6, False),
    ('645300', 'Cotisations aux caisses de retraite', 6, False),
    ('645400', "Cotisations à l'assurance chômage", 6, False),
    ('645800', 'Cotisations aux autres organismes sociaux', 6, False),
    ('647000', 'Autres charges sociales', 6, False),
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

# --- Plan comptable étendu (PCG) --------------------------------------------
# Comptes usuels supplémentaires pour une comptabilité complète (provisions,
# immobilisations détaillées, stocks, TVA détaillée, charges et produits financiers et
# exceptionnels, dotations et reprises…). Ajoutés sans jamais écraser un compte existant.
PCG_EXTENDED_ACCOUNTS = [
    ('101300', 'Capital souscrit, appelé, versé', 1, False),
    ('104100', "Primes d'émission", 1, False),
    ('105000', 'Écarts de réévaluation', 1, False),
    ('106100', 'Réserve légale', 1, False),
    ('106300', 'Réserves statutaires ou contractuelles', 1, False),
    ('108000', "Compte de l'exploitant", 1, False),
    ('131000', "Subventions d'équipement", 1, False),
    ('139000', "Subventions d'investissement inscrites au compte de résultat", 1, False),
    ('145000', 'Amortissements dérogatoires', 1, False),
    ('151100', 'Provisions pour litiges', 1, False),
    ('151800', 'Autres provisions pour risques', 1, False),
    ('153000', 'Provisions pour pensions et obligations similaires', 1, False),
    ('158000', 'Autres provisions pour charges', 1, False),
    ('165000', 'Dépôts et cautionnements reçus', 1, False),
    ('168800', 'Intérêts courus sur emprunts', 1, False),
    ('201100', 'Frais de constitution', 2, False),
    ('205000', 'Concessions, brevets, licences, logiciels', 2, False),
    ('206000', 'Droit au bail', 2, False),
    ('207000', 'Fonds commercial', 2, False),
    ('211000', 'Terrains', 2, False),
    ('213100', 'Constructions — bâtiments', 2, False),
    ('213500', 'Installations générales, agencements des constructions', 2, False),
    ('215400', 'Matériel industriel', 2, False),
    ('215500', 'Outillage industriel', 2, False),
    ('218100', 'Installations générales, agencements, aménagements divers', 2, False),
    ('218200', 'Matériel de transport', 2, False),
    ('218400', 'Mobilier', 2, False),
    ('231000', 'Immobilisations corporelles en cours', 2, False),
    ('261000', 'Titres de participation', 2, False),
    ('271000', 'Titres immobilisés', 2, False),
    ('275000', 'Dépôts et cautionnements versés', 2, False),
    ('280500', 'Amortissements des concessions, brevets, logiciels', 2, False),
    ('281350', 'Amortissements des installations et agencements des constructions', 2, False),
    ('281540', 'Amortissements du matériel industriel', 2, False),
    ('281810', 'Amortissements des installations et agencements divers', 2, False),
    ('281820', 'Amortissements du matériel de transport', 2, False),
    ('281840', 'Amortissements du mobilier', 2, False),
    ('290700', 'Dépréciations du fonds commercial', 2, False),
    ('296100', 'Dépréciations des titres de participation', 2, False),
    ('310000', 'Matières premières', 3, False),
    ('355000', 'Produits finis', 3, False),
    ('370000', 'Stocks de marchandises', 3, False),
    ('391000', 'Dépréciations des matières premières', 3, False),
    ('397000', 'Dépréciations des stocks de marchandises', 3, False),
    ('404000', "Fournisseurs d'immobilisations", 4, True),
    ('409100', 'Fournisseurs — avances et acomptes versés', 4, False),
    ('409800', 'Fournisseurs — rabais, remises, ristournes à obtenir', 4, False),
    ('413000', 'Clients — effets à recevoir', 4, False),
    ('416000', 'Clients douteux ou litigieux', 4, True),
    ('419800', 'Clients — rabais, remises, ristournes à accorder', 4, False),
    ('442000', 'État — impôts et taxes recouvrables sur des tiers (prélèvements sur dividendes)', 4, False),
    ('444000', 'État — impôts sur les bénéfices', 4, False),
    ('445200', 'TVA due intracommunautaire', 4, False),
    ('445620', 'TVA déductible sur immobilisations', 4, False),
    ('445670', 'Crédit de TVA à reporter', 4, False),
    ('445800', 'TVA à régulariser ou en attente', 4, False),
    ('445810', 'TVA — acomptes versés (régime simplifié)', 4, False),
    ('445860', 'TVA sur factures non parvenues', 4, False),
    ('445870', 'TVA sur factures à établir', 4, False),
    ('448600', 'État — charges à payer', 4, False),
    ('457000', 'Associés — dividendes à payer', 4, False),
    ('462000', "Créances sur cessions d'immobilisations", 4, False),
    ('471000', "Compte d'attente", 4, False),
    ('487000', "Produits constatés d'avance", 4, False),
    ('491000', 'Dépréciations des comptes clients', 4, False),
    ('503000', 'Valeurs mobilières de placement — actions', 5, False),
    ('508000', 'Autres valeurs mobilières de placement', 5, False),
    ('511200', 'Chèques à encaisser', 5, False),
    ('517000', 'Autres organismes financiers', 5, False),
    ('518600', 'Intérêts courus à payer', 5, False),
    ('519000', 'Concours bancaires courants', 5, False),
    ('580000', 'Virements internes', 5, False),
    ('590000', 'Dépréciations des valeurs mobilières de placement', 5, False),
    ('601000', 'Achats stockés — matières premières', 6, False),
    ('602200', 'Fournitures consommables', 6, False),
    ('603100', 'Variation des stocks de matières premières', 6, False),
    ('603700', 'Variation des stocks de marchandises', 6, False),
    ('604000', "Achats d'études et prestations de services", 6, False),
    ('606300', "Fournitures d'entretien et de petit équipement", 6, False),
    ('607000', 'Achats de marchandises', 6, False),
    ('609000', 'Rabais, remises et ristournes obtenus sur achats', 6, False),
    ('613200', 'Locations immobilières', 6, False),
    ('613500', 'Locations mobilières', 6, False),
    ('614000', 'Charges locatives et de copropriété', 6, False),
    ('615500', 'Entretien et réparations sur biens mobiliers', 6, False),
    ('618500', 'Frais de colloques, séminaires, conférences', 6, False),
    ('621100', 'Personnel intérimaire', 6, False),
    ('622700', "Frais d'actes et de contentieux", 6, False),
    ('623100', 'Annonces et insertions', 6, False),
    ('623400', 'Cadeaux à la clientèle', 6, False),
    ('624100', 'Transports sur achats', 6, False),
    ('624200', 'Transports sur ventes', 6, False),
    ('635110', 'Contribution économique territoriale (CFE, CVAE)', 6, False),
    ('635120', 'Taxes foncières', 6, False),
    ('635400', "Droits d'enregistrement et de timbre", 6, False),
    ('637800', 'Taxes diverses', 6, False),
    ('644000', "Rémunération du travail de l'exploitant", 6, False),
    ('646000', "Cotisations sociales personnelles de l'exploitant", 6, False),
    ('648000', 'Autres charges de personnel', 6, False),
    ('651000', 'Redevances pour concessions, brevets, licences', 6, False),
    ('654000', 'Pertes sur créances irrécouvrables', 6, False),
    ('658000', 'Charges diverses de gestion courante', 6, False),
    ('661600', 'Intérêts bancaires et sur opérations de financement', 6, False),
    ('665000', 'Escomptes accordés', 6, False),
    ('666000', 'Pertes de change', 6, False),
    ('668000', 'Autres charges financières', 6, False),
    ('671200', 'Pénalités, amendes fiscales et pénales', 6, False),
    ('675000', "Valeurs comptables des éléments d'actif cédés", 6, False),
    ('678000', 'Autres charges exceptionnelles', 6, False),
    ('681100', 'Dotations aux amortissements des immobilisations', 6, False),
    ('681500', "Dotations aux provisions d'exploitation", 6, False),
    ('681700', 'Dotations aux dépréciations des actifs circulants', 6, False),
    ('686000', 'Dotations aux amortissements, dépréciations et provisions — charges financières', 6, False),
    ('687000', 'Dotations aux amortissements, dépréciations et provisions — charges exceptionnelles', 6, False),
    ('691000', 'Participation des salariés aux résultats', 6, False),
    ('695000', 'Impôts sur les bénéfices', 6, False),
    ('701000', 'Ventes de produits finis', 7, False),
    ('704000', 'Travaux', 7, False),
    ('708500', 'Ports et frais accessoires facturés', 7, False),
    ('709000', 'Rabais, remises et ristournes accordés', 7, False),
    ('713000', 'Variation des stocks (en-cours de production, produits)', 7, False),
    ('721000', 'Production immobilisée — immobilisations incorporelles', 7, False),
    ('722000', 'Production immobilisée — immobilisations corporelles', 7, False),
    ('740000', "Subventions d'exploitation", 7, False),
    ('751000', 'Redevances pour concessions, brevets, licences', 7, False),
    ('761000', 'Produits de participations', 7, False),
    ('764000', 'Revenus des valeurs mobilières de placement', 7, False),
    ('765000', 'Escomptes obtenus', 7, False),
    ('766000', 'Gains de change', 7, False),
    ('768000', 'Autres produits financiers', 7, False),
    ('771000', 'Produits exceptionnels sur opérations de gestion', 7, False),
    ('775000', "Produits des cessions d'éléments d'actif", 7, False),
    ('777000', "Quote-part des subventions d'investissement virée au résultat", 7, False),
    ('778000', 'Autres produits exceptionnels', 7, False),
    ('781100', 'Reprises sur amortissements des immobilisations', 7, False),
    ('781500', "Reprises sur provisions d'exploitation", 7, False),
    ('781700', 'Reprises sur dépréciations des actifs circulants', 7, False),
    ('786000', 'Reprises sur dépréciations et provisions — produits financiers', 7, False),
    ('787000', 'Reprises sur dépréciations et provisions — produits exceptionnels', 7, False),
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


def _entry_already_exists(conn, source_type, source_id, entity_id=None):
    """Vérifie si une écriture a déjà été générée pour cette source, pour ne
    jamais dupliquer une écriture si la route déclenchante est appelée deux
    fois (ex. double clic, nouvelle tentative après erreur réseau)."""
    row = conn.execute(
        'SELECT id FROM accounting_entries WHERE source_type=? AND source_id=? AND entity_id IS ? LIMIT 1',
        (source_type, source_id, entity_id),
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


def _row_get(row, key, default=None):
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[key] if key in keys else default


def _invoice_items(invoice):
    try:
        return json.loads(_row_get(invoice, 'line_items') or '[]')
    except (TypeError, ValueError):
        return []


def _vat_due_on_payment(invoice, nature):
    """TVA exigible à l'encaissement : prestations de services sans option pour les
    débits (CGI art. 269, 2-c). Les livraisons de biens sont exigibles à la livraison."""
    return nature == 'services' and not int(_row_get(invoice, 'vat_on_debits') or 0)


def _sale_revenue_and_vat_lines(invoice, credit_side='credit', revenue_override=None, items=None):
    """Lignes produits (707 biens / 706 services) et TVA (445710 exigible ou 445740 en
    attente) d'une facture de vente, ventilées par nature et par taux. Les montants
    reprennent exactement le calcul de la facture (TVA par taux arrondie une fois).

    Retourne (lignes, pending) où pending = {taux: {'vat','base'}} pour la TVA mise en
    attente jusqu'à l'encaissement. Les lignes de déduction d'acomptes (facture finale)
    sont exclues des produits : elles sont apurées par le compte 419100."""
    items = items if items is not None else _invoice_items(invoice)
    default_nature = _row_get(invoice, 'operation_nature') or 'services'
    if default_nature == 'mixte':
        default_nature = 'services'
    revenue = {}
    vat_lines = {}
    pending = {}
    if not items:
        # Données historiques sans lignes exploitables : une seule ligne de produit.
        account = revenue_override or REVENUE_ACCOUNT_BY_NATURE.get(default_nature, SERVICES_REVENUE_ACCOUNT)
        lines = [{'account_code': account, credit_side: float(_row_get(invoice, 'subtotal') or 0)}]
        if float(_row_get(invoice, 'vat_amount') or 0):
            lines.append({'account_code': VAT_COLLECTED_ACCOUNT, credit_side: float(_row_get(invoice, 'vat_amount') or 0)})
        return lines, pending
    for g in amounts.nature_rate_breakdown(items, default_nature):
        nature = g['nature']
        if nature != 'deduction':
            account = revenue_override or REVENUE_ACCOUNT_BY_NATURE[nature]
            revenue[account] = round(revenue.get(account, 0.0) + g['base'], 2)
        if not g['vat']:
            continue
        vat_nature = nature
        if nature == 'deduction':
            # La TVA d'acompte déjà facturée suit la nature dominante de la facture.
            vat_nature = default_nature
        vat_account = VAT_PENDING_ACCOUNT if _vat_due_on_payment(invoice, vat_nature) else VAT_COLLECTED_ACCOUNT
        key = (vat_account, g['rate'])
        cur = vat_lines.setdefault(key, {'vat': 0.0, 'base': 0.0})
        cur['vat'] = round(cur['vat'] + g['vat'], 2)
        cur['base'] = round(cur['base'] + g['base'], 2)
    lines = []
    for account in sorted(revenue):
        if round(revenue[account], 2):
            lines.append({'account_code': account, credit_side: revenue[account]} if revenue[account] > 0
                         else {'account_code': account, ('debit' if credit_side == 'credit' else 'credit'): -revenue[account]})
    for (account, rate), v in sorted(vat_lines.items()):
        if not round(v['vat'], 2):
            continue
        if v['vat'] > 0:
            lines.append({'account_code': account, credit_side: v['vat'], 'vat_rate': rate, 'vat_base': v['base']})
        else:
            lines.append({'account_code': account, ('debit' if credit_side == 'credit' else 'credit'): -v['vat'],
                          'vat_rate': rate, 'vat_base': -v['base']})
        if account == VAT_PENDING_ACCOUNT:
            pending[str(rate)] = {'vat': v['vat'], 'base': v['base']}
    return lines, pending


def _store_vat_pending(conn, invoice, pending):
    try:
        conn.execute('UPDATE outgoing_invoices SET vat_pending_json=? WHERE id=? AND entity_id IS ?',
                     (json.dumps(pending), invoice['id'], _row_get(invoice, 'entity_id')))
    except Exception:
        # Colonne absente sur un schéma très ancien : la TVA reste en attente au bilan,
        # elle sera virée manuellement — jamais une écriture de vente bloquée pour ça.
        pass


def _read_vat_pending(conn, invoice_id, entity_id):
    try:
        row = conn.execute('SELECT vat_pending_json FROM outgoing_invoices WHERE id=? AND entity_id IS ?',
                           (invoice_id, entity_id)).fetchone()
    except Exception:
        return {}
    if not row or not row['vat_pending_json']:
        return {}
    try:
        data = json.loads(row['vat_pending_json'])
    except (TypeError, ValueError):
        return {}
    return {k: {'vat': float(v.get('vat') or 0), 'base': float(v.get('base') or 0)} for k, v in data.items()}


def _pending_transfer(pending, share, settle_all):
    """Part de la TVA en attente à rendre exigible pour un encaissement (ou à annuler
    pour un avoir) représentant `share` du solde restant dû. Retourne
    ({taux: {'vat','base'}} transférés, pending restant)."""
    moved, left = {}, {}
    for rate, v in pending.items():
        if settle_all:
            t_vat, t_base = v['vat'], v['base']
        else:
            t_vat = min(v['vat'], amounts.money(v['vat'] * share))
            t_base = min(v['base'], amounts.money(v['base'] * share))
        if t_vat:
            moved[rate] = {'vat': round(t_vat, 2), 'base': round(t_base, 2)}
        rest = {'vat': round(v['vat'] - t_vat, 2), 'base': round(v['base'] - t_base, 2)}
        if rest['vat']:
            left[rate] = rest
    return moved, left


def _invoice_open_balance(conn, invoice, exclude_payment_id=None):
    """Reste dû TTC (encaissements et avoirs émis déduits) avant l'événement courant."""
    entity_id = _row_get(invoice, 'entity_id')
    paid = conn.execute(
        'SELECT COALESCE(SUM(amount),0) AS n FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ? AND id<>?',
        (invoice['id'], entity_id, exclude_payment_id or 0)).fetchone()['n']
    try:
        credited = conn.execute(
            "SELECT COALESCE(SUM(total),0) AS n FROM outgoing_credit_notes WHERE original_invoice_id=? AND entity_id IS ? AND status='issued'",
            (invoice['id'], entity_id)).fetchone()['n']
    except Exception:
        credited = 0
    return round(float(_row_get(invoice, 'total') or 0) - float(paid or 0) - float(credited or 0), 2)


def generate_sale_entry(conn, invoice, commit=True):
    """Génère l'écriture de vente (journal VE) pour une facture client émise :
    Clients (411) au débit du TTC ; au crédit, Ventes de marchandises (707) pour les
    biens et Prestations de services (706) pour les services, et la TVA :
    - 445710 « TVA collectée » quand elle est exigible à la facturation (biens, ou
      services avec option pour les débits) ;
    - 445740 « TVA collectée en attente » pour les services sans option débits : elle
      devient exigible à l'encaissement (virement 445740 -> 445710 à chaque règlement).
    invoice : ligne de outgoing_invoices. Idempotent."""
    entity_id = invoice['entity_id'] if 'entity_id' in invoice.keys() else None
    if _entry_already_exists(conn, 'outgoing_invoice', invoice['id'], entity_id):
        return None
    lines = [
        {'account_code': '411000', 'debit': invoice['total'], 'auxiliary_name': invoice['client_name']},
    ]
    product_lines, pending = _sale_revenue_and_vat_lines(invoice)
    lines.extend(product_lines)
    entry_id = create_entry(
        conn, 'VE', invoice['issue_date'] or paris_today(),
        f"Facture {invoice['invoice_number']} — {invoice['client_name']}",
        lines, source_type='outgoing_invoice', source_id=invoice['id'],
        entity_id=entity_id, piece_ref=invoice['invoice_number'], commit=False,
    )
    _store_vat_pending(conn, invoice, pending)
    if commit:
        conn.commit()
    return entry_id



def generate_customer_deposit_entry(conn, invoice, commit=True):
    """Comptabilise une facture d'acompte sans reconnaître prématurément le CA.

    411 au débit du TTC, 4191 au crédit du HT d'acompte et TVA au crédit (445710, ou
    445740 pour un acompte sur prestation de services exigible à l'encaissement).
    Idempotent par facture et entité.
    """
    entity_id = invoice['entity_id'] if 'entity_id' in invoice.keys() else None
    if _entry_already_exists(conn, 'customer_deposit_invoice', invoice['id'], entity_id):
        return None
    lines=[
        {'account_code':'411000','debit':invoice['total'],'auxiliary_name':invoice['client_name']},
    ]
    product_lines, pending = _sale_revenue_and_vat_lines(invoice, revenue_override='419100')
    lines.extend(product_lines)
    entry_id = create_entry(conn,'VE',invoice['issue_date'] or paris_today(),
        f"Facture d'acompte {invoice['invoice_number']} — {invoice['client_name']}",
        lines,source_type='customer_deposit_invoice',source_id=invoice['id'],entity_id=entity_id,
        piece_ref=invoice['invoice_number'], commit=False)
    _store_vat_pending(conn, invoice, pending)
    if commit:
        conn.commit()
    return entry_id


def generate_customer_final_entry(conn, invoice, commit=True):
    """Comptabilise la facture finale et apure le HT des acomptes déjà facturés."""
    entity_id = invoice['entity_id'] if 'entity_id' in invoice.keys() else None
    if _entry_already_exists(conn, 'customer_final_invoice', invoice['id'], entity_id):
        return None
    applied_ht=float(invoice['deposit_applied_subtotal'] or 0) if 'deposit_applied_subtotal' in invoice.keys() else 0.0
    items = _invoice_items(invoice)
    for it in items:
        # Factures finales antérieures : la déduction était une ligne à prix négatif.
        if not it.get('deposit_deduction') and str(it.get('label') or '').startswith('Déduction acomptes'):
            it['deposit_deduction'] = True
    lines=[{'account_code':'411000','debit':invoice['total'],'auxiliary_name':invoice['client_name']}]
    if applied_ht:
        lines.append({'account_code':'419100','debit':applied_ht})
    product_lines, pending = _sale_revenue_and_vat_lines(invoice, items=items)
    lines.extend(product_lines)
    entry_id = create_entry(conn,'VE',invoice['issue_date'] or paris_today(),
        f"Facture finale {invoice['invoice_number']} — {invoice['client_name']}",
        lines,source_type='customer_final_invoice',source_id=invoice['id'],entity_id=entity_id,
        piece_ref=invoice['invoice_number'], commit=False)
    _store_vat_pending(conn, invoice, pending)
    if commit:
        conn.commit()
    return entry_id

def generate_sale_credit_entry(conn, credit, revenue_account=None):
    """Génère l'écriture comptable d'un avoir client émis.

    L'écriture inverse la vente d'origine : produits (707 biens / 706 services, ou
    419100 pour un avoir sur facture d'acompte) et TVA au débit, client (411) au
    crédit. La TVA encore en attente d'encaissement (445740) de la facture d'origine
    est annulée en priorité, au prorata ; le reste minore la TVA collectée (445710).
    Idempotente par avoir et par entité.
    """
    entity_id = credit['entity_id'] if 'entity_id' in credit.keys() else None
    if _entry_already_exists(conn, 'outgoing_credit_note', credit['id'], entity_id):
        return None
    original = None
    if _row_get(credit, 'original_invoice_id'):
        original = conn.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?',
                                (credit['original_invoice_id'], entity_id)).fetchone()
    items = _invoice_items(credit)
    default_nature = (_row_get(original, 'operation_nature') if original is not None else None) or 'services'
    if default_nature == 'mixte':
        default_nature = 'services'
    lines = []
    if items:
        revenue = {}
        vat_by_rate = {}
        for g in amounts.nature_rate_breakdown(items, default_nature):
            if g['nature'] == 'deduction':
                # Avoir sur facture finale : la part « acomptes déduits » réintègre l'acompte
                # (419100), comme l'écriture de la facture finale l'avait apuré.
                account = '419100'
            else:
                account = revenue_account or REVENUE_ACCOUNT_BY_NATURE.get(g['nature'], SERVICES_REVENUE_ACCOUNT)
            revenue[account] = round(revenue.get(account, 0.0) + g['base'], 2)
            if g['vat']:
                cur = vat_by_rate.setdefault(str(g['rate']), {'vat': 0.0, 'base': 0.0})
                cur['vat'] = round(cur['vat'] + g['vat'], 2)
                cur['base'] = round(cur['base'] + g['base'], 2)
        # Écart d'arrondi éventuel entre le détail des lignes et les totaux stockés.
        diff_ht = round(float(credit['subtotal'] or 0) - sum(revenue.values()), 2)
        if diff_ht and revenue:
            k = max(revenue, key=lambda a: revenue[a])
            revenue[k] = round(revenue[k] + diff_ht, 2)
        for account in sorted(revenue):
            if revenue[account] > 0:
                lines.append({'account_code': account, 'debit': revenue[account]})
            elif revenue[account] < 0:
                lines.append({'account_code': account, 'credit': -revenue[account]})
        pending_moved = {}
        if original is not None:
            pending = _read_vat_pending(conn, original['id'], entity_id)
            if pending:
                balance = _invoice_open_balance(conn, original)
                # L'avoir vient d'être inséré : il est déjà déduit du solde, on le réintègre.
                balance_before = round(balance + float(credit['total'] or 0), 2)
                share = float(credit['total'] or 0) / balance_before if balance_before > 0.005 else 1.0
                moved, left = _pending_transfer(pending, min(share, 1.0), share >= 0.9999)
                for rate, v in moved.items():
                    cap = vat_by_rate.get(rate, {'vat': 0.0})['vat']
                    if v['vat'] > cap:
                        v['vat'] = cap
                    pending_moved[rate] = v
                left = {r: {'vat': round(pending[r]['vat'] - pending_moved.get(r, {'vat': 0})['vat'], 2),
                            'base': round(pending[r]['base'] - pending_moved.get(r, {'base': 0})['base'], 2)}
                        for r in pending}
                _store_vat_pending(conn, original, {r: v for r, v in left.items() if v['vat']})
        vat_total = 0.0
        for rate in sorted(vat_by_rate, key=float):
            v = vat_by_rate[rate]
            moved = pending_moved.get(rate, {'vat': 0.0, 'base': 0.0})
            if moved['vat']:
                lines.append({'account_code': VAT_PENDING_ACCOUNT, 'debit': moved['vat'], 'vat_rate': float(rate), 'vat_base': moved['base']})
            rest = round(v['vat'] - moved['vat'], 2)
            if rest:
                lines.append({'account_code': VAT_COLLECTED_ACCOUNT, 'debit': rest, 'vat_rate': float(rate),
                              'vat_base': round(v['base'] - moved['base'], 2)})
            vat_total = round(vat_total + v['vat'], 2)
        diff_vat = round(float(credit['vat_amount'] or 0) - vat_total, 2)
        if diff_vat:
            lines.append({'account_code': VAT_COLLECTED_ACCOUNT, 'debit' if diff_vat > 0 else 'credit': abs(diff_vat)})
    else:
        lines.append({'account_code': revenue_account or SERVICES_REVENUE_ACCOUNT, 'debit': credit['subtotal']})
        if credit['vat_amount']:
            lines.append({'account_code': VAT_COLLECTED_ACCOUNT, 'debit': credit['vat_amount']})
    lines.append({
        'account_code': '411000', 'credit': credit['total'],
        'auxiliary_name': credit['client_name'],
    })
    return create_entry(
        conn, 'VE', credit['issue_date'] or paris_today(),
        f"Avoir {credit['credit_number']} — {credit['client_name']}",
        lines, source_type='outgoing_credit_note', source_id=credit['id'],
        entity_id=entity_id, piece_ref=credit['credit_number'],
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


def _letter_pair(conn, account_code, original_source_type, original_source_id, new_entry_id, entity_id=None):
    """Lettre automatiquement la ligne d'origine (facture) et la ligne de
    règlement qui vient d'être créée, sur le même compte collectif (411 ou
    401), si les deux existent, ne sont pas déjà lettrées et que leurs
    montants se compensent exactement. Ne lève jamais d'erreur : le lettrage
    automatique est une aide, pas une contrainte — s'il ne peut pas
    s'appliquer proprement, l'écriture reste valide, simplement non lettrée."""
    original_line = conn.execute(
        """SELECT l.id, l.debit, l.credit FROM accounting_entry_lines l
           JOIN accounting_entries e ON e.id = l.entry_id
           WHERE e.source_type=? AND e.source_id=? AND e.entity_id IS ? AND l.account_code=? AND l.lettrage_code IS NULL
           LIMIT 1""",
        (original_source_type, original_source_id, entity_id, account_code),
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
    set_lettrage(conn, [original_line['id'], new_line['id']], code)
    conn.commit()


def set_lettrage(conn, line_ids, code, lettrage_date=None):
    """Lettre des lignes et mémorise la date de lettrage (colonne FEC « DateLet »)."""
    when = (lettrage_date or paris_today()).isoformat() if not isinstance(lettrage_date, str) else lettrage_date
    for line_id in line_ids:
        try:
            conn.execute('UPDATE accounting_entry_lines SET lettrage_code=?,lettrage_date=? WHERE id=?', (code, when, line_id))
        except Exception:
            conn.execute('UPDATE accounting_entry_lines SET lettrage_code=? WHERE id=?', (code, line_id))


def generate_sale_payment_entry(conn, invoice, payment_date=None):
    """Génère l'écriture de règlement (journal BQ) quand une facture client
    est marquée payée : Banque (512) au débit, Clients (411) au crédit.
    Lettre automatiquement cette écriture avec la facture d'origine sur le
    compte 411. Datée du jour réel de l'encaissement. Idempotent."""
    source_type = 'outgoing_invoice_payment'
    entity_id = invoice['entity_id'] if 'entity_id' in invoice.keys() else None
    if _entry_already_exists(conn, source_type, invoice['id'], entity_id):
        return None
    entry_id = create_entry(
        conn, 'BQ', payment_date or paris_today(),
        f"Règlement facture {invoice['invoice_number']} — {invoice['client_name']}",
        [
            {'account_code': '512000', 'debit': invoice['total']},
            {'account_code': '411000', 'credit': invoice['total'], 'auxiliary_name': invoice['client_name']},
        ],
        source_type=source_type, source_id=invoice['id'], entity_id=entity_id,
    )
    _letter_pair(conn, '411000', 'outgoing_invoice', invoice['id'], entry_id, entity_id)
    return entry_id


def generate_sale_partial_payment_entry(conn, invoice, payment, commit=True, bank_account='512000'):
    """Comptabilise un règlement client individuel. Chaque règlement possède
    sa propre clé source, ce qui autorise plusieurs paiements sur une facture
    tout en restant idempotent. Le lettrage complet est laissé au moteur de
    lettrage lorsque le solde 411 est entièrement compensé."""
    entity_id = invoice['entity_id'] if 'entity_id' in invoice.keys() else None
    amount = round(float(payment['amount'] or 0), 2)
    if amount <= 0:
        raise AccountingError("Le montant du règlement doit être strictement positif.")
    source_type = 'outgoing_invoice_payment_v2'
    if _entry_already_exists(conn, source_type, payment['id'], entity_id):
        return None
    lines = [
            {'account_code': bank_account or '512000', 'debit': amount},
            {'account_code': '411000', 'credit': amount, 'auxiliary_name': invoice['client_name']},
    ]
    # TVA sur encaissements : la part de TVA en attente (445740) correspondant à ce
    # règlement devient exigible (445710), au prorata du solde restant dû ; le
    # règlement qui solde la facture vire tout le reliquat (pas d'écart d'arrondi).
    pending = _read_vat_pending(conn, invoice['id'], entity_id)
    left = pending
    if pending:
        balance_before = _invoice_open_balance(conn, invoice, exclude_payment_id=payment['id'])
        settle_all = amount >= balance_before - 0.005
        share = amount / balance_before if balance_before > 0.005 else 1.0
        moved, left = _pending_transfer(pending, min(share, 1.0), settle_all)
        for rate in sorted(moved, key=float):
            v = moved[rate]
            lines.append({'account_code': VAT_PENDING_ACCOUNT, 'debit': v['vat'], 'vat_rate': float(rate), 'vat_base': v['base'],
                          'label': f"TVA exigible sur encaissement — facture {invoice['invoice_number']}"})
            lines.append({'account_code': VAT_COLLECTED_ACCOUNT, 'credit': v['vat'], 'vat_rate': float(rate), 'vat_base': v['base'],
                          'label': f"TVA exigible sur encaissement — facture {invoice['invoice_number']}"})
    entry_id = create_entry(
        conn, 'BQ', payment['payment_date'] or paris_today(),
        f"Règlement facture {invoice['invoice_number']} — {invoice['client_name']}",
        lines,
        source_type=source_type, source_id=payment['id'], entity_id=entity_id,
        piece_ref=_row_get(payment, 'reference') or invoice['invoice_number'], commit=False,
    )
    if pending:
        _store_vat_pending(conn, invoice, left)
    if commit:
        conn.commit()
    return entry_id


def generate_purchase_entry(conn, purchase, commit=True):
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
        conn, 'AC', purchase['issue_date'] or paris_today(),
        f"Facture {purchase['invoice_number']} — {purchase['supplier_name']}",
        lines, source_type='purchase_invoice', source_id=purchase['id'],
        entity_id=purchase['entity_id'] if 'entity_id' in purchase.keys() else None,
        commit=commit, piece_ref=purchase['invoice_number'],
    )



def reverse_purchase_entry(conn, purchase, commit=False):
    """Extourne l'écriture d'achat d'une facture fournisseur corrigée ou rejetée.

    L'écriture d'origine n'est jamais modifiée ni supprimée (inaltérabilité) : on passe une
    écriture inverse à la même date, puis on renomme la source de l'écriture d'origine
    (« purchase_invoice_superseded:n ») pour qu'une nouvelle écriture « purchase_invoice »
    puisse être générée avec les montants corrigés. Retourne False s'il n'y avait rien à extourner."""
    entity_id = purchase['entity_id'] if 'entity_id' in purchase.keys() else None
    entity_key = int(entity_id) if entity_id is not None else 0
    original = conn.execute(
        "SELECT * FROM accounting_entries WHERE source_type='purchase_invoice' AND source_id=? AND entity_id IS ? LIMIT 1",
        (purchase['id'], entity_id)).fetchone()
    if not original:
        return False
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM accounting_entries WHERE source_type LIKE 'purchase_invoice_superseded:%%' AND source_id=? AND entity_id IS ?",
        (purchase['id'], entity_id)).fetchone()['n'] + 1
    lines = conn.execute(
        'SELECT account_code,auxiliary_name,debit,credit FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order',
        (original['id'],)).fetchall()
    reversed_lines = [{'account_code': l['account_code'], 'auxiliary_name': l['auxiliary_name'],
                       'debit': l['credit'] or 0, 'credit': l['debit'] or 0} for l in lines]
    superseded = f'purchase_invoice_superseded:{n}'
    conn.execute("UPDATE accounting_entries SET source_type=? WHERE id=?", (superseded, original['id']))
    conn.execute("UPDATE accounting_source_claims SET source_type=? WHERE entity_key=? AND source_type='purchase_invoice' AND source_id=?",
                 (superseded, entity_key, purchase['id']))
    create_entry(conn, 'AC', original['entry_date'],
                 f"Extourne — {original['label']}", reversed_lines,
                 source_type=f'purchase_invoice_reversal:{n}', source_id=purchase['id'],
                 entity_id=entity_id, commit=commit)
    return True


def generate_purchase_credit_entry(conn, credit, commit=True):
    """Comptabilise un avoir fournisseur : 401 au débit, charge et TVA au crédit."""
    entity_id=credit['entity_id'] if 'entity_id' in credit.keys() else None
    if _entry_already_exists(conn,'purchase_credit_note',credit['id'],entity_id): return None
    charge_account=_category_account(conn,credit['category'])
    lines=[{'account_code':'401000','debit':credit['total'],'auxiliary_name':credit['supplier_name']},
           {'account_code':charge_account,'credit':credit['subtotal']}]
    if credit['vat_amount']:
        lines.append({'account_code':'445660','credit':credit['vat_amount']})
    return create_entry(conn,'AC',credit['issue_date'] or paris_today(),
        f"Avoir fournisseur {credit['credit_number']} — {credit['supplier_name']}",
        lines,source_type='purchase_credit_note',source_id=credit['id'],entity_id=entity_id,commit=commit)


def generate_purchase_payment_entry(conn, purchase, payment_date=None):
    """Génère l'écriture de règlement (journal BQ) quand une facture
    fournisseur est marquée payée : Fournisseurs (401) au débit, Banque (512)
    au crédit. Lettre automatiquement cette écriture avec la facture
    d'origine sur le compte 401. Datée du jour réel du paiement. Idempotent."""
    source_type = 'purchase_invoice_payment'
    entity_id = purchase['entity_id'] if 'entity_id' in purchase.keys() else None
    if _entry_already_exists(conn, source_type, purchase['id'], entity_id):
        return None
    entry_id = create_entry(
        conn, 'BQ', payment_date or paris_today(),
        f"Règlement facture {purchase['invoice_number']} — {purchase['supplier_name']}",
        [
            {'account_code': '401000', 'debit': purchase['total'], 'auxiliary_name': purchase['supplier_name']},
            {'account_code': '512000', 'credit': purchase['total']},
        ],
        source_type=source_type, source_id=purchase['id'], entity_id=entity_id,
    )
    _letter_pair(conn, '401000', 'purchase_invoice', purchase['id'], entry_id, entity_id)
    return entry_id


def generate_purchase_partial_payment_entry(conn, purchase, payment, commit=True, bank_account='512000'):
    """Comptabilise un règlement fournisseur individuel, entity-scoped et idempotent.
    commit=False laisse l'appelant valider la transaction (rapprochement bancaire : le
    paiement, l'écriture et l'affectation doivent être enregistrés ensemble ou pas du tout)."""
    entity_id = purchase['entity_id'] if 'entity_id' in purchase.keys() else None
    amount = round(float(payment['amount'] or 0), 2)
    if amount <= 0:
        raise AccountingError("Le montant du règlement doit être strictement positif.")
    source_type = 'purchase_invoice_payment_v2'
    if _entry_already_exists(conn, source_type, payment['id'], entity_id):
        return None
    return create_entry(conn, 'BQ', payment['payment_date'] or paris_today(),
        f"Règlement facture {purchase['invoice_number']} — {purchase['supplier_name']}",
        [
            {'account_code':'401000','debit':amount,'auxiliary_name':purchase['supplier_name']},
            {'account_code':bank_account or '512000','credit':amount},
        ], source_type=source_type, source_id=payment['id'], entity_id=entity_id, commit=commit,
        piece_ref=_row_get(payment, 'reference') or purchase['invoice_number'])


def seed_accounting_defaults(conn):
    """Insère le plan comptable, les journaux et la correspondance de
    catégories par défaut s'ils ne sont pas déjà présents. Idempotent : ne
    duplique jamais, n'écrase jamais une valeur déjà personnalisée par
    l'utilisateur (INSERT OR IGNORE)."""
    now = datetime.utcnow().isoformat()
    for code, label, klass, collective in DEFAULT_CHART_OF_ACCOUNTS + PCG_EXTENDED_ACCOUNTS:
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
    # Bases existantes : 455000 (comptes courants d'associés) était classé en capitaux
    # propres. C'est une dette (classe 4) — reclassement idempotent.
    conn.execute("UPDATE accounting_chart_of_accounts SET account_class=4 WHERE code='455000' AND account_class=1")
    conn.commit()


class AccountingError(ValueError):
    """Levée quand une écriture ne respecte pas les règles de la partie
    double, ou référence un compte/journal inexistant. Ne jamais attraper
    cette exception pour forcer une écriture déséquilibrée : c'est
    précisément ce qu'elle empêche."""


def _next_piece_number(conn, journal_code, entry_date, entity_id=None):
    """Réserve atomiquement le prochain numéro par entité/journal/année.

    L'UPSERT est sérialisé par PostgreSQL sur la clé primaire et évite le
    classique SELECT MAX()+1, vulnérable à deux créations simultanées.
    SQLite récent supporte le même INSERT ... ON CONFLICT ... RETURNING.
    """
    year = str(entry_date)[:4]
    entity_key = int(entity_id) if entity_id is not None else 0
    # Amorçage compatible avec les bases existantes : si la table de
    # séquence est encore vide après migration, repartir du plus grand numéro
    # déjà comptabilisé au lieu de recommencer à 00001.
    prefix = f'{journal_code}-{year}-'
    legacy = conn.execute(
        """SELECT piece_number FROM accounting_entries
           WHERE journal_code=? AND piece_number LIKE ? AND entity_id IS ?
           ORDER BY piece_number DESC LIMIT 1""",
        (journal_code, prefix + '%', entity_id),
    ).fetchone()
    seed = 0
    if legacy:
        try:
            seed = int(str(legacy['piece_number']).rsplit('-', 1)[-1])
        except (ValueError, IndexError):
            seed = 0
    row = conn.execute(
        """INSERT INTO accounting_piece_sequences(entity_key,journal_code,fiscal_year,last_sequence)
           VALUES(?,?,?,?)
           ON CONFLICT(entity_key,journal_code,fiscal_year) DO UPDATE SET
             last_sequence=accounting_piece_sequences.last_sequence+1
           RETURNING last_sequence""",
        (entity_key, journal_code, year, seed + 1),
    ).fetchone()
    return f'{journal_code}-{year}-{int(row["last_sequence"]):05d}'


def create_entry(conn, journal_code, entry_date, label, lines,
                  source_type=None, source_id=None, created_by=None, entity_id=None,
                  commit=True, piece_ref=None):
    """Crée une écriture comptable en partie double, avec ses lignes.

    piece_ref : référence de la pièce justificative (numéro de facture, d'avoir…),
    exportée dans la colonne FEC « PieceRef » ; à défaut, le numéro de pièce interne.

    lines : liste de dicts {account_code, debit=0, credit=0, label=None,
    auxiliary_name=None, vat_rate=None, vat_base=None}. vat_rate/vat_base renseignent
    le taux et la base HT d'une ligne de TVA (déclaration CA3 par taux).
    Chaque ligne doit avoir soit un débit soit un
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
    entity_key = int(entity_id) if entity_id is not None else 0
    closure = conn.execute(
        'SELECT closed_until FROM accounting_entity_closure WHERE entity_key=?', (entity_key,)
    ).fetchone()
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
            'vat_rate': raw.get('vat_rate'),
            'vat_base': raw.get('vat_base'),
        })

    if abs(total_debit - total_credit) > 0.01:
        raise AccountingError(
            f"Écriture déséquilibrée : total débit {total_debit:.2f} € "
            f"≠ total crédit {total_credit:.2f} €."
        )

    now = datetime.utcnow().isoformat()

    # Idempotence DB : une source métier ne peut produire qu'une seule OD
    # pour une même entité, même si deux requêtes arrivent simultanément.
    claim_key = None
    if source_type is not None and source_id is not None:
        claim_key = (entity_key, str(source_type), int(source_id))
        existing = conn.execute(
            '''SELECT id FROM accounting_entries
               WHERE source_type=? AND source_id=? AND entity_id IS ? LIMIT 1''',
            (str(source_type), int(source_id), entity_id),
        ).fetchone()
        if existing:
            raise AccountingError(
                f"Une écriture existe déjà pour la source {source_type} #{source_id}."
            )
        claim = conn.execute(
            '''INSERT OR IGNORE INTO accounting_source_claims
               (entity_key,source_type,source_id,entry_id,created_at) VALUES(?,?,?,?,?)''',
            (*claim_key, None, now),
        )
        if claim.rowcount == 0:
            raise AccountingError(
                f"Une écriture est déjà en cours ou existe pour la source {source_type} #{source_id}."
            )

    piece_number = _next_piece_number(conn, journal_code, entry_date_str, entity_id)

    cur = conn.execute(
        'INSERT INTO accounting_entries'
        '(journal_code,piece_number,entry_date,label,source_type,source_id,is_locked,created_by,created_at,entity_id)'
        ' VALUES(?,?,?,?,?,?,0,?,?,?)',
        (journal_code, piece_number, entry_date_str, label, source_type, source_id, created_by, now, entity_id),
    )
    entry_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    if piece_ref:
        conn.execute('UPDATE accounting_entries SET piece_ref=? WHERE id=?', (str(piece_ref)[:120], entry_id))
    if claim_key is not None:
        conn.execute(
            'UPDATE accounting_source_claims SET entry_id=? WHERE entity_key=? AND source_type=? AND source_id=?',
            (entry_id, *claim_key),
        )
    for order, ln in enumerate(clean_lines):
        if ln['vat_rate'] is not None:
            conn.execute(
                'INSERT INTO accounting_entry_lines'
                '(entry_id,account_code,auxiliary_name,label,debit,credit,line_order,vat_rate,vat_base)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (entry_id, ln['account_code'], ln['auxiliary_name'], ln['label'],
                 ln['debit'], ln['credit'], order, float(ln['vat_rate']),
                 round(float(ln['vat_base']), 2) if ln['vat_base'] is not None else None),
            )
            continue
        conn.execute(
            'INSERT INTO accounting_entry_lines'
            '(entry_id,account_code,auxiliary_name,label,debit,credit,line_order)'
            ' VALUES(?,?,?,?,?,?,?)',
            (entry_id, ln['account_code'], ln['auxiliary_name'], ln['label'],
             ln['debit'], ln['credit'], order),
        )
    if commit:
        conn.commit()
    return entry_id


# --- Export FEC (Fichier des Écritures Comptables) -------------------------
# Format réglementaire français (arrêté du 29 juillet 2013, art. A47 A-1 du LPF) :
# 18 colonnes obligatoires, séparateur "|", montants avec la VIRGULE comme
# séparateur décimal (art. A47 A-1 : « les montants … avec la virgule comme
# séparateur décimal »), sans séparateur de milliers — ne jamais y appliquer
# fr_number (espaces insécables). Encodage : UTF-8 sans BOM (accepté par l'outil
# Test Compta Demat ; l'arrêté autorise aussi ISO 8859-15). Le caractère "|"
# et les retours à la ligne sont retirés des libellés. Nom de fichier imposé :
# SIREN + FEC + date de clôture (AAAAMMJJ) + .txt.
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
    """Formate un montant au format FEC : virgule décimale, deux décimales,
    aucun séparateur de milliers (1234,50). Ne jamais utiliser fr_number ici —
    il insère des espaces insécables entre les milliers."""
    return f"{float(value or 0):.2f}".replace('.', ',')


def _fec_text(value):
    """Libellé FEC sûr : sans séparateur "|" ni saut de ligne/tabulation."""
    s = '' if value is None else str(value)
    for ch in ('\r\n', '\n', '\r', '\t'):
        s = s.replace(ch, ' ')
    return s.replace('|', '/').strip()


_AUX_PREFIX = {'411': 'C', '401': 'F', '421': 'S', '467': 'D', '419': 'C', '455': 'A'}


def _auxiliary_codes(conn, entity_id, pairs):
    """Codes auxiliaires stables (CompAuxNum) par tiers : C00001 pour un client,
    F00001 pour un fournisseur… Attribués une fois, mémorisés, jamais réattribués :
    le même tiers garde le même code d'un export à l'autre."""
    entity_key = int(entity_id) if entity_id is not None else 0
    out = {}
    try:
        existing = conn.execute(
            'SELECT root,name_key,aux_code FROM accounting_auxiliary_codes WHERE entity_key=?', (entity_key,)).fetchall()
    except Exception:
        existing = None
    if existing is None:
        # Table absente (schéma non migré) : codes dérivés, stables mais non mémorisés.
        for account, name in pairs:
            root = str(account)[:3]
            key = ' '.join(str(name).lower().split())
            out[(account, name)] = _AUX_PREFIX.get(root, 'T') + ''.join(ch for ch in key.upper() if ch.isalnum())[:10]
        return out
    known = {(r['root'], r['name_key']): r['aux_code'] for r in existing}
    counters = {}
    for r in existing:
        try:
            prefix = r['aux_code'][0]
            counters[prefix] = max(counters.get(prefix, 0), int(r['aux_code'][1:]))
        except (ValueError, IndexError, TypeError):
            pass
    created = False
    for account, name in sorted(set(pairs), key=lambda p: (str(p[0]), str(p[1]).lower())):
        root = str(account)[:3]
        key = ' '.join(str(name).lower().split())
        code = known.get((root, key))
        if not code:
            prefix = _AUX_PREFIX.get(root, 'T')
            counters[prefix] = counters.get(prefix, 0) + 1
            code = f"{prefix}{counters[prefix]:05d}"
            conn.execute('INSERT OR IGNORE INTO accounting_auxiliary_codes(entity_key,root,name_key,aux_code,label,created_at) VALUES(?,?,?,?,?,?)',
                         (entity_key, root, key, code, str(name)[:200], datetime.utcnow().isoformat()))
            known[(root, key)] = code
            created = True
        out[(account, name)] = code
    if created:
        conn.commit()
    return out


def generate_fec(conn, date_from, date_to, entity_id=None):
    """Génère le contenu FEC (liste de lignes, la première étant l'en-tête)
    pour toutes les écritures dont la date est comprise entre date_from et
    date_to (inclus). entity_id filtre sur une seule entité à la fois —
    None signifie explicitement « société mère uniquement » (entity_id NULL
    en base), jamais « toutes les entités confondues » : chaque entité est
    un sujet fiscal distinct et doit avoir son propre fichier FEC, jamais un
    mélange. Retourne une liste de listes de chaînes — une ligne par
    écriture comptable, classées par journal, puis par date d'écriture, puis par
    numéro de pièce (EcritureNum).

    - CompAuxNum / CompAuxLib : code auxiliaire stable du tiers (C00001…) et son nom,
      au lieu du compte collectif 411000 répété pour tous les clients ;
    - PieceRef : numéro du document justificatif (facture, avoir…) ;
    - DateLet : date du lettrage quand la ligne est lettrée."""
    if entity_id:
        entries = conn.execute(
            """SELECT e.*, j.label AS journal_label FROM accounting_entries e
               JOIN accounting_journals j ON j.code = e.journal_code
               WHERE e.entry_date BETWEEN ? AND ? AND e.entity_id=?
               ORDER BY e.journal_code, e.entry_date, e.id""",
            (str(date_from), str(date_to), entity_id),
        ).fetchall()
    else:
        entries = conn.execute(
            """SELECT e.*, j.label AS journal_label FROM accounting_entries e
               JOIN accounting_journals j ON j.code = e.journal_code
               WHERE e.entry_date BETWEEN ? AND ? AND e.entity_id IS NULL
               ORDER BY e.journal_code, e.entry_date, e.id""",
            (str(date_from), str(date_to)),
        ).fetchall()

    rows = [FEC_COLUMNS]
    all_lines = []
    for e in entries:
        lines = conn.execute(
            """SELECT l.*, a.label AS account_label FROM accounting_entry_lines l
               JOIN accounting_chart_of_accounts a ON a.code = l.account_code
                    AND (a.entity_id IS NULL OR a.entity_id=?)
               WHERE l.entry_id=? ORDER BY l.line_order""",
            (e['entity_id'], e['id']),
        ).fetchall()
        all_lines.append((e, lines))
    aux = _auxiliary_codes(conn, entity_id, [(l['account_code'], l['auxiliary_name'])
                                             for _, lines in all_lines for l in lines if l['auxiliary_name']])
    numbers = fec_entry_numbers(conn, entries, entity_id)
    for e, lines in all_lines:
        validation_date = _fec_date(paris_date_of(e['created_at'])) if e['created_at'] else _fec_date(e['entry_date'])
        piece_ref = _row_get(e, 'piece_ref') or e['piece_number']
        for l in lines:
            aux_code = aux.get((l['account_code'], l['auxiliary_name'])) if l['auxiliary_name'] else ''
            rows.append([
                e['journal_code'],
                _fec_text(e['journal_label']),
                numbers.get(e['id'], e['piece_number']),
                _fec_date(e['entry_date']),
                l['account_code'],
                _fec_text(l['account_label']),
                aux_code or '',
                _fec_text(l['auxiliary_name'] or ''),
                _fec_text(piece_ref),
                _fec_date(e['entry_date']),
                _fec_text(l['label'] or e['label']),
                _fec_amount(l['debit']),
                _fec_amount(l['credit']),
                _fec_text(l['lettrage_code'] or ''),
                # Lettrage antérieur à la mémorisation de sa date : date de validation à défaut.
                (_fec_date(_row_get(l, 'lettrage_date')) or validation_date) if l['lettrage_code'] else '',
                validation_date,
                '',
                '',
            ])
    return rows


def fec_entry_numbers(conn, entries, entity_id=None):
    """EcritureNum chronologique : par journal et par année civile, les écritures sont
    numérotées dans l'ordre (date d'écriture, ordre de saisie) depuis le 1er janvier —
    une écriture saisie après coup à une date antérieure (règlement daté du jour réel)
    reprend sa place chronologique. Le numéro de pièce interne reste inchangé en base
    ; seules les périodes ouvertes peuvent encore recevoir des écritures, la
    numérotation d'une période clôturée est donc définitive."""
    out = {}
    groups = {}
    for e in entries:
        groups.setdefault((e['journal_code'], str(e['entry_date'])[:4]), True)
    ef = 'entity_id=?' if entity_id else 'entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    for journal, year in groups:
        ids = conn.execute(
            f"""SELECT id FROM accounting_entries WHERE journal_code=? AND entry_date BETWEEN ? AND ? AND {ef}
                ORDER BY entry_date, id""",
            (journal, f'{year}-01-01', f'{year}-12-31', *ep)).fetchall()
        for n, row in enumerate(ids, start=1):
            out[row['id']] = f"{journal}-{year}-{n:05d}"
    return out


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

DEGRESSIVE_MIN_YEARS = 3


def degressive_coefficient(useful_life_years):
    """Coefficient fiscal de l'amortissement dégressif (CGI art. 39 A) : 1,25 pour 3 ou
    4 ans, 1,75 pour 5 ou 6 ans, 2,25 au-delà. None si la durée est inférieure à 3 ans."""
    n = float(useful_life_years or 0)
    if n < DEGRESSIVE_MIN_YEARS:
        return None
    return 1.25 if n <= 4 else (1.75 if n <= 6 else 2.25)


def degressive_schedule(purchase_amount, useful_life_years, purchase_date):
    """Plan d'amortissement dégressif par année civile : {année: dotation}.

    - taux = coefficient / durée ; première annuité au prorata des mois à compter du
      premier jour du mois d'acquisition ;
    - chaque année, la dotation est le maximum entre l'annuité dégressive (VNC × taux) et
      l'annuité linéaire sur la durée restante (VNC / années restantes) : bascule en
      linéaire dès qu'elle devient plus favorable ;
    - la dernière annuité solde la valeur nette (pas d'annuité supplémentaire)."""
    n = int(round(float(useful_life_years)))
    coef = degressive_coefficient(n)
    if coef is None:
        raise ValueError("L'amortissement dégressif exige une durée d'au moins 3 ans.")
    from decimal import Decimal, ROUND_HALF_UP
    cent = Decimal('0.01')
    start = date.fromisoformat(str(purchase_date)[:10])
    rate = Decimal(str(coef)) / Decimal(n)   # calcul décimal exact (0,35 et non 0,3499…)
    vnc = Decimal(str(purchase_amount))
    plan = {}
    for k in range(n):
        year = start.year + k
        remaining_years = Decimal(n - k)
        if k == n - 1:
            dot = vnc
        elif k == 0:
            months = Decimal(12 - start.month + 1)
            dot = max(vnc * rate, vnc / remaining_years) * months / Decimal(12)
        else:
            dot = max(vnc * rate, vnc / remaining_years)
        dot = min(dot, vnc).quantize(cent, rounding=ROUND_HALF_UP)
        plan[year] = float(dot)
        vnc = (vnc - dot).quantize(cent, rounding=ROUND_HALF_UP)
    return plan


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
    method = (asset['method'] if 'method' in asset.keys() else None) or 'linear'
    if method == 'degressive' and degressive_coefficient(asset['useful_life_years']):
        theoretical = degressive_schedule(asset['purchase_amount'], asset['useful_life_years'],
                                          asset['purchase_date']).get(calendar_year, 0.0)
    elif calendar_year == purchase_date_obj.year:
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


def depreciation_period_end(period_label):
    """Date de fin de la période d'amortissement : « 2025 » -> 31/12/2025,
    « 2025-03 » -> 31/03/2025, « 2025-T2 » -> 30/06/2025 ; à défaut, aujourd'hui."""
    import calendar
    import re as _re
    period = str(period_label or '').strip()
    m = _re.fullmatch(r'(\d{4})', period)
    if m:
        return f"{period}-12-31"
    m = _re.fullmatch(r'(\d{4})-(\d{2})', period)
    if m and 1 <= int(m.group(2)) <= 12:
        y, mo = int(m.group(1)), int(m.group(2))
        return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    m = _re.fullmatch(r'(\d{4})-[TtQq]([1-4])', period)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        mo = q * 3
        return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    return paris_today().isoformat()


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
    # La dotation d'une année est rattachée à la fin de cette période (31/12), pas au
    # jour où l'utilisateur clique : passée en janvier N+1, elle tombait sur le mauvais
    # exercice.
    entry_date = depreciation_period_end(period_label)
    entry_id = create_entry(
        conn, 'OD', entry_date,
        f"Dotation aux amortissements — {asset['label']} ({period_label})",
        [
            {'account_code': asset['expense_account'], 'debit': amount},
            {'account_code': asset['depreciation_account'], 'credit': amount},
        ],
        source_type=f"fixed_asset_depreciation:{period_label}", source_id=asset['id'],
        entity_id=asset['entity_id'] if 'entity_id' in asset.keys() else None,
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


def reverse_cutoff_entry(conn, cutoff_id, reversal_date, created_by=None, entity_id=None):
    """Génère la contre-passation (extourne) d'un cut-off existant : mêmes
    comptes et montant, débit et crédit inversés, datée du jour choisi
    (typiquement le tout début de la période suivante). Refuse si ce
    cut-off a déjà été extourné — jamais une double contre-passation."""
    ef = 'entity_id=?' if entity_id else 'entity_id IS NULL'
    ep = (cutoff_id, entity_id) if entity_id else (cutoff_id,)
    cutoff = conn.execute(f'SELECT * FROM cutoff_entries WHERE id=? AND {ef}', ep).fetchone()
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
    conn.execute(
        f'UPDATE cutoff_entries SET reversal_entry_id=? WHERE id=? AND {ef}',
        (reversal_id, cutoff_id, entity_id) if entity_id else (reversal_id, cutoff_id),
    )
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
    # Point de départ des soldes : les à-nouveaux (journal AN) du dernier exercice
    # clôturé s'ils existent — ils reprennent déjà les soldes antérieurs et le résultat.
    opening = last_opening_date(conn, as_of_date, entity_id)
    since_clause = ' AND e.entry_date>=?' if opening else ''
    since_params = (opening,) if opening else ()
    rows = conn.execute(
        f"""SELECT l.account_code, a.account_class, a.label,
               COALESCE(SUM(l.debit),0) total_debit, COALESCE(SUM(l.credit),0) total_credit
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            JOIN accounting_chart_of_accounts a ON a.code=l.account_code
            WHERE e.entry_date<=? AND a.account_class IN (1,2,4,5) AND {ef}{since_clause}
            GROUP BY l.account_code, a.account_class, a.label
            ORDER BY l.account_code""",
        (str(as_of_date), *ep, *since_params),
    ).fetchall()
    # Résultats des exercices précédents non encore repris en à-nouveaux : sans eux, le
    # bilan était déséquilibré dès la deuxième année (les soldes de trésorerie et de tiers
    # cumulent tout l'historique, le résultat seulement l'exercice en cours).
    prior_start = opening or '0000-01-01'
    try:
        prior_end = (date.fromisoformat(str(exercice_start_date)[:10]) - timedelta(days=1)).isoformat()
    except ValueError:
        prior_end = None
    resultats_anterieurs = 0.0
    if prior_end and prior_end >= prior_start:
        resultats_anterieurs = compute_compte_resultat(conn, prior_start, prior_end, entity_id)['resultat']

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
            if code.startswith('16'):
                emprunts += -net
            elif code.startswith('455'):
                # Compte courant d'associé resté en classe 1 sur un plan personnalisé :
                # c'est une dette, jamais des capitaux propres.
                dettes += -net
                if net:
                    detail_passif_tiers.append({'code': code, 'label': label, 'amount': -net})
            else:
                capitaux_propres_hors_resultat += -net

    capitaux_propres_hors_resultat += resultats_anterieurs
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
        'resultats_anterieurs': round(resultats_anterieurs, 2),
        'emprunts': round(emprunts, 2), 'dettes': round(dettes, 2), 'total': total_passif,
        'detail_tiers': detail_passif_tiers,
    }
    return actif, passif, resultat_data


def last_opening_date(conn, as_of_date, entity_id=None):
    """Date des derniers à-nouveaux (journal AN) au plus tard à as_of_date, ou None."""
    ef = 'entity_id=?' if entity_id else 'entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    row = conn.execute(
        f"SELECT MAX(entry_date) d FROM accounting_entries WHERE journal_code='AN' AND source_type='opening_balance' AND entry_date<=? AND {ef}",
        (str(as_of_date), *ep)).fetchone()
    return row['d'] if row and row['d'] else None


def generate_opening_entries(conn, exercice_start, exercice_end, entity_id=None, created_by=None):
    """Clôture d'exercice : génère les à-nouveaux (journal AN) datés du lendemain de la
    clôture.

    - chaque compte de bilan (classes 1 à 5) est repris pour son solde, tiers par tiers
      pour les comptes collectifs (le lettrage reste possible) ;
    - le résultat de l'exercice (produits - charges de exercice_start à exercice_end)
      est porté en 120000 (bénéfice, crédit) ou 129000 (perte, débit), en attente
      d'affectation par l'assemblée ;
    - les résultats d'exercices antérieurs jamais repris sont portés en report à
      nouveau (110000 créditeur / 119000 débiteur).
    L'écriture est équilibrée par construction (balance générale équilibrée).
    Idempotent : une seule ouverture par date et par entité."""
    start = date.fromisoformat(str(exercice_start)[:10])
    end = date.fromisoformat(str(exercice_end)[:10])
    if start > end:
        raise AccountingError("La date de début d'exercice doit précéder la date de clôture.")
    opening_day = end + timedelta(days=1)
    source_id = int(opening_day.strftime('%Y%m%d'))
    if _entry_already_exists(conn, 'opening_balance', source_id, entity_id):
        raise AccountingError(f"Les à-nouveaux au {opening_day.strftime('%d/%m/%Y')} ont déjà été générés.")
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    previous = last_opening_date(conn, end.isoformat(), entity_id)
    since_clause = ' AND e.entry_date>=?' if previous else ''
    since_params = (previous,) if previous else ()
    balances = conn.execute(
        f"""SELECT l.account_code, COALESCE(l.auxiliary_name,'') aux, a.account_class,
               COALESCE(SUM(l.debit),0) d, COALESCE(SUM(l.credit),0) c
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            JOIN accounting_chart_of_accounts a ON a.code=l.account_code
            WHERE e.entry_date<=? AND a.account_class IN (1,2,3,4,5) AND {ef}{since_clause}
            GROUP BY l.account_code, COALESCE(l.auxiliary_name,''), a.account_class
            ORDER BY l.account_code""",
        (end.isoformat(), *ep, *since_params)).fetchall()
    lines = []
    for b in balances:
        net = round(float(b['d']) - float(b['c']), 2)
        if not net:
            continue
        ln = {'account_code': b['account_code'], 'label': 'À-nouveau'}
        if b['aux']:
            ln['auxiliary_name'] = b['aux']
        if net > 0:
            ln['debit'] = net
        else:
            ln['credit'] = -net
        lines.append(ln)
    if previous and previous > start.isoformat():
        # Les charges et produits antérieurs aux derniers à-nouveaux sont déjà repris.
        start = date.fromisoformat(previous)
    resultat = compute_compte_resultat(conn, start.isoformat(), end.isoformat(), entity_id)['resultat']
    prior = 0.0
    prior_from = previous or '0000-01-01'
    prior_to = (start - timedelta(days=1)).isoformat()
    if prior_to >= prior_from:
        prior = compute_compte_resultat(conn, prior_from, prior_to, entity_id)['resultat']
    if round(resultat, 2):
        if resultat > 0:
            lines.append({'account_code': '120000', 'credit': round(resultat, 2), 'label': "Résultat de l'exercice (bénéfice)"})
        else:
            lines.append({'account_code': '129000', 'debit': round(-resultat, 2), 'label': "Résultat de l'exercice (perte)"})
    if round(prior, 2):
        if prior > 0:
            lines.append({'account_code': '110000', 'credit': round(prior, 2), 'label': 'Report à nouveau'})
        else:
            lines.append({'account_code': '119000', 'debit': round(-prior, 2), 'label': 'Report à nouveau'})
    if len(lines) < 2:
        raise AccountingError("Aucun solde à reprendre en à-nouveaux pour cet exercice.")
    return create_entry(conn, 'AN', opening_day,
                        f"À-nouveaux au {opening_day.strftime('%d/%m/%Y')} (clôture du {end.strftime('%d/%m/%Y')})",
                        lines, source_type='opening_balance', source_id=source_id,
                        created_by=created_by, entity_id=entity_id)


# --- TVA collectée par taux (préparation CA3) ---------------------------------------------
# Lignes de la déclaration 3310-CA3 (opérations en France métropolitaine) par taux.
CA3_LINES = {
    20.0: ('08', 'Taux normal 20 %'),
    10.0: ('9B', 'Taux réduit 10 %'),
    5.5: ('09', 'Taux réduit 5,5 %'),
    2.1: ('2,1 %', 'Taux particulier 2,1 % (France métropolitaine)'),
}


def ca3_collected_by_rate(conn, date_from, date_to, entity_id=None):
    """TVA collectée exigible de la période, ventilée par taux à partir des mouvements
    du compte 445710 (TVA sur encaissements comprise : elle n'y arrive qu'au règlement).

    Retourne une liste triée {'line','label','rate','base','vat'} ; les lignes de TVA
    historiques sans taux mémorisé sont regroupées en « non ventilé » (rate=None)."""
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    rows = conn.execute(
        f"""SELECT l.vat_rate AS rate,
                   COALESCE(SUM(l.credit-l.debit),0) AS vat,
                   COALESCE(SUM(CASE WHEN l.credit>0 THEN COALESCE(l.vat_base,0) ELSE -COALESCE(l.vat_base,0) END),0) AS base
            FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code=? AND e.entry_date BETWEEN ? AND ? AND {ef}
              AND COALESCE(e.source_type,'')<>'vat_liquidation'
            GROUP BY l.vat_rate""",
        (VAT_COLLECTED_ACCOUNT, str(date_from), str(date_to), *ep)).fetchall()
    out = []
    for r in rows:
        vat = round(float(r['vat'] or 0), 2)
        if not vat:
            continue
        rate = None if r['rate'] is None else amounts.rate_key(r['rate'])
        line, label = CA3_LINES.get(rate, ('—', f"Autre taux ({amounts.fr_rate(rate)})" if rate is not None
                                           else 'TVA non ventilée (écritures antérieures au suivi par taux)'))
        out.append({'line': line, 'label': label, 'rate': rate, 'base': round(float(r['base'] or 0), 2), 'vat': vat})
    out.sort(key=lambda x: (x['rate'] is None, -(x['rate'] or 0)))
    return out


def pending_vat_balance(conn, as_of, entity_id=None):
    """Solde de TVA collectée en attente d'encaissement (445740) à une date."""
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.credit-l.debit),0) n FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code=? AND e.entry_date<=? AND {ef}""",
        (VAT_PENDING_ACCOUNT, str(as_of), *ep)).fetchone()
    return round(float(row['n'] or 0), 2)


# --- Cession d'immobilisation --------------------------------------------------------------
def dispose_fixed_asset(conn, asset, disposal_date, price_ht=0.0, vat_rate=0.0, entity_id=None,
                        created_by=None, complementary=True):
    """Sortie d'une immobilisation cédée ou mise au rebut :
    - dotation complémentaire de l'année de cession, au prorata jusqu'à la date de sortie
      (si l'annuité de l'année n'a pas déjà été passée) ;
    - écriture de sortie : amortissements cumulés (28) et valeur nette comptable (675) au
      débit, valeur d'origine (2x) au crédit ;
    - prix de cession : créance sur cession (462) au débit, produit de cession (775) et TVA
      collectée au crédit.
    Renvoie {'entry_id', 'vnc', 'gain', 'complementary'}. Une seule sortie par actif."""
    if asset['status'] == 'disposed':
        raise AccountingError("Cette immobilisation est déjà cédée.")
    day = date.fromisoformat(str(disposal_date)[:10])
    purchase = date.fromisoformat(str(asset['purchase_date'])[:10])
    if day < purchase:
        raise AccountingError("La date de cession précède la date d'acquisition.")
    price_ht = round(float(price_ht or 0), 2)
    vat_rate = float(vat_rate or 0)
    if price_ht < 0 or vat_rate not in (0.0, 5.5, 10.0, 20.0):
        raise AccountingError("Prix ou taux de TVA de cession invalide.")
    comp = 0.0
    label_year = str(day.year)
    already = conn.execute('SELECT 1 FROM fixed_asset_depreciation_runs WHERE asset_id=? AND period_label=?',
                           (asset['id'], label_year)).fetchone()
    # Dotations mensuelles ou trimestrielles déjà passées dans l'année (« 2026-03 », « 2026-T1 »).
    posted_year = float(conn.execute(
        'SELECT COALESCE(SUM(amount),0) t FROM fixed_asset_depreciation_runs WHERE asset_id=? AND period_label LIKE ?',
        (asset['id'], label_year + '-%')).fetchone()['t'])
    if complementary and not already:
        full = compute_depreciation_for_year(conn, asset, day.year) + posted_year
        start = max(date(day.year, 1, 1), purchase)
        span = (date(day.year, 12, 31) - start).days + 1
        prorata = full * ((day - start).days + 1) / span if span > 0 else 0.0
        comp = round(max(0.0, prorata - posted_year), 2)
        if comp > 0:
            entry = create_entry(conn, 'OD', day, f"Dotation complémentaire avant cession — {asset['label']}",
                                 [{'account_code': asset['expense_account'], 'debit': comp},
                                  {'account_code': asset['depreciation_account'], 'credit': comp}],
                                 source_type='fixed_asset_depreciation', source_id=asset['id'],
                                 entity_id=entity_id, created_by=created_by, commit=False)
            conn.execute('INSERT INTO fixed_asset_depreciation_runs(asset_id,period_label,amount,entry_id,created_at) VALUES(?,?,?,?,?)',
                         (asset['id'], label_year, comp, entry, datetime.now(timezone.utc).isoformat()))
    accumulated = round(float(conn.execute('SELECT COALESCE(SUM(amount),0) t FROM fixed_asset_depreciation_runs WHERE asset_id=?',
                                           (asset['id'],)).fetchone()['t']), 2)
    gross = round(float(asset['purchase_amount']), 2)
    accumulated = min(accumulated, gross)
    vnc = round(gross - accumulated, 2)
    lines = []
    if accumulated:
        lines.append({'account_code': asset['depreciation_account'], 'debit': accumulated, 'label': 'Amortissements cumulés'})
    if vnc:
        lines.append({'account_code': '675000', 'debit': vnc, 'label': 'Valeur nette comptable'})
    lines.append({'account_code': asset['asset_account'], 'credit': gross, 'label': "Valeur d'origine"})
    vat = round(price_ht * vat_rate / 100, 2)
    if price_ht:
        lines.append({'account_code': '462000', 'debit': round(price_ht + vat, 2), 'label': 'Prix de cession'})
        lines.append({'account_code': '775000', 'credit': price_ht, 'label': 'Produit de cession'})
        if vat:
            lines.append({'account_code': VAT_COLLECTED_ACCOUNT, 'credit': vat, 'label': 'TVA sur cession',
                          'vat_rate': vat_rate, 'vat_base': price_ht})
    entry_id = create_entry(conn, 'OD', day, f"Cession — {asset['label']}", lines,
                            source_type='fixed_asset_disposal', source_id=asset['id'],
                            entity_id=entity_id, created_by=created_by, commit=False)
    conn.execute("""UPDATE fixed_assets SET status='disposed',disposed_at=?,disposal_date=?,disposal_price=?,disposal_vat=?,
                    disposal_entry_id=? WHERE id=?""",
                 (datetime.now(timezone.utc).isoformat(), day.isoformat(), price_ht, vat, entry_id, asset['id']))
    conn.commit()
    labels = [str(r['period_label']) for r in conn.execute(
        'SELECT period_label FROM fixed_asset_depreciation_runs WHERE asset_id=?', (asset['id'],)).fetchall()]
    missing = [y for y in range(purchase.year, day.year) if not any(l[:4] == str(y) for l in labels)]
    return {'entry_id': entry_id, 'vnc': vnc, 'gain': round(price_ht - vnc, 2), 'complementary': comp,
            'accumulated': accumulated, 'missing_years': missing}
