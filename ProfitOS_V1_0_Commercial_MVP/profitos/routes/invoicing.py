from datetime import timedelta
import uuid
import statistics
import xml.etree.ElementTree as ET
import base64
import math
import hashlib
from pypdf.errors import PyPdfError
from profitos.document_extraction import PdfTextUnavailable, is_pdf_text_unavailable
from profitos.runtime import *
from profitos import db as dbmod
from profitos import invoice_amounts as amounts
from profitos.accounting import paris_today
from profitos.plan_usage import quota_state, record_usage
from profitos.feature_access import requires_paid_plan
from profitos.entities import current_entity_id
from profitos.accounting import (generate_sale_entry, generate_sale_payment_entry, generate_sale_partial_payment_entry,
    generate_sale_credit_entry, generate_customer_deposit_entry, generate_customer_final_entry, generate_purchase_entry, generate_purchase_credit_entry,
    generate_purchase_payment_entry, generate_purchase_partial_payment_entry, reverse_purchase_entry, AccountingError)
from profitos.webhooks_outbound import deliver_webhook
from profitos.weinvoice import (submit_invoice_file, get_invoice_timeline,
    invoice_status_from_timeline, sandbox_force_invoice_status,
    WeInvoiceAPIError, WeInvoiceConfigError, list_inbound_invoices,
    get_inbound_invoice_content, download_inbound_invoice_readable, download_inbound_invoice_original, apply_inbound_lifecycle_action)


# --- Facturation électronique : statuts qui retiennent un paiement ou une relance --------------------
# Le cycle de vie WeInvoice est volontairement séparé de l'état de paiement ProfitOS. Conséquence : une
# facture refusée, contestée ou suspendue restait « à payer » (achats) ou « à encaisser » (ventes). Ces
# helpers rendent la situation visible et bloquent les deux actions risquées : payer par virement SEPA
# une facture d'achat contestée, et relancer un client qui a refusé la facture.
_EINVOICE_HOLD_BY_STATUS = {
    'REFUSED': 'refusée', 'IN_DISPUTE': 'en litige', 'DISPUTED': 'en litige',
    'SUSPENDED': 'suspendue', 'REJECTED': 'rejetée',
}
_EINVOICE_HOLD_BY_CODE = {'210': 'refusée', '207': 'en litige', '208': 'suspendue', '213': 'rejetée'}


def _einvoice_hold(row):
    """Libellé (« refusée », « en litige », « suspendue » ou « rejetée ») si le statut de facturation
    électronique de la facture interdit de la payer ou de la relancer, sinon None. Le statut textuel
    prime ; le code réglementaire ne sert que lorsque le statut est absent."""
    keys = row.keys() if hasattr(row, 'keys') else []
    status = str((row['weinvoice_status'] if 'weinvoice_status' in keys else '') or '').strip().upper()
    if status:
        return _EINVOICE_HOLD_BY_STATUS.get(status)
    code = str((row['weinvoice_regulatory_code'] if 'weinvoice_regulatory_code' in keys else '') or '').strip()
    return _EINVOICE_HOLD_BY_CODE.get(code)


def _compute_line_items(form):
    """Lit jusqu'à 8 lignes de facture depuis le formulaire (libellé, nature, quantité,
    prix unitaire, taux de TVA). Les lignes vides sont ignorées — pas de JS dynamique
    nécessaire, conforme à la politique de sécurité stricte de l'app (pas de script
    inline). Saisie française acceptée (« 12,345 », « 1 000 »)."""
    items=[]
    for i in range(1,9):
        label=form.get(f'label_{i}','').strip()
        if not label: continue
        qty=amounts.parse_number(form.get(f'qty_{i}','1'),1.0)
        unit_price=amounts.parse_number(form.get(f'price_{i}','0'),0.0)
        vat_rate=amounts.parse_number(form.get(f'vat_{i}','20'),20.0)
        # Quantité et prix unitaire conservés à 4 décimales au plus ; total de ligne
        # arrondi au centime au demi supérieur (3 × 12,345 € = 37,04 €, jamais 37,035).
        qty=amounts.quantize(qty,4)
        unit_price=amounts.quantize(unit_price,4)
        line_total=amounts.line_total(qty,unit_price)
        item={'label':label,'qty':qty,'unit_price':unit_price,'vat_rate':amounts.rate_key(vat_rate),'line_total':line_total}
        nature=(form.get(f'nature_{i}') or '').strip()
        if nature in ('biens','services'):
            item['nature']=nature
        items.append(item)
    return items


# Taux de TVA applicables en France (métropole, Corse, DROM). Un taux hors liste est
# presque toujours une faute de frappe (ex. 200 au lieu de 20).
ALLOWED_VAT_RATES=(0.0,0.9,1.05,1.75,2.1,5.5,8.5,10.0,13.0,20.0)


def _line_items_error(items):
    """Message d'erreur si les lignes saisies ne peuvent pas produire un document valide,
    sinon None. Une ligne de remise négative reste possible, mais le total HT doit rester
    strictement positif (une facture négative se fait par avoir)."""
    for it in items:
        if it['qty']<=0:
            return f"Quantité invalide sur la ligne « {it['label']} » : elle doit être supérieure à 0."
        if not any(abs(it['vat_rate']-r)<1e-9 for r in ALLOWED_VAT_RATES):
            rates=', '.join(f"{r:g}".replace('.',',') for r in ALLOWED_VAT_RATES)
            return f"Taux de TVA invalide sur la ligne « {it['label']} » ({amounts.fr_rate(it['vat_rate'])}). Taux acceptés : {rates} %."
    if round(sum(it['line_total'] for it in items),2)<=0:
        return "Le total HT doit être positif. Pour annuler ou rembourser une facture, émettez un avoir."
    return None


def _totals(items):
    """Totaux arrondis au centime, calculés comme la facture électronique : TVA calculée
    une fois par taux sur la base HT de ce taux, puis additionnée (BR-CO-14/17). Une TVA
    calculée ligne à ligne donnait 1,56 € pour 0,55 € + 1,00 € par taux : la facture
    Factur-X était rejetée et l'écriture ne correspondait pas au PDF."""
    return amounts.totals(items)


def _g(row, key, default=None):
    """Lecture tolérante d'un champ (sqlite Row, Row ProfitOS ou dict)."""
    if row is None:
        return default
    try:
        keys = row.keys()
    except AttributeError:
        return default
    value = row[key] if key in keys else default
    return default if value is None else value


from profitos.company_profile import (LEGAL_FORMS, LEGAL_FORMS_WITH_CAPITAL, LEGAL_FORMS_WITHOUT_REGISTRY,
    DEFAULT_LATE_PENALTY, DEFAULT_ESCOMPTE)

from profitos.invoice_compliance import (VAT_TREATMENTS, VAT_TREATMENT_LABELS, OPERATION_NATURE_LABELS,
    vat_category as _vat_category, vat_exemption as _vat_exemption, legal_mentions as _legal_mentions,
    seller_legal_footer as _seller_legal_footer)


INVOICE_HEADER_COLUMNS = ('client_name','client_address','client_email','client_siren','client_vat_number',
    'counterparty_country','client_is_public','due_date','service_date','operation_nature','vat_treatment',
    'vat_exemption_reason','delivery_address','vat_on_debits','ereporting_scope','ereporting_category')


def _parse_invoice_header(form, issue_date=None):
    """En-tête d'une facture (création ou modification d'un brouillon) : retourne
    (valeurs, message d'erreur ou None). Contrôle SIREN (clé de Luhn), n° de TVA client,
    pays, dates et périmètre e-reporting."""
    v={}
    v['client_name']=(form.get('client_name') or '').strip()
    v['client_address']=(form.get('client_address') or '').strip().replace('\r\n','\n')
    v['client_email']=(form.get('client_email') or '').strip()
    v['client_siren']=re.sub(r'\D','',form.get('client_siren') or '')[:9] or None
    v['client_vat_number']=re.sub(r'[\s.\-]','',form.get('client_vat_number') or '').upper() or None
    v['counterparty_country']=(form.get('counterparty_country') or '').strip().upper() or None
    v['client_is_public']='1' if form.get('client_is_public') in ('on','1') else None
    v['due_date']=(form.get('due_date') or '').strip() or None
    v['service_date']=(form.get('service_date') or '').strip() or None
    nature=form.get('operation_nature','services')
    v['operation_nature']=nature if nature in ('biens','services','mixte') else 'services'
    treatment=form.get('vat_treatment') or 'standard'
    v['vat_treatment']=treatment if treatment in VAT_TREATMENTS and treatment!='franchise' else 'standard'
    v['vat_exemption_reason']=(form.get('vat_exemption_reason') or '').strip()[:200] or None
    v['delivery_address']=(form.get('delivery_address') or '').strip().replace('\r\n','\n') or None
    v['vat_on_debits']=1 if form.get('vat_on_debits') in ('on','1') else 0
    v['ereporting_scope']=(form.get('ereporting_scope') or '').strip() or None
    v['ereporting_category']=(form.get('ereporting_category') or '').strip() or None
    if v['client_siren'] and not siren_is_valid(v['client_siren']):
        return v,"SIREN client invalide : 9 chiffres avec une clé de contrôle correcte (laissez vide si inconnu)."
    if v['client_vat_number'] and not fr_vat_is_valid(v['client_vat_number'], v['client_siren'] if (v['client_vat_number'] or '').startswith('FR') else None):
        return v,"N° de TVA intracommunautaire du client invalide (vérifiez le préfixe pays et la clé)."
    if v['counterparty_country'] and not re.fullmatch(r'[A-Z]{2}',v['counterparty_country']):
        return v,"Pays du client : code ISO à 2 lettres (FR, DE, BE…)."
    if v['ereporting_scope'] not in (None,'b2c','international_b2b'):
        return v,"Périmètre e-reporting invalide."
    if v['ereporting_scope']=='b2c' and v['ereporting_category'] not in ('TLB1','TPS1','TNT1','TMA1'):
        return v,"Catégorie e-reporting B2C requise."
    if v['ereporting_scope']=='international_b2b' and (not v['counterparty_country'] or v['counterparty_country']=='FR'):
        return v,"Pays étranger (code ISO à 2 lettres) requis pour une vente B2B internationale."
    for key,label in (('due_date',"Date d'échéance"),('service_date',"Date de la vente ou de la prestation")):
        if v[key]:
            try: date.fromisoformat(v[key])
            except ValueError: return v,f"{label} invalide."
    if v['due_date'] and issue_date and v['due_date'] < str(issue_date)[:10]:
        return v,"La date d'échéance ne peut pas être antérieure à la date d'émission."
    return v,None


def _insert_draft_invoice(c, values):
    """Insère un brouillon de facture (sans numéro : attribué à l'émission). values :
    colonnes de outgoing_invoices. Retourne (id, jeton public)."""
    data=dict(values)
    data.setdefault('status','draft')
    data.setdefault('created_at',now())
    data.setdefault('issue_date',paris_today().isoformat())
    data.setdefault('public_token',secrets.token_urlsafe(20))
    data['invoice_number']=None
    cols=list(data)
    c.execute(f"INSERT INTO outgoing_invoices({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",[data[k] for k in cols])
    new_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    return new_id,data['public_token']


def _quote_invoice_base(q, eid):
    """Champs de facture repris d'un devis : client, SIREN, n° de TVA, pays, nature de
    l'opération et adresse de livraison."""
    return {'client_name':q['client_name'],'client_address':q['client_address'],'client_email':q['client_email'],
            'client_siren':_g(q,'client_siren'),'client_vat_number':_g(q,'client_vat_number'),
            'counterparty_country':_g(q,'counterparty_country'),'operation_nature':_g(q,'operation_nature','services') or 'services',
            'delivery_address':_g(q,'delivery_address'),'entity_id':eid,
            'due_date':(paris_today()+timedelta(days=30)).isoformat(),'source_quote_id':q['id']}


def _seller_identity(c, entity_id):
    """Identité complète de l'émetteur d'un document : la société mère ou la filiale
    à laquelle appartient la facture (jamais la société mère par défaut), avec ses
    mentions légales (forme, capital, RCS, régime de TVA, conditions de paiement)."""
    from profitos.entities import resolve_entity
    ident = dict(resolve_entity(c, entity_id))
    table, key = ('company', 1) if not entity_id else ('entities', entity_id)
    try:
        row = c.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    except Exception:
        row = None
    for field in ('city', 'legal_form', 'share_capital', 'rcs_city', 'rm_number', 'vat_regime',
                  'vat_on_debits', 'late_penalty_rate', 'escompte_text', 'payment_terms', 'country'):
        ident[field] = str(_g(row, field, '') or '')
    ident['country'] = (ident['country'] or 'FR').upper()
    return ident


def _seller_vat_franchise(company):
    return (_g(company, 'vat_regime', '') or '') == 'franchise_293B'


def _invoice_vat_treatment(inv, company=None):
    if company is not None and _seller_vat_franchise(company):
        return 'franchise'
    t = (_g(inv, 'vat_treatment', '') or 'standard')
    return t if t in VAT_TREATMENTS else 'standard'


def _client_country(inv):
    country = (_g(inv, 'counterparty_country', '') or '').strip().upper()
    return country if len(country) == 2 else 'FR'


def _is_french_b2b(inv):
    return _client_country(inv) == 'FR' and (_g(inv, 'ereporting_scope', '') or '') != 'b2c'


def _mention(checks, label, ok, scope):
    checks.append({'label': label, 'ok': bool(ok), 'scope': scope})


def _check_mandatory_mentions(inv,company):
    """Mentions obligatoires d'une facture française (CGI art. 242 nonies A, C. com.
    art. L441-9 et R123-237, réforme de la facturation électronique 2026).

    Chaque contrôle porte un « scope » : 'seller' (profil entreprise), 'buyer' (fiche
    client / facture) ou 'invoice'. Le numéro et la date d'émission sont attribués
    automatiquement à l'émission : ils ne bloquent jamais un brouillon. Cette liste est
    BLOQUANTE à l'émission, à l'envoi et à la transmission électronique."""
    checks=[]
    is_draft = (_g(inv, 'status', 'draft') == 'draft')
    legal_form = _g(company, 'legal_form', '')
    _mention(checks, "Nom (dénomination) de l'émetteur", _g(company, 'name'), 'seller')
    _mention(checks, "Adresse de l'émetteur", _g(company, 'address'), 'seller')
    postal = re.sub(r'\s', '', str(_g(company, 'postal_code', '') or ''))
    seller_country = (_g(company, 'country', '') or 'FR').upper()
    postal_ok = bool(re.fullmatch(r'\d{5}', postal)) if seller_country == 'FR' else bool(postal)
    _mention(checks, "Code postal et ville de l'émetteur", postal_ok and _g(company, 'city'), 'seller')
    siret=re.sub(r'\D','',str(_g(company, 'siret', '') or ''))
    _mention(checks, "SIRET de l'émetteur (14 chiffres)", re.fullmatch(r'\d{14}', siret) and siret_is_valid(siret), 'seller')
    _mention(checks, "Forme juridique de l'émetteur", legal_form, 'seller')
    if legal_form in LEGAL_FORMS_WITH_CAPITAL:
        _mention(checks, "Capital social de l'émetteur", _g(company, 'share_capital'), 'seller')
    if legal_form not in LEGAL_FORMS_WITHOUT_REGISTRY:
        _mention(checks, "Immatriculation (RCS + ville, ou répertoire des métiers)",
                 _g(company, 'rcs_city') or _g(company, 'rm_number'), 'seller')
    if not _seller_vat_franchise(company):
        _mention(checks, "N° TVA intracommunautaire de l'émetteur", _g(company, 'vat_number'), 'seller')
    _mention(checks, 'Nom du client', _g(inv, 'client_name'), 'buyer')
    _mention(checks, 'Adresse du client', _g(inv, 'client_address'), 'buyer')
    if _is_french_b2b(inv):
        siren=str(_g(inv, 'client_siren', '') or '')
        _mention(checks, 'SIREN du client (9 chiffres)', re.fullmatch(r'\d{9}', siren) and siren_is_valid(siren), 'buyer')
    treatment = _invoice_vat_treatment(inv, company)
    if treatment in ('intra_eu_goods', 'intra_eu_services', 'reverse_charge_fr'):
        _mention(checks, "N° TVA intracommunautaire du client (autoliquidation / opération intracommunautaire)",
                 _g(inv, 'client_vat_number'), 'buyer')
    if treatment == 'exempt':
        _mention(checks, "Motif d'exonération de TVA (article du CGI)", _g(inv, 'vat_exemption_reason'), 'invoice')
    _mention(checks, 'Numéro de facture unique' + (' (attribué à l’émission)' if is_draft else ''),
             is_draft or _g(inv, 'invoice_number'), 'invoice')
    _mention(checks, "Date d'émission" + (' (date du jour à l’émission)' if is_draft else ''),
             is_draft or _g(inv, 'issue_date'), 'invoice')
    _mention(checks, "Date d'échéance / conditions de règlement", _g(inv, 'due_date'), 'invoice')
    op_nature = _g(inv, 'operation_nature')
    _mention(checks, "Nature de l'opération (biens / services / mixte)", op_nature in ('biens', 'services', 'mixte'), 'invoice')
    try:
        items=json.loads(_g(inv, 'line_items') or '[]')
    except (json.JSONDecodeError,TypeError):
        items=[]
    _mention(checks, 'Au moins une ligne avec désignation, quantité, prix unitaire et taux de TVA', items, 'invoice')
    if items:
        if treatment == 'standard':
            _mention(checks, 'Taux de TVA renseigné sur chaque ligne (0 % : choisir un régime d’exonération)',
                     all(float(it.get('vat_rate') or 0) > 0 for it in items), 'invoice')
        else:
            _mention(checks, 'Taux de TVA à 0 % sur chaque ligne (opération sans TVA française)',
                     all(float(it.get('vat_rate') or 0) == 0 for it in items), 'invoice')
    _mention(checks, 'Montants HT et TTC calculés', _g(inv, 'subtotal') is not None and _g(inv, 'total') is not None, 'invoice')
    missing=[c['label'] for c in checks if not c['ok']]
    return checks,missing


from profitos.fr_identifiers import (siren_is_valid, siret_is_valid, fr_vat_is_valid, iban_is_valid,
    fr_postal_code_is_valid, split_address as _split_address)


_CII_NS = {
    'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
    'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
    'qdt': 'urn:un:unece:uncefact:data:standard:QualifiedDataType:100',
    'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
}


def _cii_date(iso_date):
    """Convertit une date ISO (AAAA-MM-JJ) au format udt:DateTimeString qualifié
    102 (AAAAMMJJ) exigé par le schéma CII."""
    try:
        return datetime.strptime(iso_date, '%Y-%m-%d').strftime('%Y%m%d')
    except (ValueError, TypeError):
        return None


def _cii_amount(el_parent, tag, value, ns, currency=None):
    el = ET.SubElement(el_parent, f'{{{ns["ram"]}}}{tag}')
    if currency:
        el.set('currencyID', currency)
    el.text = f'{value:.2f}'
    return el


def _cii_price(value):
    """Prix unitaire : 2 décimales, jusqu'à 4 si nécessaires (12.345)."""
    text = f"{amounts.quantize(value, 4):.4f}"
    return text[:-2] if text.endswith('00') else text


def _cii_qty(value):
    return amounts.fr_qty(value).replace(' ','').replace(',','.') or '0'


def _cii_el(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, f'{{{_CII_NS["ram"]}}}{tag}')
    for k, v in attrs.items():
        el.set(k, v)
    if text is not None:
        el.text = str(text)
    return el


def _cii_datetime(parent, tag, iso_date, qualified=False):
    holder = _cii_el(parent, tag)
    ns = _CII_NS['qdt'] if qualified else _CII_NS['udt']
    d = ET.SubElement(holder, f'{{{ns}}}DateTimeString')
    d.set('format', '102')
    d.text = _cii_date(str(iso_date)[:10]) or ''
    return holder


def _cii_address(parent, address_text, country, postal_code=None, city=None):
    """PostalTradeAddress dans l'ordre du schéma CII : PostcodeCode, LineOne, LineTwo,
    LineThree, CityName, CountryID."""
    parts = _split_address(address_text)
    postcode = postal_code or parts['postcode']
    city_name = city or parts['city']
    street = [l for l in parts['street'] if l and l != city_name]
    addr = _cii_el(parent, 'PostalTradeAddress')
    if postcode:
        _cii_el(addr, 'PostcodeCode', postcode)
    for tag, line in zip(('LineOne', 'LineTwo', 'LineThree'), street[:3]):
        _cii_el(addr, tag, line)
    if city_name:
        _cii_el(addr, 'CityName', city_name)
    _cii_el(addr, 'CountryID', (country or 'FR').upper())
    return addr


def _facturx_notes(inv, company):
    """Notes réglementaires françaises (BR-FR-05) : PMT (indemnité forfaitaire de
    recouvrement), PMD (pénalités de retard), AAB (escompte)."""
    from profitos.company_profile import DEFAULT_LATE_PENALTY, DEFAULT_ESCOMPTE
    penalty = (_g(company, 'late_penalty_rate', '') or '').strip() or DEFAULT_LATE_PENALTY
    escompte = (_g(company, 'escompte_text', '') or '').strip() or DEFAULT_ESCOMPTE
    notes = [
        ('PMT', "Indemnité forfaitaire pour frais de recouvrement en cas de retard de paiement : 40 € (art. L441-10 et D441-5 du Code de commerce)."),
        ('PMD', f"Pénalités de retard exigibles dès le lendemain de l'échéance : {penalty}."),
        ('AAB', escompte),
    ]
    if _g(inv, 'notes'):
        notes.append(('AAI', str(_g(inv, 'notes'))[:1000]))
    return notes


def generate_facturx_xml(inv, items, company, type_code='380', referenced=None, prepaid=0.0, payment_terms_text=None):
    """XML Cross Industry Invoice (CII D16B), profil EN 16931, pour Factur-X.

    - montants : mêmes chiffres que la base, le PDF et l'écriture (TVA calculée une fois
      par taux sur la base arrondie, TVA totale = somme des taux, BR-CO-14) ;
    - vendeur = entité émettrice de la facture (filiale ou société mère), avec code
      postal, ville, n° de TVA et mentions de forme/capital (BT-33) ;
    - acheteur : SIREN, pays, n° de TVA (autoliquidation), adresse structurée ;
    - catégories de TVA S, Z, E (motif), AE, K, G ; franchise 293 B = E ;
    - notes PMT / PMD / AAB ; conditions et échéance si un montant reste dû ;
    - avoir (381) : InvoiceReferencedDocument APRÈS la MonetarySummation (ordre D16B).
    Non validé ici contre le XSD/Schematron officiels (non embarqués) : contrôle externe
    recommandé (FNFE-MPE, plateforme agréée)."""
    ns = _CII_NS
    for prefix, uri in ns.items():
        ET.register_namespace(prefix, uri)
    items = [dict(it) for it in (items or [])]
    subtotal, vat_total, grand_total = amounts.totals(items)
    breakdown = amounts.vat_breakdown(items)

    root = ET.Element(f'{{{ns["rsm"]}}}CrossIndustryInvoice')
    ctx = ET.SubElement(root, f'{{{ns["rsm"]}}}ExchangedDocumentContext')
    # BT-23 — cadre de facturation (règle française BR-FR-08, obligatoire) : B/S/M selon
    # la nature de l'opération, suivi de 1 (facture « classique ») ou 4 (facture définitive
    # entièrement couverte par les acomptes versés, BR-FR-CO-09).
    nature_letter = {'biens': 'B', 'services': 'S', 'mixte': 'M'}.get(
        (_g(inv, 'operation_nature', 'services') or 'services'), 'S')
    fully_prepaid = type_code == '380' and float(prepaid or 0) > 0 and round(grand_total - float(prepaid or 0), 2) <= 0
    _cii_el(_cii_el(ctx, 'BusinessProcessSpecifiedDocumentContextParameter'), 'ID',
            nature_letter + ('4' if fully_prepaid else '1'))
    guideline = _cii_el(ctx, 'GuidelineSpecifiedDocumentContextParameter')
    _cii_el(guideline, 'ID', 'urn:cen.eu:en16931:2017')

    doc = ET.SubElement(root, f'{{{ns["rsm"]}}}ExchangedDocument')
    _cii_el(doc, 'ID', _g(inv, 'invoice_number', '') or '')
    _cii_el(doc, 'TypeCode', type_code)
    _cii_datetime(doc, 'IssueDateTime', _g(inv, 'issue_date', ''))
    for subject, content in _facturx_notes(inv, company):
        note = _cii_el(doc, 'IncludedNote')
        _cii_el(note, 'Content', content)
        _cii_el(note, 'SubjectCode', subject)

    txn = ET.SubElement(root, f'{{{ns["rsm"]}}}SupplyChainTradeTransaction')
    for idx, it in enumerate(items, start=1):
        rate = amounts.rate_key(it.get('vat_rate', 0))
        category = _vat_category(inv, company, rate)
        line = _cii_el(txn, 'IncludedSupplyChainTradeLineItem')
        _cii_el(_cii_el(line, 'AssociatedDocumentLineDocument'), 'LineID', str(idx))
        _cii_el(_cii_el(line, 'SpecifiedTradeProduct'), 'Name', it.get('label', '') or '')
        agreement = _cii_el(line, 'SpecifiedLineTradeAgreement')
        price = _cii_el(agreement, 'NetPriceProductTradePrice')
        # BR-27 : prix net jamais négatif — une déduction se porte en quantité négative.
        unit_price = abs(float(it.get('unit_price', 0) or 0))
        _cii_el(price, 'ChargeAmount', _cii_price(unit_price))
        qty = float(it.get('qty', 1) or 0)
        if float(it.get('unit_price', 0) or 0) < 0 and qty > 0:
            qty = -qty
        _cii_el(_cii_el(line, 'SpecifiedLineTradeDelivery'), 'BilledQuantity', _cii_qty(qty), unitCode='C62')
        settlement = _cii_el(line, 'SpecifiedLineTradeSettlement')
        tax = _cii_el(settlement, 'ApplicableTradeTax')
        _cii_el(tax, 'TypeCode', 'VAT')
        _cii_el(tax, 'CategoryCode', category)
        _cii_el(tax, 'RateApplicablePercent', f"{(rate if category in ('S',) else 0):g}")
        summation = _cii_el(settlement, 'SpecifiedTradeSettlementLineMonetarySummation')
        _cii_el(summation, 'LineTotalAmount', f"{amounts.money(it.get('line_total', 0)):.2f}")

    # --- Vendeur / acheteur ----------------------------------------------------------------
    agreement = _cii_el(txn, 'ApplicableHeaderTradeAgreement')
    seller = _cii_el(agreement, 'SellerTradeParty')
    _cii_el(seller, 'Name', _g(company, 'name', '') or '')
    legal_info = _seller_legal_footer(company)
    if _g(company, 'legal_form'):
        _cii_el(seller, 'Description', legal_info)
    seller_siret = re.sub(r'\D', '', str(_g(company, 'siret', '') or ''))
    if len(seller_siret) == 14:
        org = _cii_el(seller, 'SpecifiedLegalOrganization')
        _cii_el(org, 'ID', seller_siret[:9], schemeID='0002')
    _cii_address(seller, _g(company, 'address', ''), _g(company, 'country', 'FR') or 'FR',
                 postal_code=re.sub(r'\s', '', str(_g(company, 'postal_code', '') or '')) or None,
                 city=_g(company, 'city') or None)
    if len(seller_siret) == 14:
        # BT-34 — adresse électronique de routage « bare-SIREN » : schemeID 0225
        # (confirmé par WeInvoice, erreur BR-FR-21).
        _cii_el(_cii_el(seller, 'URIUniversalCommunication'), 'URIID', seller_siret[:9], schemeID='0225')
    if _g(company, 'vat_number'):
        _cii_el(_cii_el(seller, 'SpecifiedTaxRegistration'), 'ID', _g(company, 'vat_number'), schemeID='VA')

    buyer = _cii_el(agreement, 'BuyerTradeParty')
    _cii_el(buyer, 'Name', _g(inv, 'client_name', '') or '')
    client_siren = re.sub(r'\D', '', str(_g(inv, 'client_siren', '') or ''))
    if len(client_siren) == 9:
        org = _cii_el(buyer, 'SpecifiedLegalOrganization')
        _cii_el(org, 'ID', client_siren, schemeID='0002')
    buyer_country = (_g(inv, 'counterparty_country', '') or 'FR').upper()
    _cii_address(buyer, _g(inv, 'client_address', ''), buyer_country)
    if len(client_siren) == 9:
        _cii_el(_cii_el(buyer, 'URIUniversalCommunication'), 'URIID', client_siren, schemeID='0225')
    if _g(inv, 'client_vat_number'):
        _cii_el(_cii_el(buyer, 'SpecifiedTaxRegistration'), 'ID', _g(inv, 'client_vat_number'), schemeID='VA')

    # --- Livraison -------------------------------------------------------------------------
    delivery_hdr = _cii_el(txn, 'ApplicableHeaderTradeDelivery')
    delivery_address = _g(inv, 'delivery_address')
    intra_supply = any(_vat_category(inv, company, b['rate']) == 'K' for b in breakdown)
    if delivery_address:
        ship_to = _cii_el(delivery_hdr, 'ShipToTradeParty')
        _cii_address(ship_to, delivery_address, buyer_country)
    elif intra_supply:
        # BR-IC-12 : livraison intracommunautaire -> pays de livraison (BT-80) obligatoire.
        ship_to = _cii_el(delivery_hdr, 'ShipToTradeParty')
        _cii_el(_cii_el(ship_to, 'PostalTradeAddress'), 'CountryID', buyer_country)
    delivery_date = _g(inv, 'service_date') or (_g(inv, 'issue_date') if intra_supply else None)
    if delivery_date:
        # BR-IC-11 : date de livraison (BT-72) obligatoire pour une livraison intracommunautaire.
        event = _cii_el(delivery_hdr, 'ActualDeliverySupplyChainEvent')
        _cii_datetime(event, 'OccurrenceDateTime', delivery_date)

    # --- Règlement -------------------------------------------------------------------------
    settlement_hdr = _cii_el(txn, 'ApplicableHeaderTradeSettlement')
    _cii_el(settlement_hdr, 'InvoiceCurrencyCode', 'EUR')
    iban = re.sub(r'\s', '', str(_g(company, 'iban', '') or ''))
    due_amount = round(grand_total - float(prepaid or 0), 2)
    if iban and type_code in ('380', '386') and due_amount > 0:
        means = _cii_el(settlement_hdr, 'SpecifiedTradeSettlementPaymentMeans')
        _cii_el(means, 'TypeCode', '58')  # virement SEPA
        _cii_el(_cii_el(means, 'PayeePartyCreditorFinancialAccount'), 'IBANID', iban)
    exemption_reason, exemption_code = _vat_exemption(inv, company)
    on_collection = (_g(inv, 'operation_nature', 'services') or 'services') == 'services' and not int(_g(inv, 'vat_on_debits', 0) or 0)
    for b in breakdown:
        category = _vat_category(inv, company, b['rate'])
        tax = _cii_el(settlement_hdr, 'ApplicableTradeTax')
        _cii_el(tax, 'CalculatedAmount', f"{b['vat']:.2f}")
        _cii_el(tax, 'TypeCode', 'VAT')
        if category in ('E', 'AE', 'K', 'G') and exemption_reason:
            _cii_el(tax, 'ExemptionReason', exemption_reason)
        _cii_el(tax, 'BasisAmount', f"{b['base']:.2f}")
        _cii_el(tax, 'CategoryCode', category)
        if category in ('E', 'AE', 'K', 'G') and exemption_code:
            _cii_el(tax, 'ExemptionReasonCode', exemption_code)
        if category == 'S':
            # BT-8 : 5 = TVA exigible à la date de facture (débits), 72 = à l'encaissement.
            _cii_el(tax, 'DueDateTypeCode', '72' if on_collection else '5')
        _cii_el(tax, 'RateApplicablePercent', f"{(b['rate'] if category == 'S' else 0):g}")
    terms_text = payment_terms_text or _g(company, 'payment_terms')
    if due_amount > 0 and (_g(inv, 'due_date') or terms_text):
        terms = _cii_el(settlement_hdr, 'SpecifiedTradePaymentTerms')
        if terms_text:
            _cii_el(terms, 'Description', terms_text)
        if _g(inv, 'due_date'):
            _cii_datetime(terms, 'DueDateDateTime', _g(inv, 'due_date'))
    summation = _cii_el(settlement_hdr, 'SpecifiedTradeSettlementHeaderMonetarySummation')
    _cii_el(summation, 'LineTotalAmount', f"{subtotal:.2f}")
    _cii_el(summation, 'TaxBasisTotalAmount', f"{subtotal:.2f}")
    _cii_el(summation, 'TaxTotalAmount', f"{vat_total:.2f}", currencyID='EUR')
    _cii_el(summation, 'GrandTotalAmount', f"{grand_total:.2f}")
    if prepaid:
        _cii_el(summation, 'TotalPrepaidAmount', f"{float(prepaid):.2f}")
    _cii_el(summation, 'DuePayableAmount', f"{max(due_amount, 0):.2f}")
    if referenced:
        ref = _cii_el(settlement_hdr, 'InvoiceReferencedDocument')
        _cii_el(ref, 'IssuerAssignedID', referenced.get('number') or '')
        if referenced.get('issue_date'):
            _cii_datetime(ref, 'FormattedIssueDateTime', referenced['issue_date'], qualified=True)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='utf-8')


# Sous-ensemble des règles métier EN16931 (BR-*) les plus fréquemment citées comme
# cause de rejet en pratique — la couche Schematron, distincte de la simple validité
# XSD. Ce n'est PAS l'intégralité des ~140 règles officielles du CEN/TC 434 : c'est
# une vérification raisonnable, pas une garantie de conformité complète.
def validate_facturx_business_rules(inv, items, company):
    """Vérifie un sous-ensemble de règles métier EN16931 directement sur les données
    de la facture (avant génération XML). Retourne (règles, échecs) où chaque règle est
    {code, label, ok}. Python pur — vérifiable sans fpdf2 ni validateur externe."""
    rules = []

    def check(code, label, ok):
        rules.append({'code': code, 'label': label, 'ok': bool(ok)})

    items = items or []
    check('BR-01', "Identifiant de spécification présent (profil EN16931)", True)
    check('BR-02', 'Numéro de facture renseigné (BT-1)', bool(_g(inv, 'invoice_number')))
    check('BR-03', "Date d'émission renseignée (BT-2)", bool(_g(inv, 'issue_date')))
    check('BR-05', 'Code devise renseigné (BT-5 = EUR)', True)
    check('BR-06', 'Nom du vendeur renseigné (BT-27)', bool(_g(company, 'name')))
    check('BR-07', "Nom de l'acheteur renseigné (BT-44)", bool(_g(inv, 'client_name')))
    check('BR-08', "Adresse postale du vendeur (BG-5)", bool(_g(company, 'address')))
    check('BR-09', "Adresse postale de l'acheteur (BG-8)", bool(_g(inv, 'client_address')))
    check('BR-16', 'Au moins une ligne de facture (BG-25)', bool(items))
    check('BR-27', 'Aucun prix unitaire net négatif (déductions en quantité négative)',
          all(float(it.get('unit_price', 0) or 0) >= 0 for it in items))
    line_sum = round(sum(amounts.money(it.get('line_total', 0)) for it in items), 2)
    subtotal = round(float(_g(inv, 'subtotal', 0) or 0), 2)
    check('BR-CO-10', f"Somme des lignes HT ({fr_number(line_sum,2)} €) = total HT facture ({fr_number(subtotal,2)} €)",
          abs(line_sum - subtotal) < 0.005)
    breakdown = amounts.vat_breakdown(items)
    vat_sum = round(sum(b['vat'] for b in breakdown), 2)
    vat_amount = round(float(_g(inv, 'vat_amount', 0) or 0), 2)
    check('BR-CO-14', f"TVA totale ({fr_number(vat_amount,2)} €) = somme des TVA par taux ({fr_number(vat_sum,2)} €)",
          abs(vat_sum - vat_amount) < 0.005)
    rate_ok = all(abs(b['vat'] - amounts.money(b['base'] * b['rate'] / 100)) < 0.005 for b in breakdown)
    check('BR-CO-17', 'TVA de chaque taux = base du taux × taux, arrondie au centime', rate_ok)
    total = round(float(_g(inv, 'total', 0) or 0), 2)
    check('BR-CO-15', f"Total TTC ({fr_number(total,2)} €) = HT + TVA ({fr_number(subtotal + vat_amount,2)} €)",
          abs(total - round(subtotal + vat_amount, 2)) < 0.005)
    check('BR-CO-25', "Échéance ou conditions de paiement présentes (montant dû positif)",
          total <= 0 or bool(_g(inv, 'due_date')) or bool(_g(company, 'payment_terms')))
    categories = {_vat_category(inv, company, it.get('vat_rate', 0)) for it in items}
    if 'AE' in categories:
        check('BR-AE-02', "Autoliquidation : n° de TVA du vendeur et de l'acheteur renseignés",
              bool(_g(company, 'vat_number')) and bool(_g(inv, 'client_vat_number')))
    if 'K' in categories:
        check('BR-IC-02', "Livraison intracommunautaire : n° de TVA du vendeur et de l'acheteur renseignés",
              bool(_g(company, 'vat_number')) and bool(_g(inv, 'client_vat_number')))
    if categories & {'E', 'AE', 'K', 'G'}:
        reason, _ = _vat_exemption(inv, company)
        check('BR-E-10', "Motif d'exonération de TVA renseigné (BT-120)", bool(reason))
        check('BR-E-05', "Taux de TVA nul pour les lignes exonérées / autoliquidées",
              all(float(it.get('vat_rate', 0) or 0) == 0 for it in items))
    check('BR-S-05', "Taux positif pour toute ligne en catégorie S", True)
    missing = [r for r in rules if not r['ok']]
    return rules, missing


def _display_status(inv):
    status=inv['status']
    if status in ('sent','partially_paid') and inv['due_date']:
        try:
            if date.fromisoformat(inv['due_date']) < paris_today():
                return 'overdue'
        except (TypeError,ValueError):
            pass
    return status


def invoice_label(inv):
    """Numéro affichable : le numéro légal d'une facture émise, « Brouillon n°<id> » pour
    un brouillon (le numéro n'est attribué qu'à l'émission)."""
    number = _g(inv, 'invoice_number')
    if number:
        return number
    return f"Brouillon n°{_g(inv, 'id', '')}"


from profitos.invoice_numbering import SeriesLockTimeout


def _next_invoice_number(c, entity_id=None, issue_date=None):
    """Numéro légal suivant de la série de l'entité, réservé sous verrou dans la
    transaction en cours (voir profitos/invoice_numbering.py). À n'appeler qu'à
    l'émission, dans la même transaction que la mise à jour de la facture."""
    from profitos.invoice_numbering import next_document_number
    return next_document_number(c, 'invoice', entity_id, issue_date or paris_today().isoformat())


def _is_public_buyer(inv):
    """Acheteur public (État, collectivités, établissements publics) : SIREN commençant
    par 1 ou 2, ou case cochée. Ses factures doivent être déposées sur Chorus Pro."""
    if str(_g(inv, 'client_is_public', '') or '') in ('1', 'on', 'true'):
        return True
    siren = re.sub(r'\D', '', str(_g(inv, 'client_siren', '') or ''))
    return len(siren) == 9 and siren[0] in '12'


class InvoiceIssueError(Exception):
    """Émission refusée. missing : mentions obligatoires manquantes (liste)."""
    def __init__(self, message, missing=None):
        super().__init__(message)
        self.missing = missing or []


def _register_public_token(org_id, invoice_id, token):
    """Inscrit le lien public d'une facture dans la base partagée (idempotent)."""
    if not token or not org_id:
        return
    ac=auth_cx()
    try:
        ac.execute('INSERT OR IGNORE INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
                   (token,org_id,invoice_id,now()))
        ac.commit()
    finally:
        ac.close()


def _sale_entry_for(c, inv):
    kind=_g(inv,'invoice_kind','standard') or 'standard'
    if kind=='deposit':
        return generate_customer_deposit_entry(c,inv,commit=False)
    if kind=='final':
        return generate_customer_final_entry(c,inv,commit=False)
    return generate_sale_entry(c,inv,commit=False)


def issue_invoice(c, invoice_id, entity_id, org_id=None):
    """Émet un brouillon : contrôle des mentions obligatoires, numéro légal attribué sous
    verrou (année d'émission en heure de Paris), date d'émission du jour, montants
    recalculés depuis les lignes, écriture de vente — le tout dans UNE transaction.
    Si l'écriture est refusée (période clôturée…), rien n'est conservé : la facture reste
    un brouillon sans numéro. Retourne la facture émise ; lève InvoiceIssueError.
    Le lien public est inscrit après validation (base partagée)."""
    inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?',(invoice_id,entity_id)).fetchone()
    if not inv:
        raise InvoiceIssueError("Facture introuvable.")
    if inv['status']!='draft':
        raise InvoiceIssueError("Seule une facture en brouillon peut être émise.")
    try:
        items=json.loads(inv['line_items'] or '[]')
    except (TypeError, ValueError):
        items=[]
    if not items:
        raise InvoiceIssueError("La facture ne contient aucune ligne.")
    if (_g(inv,'invoice_kind','standard') or 'standard')!='final':
        items_error=_line_items_error(items)
        if items_error:
            raise InvoiceIssueError(items_error)
    issue_date=paris_today().isoformat()
    if inv['due_date'] and str(inv['due_date'])[:10] < issue_date:
        raise InvoiceIssueError("L'échéance de ce brouillon est déjà passée : modifiez-la avant d'émettre la facture.")
    seller=_seller_identity(c,entity_id)
    _, missing=_check_mandatory_mentions(inv,seller)
    if missing:
        raise InvoiceIssueError(f"Émission impossible : {len(missing)} mention(s) obligatoire(s) manquante(s).", missing)
    subtotal,vat_amount,total=_totals(items)
    try:
        number=_next_invoice_number(c,entity_id,issue_date)
        cur=c.execute("""UPDATE outgoing_invoices SET status='sent',invoice_number=?,issue_date=?,sent_at=?,
                         subtotal=?,vat_amount=?,total=?,public_token=COALESCE(public_token,?)
                         WHERE id=? AND entity_id IS ? AND status='draft'""",
                      (number,issue_date,now(),subtotal,vat_amount,total,secrets.token_urlsafe(20),invoice_id,entity_id))
        if getattr(cur,'rowcount',1)==0:
            raise InvoiceIssueError("La facture a déjà été émise entre-temps.")
        issued=c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?',(invoice_id,entity_id)).fetchone()
        _sale_entry_for(c,issued)
        c.commit()
    except InvoiceIssueError:
        c.rollback(); raise
    except AccountingError as e:
        c.rollback()
        log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"vente facture {invoice_id}: {e}")
        raise InvoiceIssueError(f"Facture non émise : l'écriture comptable a été refusée ({e}).")
    except dbmod.IntegrityError:
        c.rollback()
        log_ops_event('INVOICE_NUMBER_CONFLICT',outcome='ERROR',detail=f"facture {invoice_id}")
        raise InvoiceIssueError("Facture non émise : conflit de numérotation. Réessayez.")
    except SeriesLockTimeout:
        c.rollback()
        log_ops_event('INVOICE_NUMBER_LOCK_TIMEOUT',outcome='WARNING',detail=f"facture {invoice_id}")
        raise InvoiceIssueError("Facture non émise : une autre émission est en cours. Réessayez dans quelques secondes.")
    if issued['public_token']:
        try:
            _register_public_token(org_id or session.get('org_id'),invoice_id,issued['public_token'])
        except Exception:
            log_ops_event('INVOICE_PUBLIC_TOKEN_FAILED',outcome='WARNING',detail=f"facture {invoice_id}")
    return issued


def _next_credit_number(c, entity_id=None, issue_date=None):
    """Numéro d'avoir : série propre à l'entité, réservée sous verrou (voir
    profitos/invoice_numbering.py)."""
    from profitos.invoice_numbering import next_document_number
    return next_document_number(c, 'credit', entity_id, issue_date or paris_today().isoformat())


def _next_delivery_number(c, entity_id=None, delivery_date=None):
    """Numéro de bon de livraison : série BL propre à l'entité et à l'année (heure de
    Paris), réservée sous verrou comme les factures (voir profitos/invoice_numbering.py)."""
    from profitos.invoice_numbering import next_document_number
    return next_document_number(c, 'delivery', entity_id, delivery_date or paris_today().isoformat())


def _credited_total(c, invoice_id, entity_id=None):
    row=c.execute("SELECT COALESCE(SUM(total),0) AS n FROM outgoing_credit_notes WHERE original_invoice_id=? AND entity_id IS ? AND status='issued'",(invoice_id,entity_id)).fetchone()
    return float(row['n'] or 0)

def _invoice_paid_total(c, invoice_id, entity_id=None):
    row=c.execute("SELECT COALESCE(SUM(amount),0) AS n FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ?",(invoice_id,entity_id)).fetchone()
    return round(float(row['n'] or 0),2)

def _invoice_balance(c, invoice):
    """Reste dû = TTC − encaissements − avoirs émis. Sans la déduction des avoirs, une facture
    créditée restait « à payer » et « Marquer payée » passait un encaissement fictif en banque."""
    paid=_invoice_paid_total(c,invoice['id'],invoice['entity_id'])
    credited=_credited_total(c,invoice['id'],invoice['entity_id'])
    return max(0.0, round(float(invoice['total'] or 0)-paid-credited,2))


def _record_einvoice_event(c, invoice, event_type, *, remote_id=None, status=None, regulatory_code=None, idempotency_key=None, detail=None):
    """Journal append-only du cycle de vie e-invoicing, isolé par entité."""
    key=(idempotency_key or '').strip() or None
    c.execute("""INSERT OR IGNORE INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,regulatory_code,idempotency_key,detail,occurred_at)
                 VALUES(?,?,'weinvoice',?,?,?,?,?,?,?)""",
              (invoice['entity_id'],invoice['id'],event_type,remote_id,status,
               str(regulatory_code) if regulatory_code is not None else None,key,(detail or '')[:2000],now()))


def _stage_ereporting_record(c, invoice, record_type='transaction'):
    """Prépare les données à remettre à une plateforme agréée; n'envoie jamais directement à la DGFiP."""
    c.execute("""INSERT OR IGNORE INTO ereporting_records(entity_id,record_type,source_type,source_id,period_date,amount_ht,vat_amount,amount_ttc,payment_amount,operation_nature,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?,?)""",
              (invoice['entity_id'],record_type,'outgoing_invoice',invoice['id'],invoice['issue_date'] or paris_today().isoformat(),
               float(invoice['subtotal'] or 0),float(invoice['vat_amount'] or 0),float(invoice['total'] or 0),0.0,
               invoice['operation_nature'] if 'operation_nature' in invoice.keys() else None,now(),now()))


def _stage_payment_ereporting(c, invoice, payment):
    """Prépare un e-reporting de paiement; la plateforme agréée décidera du périmètre réglementaire effectif."""
    c.execute("""INSERT OR IGNORE INTO ereporting_records(entity_id,record_type,source_type,source_id,period_date,amount_ht,vat_amount,amount_ttc,payment_amount,operation_nature,status,created_at,updated_at)
                 VALUES(?,'payment','outgoing_invoice_payment',?,?,?,?,?,?,?, 'pending',?,?)""",
              (invoice['entity_id'],payment['id'],payment['payment_date'],0.0,0.0,0.0,float(payment['amount'] or 0),
               invoice['operation_nature'] if 'operation_nature' in invoice.keys() else None,now(),now()))


def _current_invoice(c, invoice_id):
    from profitos.entities import current_entity_id
    return c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?',
                     (invoice_id,current_entity_id())).fetchone()


def _current_credit(c, credit_id):
    from profitos.entities import current_entity_id
    return c.execute('SELECT * FROM outgoing_credit_notes WHERE id=? AND entity_id IS ?',
                     (credit_id,current_entity_id())).fetchone()


def _next_quote_number(c, entity_id=None):
    year=paris_today().year
    prefix=f"DEV-{year}-" if not entity_id else f"DEV-E{entity_id}-{year}-"
    rows=c.execute("SELECT quote_number FROM outgoing_quotes WHERE quote_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['quote_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    return f"{prefix}{highest+1:03d}"


def _quote_status_label(status):
    return {'draft':'Brouillon','sent':'Envoyé','accepted':'Accepté','refused':'Refusé','converted':'Facturé'}.get(status,status)


_PURCHASE_PDF_MAX_BYTES = 5 * 1024 * 1024

def _purchase_money(raw):
    if raw is None: return None
    v=str(raw).replace('\u00a0',' ').replace('€','').strip()
    v=re.sub(r'[^0-9,\.\- ]','',v).replace(' ','')
    if ',' in v and '.' in v:
        v=v.replace('.','').replace(',','.') if v.rfind(',')>v.rfind('.') else v.replace(',','')
    elif ',' in v: v=v.replace(',','.')
    try: return float(v)
    except Exception: return None


def _purchase_form_date(raw, label):
    """Validate an optional HTML date field and return canonical ISO YYYY-MM-DD."""
    value=(raw or '').strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{label} invalide : saisissez une date valide (jj/mm/aaaa).")


def _purchase_pdf_date(raw):
    if not raw:
        return None
    v=str(raw).strip()
    # ISO: YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD
    m=re.fullmatch(r'(\d{4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})',v)
    if m:
        try: return date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
        except ValueError: return None
    # French invoices: DD/MM/YYYY (also accepts - and .)
    m=re.fullmatch(r'(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2}|\d{4})',v)
    if m:
        year=int(m.group(3))
        if year < 100: year += 2000
        try: return date(year,int(m.group(2)),int(m.group(1)))
        except ValueError: return None
    return None

def _purchase_pdf_extract(path):
    reader=PdfReader(str(path))
    text='\n'.join((page.extract_text() or '') for page in reader.pages[:12]).strip()
    if len(text)<20:
        raise PdfTextUnavailable("PDF sans couche texte exploitable.")

    def first(patterns):
        for pat in patterns:
            m=re.search(pat,text,re.I|re.M)
            if m: return m.group(1).strip()
        return None

    number=first([
        r'(?:facture|invoice)\s*(?:n(?:°|o)?|num(?:e|é)ro|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9._\-/]{2,})',
        r'(?:n(?:°|o)?\s*(?:de\s+)?facture|num(?:e|é)ro\s+(?:de\s+)?facture|invoice\s*(?:number|no\.?))\s*[:\-]?\s*\n?\s*([A-Z0-9][A-Z0-9._\-/]{2,})'
    ])
    issue_raw=first([
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)'
    ])
    due_raw=first([
        r'(?:date\s+d[’\']?échéance|échéance|echeance|due\s+date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?échéance|échéance|echeance|due\s+date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)'
    ])
    total=_purchase_money(first([
        r'(?:total\s*ttc|net\s*(?:à|a)\s*payer|amount\s*due|total\s*due)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)'
    ]))
    subtotal=_purchase_money(first([
        r'(?:sous[\-\s]?total\s*ht|total\s*ht|montant\s*ht|subtotal)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)'
    ]))
    vat=_purchase_money(first([
        r'(?:montant\s*)?tva(?:\s*\([^)]*\))?\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)',
        r'(?:vat)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)'
    ]))
    if subtotal is None and total is not None and vat is not None: subtotal=round(total-vat,2)
    if vat is None and total is not None and subtotal is not None: vat=round(total-subtotal,2)

    vendor=first([
        r'(?:fournisseur|supplier|vendor|émis\s+par|emis\s+par|issued\s+by)\s*[:\-]\s*([^\n]{2,120})'
    ])
    if not vendor:
        for line in [x.strip() for x in text.splitlines()[:18] if x.strip()]:
            if len(line)<2 or len(line)>100: continue
            if re.search(r'^(facture|invoice|date|échéance|echeance|total|tva|client|description)\b',line,re.I): continue
            if re.fullmatch(r'[0-9\s.,€+\-/]+',line): continue
            vendor=line; break

    issue=_purchase_pdf_date(issue_raw) if issue_raw else None
    due=_purchase_pdf_date(due_raw) if due_raw else None
    if issue_raw and not issue:
        raise ValueError("Date de facture détectée mais illisible. Vérifiez-la manuellement.")
    if due_raw and not due:
        raise ValueError("Date d'échéance détectée mais illisible. Vérifiez-la manuellement.")

    missing=[]
    if not vendor: missing.append("fournisseur")
    if not number: missing.append("numéro")
    if subtotal is None: missing.append("HT")
    if vat is None: missing.append("TVA")
    if total is None: missing.append("TTC")
    if missing:
        raise ValueError("Champs non détectés : "+", ".join(missing)+". Corrigez le PDF ou saisissez la facture manuellement.")
    if abs(round(subtotal+vat-total,2))>0.02:
        raise ValueError("Les montants HT + TVA ne correspondent pas au TTC. Import refusé par sécurité.")
    return {'supplier_name':vendor,'invoice_number':number,
            'issue_date':issue.isoformat() if issue else '',
            'due_date':due.isoformat() if due else '',
            'subtotal':subtotal,'vat_amount':vat,'total':total}


_PURCHASE_AI_PROMPT = (
    "Tu analyses une facture d'achat (fournisseur) fournie en pièce jointe. "
    "Réponds UNIQUEMENT avec un objet JSON, sans aucun texte avant ou après, "
    "exactement sous cette forme :\n"
    '{"supplier_name": "...", "invoice_number": "...", "issue_date": "AAAA-MM-JJ", '
    '"due_date": "AAAA-MM-JJ ou chaîne vide si absente", "subtotal": nombre_HT, '
    '"vat_amount": nombre_TVA, "total": nombre_TTC}\n'
    "Si un champ est illisible ou absent du document, mets une chaîne vide (ou null "
    "pour les nombres). N'invente jamais une valeur que tu ne peux pas lire "
    "réellement sur le document — une extraction incomplète mais honnête vaut "
    "mieux qu'une valeur inventée."
)


def _purchase_ai_extract(file_bytes, mime_type):
    """Extraction par l'API Claude (vision) — repli pour les PDF scannés et les
    photos, sans couche de texte exploitable. Ne remplace pas _purchase_pdf_extract
    (gratuite, appelée en premier) : n'est utilisée qu'en complément, jamais à la
    place. Retourne la même structure que _purchase_pdf_extract pour s'intégrer
    sans changement au reste du flux d'import."""
    if not ANTHROPIC_API_KEY:
        raise ValueError(
            "Ce document ne contient pas de texte exploitable et la lecture automatique "
            "des PDF scannés et des photos n'est pas disponible pour le moment. "
            "Saisissez la facture manuellement."
        )
    from profitos.plan_usage import consume_ai_extraction_quota; consume_ai_extraction_quota()  # quota IA mensuel de la formule (ValueError si atteint)
    content_type = 'document' if mime_type == 'application/pdf' else 'image'
    b64 = base64.b64encode(file_bytes).decode('utf-8')
    headers = {
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }
    payload = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 1024,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': content_type, 'source': {'type': 'base64', 'media_type': mime_type, 'data': b64}},
                {'type': 'text', 'text': _PURCHASE_AI_PROMPT},
            ],
        }],
    }
    try:
        resp = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, timeout=45)
    except requests.RequestException as e:
        raise ValueError(f"Connexion à l'API d'extraction impossible : {e}") from e
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:300]
        raise ValueError(f"L'extraction par IA a échoué ({resp.status_code}) — détail : {detail}")

    try:
        data = resp.json()
        text_out = ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text').strip()
        text_out = re.sub(r'^```(?:json)?\s*|\s*```$', '', text_out)
        parsed = json.loads(text_out)
    except (ValueError, KeyError, AttributeError) as e:
        raise ValueError("Réponse d'extraction IA illisible — réessayez ou saisissez la facture manuellement.") from e

    supplier_name = (parsed.get('supplier_name') or '').strip()
    invoice_number = (parsed.get('invoice_number') or '').strip()
    issue_raw = (parsed.get('issue_date') or '').strip()
    due_raw = (parsed.get('due_date') or '').strip()
    subtotal, vat_amount, total = parsed.get('subtotal'), parsed.get('vat_amount'), parsed.get('total')

    missing = []
    if not supplier_name: missing.append("fournisseur")
    if not invoice_number: missing.append("numéro")
    if subtotal is None: missing.append("HT")
    if vat_amount is None: missing.append("TVA")
    if total is None: missing.append("TTC")
    if missing:
        raise ValueError("Champs non détectés par l'IA : " + ", ".join(missing) + ". Saisissez la facture manuellement.")
    try:
        subtotal, vat_amount, total = round(float(subtotal), 2), round(float(vat_amount), 2), round(float(total), 2)
    except (TypeError, ValueError):
        raise ValueError("Montants détectés par l'IA illisibles. Saisissez la facture manuellement.")
    if abs(round(subtotal + vat_amount - total, 2)) > 0.02:
        raise ValueError("Les montants HT + TVA ne correspondent pas au TTC (extraction IA). Import refusé par sécurité.")
    try:
        issue = date.fromisoformat(issue_raw) if issue_raw else None
        due = date.fromisoformat(due_raw) if due_raw else None
    except ValueError:
        raise ValueError("Date détectée par l'IA dans un format inattendu. Vérifie-la manuellement.")

    return {'supplier_name': supplier_name, 'invoice_number': invoice_number,
            'issue_date': issue.isoformat() if issue else '',
            'due_date': due.isoformat() if due else '',
            'subtotal': subtotal, 'vat_amount': vat_amount, 'total': total}


def _purchase_pdf_dir():
    org_id = session.get('org_id')
    if not org_id:
        raise RuntimeError("Organisation non sélectionnée")
    root = UP / "purchase_documents" / str(int(org_id))
    root.mkdir(parents=True, exist_ok=True)
    return root

def _purchase_pdf_dir_for_org(org_id):
    """Variante de _purchase_pdf_dir() utilisable hors contexte de session —
    pour le webhook de la boîte mail fournisseurs, qui reçoit des documents
    sans utilisateur connecté."""
    root = UP / "purchase_documents" / str(int(org_id))
    root.mkdir(parents=True, exist_ok=True)
    return root

def get_or_create_supplier_inbox_token(org_id, entity_id):
    """Jeton de boîte fournisseurs propre à une entité juridique.

    Le token ne remplace pas la signature cryptographique du fournisseur
    d'email entrant ; il sert uniquement au routage organisation + entité.
    """
    ac = auth_cx()
    entity_key = int(entity_id) if entity_id is not None else 0
    row = ac.execute(
        'SELECT token FROM supplier_inbox_entity_tokens WHERE organization_id=? AND entity_key=?',
        (org_id, entity_key),
    ).fetchone()
    if row:
        ac.close(); return row['token']
    token = secrets.token_urlsafe(18).lower().replace('-', '').replace('_', '')
    ac.execute(
        'INSERT INTO supplier_inbox_entity_tokens(token,organization_id,entity_id,entity_key,created_at) VALUES(?,?,?,?,?)',
        (token, org_id, entity_id, entity_key, now()),
    )
    ac.commit(); ac.close(); return token

def _save_purchase_document(uploaded):
    """Enregistre un justificatif d'achat — PDF ou photo (JPEG/PNG/WEBP). Retourne
    (nom_fichier_stocké, mime_type) ou (None, None) si aucun fichier envoyé."""
    if not uploaded or not uploaded.filename:
        return None, None
    data = uploaded.read(_PURCHASE_PDF_MAX_BYTES + 1)
    if len(data) > _PURCHASE_PDF_MAX_BYTES:
        raise ValueError("Le justificatif dépasse la taille maximale de 5 Mo.")

    if data.startswith(b"%PDF-"):
        mime, ext = 'application/pdf', '.pdf'
    elif data.startswith(b"\xff\xd8\xff"):
        mime, ext = 'image/jpeg', '.jpg'
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, ext = 'image/png', '.png'
    elif len(data) > 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime, ext = 'image/webp', '.webp'
    else:
        raise ValueError("Format non reconnu — envoie un PDF, une photo JPEG, PNG ou WEBP.")

    stored = uuid.uuid4().hex + ext
    (_purchase_pdf_dir() / stored).write_bytes(data)
    return stored, mime


def _save_purchase_pdf(uploaded):
    """Conservé pour compatibilité : PDF uniquement. Préférer _save_purchase_document."""
    if not uploaded or not uploaded.filename:
        return None
    if not uploaded.filename.lower().endswith(".pdf"):
        raise ValueError("Le justificatif doit être un fichier PDF.")
    data = uploaded.read(_PURCHASE_PDF_MAX_BYTES + 1)
    if len(data) > _PURCHASE_PDF_MAX_BYTES:
        raise ValueError("Le PDF dépasse la taille maximale de 5 Mo.")
    if not data.startswith(b"%PDF-"):
        raise ValueError("Le fichier envoyé n'est pas un PDF valide.")
    stored = uuid.uuid4().hex + ".pdf"
    (_purchase_pdf_dir() / stored).write_bytes(data)
    return stored


PURCHASE_CATEGORIES = [
    ('carburant', 'Carburant'),
    ('logiciel_saas', 'Logiciel / SaaS'),
    ('assurance', 'Assurance'),
    ('telephone', 'Téléphone'),
    ('sous_traitance', 'Sous-traitance'),
    ('materiel', 'Matériel'),
    ('loyer', 'Loyer'),
    ('autre', 'Autre'),
]
PURCHASE_CATEGORY_LABELS = dict(PURCHASE_CATEGORIES)

# (borne basse jours, borne haute jours, libellé, nombre de jours canonique)
_FREQUENCY_BUCKETS = [
    (25, 35, 'Mensuel', 30),
    (55, 70, 'Bimestriel', 60),
    (80, 100, 'Trimestriel', 91),
    (170, 195, 'Semestriel', 182),
    (340, 390, 'Annuel', 365),
]


def _detect_recurring_suppliers(rows):
    """Analyse purement statistique sur l'historique réel de purchase_invoices —
    aucune donnée inventée, aucune dépendance à Cash Intelligence / Financial Brain /
    AI CFO. Retourne une liste de fournisseurs dont le rythme de facturation est
    suffisamment régulier pour être qualifié d'abonnement/charge récurrente."""
    by_supplier = {}
    for r in rows:
        if not r['issue_date']: continue
        by_supplier.setdefault(r['supplier_name'] or 'Fournisseur inconnu', []).append(r)

    results = []
    for supplier, invoices in by_supplier.items():
        if len(invoices) < 2:
            continue
        parsed = []
        for inv in invoices:
            try:
                d = datetime.strptime(inv['issue_date'], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                continue
            parsed.append((d, inv))
        parsed.sort(key=lambda x: x[0])
        if len(parsed) < 2:
            continue

        gaps = [(parsed[i+1][0] - parsed[i][0]).days for i in range(len(parsed)-1)]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            continue
        avg_gap = statistics.mean(gaps)

        # Régularité : si on a plusieurs intervalles, ils doivent rester cohérents
        # entre eux (coefficient de variation modéré), sinon ce n'est pas un vrai
        # rythme récurrent mais des achats ponctuels qui se recoupent par hasard.
        if len(gaps) >= 2:
            cv = statistics.pstdev(gaps) / avg_gap if avg_gap else 999
            if cv > 0.5:
                continue

        label, canonical_days = None, None
        for lo, hi, lbl, canon in _FREQUENCY_BUCKETS:
            if lo <= avg_gap <= hi:
                label, canonical_days = lbl, canon
                break
        if not label:
            continue

        amounts = [inv['total'] or 0 for _, inv in parsed]
        last_date, last_invoice = parsed[-1]
        last_amount = last_invoice['total'] or 0
        baseline_amounts = amounts[:-1] if len(amounts) > 1 else amounts
        usual_amount = statistics.median(baseline_amounts)
        anomaly = usual_amount > 0 and last_amount > usual_amount * 1.15

        results.append({
            'supplier': supplier,
            'category': PURCHASE_CATEGORY_LABELS.get(last_invoice['category'] or 'autre', 'Autre'),
            'usual_amount': usual_amount,
            'frequency': label,
            'occurrences': len(parsed),
            'last_date': last_date.isoformat(),
            'last_amount': last_amount,
            'next_due_estimate': (last_date + timedelta(days=canonical_days)).isoformat(),
            'annual_cost_estimate': round(usual_amount * (365 / canonical_days), 2),
            'anomaly': anomaly,
        })

    results.sort(key=lambda x: x['annual_cost_estimate'], reverse=True)
    return results



def _recurring_add_period(iso_date, frequency, interval_count=1):
    """Retourne la prochaine échéance en conservant au mieux le jour du mois."""
    import calendar
    d=date.fromisoformat(iso_date)
    n=max(1,int(interval_count or 1))
    if frequency=='weekly':
        return (d+timedelta(weeks=n)).isoformat()
    if frequency=='yearly':
        months=12*n
    elif frequency=='quarterly':
        months=3*n
    else:
        months=n
    absolute=(d.year*12+d.month-1)+months
    year,month=divmod(absolute,12)
    month+=1
    day=min(d.day,calendar.monthrange(year,month)[1])
    return date(year,month,day).isoformat()


def _generate_due_recurring_invoices(c, entity_id, through_date=None):
    """Génère les brouillons dus pour une entité. Idempotence garantie en base."""
    through=through_date or paris_today().isoformat()
    entity_key=int(entity_id or 0)
    templates=c.execute("""SELECT * FROM recurring_invoice_templates
        WHERE entity_id IS ? AND status='active' AND next_run_date<=?
        ORDER BY next_run_date,id""",(entity_id,through)).fetchall()
    created=[]
    for tpl in templates:
        run_date=tpl['next_run_date']
        while run_date and run_date<=through and (not tpl['end_date'] or run_date<=tpl['end_date']):
            exists=c.execute("SELECT invoice_id FROM recurring_invoice_runs WHERE template_id=? AND entity_key=? AND run_date=?",
                             (tpl['id'],entity_key,run_date)).fetchone()
            if not exists:
                items=json.loads(tpl['line_items'] or '[]')
                subtotal,vat_amount,total=_totals(items)
                due=(date.fromisoformat(run_date)+timedelta(days=max(0,int(tpl['due_days'] or 0)))).isoformat()
                # Brouillon sans numéro : le numéro légal est attribué à l'émission.
                invoice_id,token=_insert_draft_invoice(c,{
                    'client_name':tpl['client_name'],'client_address':tpl['client_address'],'client_email':tpl['client_email'],
                    'issue_date':run_date,'due_date':due,'line_items':tpl['line_items'],'subtotal':subtotal,
                    'vat_amount':vat_amount,'total':total,'notes':tpl['notes'],'client_siren':tpl['client_siren'],
                    'operation_nature':tpl['operation_nature'],'vat_on_debits':tpl['vat_on_debits'],
                    'delivery_address':tpl['delivery_address'],'entity_id':entity_id,'recurring_template_id':tpl['id'],
                    'recurring_run_date':run_date})
                number=f"Brouillon n°{invoice_id}"
                c.execute("INSERT INTO recurring_invoice_runs(template_id,entity_key,run_date,invoice_id,created_at) VALUES(?,?,?,?,?)",
                          (tpl['id'],entity_key,run_date,invoice_id,now()))
                created.append((invoice_id,number,token))
            run_date=_recurring_add_period(run_date,tpl['frequency'],tpl['interval_count'])
        status='completed' if tpl['end_date'] and run_date>tpl['end_date'] else 'active'
        c.execute("UPDATE recurring_invoice_templates SET next_run_date=?,status=?,updated_at=? WHERE id=? AND entity_id IS ?",
                  (run_date,status,now(),tpl['id'],entity_id))
    return created


from profitos.ereporting import build_flux10
from profitos.weinvoice import submit_ereporting_flow

from profitos.einvoice_validation import validate_outgoing_invoice
from profitos.weinvoice import (
    get_ereporting_transmission, get_ereporting_proof,
    validate_ereporting_fiscal_readiness, production_readiness,
)

WEINVOICE_MANUAL_SYNC_MAX=15  # factures traitées par clic (le reste : job horaire)


def _inbound_original_reference(raw):
    """Extrait la facture précédente d'un UBL/CII entrant."""
    try: root=ET.fromstring(raw)
    except Exception: return None
    for parent_name,id_name in (('BillingReference','ID'),('InvoiceReferencedDocument','IssuerAssignedID')):
        for el in root.iter():
            if el.tag.rsplit('}',1)[-1] == parent_name:
                for child in el.iter():
                    if child.tag.rsplit('}',1)[-1] == id_name and (child.text or '').strip():
                        return child.text.strip()
    return None

def _is_inbound_credit_note(item, raw):
    kind=str(item.get('type') or '').upper().replace('-','_')
    if 'CREDIT' in kind or 'AVOIR' in kind: return True
    try:
        root=ET.fromstring(raw)
        if root.tag.rsplit('}',1)[-1].upper() == 'CREDITNOTE': return True
        for el in root.iter():
            if el.tag.rsplit('}',1)[-1] in ('TypeCode','InvoiceTypeCode','CreditNoteTypeCode'):
                if (el.text or '').strip() in {'261','381','396','502','503'}: return True
    except Exception: pass
    return False


class WeInvoiceInboundNotConfigured(Exception):
    """Aucune société WeInvoice n'est rattachée à l'entité."""


def sync_weinvoice_inbound(c, eid, org_id, max_items=None):
    """Lot F1 — importe les factures électroniques INBOUND WeInvoice d'une entité.

    Appelée par le bouton « Synchroniser » (plafonnée via max_items pour rester
    sous le timeout gunicorn) et par le job planifié einvoice_inbound_sync
    (profitos/jobs.py, sans plafond). Idempotence par (entity_id,
    eInvoicingId). Le document PDF lisible est conservé comme justificatif;
    l'écriture comptable est générée dans la même transaction que la facture
    locale. Lève WeInvoiceInboundNotConfigured, WeInvoiceAPIError ou
    WeInvoiceConfigError.
    """
    settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',(eid or 0,)).fetchone()
    if not settings or not settings['weinvoice_company_id']:
        raise WeInvoiceInboundNotConfigured()
    org_remote=settings['weinvoice_company_id']
    # Parcourt toutes les pages WeInvoice. La limite de 100 est celle
    # d'une page API, pas celle d'une synchronisation ProfitOS.
    inbound_items=[]; page=1; page_size=100
    while True:
        payload=list_inbound_invoices(org_remote,page=page,page_size=page_size)
        batch=payload.get('invoices',[])
        inbound_items.extend(batch)
        total=payload.get('total')
        if isinstance(total,int):
            if len(inbound_items) >= total: break
        elif len(batch) < page_size:
            break
        if not batch: break
        page += 1
        if page > 1000:
            raise WeInvoiceAPIError("Pagination WeInvoice anormalement longue.")
    created=0; skipped=0; ignored=0; failed=[]
    pdf_dir=_purchase_pdf_dir_for_org(org_id)
    processed=0; remaining=0
    for item in inbound_items:
        stored=None
        remote_id=str(item.get('id') or '').strip()
        if not remote_id or str(item.get('direction') or '').upper()!='INBOUND':
            continue
        if (c.execute('SELECT 1 FROM purchase_invoices WHERE entity_id IS ? AND weinvoice_invoice_id=?',(eid,remote_id)).fetchone()
            or c.execute('SELECT 1 FROM purchase_credit_notes WHERE entity_id IS ? AND weinvoice_invoice_id=?',(eid,remote_id)).fetchone()):
            skipped+=1; continue
        if max_items is not None and processed>=max_items:
            remaining+=1
            continue
        processed+=1
        # Un document terminalement rejeté n'est pas une facture fournisseur
        # importable. Il reste visible chez WeInvoice mais ne doit pas être
        # compté comme une panne de synchronisation ProfitOS.
        status=str(item.get('status') or '').upper()
        regulatory=str(item.get('regulatoryStatusCode') or item.get('cdvCode') or '').strip()
        if status in {'REJECTED','REJETEE','REJETÉE'} or regulatory == '213':
            ignored+=1
            continue
        try:
            # Le contenu structure est la source metier indispensable.
            # Le socle original et le lisible sont des enrichissements :
            # leur indisponibilite ne doit pas bloquer une facture valide.
            content=get_inbound_invoice_content(org_remote,remote_id)
            raw_original=b''
            try:
                raw_original=download_inbound_invoice_original(org_remote,remote_id)
            except WeInvoiceAPIError as exc:
                log_ops_event('WEINVOICE_INBOUND_ORIGINAL_UNAVAILABLE','WARNING',
                              detail=f'eInvoicingId={remote_id} error={type(exc).__name__}')
            subtotal=float(content.get('totalHt') if content.get('totalHt') is not None else (item.get('totalHt') or 0))
            vat=float(content.get('totalVat') or item.get('totalVat') or 0)
            total=float(content.get('totalTtc') if content.get('totalTtc') is not None else subtotal+vat)
            if not all(math.isfinite(v) and v>=0 for v in (subtotal,vat,total)):
                raise ValueError('montants non valides')
            number=str(item.get('number') or remote_id)[:100]
            supplier=str(item.get('vendorName') or item.get('vendorSiren') or 'Fournisseur électronique')[:255]
            issue=str(item.get('date') or '')[:10] or None
            due=str(item.get('dueDate') or '')[:10] or None
            if _is_inbound_credit_note(item,raw_original):
                original_number=_inbound_original_reference(raw_original)
                if not original_number: raise ValueError("avoir fournisseur sans référence à la facture d'origine")
                original=c.execute('SELECT * FROM purchase_invoices WHERE entity_id IS ? AND invoice_number=? ORDER BY id DESC LIMIT 1',(eid,original_number)).fetchone()
                if not original: raise ValueError("facture fournisseur d'origine introuvable")
                # Comme pour une facture normale, le PDF lisible est un
                # enrichissement documentaire et non une condition d'import.
                try:
                    pdf=download_inbound_invoice_readable(org_remote,remote_id)
                except WeInvoiceAPIError as exc:
                    pdf=None
                    log_ops_event('WEINVOICE_INBOUND_CREDIT_READABLE_UNAVAILABLE','WARNING',
                                  detail=f'eInvoicingId={remote_id} error={type(exc).__name__}')
                if pdf:
                    stored=f"weinvoice_credit_{hashlib.sha256((str(eid)+':'+remote_id).encode()).hexdigest()[:24]}.pdf"
                    (pdf_dir/stored).write_bytes(pdf)
                c.execute("""INSERT INTO purchase_credit_notes(
                    entity_id,original_purchase_id,original_invoice_number,credit_number,supplier_name,issue_date,
                    subtotal,vat_amount,total,category,notes,document_path,weinvoice_invoice_id,weinvoice_status,
                    weinvoice_regulatory_code,weinvoice_last_sync_at,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid,original['id'],original_number,number,supplier,issue,subtotal,vat,total,
                     original['category'] or 'autre','Avoir reçu par facturation électronique WeInvoice',stored,
                     remote_id,str(item.get('status') or ''),str(item.get('regulatoryStatusCode') or ''),now(),now()))
                credit_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                credit=c.execute('SELECT * FROM purchase_credit_notes WHERE id=? AND entity_id IS ?',(credit_id,eid)).fetchone()
                generate_purchase_credit_entry(c,credit,commit=False)
                c.commit(); created+=1
                continue
            try:
                pdf=download_inbound_invoice_readable(org_remote,remote_id)
            except WeInvoiceAPIError as exc:
                pdf=None
                log_ops_event('WEINVOICE_INBOUND_READABLE_UNAVAILABLE','WARNING',
                              detail=f'eInvoicingId={remote_id} error={type(exc).__name__}')
            if pdf:
                stored=f"weinvoice_{hashlib.sha256(remote_id.encode()).hexdigest()[:24]}.pdf"
                (pdf_dir/stored).write_bytes(pdf)
            validation='pending'
            settings_row=c.execute('SELECT require_purchase_validation FROM app_settings WHERE id=1').fetchone()
            if not (settings_row and settings_row['require_purchase_validation']): validation='approved'
            c.execute("""INSERT INTO purchase_invoices(
                supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,
                notes,created_at,document_path,category,validation_status,entity_id,
                weinvoice_invoice_id,weinvoice_status,weinvoice_regulatory_code,weinvoice_last_sync_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (supplier,number,issue,due,subtotal,vat,total,'unpaid','Reçue par facturation électronique WeInvoice',now(),stored,'autre',validation,eid,
                 remote_id,str(item.get('status') or ''),str(item.get('regulatoryStatusCode') or ''),now()))
            new_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            if validation=='approved':
                purchase_row=c.execute('SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?',(new_id,eid)).fetchone()
                generate_purchase_entry(c,purchase_row,commit=False)
            c.commit(); created+=1
        except Exception as exc:
            c.rollback()
            try:
                if stored: (pdf_dir/stored).unlink(missing_ok=True)
            except Exception: pass
            failed.append(f"{remote_id}: {type(exc).__name__}")
            log_ops_event('WEINVOICE_INBOUND_IMPORT_FAILED','ERROR',detail=f'eInvoicingId={remote_id} error={type(exc).__name__}')
    log_ops_event('WEINVOICE_INBOUND_SYNC','INFO' if not failed else 'WARNING',detail=f'entity={eid} created={created} skipped={skipped} ignored={ignored} failed={len(failed)}')
    return {'created':created,'skipped':skipped,'ignored':ignored,'failed':failed,'remaining':remaining}


def register(app):
    @app.route('/facturation/clients')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_clients():
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        rows=c.execute("""SELECT cl.*,
          (SELECT COUNT(*) FROM outgoing_invoices i WHERE lower(i.client_name)=lower(cl.name)) invoice_count,
          (SELECT COALESCE(SUM(i.total),0) FROM outgoing_invoices i WHERE lower(i.client_name)=lower(cl.name) AND i.status='paid') paid_total
          FROM invoicing_clients cl WHERE cl.entity_id IS ? ORDER BY lower(cl.name)""",(eid,)).fetchall()
        c.close()
        return render_template('invoicing_clients.html',rows=rows)

    @app.route('/facturation/clients/nouveau',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_client_new():
        from profitos.entities import current_entity_id
        if request.method=='POST':
            name=request.form.get('name','').strip()
            if not name:
                flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_client_new'))
            siren=re.sub(r'\D','',request.form.get('siren','').strip())[:9]
            if siren and len(siren)!=9:
                flash('Le SIREN doit comporter exactement 9 chiffres (laissez vide si inconnu).')
                return redirect(url_for('invoicing_client_new'))
            c=cx()
            c.execute("""INSERT INTO invoicing_clients(name,email,address,siret,vat_number,phone,notes,created_at,updated_at,siren,entity_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (name,request.form.get('email','').strip(),request.form.get('address','').strip(),
               request.form.get('siret','').strip(),request.form.get('vat_number','').strip(),
               request.form.get('phone','').strip(),request.form.get('notes','').strip(),now(),now(),siren or None,current_entity_id()))
            c.commit(); client_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.close()
            flash(f"Client {name} créé.")
            return redirect(url_for('invoicing_client_detail',client_id=client_id))
        return render_template('invoicing_client_form.html')

    @app.route('/facturation/clients/<int:client_id>',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_client_detail(client_id):
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        client=c.execute('SELECT * FROM invoicing_clients WHERE id=? AND entity_id IS ?',(client_id,eid)).fetchone()
        if not client: c.close(); abort(404)
        if request.method=='POST':
            name=request.form.get('name','').strip()
            if not name:
                c.close(); flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_client_detail',client_id=client_id))
            siren=re.sub(r'\D','',request.form.get('siren','').strip())[:9]
            if siren and len(siren)!=9:
                c.close(); flash('Le SIREN doit comporter exactement 9 chiffres (laissez vide si inconnu).')
                return redirect(url_for('invoicing_client_detail',client_id=client_id))
            c.execute("""UPDATE invoicing_clients SET name=?,email=?,address=?,siret=?,vat_number=?,phone=?,notes=?,updated_at=?,siren=? WHERE id=? AND entity_id IS ?""",
              (name,request.form.get('email','').strip(),request.form.get('address','').strip(),
               request.form.get('siret','').strip(),request.form.get('vat_number','').strip(),
               request.form.get('phone','').strip(),request.form.get('notes','').strip(),now(),siren or None,client_id,eid))
            c.commit(); client=c.execute('SELECT * FROM invoicing_clients WHERE id=? AND entity_id IS ?',(client_id,eid)).fetchone()
            flash("Fiche client mise à jour.")
        invoices=c.execute("SELECT * FROM outgoing_invoices WHERE lower(client_name)=lower(?) AND entity_id IS ? ORDER BY id DESC",(client['name'],eid)).fetchall()
        credits=c.execute("SELECT * FROM outgoing_credit_notes WHERE lower(client_name)=lower(?) AND entity_id IS ? ORDER BY id DESC",(client['name'],eid)).fetchall()
        c.close()
        return render_template('invoicing_client_detail.html',client=client,invoices=invoices,credits=credits)

    @app.route('/facturation/devis')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quotes():
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        rows=c.execute("SELECT * FROM outgoing_quotes WHERE entity_id IS ? ORDER BY id DESC",(eid,)).fetchall()
        c.close()
        return render_template('invoicing_quotes.html',rows=rows,quote_status_label=_quote_status_label)

    @app.route('/facturation/devis/nouveau',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_new():
        c=cx()
        from profitos.entities import current_entity, current_entity_id
        company_row=current_entity(c)
        clients=c.execute("SELECT * FROM invoicing_clients WHERE entity_id IS ? ORDER BY lower(name)",(current_entity_id(),)).fetchall()
        if request.method=='POST':
            client_name=request.form.get('client_name','').strip()
            if not client_name:
                c.close(); flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_quote_new'))
            items=_compute_line_items(request.form)
            if not items:
                c.close(); flash("Au moins une ligne de devis est requise.")
                return redirect(url_for('invoicing_quote_new'))
            items_error=_line_items_error(items)
            if items_error:
                c.close(); flash(items_error)
                return redirect(url_for('invoicing_quote_new'))
            subtotal,vat_amount,total=_totals(items)
            entity_id=current_entity_id()
            quote_number=_next_quote_number(c,entity_id)
            siren=re.sub(r'\D','',request.form.get('client_siren') or '')[:9]
            if siren and not siren_is_valid(siren):
                c.close(); flash("SIREN client invalide : 9 chiffres avec une clé de contrôle correcte.")
                return redirect(url_for('invoicing_quote_new'))
            nature=request.form.get('operation_nature') or 'services'
            if nature not in ('biens','services','mixte'): nature='services'
            c.execute("""INSERT INTO outgoing_quotes
              (quote_number,client_name,client_address,client_email,issue_date,valid_until,line_items,
               subtotal,vat_amount,total,notes,status,created_at,entity_id,client_siren,operation_nature,delivery_address,
               client_vat_number,counterparty_country)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?)""",
              (quote_number,client_name,request.form.get('client_address','').strip(),
               request.form.get('client_email','').strip(),paris_today().isoformat(),
               request.form.get('valid_until') or None,json.dumps(items,ensure_ascii=False),
               subtotal,vat_amount,total,request.form.get('notes','').strip(),now(),entity_id,
               siren or None,nature,(request.form.get('delivery_address') or '').strip() or None,
               re.sub(r'[\s.\-]','',request.form.get('client_vat_number') or '').upper() or None,
               (request.form.get('counterparty_country') or '').strip().upper()[:2] or None))
            c.commit()
            quote_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            quote_token=secrets.token_urlsafe(24)
            ac=auth_cx()
            ac.execute('INSERT INTO outgoing_quote_tokens(token,organization_id,quote_local_id,created_at) VALUES(?,?,?,?)',
                       (quote_token,session['org_id'],quote_id,now()))
            ac.commit(); ac.close()
            log_activity('QUOTE_CREATED',f"Devis {quote_number} créé en brouillon")
            flash(f"Devis {quote_number} créé en brouillon.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        c.close()
        return render_template('invoicing_quote_new.html',company=company_row,clients=clients,today=paris_today().isoformat())

    @app.route('/facturation/devis/<int:quote_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_quote_detail(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone(); c.close()
        if not q: abort(404)
        return render_template('invoicing_quote_detail.html',q=q,items=json.loads(q['line_items'] or '[]'),
                               status_label=_quote_status_label(q['status']))

    @app.post('/facturation/devis/<int:quote_id>/envoyer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_send(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        if not q:
            c.close(); abort(404)
        if q['status'] not in ('draft','sent'):
            c.close()
            flash("Ce devis ne peut plus être envoyé.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        if not q['client_email']:
            c.close()
            flash("Aucun e-mail client renseigné pour ce devis.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

        ac=auth_cx()
        mapping=ac.execute(
            'SELECT * FROM outgoing_quote_tokens WHERE organization_id=? AND quote_local_id=?',
            (session['org_id'],quote_id)
        ).fetchone()
        if mapping:
            quote_token=mapping['token']
        else:
            quote_token=secrets.token_urlsafe(24)
            ac.execute(
                'INSERT INTO outgoing_quote_tokens(token,organization_id,quote_local_id,created_at) VALUES(?,?,?,?)',
                (quote_token,session['org_id'],quote_id,now())
            )
            ac.commit()
        ac.close()

        org=current_org()
        base=app_base_url()
        link=f"{base}{url_for('public_quote_view',token=quote_token)}"
        html=render_template(
            'email_transactional.html',
            title=f"Devis {q['quote_number']} — {org['name']}",
            intro=f"Voici votre devis {q['quote_number']} de {org['name']}, d'un montant de {fr_number(q['total'],2)} € TTC.",
            cta_label='Consulter et répondre au devis',
            cta_url=link,
            footer="Vous pouvez accepter ou refuser ce devis depuis la page sécurisée."
        )
        result=send_email(q['client_email'],f"Devis {q['quote_number']} — {org['name']}",html,on_behalf_of_org=org)

        if result.get('dry_run'):
            flash(f"Service e-mail non configuré — devis non envoyé réellement (mode simulation) à {q['client_email']}.")
        elif result.get('sent'):
            c.execute("UPDATE outgoing_quotes SET status='sent',sent_at=? WHERE id=? AND entity_id IS ?",(now(),quote_id,eid))
            c.commit()
            log_activity('QUOTE_SENT',f"Devis {q['quote_number']} envoyé à {q['client_email']}")
            flash(f"Devis envoyé à {q['client_email']}.")
        else:
            flash(f"Échec de l'envoi : {result.get('error','erreur inconnue')}")
        c.close()
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/accepter')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_accept(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        if not q:
            c.close(); abort(404)
        if q['status'] not in ('draft','sent'):
            c.close()
            flash("La réponse à ce devis est déjà enregistrée et verrouillée.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        # Un devis remis en main propre ou envoyé hors ProfitOS (PDF) peut être accepté ou
        # refusé directement depuis le brouillon : sinon il ne pouvait jamais être facturé.
        c.execute("UPDATE outgoing_quotes SET status='accepted',accepted_at=? WHERE id=? AND entity_id IS ?",(now(),quote_id,eid))
        c.commit(); c.close()
        log_activity('QUOTE_ACCEPTED',f"Devis {q['quote_number']} accepté")
        flash(f"Devis {q['quote_number']} accepté.")
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/refuser')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_refuse(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        if not q:
            c.close(); abort(404)
        if q['status'] not in ('draft','sent'):
            c.close()
            flash("La réponse à ce devis est déjà enregistrée et verrouillée.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        # Un devis remis en main propre ou envoyé hors ProfitOS (PDF) peut être accepté ou
        # refusé directement depuis le brouillon : sinon il ne pouvait jamais être facturé.
        c.execute("UPDATE outgoing_quotes SET status='refused',refused_at=? WHERE id=? AND entity_id IS ?",(now(),quote_id,eid))
        c.commit(); c.close()
        log_activity('QUOTE_REFUSED',f"Devis {q['quote_number']} refusé")
        flash(f"Devis {q['quote_number']} refusé.")
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/acompte')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_deposit(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        if not q: c.close(); abort(404)
        if q['status'] not in ('accepted','converted'):
            c.close(); flash("Le devis doit être accepté avant de facturer un acompte."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        try: percent=float((request.form.get('deposit_percent') or '0').replace(',','.'))
        except ValueError: percent=0
        if percent<=0 or percent>100:
            c.close(); flash("Le pourcentage d'acompte doit être compris entre 0 et 100."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        existing=c.execute("SELECT COALESCE(SUM(total),0) n FROM outgoing_invoices WHERE source_quote_id=? AND entity_id IS ? AND invoice_kind='deposit' AND status!='cancelled'",(quote_id,eid)).fetchone()['n']
        target=round(float(q['total'] or 0)*percent/100,2)
        if round(float(existing)+target,2)>round(float(q['total'] or 0),2):
            c.close(); flash("Le cumul des acomptes dépasserait le montant du devis."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        src=json.loads(q['line_items'] or '[]'); items=[]
        for it in src:
            # Même quantité, prix unitaire au prorata (4 décimales) : quantité × prix = total
            # de ligne, comme l'exige la facture électronique.
            x=dict(it); x['qty']=float(it.get('qty',1) or 1)
            x['unit_price']=amounts.quantize(float(it.get('unit_price',0))*percent/100,4)
            x['line_total']=amounts.line_total(x['qty'],x['unit_price']); items.append(x)
        subtotal,vat_amount,total=_totals(items)
        base=_quote_invoice_base(q,eid)
        base.update(line_items=json.dumps(items,ensure_ascii=False),subtotal=subtotal,vat_amount=vat_amount,total=total,
                    notes=f"Facture d'acompte de {fr_number(percent,2).rstrip('0').rstrip(',')} % sur le devis {q['quote_number']}.",
                    invoice_kind='deposit',deposit_percent=percent)
        invoice_id,_token=_insert_draft_invoice(c,base); c.commit(); c.close()
        log_activity('DEPOSIT_INVOICE_CREATED',f"Acompte (brouillon n°{invoice_id}) créé depuis {q['quote_number']}"); flash(f"Facture d'acompte créée en brouillon (Brouillon n°{invoice_id}).")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.post('/facturation/devis/<int:quote_id>/facture-finale')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_final_invoice(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        if not q: c.close(); abort(404)
        if q['status'] not in ('accepted','converted'):
            c.close(); flash("Le devis doit être accepté."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        already=c.execute("SELECT id FROM outgoing_invoices WHERE source_quote_id=? AND entity_id IS ? AND invoice_kind='final' AND status!='cancelled'",(quote_id,eid)).fetchone()
        if already:
            c.close(); flash("Une facture finale existe déjà pour ce devis."); return redirect(url_for('invoicing_detail',invoice_id=already['id']))
        draft_dep=c.execute("SELECT id FROM outgoing_invoices WHERE source_quote_id=? AND entity_id IS ? AND invoice_kind='deposit' AND status='draft' LIMIT 1",(quote_id,eid)).fetchone()
        if draft_dep:
            c.close(); flash("Émettez ou annulez les factures d'acompte en brouillon avant de créer la facture finale."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        deps=c.execute("SELECT * FROM outgoing_invoices WHERE source_quote_id=? AND entity_id IS ? AND invoice_kind='deposit' AND status NOT IN ('draft','cancelled') ORDER BY id",(quote_id,eid)).fetchall()
        dep_ht=round(sum(float(r['subtotal'] or 0) for r in deps),2); dep_vat=round(sum(float(r['vat_amount'] or 0) for r in deps),2)
        net_ht=round(float(q['subtotal'] or 0)-dep_ht,2); net_vat=round(float(q['vat_amount'] or 0)-dep_vat,2); net_total=round(net_ht+net_vat,2)
        final_items=[dict(it) for it in json.loads(q['line_items'] or '[]')]
        deposit_by_rate={}
        for dep in deps:
            for it in json.loads(dep['line_items'] or '[]'):
                rate=float(it.get('vat_rate',0)); deposit_by_rate[rate]=deposit_by_rate.get(rate,0.0)+float(it.get('line_total',0))
        for rate, amount in sorted(deposit_by_rate.items()):
            if round(amount,2):
                # Déduction en quantité négative (prix unitaire positif) : une facture
                # électronique n'admet pas de prix unitaire négatif (BR-27).
                final_items.append({'label':f"Déduction des acomptes déjà facturés — TVA {amounts.fr_rate(rate)}",'qty':-1.0,
                                    'unit_price':round(amount,2),'vat_rate':rate,'line_total':-round(amount,2),
                                    'deposit_deduction':True})
        net_ht,net_vat,net_total=_totals(final_items)
        if net_total< -0.01:
            c.close(); flash("Les acomptes dépassent le montant du devis."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        base=_quote_invoice_base(q,eid)
        base.update(line_items=json.dumps(final_items,ensure_ascii=False),subtotal=net_ht,vat_amount=net_vat,total=net_total,
                    notes=f"Facture finale du devis {q['quote_number']}. Acomptes déjà facturés : {fr_number(dep_ht+dep_vat,2)} € TTC.",
                    invoice_kind='final',deposit_applied_subtotal=dep_ht,deposit_applied_vat=dep_vat)
        invoice_id,_token=_insert_draft_invoice(c,base)
        c.execute("UPDATE outgoing_quotes SET status='converted',converted_invoice_id=? WHERE id=? AND entity_id IS ?",(invoice_id,quote_id,eid)); c.commit(); c.close()
        log_activity('FINAL_INVOICE_CREATED',f"Facture finale (brouillon n°{invoice_id}) créée depuis {q['quote_number']}"); flash(f"Facture finale créée en brouillon (Brouillon n°{invoice_id}).")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.post('/facturation/devis/<int:quote_id>/convertir')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_convert(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        if not q: c.close(); abort(404)
        if q['status']!='accepted' or q['converted_invoice_id']:
            c.close(); flash("Seul un devis accepté et non encore facturé peut être converti.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        base=_quote_invoice_base(q,eid)
        notes=f"Créée depuis le devis {q['quote_number']}." + (("\n"+q['notes']) if q['notes'] else "")
        base.update(line_items=q['line_items'],subtotal=q['subtotal'],vat_amount=q['vat_amount'],total=q['total'],notes=notes)
        invoice_id,_token=_insert_draft_invoice(c,base)
        c.execute("UPDATE outgoing_quotes SET status='converted',converted_invoice_id=? WHERE id=? AND entity_id IS ?",(invoice_id,quote_id,eid))
        c.commit(); c.close()
        log_activity('QUOTE_CONVERTED',f"Devis {q['quote_number']} converti en facture (brouillon n°{invoice_id})")
        flash(f"Devis {q['quote_number']} converti en facture brouillon (Brouillon n°{invoice_id}). Le lien public sera actif dès l'émission.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/devis/<int:quote_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_quote_pdf(quote_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?',(quote_id,eid)).fetchone()
        from profitos.entities import resolve_entity
        company_row=_seller_identity(c,eid) if q else None
        c.close()
        if not q: abort(404)
        pdf_bytes=_render_quote_pdf(q,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2'.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        return Response(pdf_bytes,mimetype='application/pdf',
          headers={'Content-Disposition':f'attachment; filename="{q["quote_number"]}.pdf"'})

    @app.get('/facturation/creances')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_receivables():
        # Vue unique des créances : factures émises ET factures importées (profitos/receivables.py).
        from profitos.receivables import open_receivables, summarize, by_customer
        c=cx()
        try:
            invoices=open_receivables(c,current=True)
        finally:
            c.close()
        totals=summarize(invoices)
        return render_template(
            'invoicing_receivables.html',
            invoices=invoices,
            clients=by_customer(invoices),
            total_due=totals['total_due'],
            overdue_due=totals['overdue_due'],
            imported_due=totals['imported_due'],
            unpaid_count=totals['count'],
            overdue_count=totals['overdue_count']
        )

    @app.get('/facturation/achats')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_list():
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        purchases=c.execute("""SELECT pi.*,COALESCE((SELECT SUM(p.amount) FROM purchase_invoice_payments p
                 WHERE p.purchase_invoice_id=pi.id AND COALESCE(CAST(p.entity_id AS TEXT),'')=COALESCE(CAST(pi.entity_id AS TEXT),'')),0) paid_total
                 FROM purchase_invoices pi WHERE pi.entity_id IS ? ORDER BY pi.due_date ASC, pi.id DESC""",(eid,)).fetchall()
        suppliers=c.execute("SELECT * FROM suppliers WHERE entity_id IS ? ORDER BY name ASC",(eid,)).fetchall()
        today=paris_today()
        total_unpaid=total_overdue=due_7=due_30=0.0
        view=[]
        for p in purchases:
            overdue=False
            open_status=p['status'] in ('unpaid','partially_paid')
            remaining=max(0.0,round(float(p['total'] or 0)-float(p['paid_total'] or 0),2)) if open_status else 0.0
            due=None
            if p['due_date']:
                try: due=date.fromisoformat(str(p['due_date'])[:10])
                except ValueError: due=None
            if open_status and remaining>0:
                overdue=bool(due and due<today)
                total_unpaid+=remaining
                if overdue: total_overdue+=remaining
                elif due and (due-today).days<=7: due_7+=remaining
                if due and not overdue and (due-today).days<=30: due_30+=remaining
            view.append({'row':p,'overdue':overdue})
        # Fournisseurs distincts : fiches + fournisseurs saisis sans fiche sur une facture.
        names={(s['name'] or '').strip().lower() for s in suppliers}
        names|={(p['supplier_name'] or '').strip().lower() for p in purchases}
        names.discard('')
        c.close()
        return render_template('purchase_list.html',purchases=view,suppliers=suppliers,
                               total_unpaid=round(total_unpaid,2),total_overdue=round(total_overdue,2),
                               due_7=round(due_7,2),due_30=round(due_30,2),supplier_count=len(names))

    @app.route('/facturation/achats/pilotage')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_analytics():
        """Pilotage des dépenses fournisseurs — uniquement des données réelles issues
        de purchase_invoices, aucun chiffre inventé. Filtre par période via ?date_from
        et ?date_to (format AAAA-MM-JJ)."""
        date_from=request.args.get('date_from','').strip()
        date_to=request.args.get('date_to','').strip()

        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        query="SELECT * FROM purchase_invoices WHERE entity_id IS ?"
        params=[eid]
        if date_from:
            query+=" AND issue_date>=?"; params.append(date_from)
        if date_to:
            query+=" AND issue_date<=?"; params.append(date_to)
        rows=c.execute(query,params).fetchall()
        c.close()

        total_ht=sum(r['subtotal'] or 0 for r in rows)
        total_vat=sum(r['vat_amount'] or 0 for r in rows)
        total_ttc=sum(r['total'] or 0 for r in rows)
        invoice_count=len(rows)

        # Dépenses par fournisseur, triées par montant décroissant.
        by_supplier={}
        for r in rows:
            name=r['supplier_name'] or 'Fournisseur inconnu'
            by_supplier.setdefault(name,{'ht':0.0,'vat':0.0,'ttc':0.0,'count':0})
            by_supplier[name]['ht']+=r['subtotal'] or 0
            by_supplier[name]['vat']+=r['vat_amount'] or 0
            by_supplier[name]['ttc']+=r['total'] or 0
            by_supplier[name]['count']+=1
        supplier_rows=sorted(
            [{'name':k,**v} for k,v in by_supplier.items()],
            key=lambda x:x['ttc'],reverse=True
        )
        top_suppliers=supplier_rows[:8]

        # Dépenses par mois (AAAA-MM), triées chronologiquement.
        by_month={}
        for r in rows:
            if not r['issue_date']: continue
            month=r['issue_date'][:7]
            by_month[month]=by_month.get(month,0.0)+(r['total'] or 0)
        months_sorted=sorted(by_month.keys())
        monthly_series=[(m,round(by_month[m])) for m in months_sorted]
        evolution_chart=bars_svg(monthly_series[-12:]) if len(monthly_series)>=2 else None
        top_suppliers_chart=bars_svg([(s['name'][:12],round(s['ttc'])) for s in top_suppliers[:6]]) if top_suppliers else None

        # Dépenses par catégorie, avec répartition en pourcentage.
        by_category={}
        for r in rows:
            cat=r['category'] or 'autre'
            if cat not in PURCHASE_CATEGORY_LABELS: cat='autre'
            by_category[cat]=by_category.get(cat,0.0)+(r['total'] or 0)
        category_rows=sorted(
            [{'key':k,'label':PURCHASE_CATEGORY_LABELS[k],'ttc':v,
              'pct':round(v/total_ttc*100,1) if total_ttc else 0}
             for k,v in by_category.items()],
            key=lambda x:x['ttc'],reverse=True
        )
        category_chart=bars_svg([(c['label'][:12],round(c['ttc'])) for c in category_rows]) if category_rows else None

        return render_template('purchase_analytics.html',
            total_ht=total_ht,total_vat=total_vat,total_ttc=total_ttc,invoice_count=invoice_count,
            supplier_rows=supplier_rows,top_suppliers=top_suppliers,monthly_series=monthly_series,
            evolution_chart=evolution_chart,top_suppliers_chart=top_suppliers_chart,
            category_rows=category_rows,category_chart=category_chart,
            date_from=date_from,date_to=date_to)

    @app.route('/facturation/achats/budgets',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_budgets():
        """Budgets mensuels par catégorie, comparés aux factures d'achat réellement
        enregistrées dans purchase_invoices. N'agit ni ne dépend de Cash Intelligence,
        Financial Brain ou AI CFO Planner — module Achats uniquement."""
        c=cx()
        eid=current_entity_id(); ekey=eid or 0
        if request.method=='POST':
            for key,_ in PURCHASE_CATEGORIES:
                raw=request.form.get(f'budget_{key}','').strip()
                try:
                    amount=max(0.0,form_float(raw)) if raw else 0.0
                except ValueError:
                    amount=0.0
                c.execute("""INSERT INTO purchase_budgets(category,entity_key,entity_id,monthly_amount,updated_at)
                             VALUES(?,?,?,?,?)
                             ON CONFLICT(category,entity_key) DO UPDATE SET
                               entity_id=excluded.entity_id,
                               monthly_amount=excluded.monthly_amount,
                               updated_at=excluded.updated_at""",
                          (key,ekey,eid,amount,now()))
            c.commit(); c.close()
            flash("Budgets mis à jour.")
            return redirect(url_for('purchase_budgets',month=request.form.get('month','')))

        month=request.args.get('month','').strip()
        if not re.fullmatch(r'\d{4}-\d{2}',month or ''):
            month=paris_today().strftime('%Y-%m')

        budget_rows=c.execute("SELECT * FROM purchase_budgets WHERE entity_key=?",(ekey,)).fetchall()
        budgets={r['category']:r['monthly_amount'] for r in budget_rows}

        spent_rows=c.execute("SELECT category, SUM(total) as spent FROM purchase_invoices WHERE entity_id IS ? AND issue_date LIKE ? GROUP BY category",(current_entity_id(),f'{month}%')).fetchall()
        spent={(r['category'] or 'autre'):(r['spent'] or 0) for r in spent_rows}
        c.close()

        overview=[]
        any_overrun=False
        for key,label in PURCHASE_CATEGORIES:
            budget=budgets.get(key,0.0)
            consumed=spent.get(key,0.0)
            remaining=budget-consumed
            pct=round(consumed/budget*100,1) if budget else (100.0 if consumed else 0.0)
            overrun=budget>0 and consumed>budget
            if overrun: any_overrun=True
            overview.append({'key':key,'label':label,'budget':budget,'consumed':consumed,
                              'remaining':remaining,'pct':pct,'overrun':overrun,'has_budget':budget>0})

        return render_template('purchase_budgets.html',overview=overview,month=month,
                               budgets=budgets,any_overrun=any_overrun,categories=PURCHASE_CATEGORIES)

    @app.route('/facturation/achats/recurrents')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_recurring():
        """Détection des dépenses récurrentes / abonnements — analyse purement
        statistique de l'historique réel dans purchase_invoices. Module informatif
        et isolé : aucune écriture, aucune interaction avec Cash Intelligence,
        Financial Brain ou AI CFO Planner."""
        c=cx()
        rows=c.execute("SELECT * FROM purchase_invoices WHERE entity_id IS ? AND issue_date IS NOT NULL",(current_entity_id(),)).fetchall()
        c.close()
        recurring=_detect_recurring_suppliers(rows)
        total_annual_estimate=sum(r['annual_cost_estimate'] for r in recurring)
        anomaly_count=sum(1 for r in recurring if r['anomaly'])
        return render_template('purchase_recurring.html',recurring=recurring,
                               total_annual_estimate=total_annual_estimate,anomaly_count=anomaly_count)

    @app.route('/facturation/achats/export')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_export():
        """Export comptable des factures fournisseurs (CSV/Excel) — même mécanisme et
        même quota mensuel que les autres exports de l'app (RECOVER, etc.)."""
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(f"Quota mensuel d'exports atteint pour la formule {org['plan']} ({quota['used']}/{quota['limit']}). Passez à une formule supérieure.")
            return redirect(url_for('purchase_list'))
        c=cx()
        purchases=c.execute("SELECT * FROM purchase_invoices WHERE entity_id IS ? ORDER BY due_date ASC, id DESC",(current_entity_id(),)).fetchall()
        c.close()
        data=[{'Fournisseur':p['supplier_name'],'N° facture':p['invoice_number'],
               "Date d'émission":fr_date(p['issue_date']) or '',"Date d'échéance":fr_date(p['due_date']) or '',
               'Montant HT (€)':round(p['subtotal'] or 0,2),'TVA (€)':round(p['vat_amount'] or 0,2),
               'Montant TTC (€)':round(p['total'] or 0,2),
               'Catégorie':PURCHASE_CATEGORY_LABELS.get(p['category'] or 'autre','Autre'),
               'Statut':{'paid':'Payée','partially_paid':'Partiellement payée'}.get(p['status'],'À payer')} for p in purchases]
        record_usage('reports_per_month',organization_id=org['id'])
        return export_response(data,'profitos-achats-fournisseurs',french_csv=True)

    @app.route('/facturation/fournisseurs/nouveau',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_new():
        from profitos.entities import current_entity_id
        if request.method=='POST':
            name=(request.form.get('name') or '').strip()
            if not name:
                flash("Le nom du fournisseur est obligatoire.")
                return redirect(url_for('supplier_new'))
            c=cx()
            c.execute("""INSERT INTO suppliers(name,email,phone,address,siret,vat_number,notes,iban,bic,created_at,entity_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (name,(request.form.get('email') or '').strip(),
                       (request.form.get('phone') or '').strip(),
                       (request.form.get('address') or '').strip(),
                       (request.form.get('siret') or '').strip(),
                       (request.form.get('vat_number') or '').strip(),
                       (request.form.get('notes') or '').strip(),
                       (request.form.get('iban') or '').replace(' ','').upper().strip(),
                       (request.form.get('bic') or '').upper().strip(),now(),current_entity_id()))
            c.commit(); c.close()
            flash("Fournisseur ajouté.")
            return redirect(url_for('purchase_list'))
        return render_template('supplier_new.html')

    @app.route('/facturation/fournisseurs/<int:supplier_id>/modifier', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_update(supplier_id):
        from profitos.sepa import validate_iban
        c = cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        supplier = c.execute("SELECT id FROM suppliers WHERE id=? AND entity_id IS ?", (supplier_id,eid)).fetchone()
        if not supplier:
            c.close(); abort(404)
        iban = (request.form.get('iban') or '').replace(' ', '').upper().strip()
        if iban and not validate_iban(iban):
            c.close()
            flash("IBAN invalide — vérifiez la saisie (format et somme de contrôle incorrects).")
            return redirect(url_for('supplier_detail', supplier_id=supplier_id))
        c.execute(
            "UPDATE suppliers SET email=?,phone=?,address=?,iban=?,bic=? WHERE id=? AND entity_id IS ?",
            ((request.form.get('email') or '').strip(), (request.form.get('phone') or '').strip(),
             (request.form.get('address') or '').strip(), iban,
             (request.form.get('bic') or '').upper().strip(), supplier_id, eid),
        )
        c.commit(); c.close()
        flash("Fournisseur mis à jour.")
        return redirect(url_for('supplier_detail', supplier_id=supplier_id))

    @app.post('/facturation/achats/importer-pdf')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_import_pdf():
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        uploaded=request.files.get('document')
        used_ai=False
        try:
            stored,mime=_save_purchase_document(uploaded)
            if not stored:
                raise ValueError("Sélectionnez un fichier PDF ou une photo.")
            path=_purchase_pdf_dir()/stored
            if mime=='application/pdf':
                try:
                    detected=_purchase_pdf_extract(path)
                except (ValueError,PyPdfError) as text_err:
                    if not is_pdf_text_unavailable(text_err) and not isinstance(text_err, PyPdfError):
                        raise
                    # PDF scanné sans texte, ou structurellement illisible par pypdf
                    # (fichier corrompu/tronqué) -> repli sur l'extraction IA, qui
                    # lit le rendu visuel du document sans dépendre de sa structure
                    # interne.
                    detected=_purchase_ai_extract(path.read_bytes(),mime)
                    used_ai=True
            else:
                detected=_purchase_ai_extract(path.read_bytes(),mime)
                used_ai=True
        except ValueError as e:
            if 'stored' in locals() and stored:
                try: (_purchase_pdf_dir()/stored).unlink(missing_ok=True)
                except OSError: pass
            flash(str(e))
            return redirect(url_for('purchase_new'))

        c=cx()
        suppliers=c.execute("SELECT * FROM suppliers WHERE entity_id IS ? ORDER BY name ASC",(eid,)).fetchall()
        open_orders=c.execute("SELECT id,order_number,supplier_name FROM purchase_orders WHERE status IN ('sent','partially_received','received') AND entity_id IS ? ORDER BY order_date DESC",(eid,)).fetchall()
        from profitos.entities import list_all_entities
        entities=list_all_entities(c)
        c.close()
        flash("Document analysé par IA. Vérifiez les informations avant d'enregistrer." if used_ai
              else "PDF analysé. Vérifiez les informations avant d'enregistrer.")
        return render_template('purchase_new.html',suppliers=suppliers,open_orders=open_orders,entities=entities,
                               detected=detected,pending_document=stored)

    @app.route('/facturation/achats/boite-mail')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_inbox_settings():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        token = get_or_create_supplier_inbox_token(session['org_id'], eid)
        domain = os.environ.get('SUPPLIER_INBOX_DOMAIN', 'achats.profitos.fr')
        c = cx()
        recent = c.execute(
            "SELECT * FROM purchase_invoices WHERE notes LIKE 'Reçu par email%%' AND entity_id IS ? ORDER BY id DESC LIMIT 20",
            (eid,),
        ).fetchall()
        c.close()
        return render_template('purchase_inbox_settings.html',
                                inbox_address=f"achats+{token}@{domain}", recent=recent)

    @app.route('/webhooks/supplier-inbox', methods=['POST'])
    def supplier_inbox_webhook():
        """Réception d'une facture fournisseur par email. Format générique attendu
        (à adapter précisément selon le fournisseur d'email entrant réellement
        configuré — Resend, Mailgun, SendGrid... les noms de champs exacts varient) :
        {"to": "achats+TOKEN@domaine", "from": "...", "subject": "...",
         "attachments": [{"filename": "...", "content_type": "...", "content": "<base64>"}]}
        Chaque pièce jointe PDF/image est passée par la même extraction que l'import
        manuel (texte natif d'abord, IA en repli), puis enregistrée en facture
        fournisseur EN ATTENTE DE VALIDATION — toujours, quel que soit le réglage de
        l'organisation, puisqu'aucun humain n'a encore vu ce document."""
        # Désactivé par défaut tant que le fournisseur d'email entrant n'est pas
        # explicitement configuré. Ce secret est une barrière ProfitOS additionnelle ;
        # il ne remplace pas la signature native du fournisseur (Resend/Mailgun/etc.).
        if os.environ.get('SUPPLIER_INBOX_WEBHOOK_ENABLED', '').strip().lower() not in ('1','true','yes','on'):
            return jsonify({'error': 'supplier inbox disabled'}), 503
        expected_secret = os.environ.get('SUPPLIER_INBOX_WEBHOOK_SECRET', '')
        supplied_secret = request.headers.get('X-ProfitOS-Inbox-Secret', '')
        if len(expected_secret) < 32 or not supplied_secret or not secrets.compare_digest(expected_secret, supplied_secret):
            return jsonify({'error': 'webhook authentication failed'}), 401

        payload = request.get_json(silent=True) or {}
        to_field = str(payload.get('to') or '')
        m = re.search(r'achats\+([a-z0-9]+)@', to_field, re.I)
        if not m:
            return jsonify({'error': 'destinataire non reconnu'}), 400
        token = m.group(1).lower()

        ac = auth_cx()
        mapping = ac.execute(
            'SELECT organization_id,entity_id FROM supplier_inbox_entity_tokens WHERE token=?', (token,)
        ).fetchone()
        ac.close()
        if not mapping:
            return jsonify({'error': 'jeton inconnu'}), 404
        org_id = mapping['organization_id']
        entity_id = mapping['entity_id']
        g.ai_quota_org_id = org_id  # quota IA décompté sur cette organisation (pas de session)

        attachments = payload.get('attachments') or []
        created, failed = [], []
        for att in attachments:
            filename = att.get('filename') or 'document'
            content_b64 = att.get('content') or ''
            try:
                data = base64.b64decode(content_b64)
            except Exception:
                failed.append(f"{filename} : pièce jointe illisible (base64 invalide)")
                continue
            if len(data) > _PURCHASE_PDF_MAX_BYTES:
                failed.append(f"{filename} : dépasse 5 Mo")
                continue
            if data.startswith(b"%PDF-"):
                mime, ext = 'application/pdf', '.pdf'
            elif data.startswith(b"\xff\xd8\xff"):
                mime, ext = 'image/jpeg', '.jpg'
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime, ext = 'image/png', '.png'
            else:
                failed.append(f"{filename} : format non reconnu (PDF/JPEG/PNG attendu)")
                continue

            pdf_dir = _purchase_pdf_dir_for_org(org_id)
            stored = uuid.uuid4().hex + ext
            (pdf_dir / stored).write_bytes(data)
            path = pdf_dir / stored

            try:
                if mime == 'application/pdf':
                    try:
                        detected = _purchase_pdf_extract(path)
                    except (ValueError, PyPdfError) as text_err:
                        if not is_pdf_text_unavailable(text_err) and not isinstance(text_err, PyPdfError):
                            raise
                        detected = _purchase_ai_extract(path.read_bytes(), mime)
                else:
                    detected = _purchase_ai_extract(path.read_bytes(), mime)
            except ValueError as e:
                path.unlink(missing_ok=True)
                failed.append(f"{filename} : {e}")
                continue

            sender = (payload.get('from') or '').strip()
            tc = tenant_cx_direct(org_id)
            tc.execute(
                """INSERT INTO purchase_invoices(
                     supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,
                     status,notes,created_at,document_path,category,validation_status,entity_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (detected['supplier_name'], detected['invoice_number'],
                 detected['issue_date'] or None, detected['due_date'] or None,
                 detected['subtotal'], detected['vat_amount'], detected['total'],
                 'unpaid', f"Reçu par email de {sender}" if sender else 'Reçu par email',
                 now(), stored, 'autre', 'pending', entity_id),
            )
            tc.commit()
            # Facture reçue par e-mail : en attente de validation, donc comptabilisée seulement
            # quand un utilisateur l'aura vérifiée et validée.
            tc.close()
            created.append(detected['invoice_number'])

        log_ops_event('SUPPLIER_INBOX_RECEIVED', outcome='INFO' if created else 'WARNING',
                       detail=f"org={org_id} entity={entity_id} créées={created} échecs={failed}")
        return jsonify({'created': created, 'failed': failed}), 200

    @app.route('/facturation/achats/weinvoice/synchroniser', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_weinvoice_sync():
        """Lot F1 — importe les factures électroniques INBOUND WeInvoice.

        Idempotence par (entity_id, eInvoicingId). Le document PDF lisible est
        conservé comme justificatif; l'écriture comptable est générée dans la
        même transaction que la facture locale.
        """
        eid=current_entity_id()
        c=cx()
        try:
            try:
                result=sync_weinvoice_inbound(c,eid,session['org_id'],max_items=WEINVOICE_MANUAL_SYNC_MAX)
            except WeInvoiceInboundNotConfigured:
                flash("Plateforme de facturation électronique non configurée pour cette entité.")
                return redirect(url_for('purchase_list'))
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                flash(str(exc)); return redirect(url_for('purchase_list'))
            created,skipped,ignored,failed=result['created'],result['skipped'],result['ignored'],result['failed']
            msg=f"Factures électroniques reçues : {created} importée(s), {skipped} déjà présente(s), {ignored} rejetée(s) ignorée(s), {len(failed)} échec(s)."
            if result['remaining']:
                msg+=f" Il reste {result['remaining']} facture(s) à importer : relancez la synchronisation ou attendez la synchronisation automatique (toutes les heures)."
            flash(msg)
            return redirect(url_for('purchase_list'))
        finally:
            c.close()

    @app.post('/facturation/achats/<int:purchase_id>/weinvoice/action')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_weinvoice_action(purchase_id):
        eid=current_entity_id(); c=cx()
        try:
            p=c.execute('SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?',(purchase_id,eid)).fetchone()
            if not p: abort(404)
            remote_id=p['weinvoice_invoice_id']
            if not remote_id:
                flash("Cette facture ne dispose pas de transmission électronique."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',(eid or 0,)).fetchone()
            if not settings or not settings['weinvoice_company_id']:
                flash("Plateforme de facturation électronique non configurée pour cette entité."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
            action=str(request.form.get('action') or '').strip()
            reason_code=str(request.form.get('reason_code') or '').strip()
            reason_label=str(request.form.get('reason_label') or '').strip()
            # WeInvoice n'accepte pas un motif libre comme reasonCode pour un refus ou un litige :
            # le code doit provenir du catalogue réglementaire (Annexe 7 / Annexe A).
            # Tant que ProfitOS n'expose pas ce catalogue dans l'UI, un motif libre est
            # transmis avec le code réglementaire AUTRE et conservé dans reasonLabel.
            if action in {'refuse','dispute'} and reason_code.upper() != 'AUTRE':
                free_reason=reason_code
                reason_code='AUTRE'
                if free_reason:
                    reason_label=(free_reason + (f' — {reason_label}' if reason_label else ''))[:2000]
            try:
                result=apply_inbound_lifecycle_action(settings['weinvoice_company_id'],remote_id,action,reason_code=reason_code,reason_label=reason_label)
                timeline=get_invoice_timeline(settings['weinvoice_company_id'],remote_id)
                status,cdv=invoice_status_from_timeline(timeline)
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                flash(str(exc)); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
            c.execute('UPDATE purchase_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=? WHERE id=? AND entity_id IS ?',
                      (str(status or result['status']),str(cdv if cdv is not None else result['regulatoryStatusCode']),now(),purchase_id,eid))
            c.execute("""INSERT OR IGNORE INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,regulatory_code,idempotency_key,detail,occurred_at)
                         VALUES(?,0,'weinvoice','purchase_buyer_action',?,?,?,?,?,?)""",
                      (eid,remote_id,str(status or result['status']),str(cdv if cdv is not None else result['regulatoryStatusCode']),f'buyer-{purchase_id}-{action}-{uuid.uuid4()}',action,now()))
            c.commit(); flash("Statut réglementaire de la facture fournisseur mis à jour.")
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        finally: c.close()

    @app.post('/facturation/achats/<int:purchase_id>/weinvoice/synchroniser')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_weinvoice_status_sync(purchase_id):
        eid=current_entity_id(); c=cx()
        try:
            p=c.execute('SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?',(purchase_id,eid)).fetchone()
            if not p: abort(404)
            if not p['weinvoice_invoice_id']:
                flash("Cette facture ne dispose pas de transmission électronique."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',(eid or 0,)).fetchone()
            if not settings or not settings['weinvoice_company_id']:
                flash("Plateforme de facturation électronique non configurée pour cette entité."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
            try: timeline=get_invoice_timeline(settings['weinvoice_company_id'],p['weinvoice_invoice_id']); status,cdv=invoice_status_from_timeline(timeline)
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc: flash(str(exc)); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
            c.execute('UPDATE purchase_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=? WHERE id=? AND entity_id IS ?', (str(status),str(cdv) if cdv is not None else None,now(),purchase_id,eid))
            c.commit(); flash("Statut de facturation électronique actualisé."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        finally: c.close()

    @app.route('/facturation/achats/nouvelle',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_new():
        eid=current_entity_id()
        c=cx()
        suppliers=c.execute("SELECT * FROM suppliers WHERE entity_id IS ? ORDER BY name ASC",(eid,)).fetchall()
        if request.method=='POST':
            supplier_id=request.form.get('supplier_id') or None
            supplier_name=(request.form.get('supplier_name') or '').strip()
            if supplier_id:
                s=c.execute("SELECT * FROM suppliers WHERE id=? AND entity_id IS ?",(supplier_id,eid)).fetchone()
                if s:
                    supplier_name=s['name']
            number=(request.form.get('invoice_number') or '').strip()
            pending_document=(request.form.get('pending_document') or '').strip()

            def _purchase_form_again(message):
                # Réaffiche le formulaire avec la saisie conservée (jamais de saisie perdue).
                from profitos.entities import list_all_entities
                detected={'supplier_name':supplier_name,'invoice_number':number,
                          'issue_date':request.form.get('issue_date') or '','due_date':request.form.get('due_date') or '',
                          'subtotal':amounts.parse_number(request.form.get('subtotal'),0.0) or 0.0,
                          'vat_amount':amounts.parse_number(request.form.get('vat_amount'),0.0) or 0.0}
                detected['total']=round(detected['subtotal']+detected['vat_amount'],2)
                open_orders=c.execute("SELECT id,order_number,supplier_name FROM purchase_orders WHERE status IN ('sent','partially_received','received') AND entity_id IS ? ORDER BY order_date DESC",(eid,)).fetchall()
                entities=list_all_entities(c)
                c.close(); flash(message)
                return render_template('purchase_new.html',suppliers=suppliers,categories=PURCHASE_CATEGORIES,open_orders=open_orders,
                                       entities=entities,detected=detected,pending_document=pending_document or None,
                                       duplicate_warning=message.startswith('Doublon')), 400
            try:
                issue_date=_purchase_form_date(request.form.get('issue_date'), "Date de facture")
                due_date=_purchase_form_date(request.form.get('due_date'), "Date d'échéance")
                subtotal=amounts.parse_number(request.form.get('subtotal'),None)
                vat=amounts.parse_number(request.form.get('vat_amount'),0.0)
                if subtotal is None or vat is None:
                    raise ValueError("Montants invalides.")
            except ValueError as exc:
                return _purchase_form_again(str(exc) if "date" in str(exc).lower() else "Montants invalides.")
            total=round(subtotal+vat,2)
            if not supplier_name or not number or subtotal < 0 or vat < 0:
                return _purchase_form_again("Fournisseur, numéro et montants valides sont obligatoires.")
            if not issue_date:
                return _purchase_form_again("La date de facture est obligatoire.")
            if due_date and due_date < issue_date:
                return _purchase_form_again("La date d'échéance ne peut pas être antérieure à la date de facture.")
            if not request.form.get('confirm_duplicate'):
                dup=c.execute("SELECT id,issue_date,total FROM purchase_invoices WHERE entity_id IS ? AND lower(supplier_name)=lower(?) AND lower(invoice_number)=lower(?) LIMIT 1",
                              (eid,supplier_name,number)).fetchone()
                if dup:
                    return _purchase_form_again(f"Doublon probable : la facture n° {number} de {supplier_name} est déjà enregistrée "
                                                f"(du {fr_date(dup['issue_date'])}, {fr_number(dup['total'],2)} € TTC). "
                                                "Cochez « Enregistrer malgré tout » si c'est bien une autre facture.")
            if pending_document and not re.fullmatch(r'[0-9a-f]{32}\.(pdf|jpg|png|webp)',pending_document):
                c.close(); abort(400)
            if pending_document and not (_purchase_pdf_dir()/pending_document).is_file():
                c.close(); flash("Le justificatif temporaire n'est plus disponible. Réimporte-le.")
                return redirect(url_for('purchase_new'))
            category=request.form.get('category','autre')
            if category not in PURCHASE_CATEGORY_LABELS: category='autre'
            po_id_raw = request.form.get('purchase_order_id')
            purchase_order_id = int(po_id_raw) if po_id_raw and po_id_raw.isdigit() else None
            entity_id_raw=request.form.get('entity_id')
            entity_id=int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None
            settings_row=c.execute('SELECT require_purchase_validation FROM app_settings WHERE id=1').fetchone()
            validation_status='pending' if (settings_row and settings_row['require_purchase_validation']) else 'approved'
            c.execute("""INSERT INTO purchase_invoices(
                         supplier_id,supplier_name,invoice_number,issue_date,due_date,
                         subtotal,vat_amount,total,status,notes,created_at,document_path,category,validation_status,purchase_order_id,entity_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (supplier_id,supplier_name,number,
                       issue_date,
                       due_date,
                       subtotal,vat,total,'unpaid',
                       (request.form.get('notes') or '').strip(),now(),pending_document or None,category,validation_status,purchase_order_id,entity_id))
            new_purchase_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            # Une facture en attente de validation n'est comptabilisée qu'une fois validée.
            # Facture + écriture sont enregistrées ensemble : si l'écriture est refusée
            # (période clôturée…), la facture n'est pas créée et l'utilisateur est prévenu.
            if validation_status=='approved':
                try:
                    purchase_row=c.execute('SELECT * FROM purchase_invoices WHERE id=?',(new_purchase_id,)).fetchone()
                    generate_purchase_entry(c,purchase_row,commit=False)
                except AccountingError as e:
                    c.rollback(); c.close()
                    log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"achat {number}: {e}")
                    flash(f"Facture fournisseur non enregistrée : {e}")
                    return redirect(url_for('purchase_new'))
            c.commit()
            deliver_webhook(c,'purchase.created',{'id':new_purchase_id,'invoice_number':number,
                'supplier_name':supplier_name,'total':total},entity_id=eid)
            c.close()
            flash("Facture fournisseur enregistrée.")
            return redirect(url_for('purchase_list'))
        open_orders=c.execute("SELECT id,order_number,supplier_name FROM purchase_orders WHERE status IN ('sent','partially_received','received') AND entity_id IS ? ORDER BY order_date DESC",(eid,)).fetchall()
        from profitos.entities import list_all_entities
        entities=list_all_entities(c)
        c.close()
        return render_template('purchase_new.html',suppliers=suppliers,categories=PURCHASE_CATEGORIES,open_orders=open_orders,entities=entities)

    @app.get('/facturation/fournisseurs/<int:supplier_id>')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_detail(supplier_id):
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        supplier=c.execute("SELECT * FROM suppliers WHERE id=? AND entity_id IS ?",(supplier_id,eid)).fetchone()
        if not supplier:
            c.close(); abort(404)
        purchases=c.execute("SELECT * FROM purchase_invoices WHERE supplier_id=? AND entity_id IS ? ORDER BY issue_date DESC,id DESC",(supplier_id,eid)).fetchall()
        total=sum(float(p['total'] or 0) for p in purchases)
        # Reste à payer réel : factures non soldées, règlements partiels déduits.
        unpaid=round(sum(_purchase_balance(c,p,eid) for p in purchases if p['status'] in ('unpaid','partially_paid')),2)
        c.close()
        return render_template('supplier_detail.html',supplier=supplier,purchases=purchases,total=total,unpaid=unpaid)


    @app.get('/facturation/dettes-fournisseurs')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_debts():
        today = paris_today()
        d7 = today + timedelta(days=7)
        d30 = today + timedelta(days=30)
        c = cx()
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        rows = c.execute("""
                SELECT id, supplier_id, supplier_name, invoice_number,
                       issue_date, due_date, total, status, created_at
                FROM purchase_invoices
                WHERE COALESCE(status, 'unpaid') != 'paid' AND entity_id IS ?
                ORDER BY CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
                         due_date ASC, id DESC
            """,(eid,)).fetchall()
        c.close()

        invoices = []
        total_due = overdue = due_7 = due_30 = 0.0
        for row in rows:
            inv = dict(row)
            amount = float(inv.get('total') or 0)
            total_due += amount
            due = None
            if inv.get('due_date'):
                try:
                    due = date.fromisoformat(str(inv['due_date'])[:10])
                except Exception:
                    pass
            inv['is_overdue'] = bool(due and due < today)
            inv['due_in_7'] = bool(due and today <= due <= d7)
            inv['due_in_30'] = bool(due and today <= due <= d30)
            if inv['is_overdue']:
                overdue += amount
            if inv['due_in_7']:
                due_7 += amount
            if inv['due_in_30']:
                due_30 += amount
            invoices.append(inv)

        return render_template(
            'supplier_debts.html',
            invoices=invoices,
            total_due=total_due,
            overdue=overdue,
            due_7=due_7,
            due_30=due_30,
        )


    @app.get('/facturation/previsions-paiements-fournisseurs')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_payment_forecast():
        today = paris_today()
        d7 = today + timedelta(days=7)
        d30 = today + timedelta(days=30)

        c = cx()
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        rows = c.execute("""
            SELECT id, supplier_id, supplier_name, invoice_number,
                   issue_date, due_date, total, status, created_at
            FROM purchase_invoices
            WHERE COALESCE(status, 'unpaid') != 'paid' AND entity_id IS ?
            ORDER BY
              CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
              due_date ASC, id DESC
        """,(eid,)).fetchall()
        c.close()

        overdue = []
        weeks_map = {}
        no_due_date = []
        supplier_map = {}
        total_forecast = 0.0
        due_7 = 0.0
        due_30 = 0.0

        for row in rows:
            inv = dict(row)
            amount = float(inv.get('total') or 0)
            due = None
            if inv.get('due_date'):
                try:
                    due = date.fromisoformat(str(inv['due_date'])[:10])
                except Exception:
                    due = None

            inv['amount'] = amount
            inv['due_date_obj'] = due
            inv['alert'] = None

            supplier_name = (inv.get('supplier_name') or 'Fournisseur sans nom').strip()
            agg = supplier_map.setdefault(
                supplier_name,
                {'supplier_name': supplier_name, 'total': 0.0, 'count': 0}
            )
            agg['total'] += amount
            agg['count'] += 1

            if due is None:
                no_due_date.append(inv)
                continue

            total_forecast += amount

            if due < today:
                inv['alert'] = 'En retard'
                overdue.append(inv)
                continue

            if due <= d7:
                due_7 += amount
                inv['alert'] = 'Échéance proche'
            if due <= d30:
                due_30 += amount

            week_start = due - timedelta(days=due.weekday())
            week_end = week_start + timedelta(days=6)
            key = week_start.isoformat()
            bucket = weeks_map.setdefault(
                key,
                {
                    'week_start': week_start,
                    'week_end': week_end,
                    'total': 0.0,
                    'invoices': [],
                }
            )
            bucket['total'] += amount
            bucket['invoices'].append(inv)

        weeks = [weeks_map[k] for k in sorted(weeks_map)]
        suppliers = sorted(supplier_map.values(), key=lambda x: x['total'], reverse=True)

        return render_template(
            'supplier_payment_forecast.html',
            today=today,
            overdue=overdue,
            weeks=weeks,
            no_due_date=no_due_date,
            suppliers=suppliers,
            total_forecast=total_forecast,
            due_7=due_7,
            due_30=due_30,
        )

    @app.get('/facturation/achats/<int:purchase_id>')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_detail(purchase_id):
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
        if not p:
            c.close(); abort(404)
        supplier=None
        if p['supplier_id']:
            supplier=c.execute("SELECT * FROM suppliers WHERE id=? AND entity_id IS ?",(p['supplier_id'],eid)).fetchone()
        einvoice_events=[]
        if p['weinvoice_invoice_id']:
            einvoice_events=c.execute("SELECT * FROM einvoice_events WHERE entity_id IS ? AND remote_id=? ORDER BY id DESC LIMIT 50",(eid,p['weinvoice_invoice_id'])).fetchall()
        c.close()
        return render_template('purchase_detail.html',p=p,supplier=supplier,einvoice_events=einvoice_events,today=paris_today().isoformat(),
                               payment_hold=_einvoice_hold(p))

    @app.route('/facturation/achats/<int:purchase_id>/modifier',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_edit(purchase_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
        if not p:
            c.close(); abort(404)
        if p['status']!='unpaid':
            c.close()
            flash("Une facture fournisseur payée est verrouillée.")
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        suppliers=c.execute("SELECT * FROM suppliers WHERE entity_id IS ? ORDER BY name ASC",(eid,)).fetchall()
        if request.method=='POST':
            supplier_id=request.form.get('supplier_id') or None
            supplier_name=(request.form.get('supplier_name') or '').strip()
            if supplier_id:
                s=c.execute("SELECT * FROM suppliers WHERE id=? AND entity_id IS ?",(supplier_id,eid)).fetchone()
                if s:
                    supplier_name=s['name']
            number=(request.form.get('invoice_number') or '').strip()
            try:
                issue_date=_purchase_form_date(request.form.get('issue_date'), "Date de facture")
                due_date=_purchase_form_date(request.form.get('due_date'), "Date d'échéance")
                subtotal=form_float(request.form.get('subtotal'))
                vat=form_float(request.form.get('vat_amount'))
            except ValueError as exc:
                c.close()
                flash(str(exc) if "date" in str(exc).lower() else "Montants invalides.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            if not supplier_name or not number or subtotal<0 or vat<0:
                c.close(); flash("Fournisseur, numéro et montants valides sont obligatoires.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            if not issue_date:
                c.close(); flash("La date de facture est obligatoire.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            if due_date and due_date<issue_date:
                c.close(); flash("La date d'échéance ne peut pas être antérieure à la date de facture.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            category=request.form.get('category','autre')
            if category not in PURCHASE_CATEGORY_LABELS: category='autre'
            c.execute("""UPDATE purchase_invoices SET supplier_id=?,supplier_name=?,invoice_number=?,
                         issue_date=?,due_date=?,subtotal=?,vat_amount=?,total=?,notes=?,category=? WHERE id=? AND entity_id IS ?""",
                      (supplier_id,supplier_name,number,issue_date,
                       due_date,subtotal,vat,round(subtotal+vat,2),
                       (request.form.get('notes') or '').strip(),category,purchase_id,eid))
            accounting_changed=any(str(p[k] or '')!=str(v or '') for k,v in (
                ('supplier_name',supplier_name),('invoice_number',number),('issue_date',issue_date),('category',category))) \
                or abs(float(p['subtotal'] or 0)-subtotal)>0.004 or abs(float(p['vat_amount'] or 0)-vat)>0.004
            needs_validation=False
            if accounting_changed:
                settings_row=c.execute('SELECT require_purchase_validation FROM app_settings WHERE id=1').fetchone()
                # Circuit de validation : une facture validée puis modifiée par quelqu'un qui
                # n'a pas le droit de valider repart en attente (sinon le montant validé
                # pouvait passer de 120 € à 90 000 € en restant « approuvé »).
                needs_validation=bool(settings_row and settings_row['require_purchase_validation']) \
                    and current_role() not in ('OWNER','ADMIN','COMPTABLE')
                try:
                    reverse_purchase_entry(c,p,commit=False)
                    if needs_validation:
                        c.execute("UPDATE purchase_invoices SET validation_status='pending',validated_by=NULL,validated_at=NULL WHERE id=? AND entity_id IS ?",(purchase_id,eid))
                    elif (p['validation_status'] or 'approved')=='approved':
                        updated=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
                        generate_purchase_entry(c,updated,commit=False)
                except AccountingError as e:
                    c.rollback(); c.close()
                    flash(f"Modification refusée : {e}")
                    return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            c.commit(); c.close()
            flash("Facture fournisseur mise à jour." + (" Elle doit être validée à nouveau." if needs_validation else ""))
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        c.close()
        return render_template('purchase_edit.html',p=p,suppliers=suppliers,categories=PURCHASE_CATEGORIES)

    @app.post('/facturation/achats/<int:purchase_id>/justificatif')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_document_upload(purchase_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
        if not p:
            c.close(); abort(404)
        try:
            stored=_save_purchase_pdf(request.files.get('document'))
        except ValueError as e:
            c.close()
            flash(str(e))
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        if not stored:
            c.close()
            flash("Sélectionnez un fichier PDF.")
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))

        old=p['document_path']
        c.execute("UPDATE purchase_invoices SET document_path=? WHERE id=? AND entity_id IS ?",(stored,purchase_id,eid))
        c.commit(); c.close()

        if old and old != stored:
            try:
                (_purchase_pdf_dir() / old).unlink(missing_ok=True)
            except OSError:
                pass
        flash("Justificatif PDF enregistré.")
        return redirect(url_for('purchase_detail',purchase_id=purchase_id))

    @app.get('/facturation/achats/<int:purchase_id>/justificatif')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_document_view(purchase_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
        c.close()
        if not p or not p['document_path']:
            abort(404)

        # document_path contains only a generated UUID filename, never a user path.
        path=_purchase_pdf_dir() / p['document_path']
        if not path.is_file():
            abort(404)
        data=path.read_bytes()
        ext=path.suffix.lower()
        mime={'.pdf':'application/pdf','.jpg':'image/jpeg','.png':'image/png','.webp':'image/webp'}.get(ext,'application/pdf')
        response=Response(data,mimetype=mime)
        safe_number=re.sub(r'[^A-Za-z0-9._-]+','-',p['invoice_number'] or 'facture')
        response.headers['Content-Disposition']=f'inline; filename="justificatif-{safe_number}{ext or ".pdf"}"'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def _purchase_paid_total(conn, purchase_id, entity_id):
        row=conn.execute("SELECT COALESCE(SUM(amount),0) AS total FROM purchase_invoice_payments WHERE purchase_invoice_id=? AND entity_id IS ?",(purchase_id,entity_id)).fetchone()
        return round(float(row['total'] or 0),2)

    def _purchase_balance(conn, purchase, entity_id):
        return max(0.0, round(float(purchase['total'] or 0)-_purchase_paid_total(conn,purchase['id'],entity_id),2))

    @app.post('/facturation/achats/<int:purchase_id>/payer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_mark_paid(purchase_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
        if not p: c.close(); abort(404)
        if p['validation_status'] in ('pending','rejected'):
            c.close(); flash("Cette facture doit être validée avant paiement."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        balance=_purchase_balance(c,p,eid)
        if balance <= .005:
            c.close(); flash("Cette facture fournisseur est déjà soldée."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        payment_date=(request.form.get('payment_date') or paris_today().isoformat()).strip()
        try:
            date.fromisoformat(payment_date)
        except ValueError:
            c.close(); flash("Date de paiement invalide."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        if payment_date>paris_today().isoformat():
            c.close(); flash("La date de paiement ne peut pas être dans le futur."); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        key=f"manual-full:{eid}:{purchase_id}:{balance:.2f}"
        try:
            c.execute("INSERT INTO purchase_invoice_payments(entity_id,purchase_invoice_id,amount,payment_date,payment_method,reference,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",(eid,purchase_id,balance,payment_date,'manual','Solde manuel',key,now()))
            payment=c.execute("SELECT * FROM purchase_invoice_payments WHERE entity_id IS ? AND idempotency_key=?",(eid,key)).fetchone()
            generate_purchase_partial_payment_entry(c,p,payment)
            c.execute("UPDATE purchase_invoices SET status='paid',paid_at=? WHERE id=? AND entity_id IS ?",(now(),purchase_id,eid))
            c.commit()
        except (AccountingError, *dbmod.IntegrityError) as e:
            c.rollback(); c.close(); flash(f"Paiement non enregistré : {e}"); return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        c.close(); flash("Solde fournisseur enregistré."); return redirect(url_for('purchase_list'))

    @app.route('/facturation/achats/virements', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_sepa_batch():
        from profitos.sepa import generate_sepa_xml, validate_iban
        from profitos.entities import resolve_entity, list_all_entities
        c = cx()

        entity_id_raw = request.values.get('entity_id')
        if entity_id_raw is None:
            # Aucun paramètre du tout (premier chargement de la page) -> utilise
            # l'entité active du sélecteur permanent comme point de départ.
            # Un entity_id_raw vide ("") signifie un choix explicite de la
            # société mère et est respecté tel quel, jamais réécrit.
            from profitos.entities import current_entity_id as _current_entity_id
            entity_id = _current_entity_id()
        else:
            entity_id = int(entity_id_raw) if entity_id_raw.isdigit() else None
        try:
            debtor_identity = resolve_entity(c, entity_id)
        except ValueError:
            c.close()
            flash("Entité sélectionnée introuvable.")
            return redirect(url_for('purchase_sepa_batch'))

        if request.method == 'POST':
            selected_ids = [int(x) for x in request.form.getlist('purchase_id')]
            if len(selected_ids) == 0:
                c.close()
                flash("Sélectionnez au moins une facture à inclure dans le virement groupé.")
                return redirect(url_for('purchase_sepa_batch', entity_id=entity_id or ''))
            placeholders = ','.join('?' * len(selected_ids))
            # Filtre aussi sur l'entité choisie — une remise SEPA n'a qu'un seul débiteur (compte
            # bancaire), on ne peut donc jamais y mélanger des factures d'une autre entité, même si
            # un client mal formé essayait de le forcer.
            entity_filter = 'p.entity_id=?' if entity_id else 'p.entity_id IS NULL'
            rows = c.execute(
                f"""SELECT p.*, s.iban AS supplier_iban, s.bic AS supplier_bic
                    FROM purchase_invoices p LEFT JOIN suppliers s ON s.id = p.supplier_id
                    WHERE p.id IN ({placeholders}) AND p.status='unpaid' AND {entity_filter}
                      AND (p.validation_status IS NULL OR p.validation_status='approved')""",
                selected_ids + ([entity_id] if entity_id else []),
            ).fetchall()
            # Une facture « refusée », « en litige », « suspendue » ou « rejetée » côté facturation
            # électronique ne part jamais dans un fichier de virement, même si un client mal formé la
            # sélectionne : elle est écartée ici et l'utilisateur en est informé.
            held_rows = [(r, _einvoice_hold(r)) for r in rows if _einvoice_hold(r)]
            if held_rows:
                rows = [r for r in rows if not _einvoice_hold(r)]
                flash("Non incluses dans le virement (statut de facturation électronique) : "
                      + ", ".join(f"{r['invoice_number']} ({hold})" for r, hold in held_rows) + ".")
            # Le virement porte sur le solde restant, jamais sur le total historique
            # de la facture (une facture peut déjà avoir reçu un paiement partiel).
            payable_rows = []
            payments = []
            for r in rows:
                balance = _purchase_balance(c, r, entity_id)
                if balance <= .005:
                    continue
                payable_rows.append((r, balance))
                payments.append({
                    'supplier_name': r['supplier_name'], 'iban': r['supplier_iban'], 'bic': r['supplier_bic'],
                    'amount': balance, 'reference': r['invoice_number'],
                })
            try:
                xml_content, msg_id, total = generate_sepa_xml(debtor_identity, payments)
            except ValueError as e:
                c.close()
                flash(f"Impossible de générer le fichier SEPA : {e}")
                return redirect(url_for('purchase_sepa_batch', entity_id=entity_id or ''))

            # Un export pain.001 est une instruction préparée, pas la preuve qu'un
            # virement a été accepté/exécuté par la banque. On trace donc le lot
            # sans créer de paiement ni d'écriture comptable.
            import hashlib
            file_sha256 = hashlib.sha256(xml_content.encode('utf-8')).hexdigest()
            try:
                c.execute("""INSERT INTO sepa_export_batches
                             (entity_id,message_id,execution_date,total_amount,payment_count,status,file_sha256,created_at,created_by)
                             VALUES(?,?,?,?,?,'exported',?,?,?)""",
                          (entity_id,msg_id,paris_today().isoformat(),total,len(payable_rows),file_sha256,now(),session.get('user_email')))
                batch = c.execute("SELECT id FROM sepa_export_batches WHERE entity_id IS ? AND message_id=?",
                                  (entity_id,msg_id)).fetchone()
                if not batch:
                    raise AccountingError("Lot SEPA introuvable après création.")
                for r, balance in payable_rows:
                    c.execute("""INSERT INTO sepa_export_items
                                 (batch_id,entity_id,purchase_invoice_id,amount,supplier_name,invoice_number,created_at)
                                 VALUES(?,?,?,?,?,?,?)""",
                              (batch['id'],entity_id,r['id'],balance,r['supplier_name'],r['invoice_number'],now()))
                c.commit()
            except (AccountingError, *dbmod.IntegrityError) as e:
                c.rollback(); c.close()
                log_ops_event('SEPA_EXPORT_FAILED', outcome='ERROR', detail=f"lot {msg_id}: {e}")
                flash(f"Export SEPA interrompu : {e}")
                return redirect(url_for('purchase_sepa_batch', entity_id=entity_id or ''))
            c.close()
            log_activity('SEPA_BATCH_GENERATED', f"Lot SEPA {msg_id} ({debtor_identity['name']}) : {len(payable_rows)} virement(s), {fr_number(total, 2)} €")
            filename = f"virements_{paris_today().isoformat()}.xml"
            return Response(
                xml_content.encode('utf-8'), mimetype='application/xml',
                headers={'Content-Disposition': f'attachment; filename="{filename}"'},
            )

        entity_filter = 'p.entity_id=?' if entity_id else 'p.entity_id IS NULL'
        candidates = c.execute(
            f"""SELECT p.*, s.iban AS supplier_iban, s.bic AS supplier_bic
               FROM purchase_invoices p LEFT JOIN suppliers s ON s.id = p.supplier_id
               WHERE p.status='unpaid' AND {entity_filter}
                 AND (p.validation_status IS NULL OR p.validation_status='approved')
               ORDER BY p.due_date""",
            ([entity_id] if entity_id else []),
        ).fetchall()
        ready, blocked, held = [], [], []
        for r in candidates:
            hold = _einvoice_hold(r)
            if hold:
                held.append((r, hold))
                continue
            if r['supplier_iban'] and validate_iban(r['supplier_iban']) and r['supplier_bic']:
                ready.append(r)
            else:
                blocked.append(r)
        company_ready = bool(debtor_identity['iban'] and validate_iban(debtor_identity['iban']) and debtor_identity['bic'])
        entities = list_all_entities(c)
        c.close()
        return render_template('purchase_sepa_batch.html', ready=ready, blocked=blocked, held=held,
                                company_ready=company_ready, company=debtor_identity,
                                entities=entities, current_entity_id=entity_id)

    @app.route('/facturation/commandes')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_orders_list():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        orders = c.execute("SELECT * FROM purchase_orders WHERE entity_id IS ? ORDER BY order_date DESC,id DESC", (eid,)).fetchall()
        totals = {}
        for o in orders:
            t = c.execute(
                "SELECT COALESCE(SUM(quantity*unit_price),0) t FROM purchase_order_lines WHERE order_id=?", (o['id'],)
            ).fetchone()['t']
            totals[o['id']] = t
        c.close()
        return render_template('purchase_orders_list.html', orders=orders, totals=totals)

    @app.route('/facturation/commandes/nouvelle', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_order_new():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        if request.method == 'POST':
            supplier_id = request.form.get('supplier_id') or None
            supplier_name = (request.form.get('supplier_name') or '').strip()
            order_date = request.form.get('order_date') or paris_today().isoformat()
            expected_delivery_date = request.form.get('expected_delivery_date') or None
            descriptions = request.form.getlist('description')
            quantities = request.form.getlist('quantity')
            unit_prices = request.form.getlist('unit_price')

            if not supplier_name:
                c.close()
                flash("Le nom du fournisseur est obligatoire.")
                return redirect(url_for('purchase_order_new'))
            lines = []
            for i, desc in enumerate(descriptions):
                desc = (desc or '').strip()
                if not desc:
                    continue
                try:
                    qty = float(quantities[i]) if i < len(quantities) else 0
                    price = float(unit_prices[i]) if i < len(unit_prices) else 0
                except (ValueError, IndexError):
                    qty = price = 0
                if qty <= 0:
                    continue
                lines.append((desc, qty, price))
            if not lines:
                c.close()
                flash("Ajoutez au moins une ligne avec une quantité positive.")
                return redirect(url_for('purchase_order_new'))

            year = paris_today().year
            prefix = f"BC-E{eid}-{year}-" if eid else f"BC-{year}-"
            seq = c.execute(
                "SELECT COUNT(*) n FROM purchase_orders WHERE order_number LIKE ? AND entity_id IS ?", (prefix + '%', eid)
            ).fetchone()['n'] + 1
            order_number = f"{prefix}{seq:04d}"
            c.execute(
                """INSERT INTO purchase_orders
                   (supplier_id,supplier_name,order_number,order_date,expected_delivery_date,status,notes,created_at,created_by,entity_id)
                   VALUES(?,?,?,?,?,'draft',?,?,?,?)""",
                (supplier_id, supplier_name, order_number, order_date, expected_delivery_date,
                 (request.form.get('notes') or '').strip(), now(), current_user()['email'], eid),
            )
            c.commit()
            order_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            for i, (desc, qty, price) in enumerate(lines):
                c.execute(
                    "INSERT INTO purchase_order_lines(order_id,description,quantity,unit_price,line_order) VALUES(?,?,?,?,?)",
                    (order_id, desc, qty, price, i),
                )
            c.commit(); c.close()
            flash(f"Bon de commande {order_number} créé.")
            return redirect(url_for('purchase_order_detail', order_id=order_id))

        suppliers = c.execute("SELECT id,name FROM suppliers WHERE entity_id IS ? ORDER BY name", (eid,)).fetchall()
        c.close()
        return render_template('purchase_order_new.html', suppliers=suppliers)

    @app.route('/facturation/commandes/<int:order_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_order_detail(order_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        order = c.execute("SELECT * FROM purchase_orders WHERE id=? AND entity_id IS ?", (order_id, eid)).fetchone()
        if not order:
            c.close(); abort(404)
        lines = c.execute(
            "SELECT * FROM purchase_order_lines WHERE order_id=? ORDER BY line_order", (order_id,)
        ).fetchall()
        total = sum((l['quantity'] or 0) * (l['unit_price'] or 0) for l in lines)
        linked_invoices = c.execute(
            "SELECT * FROM purchase_invoices WHERE purchase_order_id=? AND entity_id IS ? ORDER BY id DESC", (order_id, eid)
        ).fetchall()
        c.close()
        return render_template('purchase_order_detail.html', order=order, lines=lines, total=total,
                                linked_invoices=linked_invoices)

    @app.route('/facturation/commandes/<int:order_id>/envoyer', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_order_send(order_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        order = c.execute("SELECT status FROM purchase_orders WHERE id=? AND entity_id IS ?", (order_id, eid)).fetchone()
        if not order:
            c.close(); abort(404)
        if order['status'] == 'draft':
            c.execute("UPDATE purchase_orders SET status='sent' WHERE id=? AND entity_id IS ?", (order_id, eid))
            c.commit()
            log_activity('PURCHASE_ORDER_SENT', f"Bon de commande #{order_id} envoyé")
            flash("Bon de commande marqué comme envoyé.")
        c.close()
        return redirect(url_for('purchase_order_detail', order_id=order_id))

    @app.route('/facturation/commandes/<int:order_id>/reception', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_order_receive(order_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        order = c.execute("SELECT * FROM purchase_orders WHERE id=? AND entity_id IS ?", (order_id, eid)).fetchone()
        if not order:
            c.close(); abort(404)
        if order['status'] not in ('sent', 'partially_received'):
            c.close()
            flash("Seul un bon de commande envoyé peut recevoir une livraison.")
            return redirect(url_for('purchase_order_detail', order_id=order_id))
        lines = c.execute("SELECT * FROM purchase_order_lines WHERE order_id=?", (order_id,)).fetchall()
        for l in lines:
            key = f"received_{l['id']}"
            if key in request.form:
                try:
                    received = form_float(request.form.get(key))
                except ValueError:
                    received = l['quantity_received']
                received = max(0.0, min(received, l['quantity']))
                c.execute("UPDATE purchase_order_lines SET quantity_received=? WHERE id=?", (received, l['id']))
        c.commit()
        updated_lines = c.execute("SELECT quantity,quantity_received FROM purchase_order_lines WHERE order_id=?", (order_id,)).fetchall()
        fully_received = all(l['quantity_received'] >= l['quantity'] for l in updated_lines)
        any_received = any(l['quantity_received'] > 0 for l in updated_lines)
        new_status = 'received' if fully_received else ('partially_received' if any_received else order['status'])
        c.execute("UPDATE purchase_orders SET status=? WHERE id=? AND entity_id IS ?", (new_status, order_id, eid))
        c.commit(); c.close()
        log_activity('PURCHASE_ORDER_RECEIVED', f"Réception enregistrée pour le bon de commande #{order_id}")
        flash("Quantités reçues enregistrées.")
        return redirect(url_for('purchase_order_detail', order_id=order_id))

    @app.route('/facturation/commandes/<int:order_id>/annuler', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_order_cancel(order_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        order = c.execute("SELECT status FROM purchase_orders WHERE id=? AND entity_id IS ?", (order_id, eid)).fetchone()
        if not order:
            c.close(); abort(404)
        if order['status'] in ('received',):
            c.close()
            flash("Un bon de commande déjà reçu ne peut plus être annulé.")
            return redirect(url_for('purchase_order_detail', order_id=order_id))
        c.execute("UPDATE purchase_orders SET status='cancelled' WHERE id=? AND entity_id IS ?", (order_id, eid))
        c.commit(); c.close()
        flash("Bon de commande annulé.")
        return redirect(url_for('purchase_order_detail', order_id=order_id))

    @app.route('/facturation/achats/<int:purchase_id>/valider', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_validate(purchase_id):
        if current_role() not in ('OWNER', 'ADMIN', 'COMPTABLE'):
            flash("Seul un propriétaire, administrateur ou comptable peut valider une facture fournisseur.")
            return redirect(url_for('purchase_detail', purchase_id=purchase_id))
        validator = current_user()
        validator_email = validator['email'] if validator else None
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        p = c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?", (purchase_id, eid)).fetchone()
        if not p:
            c.close(); abort(404)
        if p['validation_status'] != 'pending':
            c.close()
            flash("Cette facture n'est pas en attente de validation.")
            return redirect(url_for('purchase_detail', purchase_id=purchase_id))
        c.execute(
            "UPDATE purchase_invoices SET validation_status='approved',validated_by=?,validated_at=?,rejection_reason=NULL WHERE id=? AND entity_id IS ?",
            (validator_email, now(), purchase_id, eid),
        )
        try:
            approved=c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?",(purchase_id,eid)).fetchone()
            generate_purchase_entry(c, approved, commit=False)
        except AccountingError as e:
            c.rollback(); c.close()
            flash(f"Validation impossible : {e}")
            return redirect(url_for('purchase_detail', purchase_id=purchase_id))
        c.commit(); c.close()
        log_activity('PURCHASE_VALIDATED', f"Facture fournisseur #{purchase_id} validée par {validator_email}")
        flash("Facture validée — elle peut maintenant être marquée comme payée.")
        return redirect(url_for('purchase_detail', purchase_id=purchase_id))

    @app.route('/facturation/achats/<int:purchase_id>/rejeter', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def purchase_reject(purchase_id):
        if current_role() not in ('OWNER', 'ADMIN', 'COMPTABLE'):
            flash("Seul un propriétaire, administrateur ou comptable peut rejeter une facture fournisseur.")
            return redirect(url_for('purchase_detail', purchase_id=purchase_id))
        validator = current_user()
        validator_email = validator['email'] if validator else None
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        p = c.execute("SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?", (purchase_id, eid)).fetchone()
        if not p:
            c.close(); abort(404)
        if p['validation_status'] != 'pending':
            c.close()
            flash("Cette facture n'est pas en attente de validation.")
            return redirect(url_for('purchase_detail', purchase_id=purchase_id))
        reason = (request.form.get('rejection_reason') or '').strip()
        c.execute(
            "UPDATE purchase_invoices SET validation_status='rejected',validated_by=?,validated_at=?,rejection_reason=? WHERE id=? AND entity_id IS ?",
            (validator_email, now(), reason or None, purchase_id, eid),
        )
        try:
            # Factures créées avant v390 : déjà comptabilisées à l'enregistrement -> extourne.
            reverse_purchase_entry(c, p, commit=False)
        except AccountingError as e:
            c.rollback(); c.close()
            flash(f"Rejet impossible : {e}")
            return redirect(url_for('purchase_detail', purchase_id=purchase_id))
        c.commit(); c.close()
        log_activity('PURCHASE_REJECTED', f"Facture fournisseur #{purchase_id} rejetée par {validator_email}")
        flash("Facture rejetée.")
        return redirect(url_for('purchase_detail', purchase_id=purchase_id))

    @app.route('/facturation')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_list():
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        rows=c.execute('SELECT * FROM outgoing_invoices WHERE entity_id IS ? ORDER BY id DESC',(eid,)).fetchall()
        ereporting_rows=c.execute("""SELECT id,transmission_number,flow_type,anchor_date,type_code,provider_reference,status,
            late_deposit,flux_status,rejection_motifs_json,last_error,submitted_at,last_checked_at,created_at
            FROM ereporting_transmissions WHERE entity_id IS ? ORDER BY id DESC LIMIT 25""",(eid,)).fetchall()
        # Tuiles « à encaisser » / « en retard » : reste dû réel (règlements et avoirs
        # déduits), factures importées comprises — mêmes chiffres que Créances et l'accueil.
        from profitos.receivables import open_receivables, summarize
        try:
            recv=summarize(open_receivables(c,current=True))
        finally:
            c.close()
        totals={'draft':0,'sent':0,'overdue':0,'paid':0,'cancelled':0}
        display_statuses={}
        for r in rows:
            status=_display_status(r)
            display_statuses[r['id']]=status
            if status in ('draft','paid','cancelled'): totals[status]+=r['total'] or 0
        totals['overdue']=recv['overdue_due']
        totals['sent']=round(recv['total_due']-recv['overdue_due'],2)
        totals['imported_due']=recv['imported_not_due']; totals['imported_overdue']=recv['imported_overdue']
        return render_template('invoicing_list.html',rows=rows,totals=totals,display_statuses=display_statuses,ereporting_rows=ereporting_rows)

    @app.route('/facturation/nouvelle',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_new():
        c=cx()
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        if not company_row or not company_row['name']:
            c.close()
            flash("Complétez d'abord votre profil entreprise (nom, adresse, SIRET) avant de créer une facture.")
            return redirect(url_for('company'))
        clients=c.execute("SELECT * FROM invoicing_clients ORDER BY lower(name)").fetchall()
        from profitos.entities import list_all_entities
        entities=list_all_entities(c)

        def _form_again(values, message, items=None):
            flash(message)
            c.close()
            return render_template('invoicing_new.html',company=company_row,today=paris_today().isoformat(),clients=clients,
                                   entities=entities,f=values,items=items or [],vat_treatment_labels=VAT_TREATMENT_LABELS), 400

        if request.method=='POST':
            header,error=_parse_invoice_header(request.form,paris_today().isoformat())
            header['notes']=(request.form.get('notes') or '').strip()
            items=_compute_line_items(request.form)
            if error:
                return _form_again(header,error,items)
            entity_id_raw=request.form.get('entity_id')
            entity_id=int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None
            if entity_id is not None:
                from profitos.entities import resolve_entity, user_can_access_entity
                try:
                    resolve_entity(c,entity_id)
                except ValueError:
                    return _form_again(header,"Entité sélectionnée introuvable.",items)
                if not user_can_access_entity(c, session.get('user_id'), entity_id):
                    c.close()
                    abort(403)
            if not header['client_name'] or not items:
                return _form_again(header,'Nom du client et au moins une ligne de facture requis.',items)
            items_error=_line_items_error(items)
            if items_error:
                return _form_again(header,items_error,items)
            subtotal,vat_amount,total=_totals(items)
            token=secrets.token_urlsafe(20)
            # Brouillon SANS numéro : le numéro légal est attribué à l'émission (série
            # continue, aucun numéro consommé par un brouillon annulé).
            cols=list(INVOICE_HEADER_COLUMNS)+['notes','line_items','subtotal','vat_amount','total','status',
                  'public_token','created_at','entity_id','issue_date']
            vals=[header[k] for k in INVOICE_HEADER_COLUMNS]+[header['notes'],json.dumps(items,ensure_ascii=False),
                  subtotal,vat_amount,total,'draft',token,now(),entity_id,paris_today().isoformat()]
            c.execute(f"INSERT INTO outgoing_invoices({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",vals)
            new_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.commit()
            c.close()
            log_activity('INVOICE_CREATED',f'Facture brouillon n°{new_id} créée ({fr_number(total,2)} € TTC)')
            flash(f'Facture créée en brouillon (Brouillon n°{new_id}). Le numéro définitif sera attribué à l\'émission.')
            # Facture émise pour une autre entité que l'entité active : on bascule sur
            # cette entité pour que la facture reste visible (sinon 404 sur sa fiche).
            if 'entity_id' in request.form:
                if entity_id: session['current_entity_id']=entity_id
                else: session.pop('current_entity_id',None)
            return redirect(url_for('invoicing_detail',invoice_id=new_id))

        defaults={'operation_nature':'services','vat_treatment':'standard',
                  'vat_on_debits':1 if str(_g(company_row,'vat_on_debits','0') or '0')=='1' else 0}
        c.close()
        return render_template('invoicing_new.html',company=company_row,today=paris_today().isoformat(),clients=clients,
                               entities=entities,f=defaults,items=[],vat_treatment_labels=VAT_TREATMENT_LABELS)

    @app.route('/facturation/<int:invoice_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_detail(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        company_row=_seller_identity(c,inv['entity_id'])
        items=json.loads(inv['line_items'] or '[]')
        mention_checks,missing_mentions=_check_mandatory_mentions(inv,company_row)
        emitter_missing_count=sum(1 for m in mention_checks if not m['ok'] and m['scope']=='seller')
        credits=c.execute("SELECT * FROM outgoing_credit_notes WHERE original_invoice_id=? AND entity_id IS ? ORDER BY id DESC",(invoice_id,inv['entity_id'])).fetchall()
        credited_total=_credited_total(c,invoice_id,inv['entity_id'])
        reminders=c.execute("SELECT * FROM invoice_reminders WHERE invoice_id=? AND entity_id IS ? ORDER BY reminder_number DESC",(invoice_id,inv['entity_id'])).fetchall()
        payments=c.execute("SELECT * FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ? ORDER BY payment_date,id",(invoice_id,inv['entity_id'])).fetchall()
        paid_total=_invoice_paid_total(c,invoice_id,inv['entity_id'])
        balance_due=max(0.0,round(float(inv['total'] or 0)-paid_total-credited_total,2))
        einvoice_events=c.execute("SELECT * FROM einvoice_events WHERE invoice_id=? AND entity_id IS ? ORDER BY id DESC LIMIT 50",(invoice_id,inv['entity_id'])).fetchall()
        c.close()
        issued=inv['status'] in ('sent','partially_paid','paid')
        public_url=f"{app_base_url()}{url_for('public_invoice_view',token=inv['public_token'])}" if issued and inv['public_token'] else None
        return render_template('invoicing_detail.html',inv=inv,items=items,display_status=_display_status(inv),
                               einvoice_hold=_einvoice_hold(inv),
                               credits=credits,credited_total=credited_total,reminders=reminders,payments=payments,
                               paid_total=paid_total,balance_due=balance_due,
                               creditable_total=max(0,float(inv['total'] or 0)-credited_total),
                               mention_checks=mention_checks,missing_mentions=missing_mentions,
                               emitter_missing_count=emitter_missing_count,einvoice_events=einvoice_events,
                               vat_breakdown=amounts.vat_breakdown(items),public_url=public_url,
                               is_public_buyer=_is_public_buyer(inv),today=paris_today().isoformat(),
                               vat_treatment_label=VAT_TREATMENT_LABELS.get(_invoice_vat_treatment(inv,company_row)))

    @app.route('/facturation/<int:invoice_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_pdf(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        from profitos.entities import resolve_entity
        company_row=_seller_identity(c,inv['entity_id'])
        c.close()
        pdf_bytes=_render_invoice_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lancez : pip install -r requirements.txt")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        filename=inv['invoice_number'] or f"brouillon-{inv['id']}"
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{filename}.pdf"'})

    @app.route('/facturation/<int:invoice_id>/proforma')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_proforma(invoice_id):
        from profitos.entities import current_entity_id, resolve_entity
        c=cx()
        eid=current_entity_id()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?',(invoice_id,eid)).fetchone()
        company_row=_seller_identity(c,eid)
        c.close()
        if not inv: abort(404)
        if inv['status']!='draft':
            flash("Le pro-forma n'a de sens que pour une facture encore au statut brouillon — celle-ci est déjà émise.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        pdf_bytes=_render_proforma_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lancez : pip install -r requirements.txt")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        log_activity('PROFORMA_GENERATED', f"Pro-forma généré pour la facture {invoice_label(inv)}")
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="proforma-brouillon-{inv["id"]}.pdf"'})

    @app.route('/facturation/livraisons')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def delivery_notes_list():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        c = cx()
        rows = c.execute(f"SELECT * FROM delivery_notes WHERE {ef} ORDER BY delivery_date DESC, id DESC", ep).fetchall()
        c.close()
        return render_template('delivery_notes_list.html', rows=rows)

    @app.route('/facturation/livraisons/nouvelle', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def delivery_note_new():
        from profitos.entities import current_entity_id
        c = cx()
        if request.method == 'POST':
            client_name = (request.form.get('client_name') or '').strip()
            client_address = (request.form.get('client_address') or '').strip()
            client_email = (request.form.get('client_email') or '').strip()
            delivery_date = request.form.get('delivery_date') or paris_today().isoformat()
            try:
                date.fromisoformat(delivery_date)
            except ValueError:
                c.close(); flash("Date de livraison invalide.")
                return redirect(url_for('delivery_note_new'))
            items = _compute_line_items(request.form)
            if not client_name or not items:
                c.close()
                flash("Nom du client et au moins une ligne sont requis.")
                return redirect(url_for('delivery_note_new'))
            siren = re.sub(r'\D', '', request.form.get('client_siren') or '')[:9]
            if siren and not siren_is_valid(siren):
                c.close(); flash("SIREN client invalide : 9 chiffres avec une clé de contrôle correcte.")
                return redirect(url_for('delivery_note_new'))
            nature = request.form.get('operation_nature') or 'biens'
            if nature not in ('biens', 'services', 'mixte'):
                nature = 'biens'
            # Échéance de paiement de la future facture (reprise à la facturation du bon).
            due_date = (request.form.get('due_date') or '').strip() or None
            if due_date:
                try:
                    date.fromisoformat(due_date)
                except ValueError:
                    c.close(); flash("Date d'échéance invalide.")
                    return redirect(url_for('delivery_note_new'))
                if due_date < delivery_date:
                    c.close(); flash("La date d'échéance ne peut pas précéder la date de livraison.")
                    return redirect(url_for('delivery_note_new'))
            delivery_number = _next_delivery_number(c, current_entity_id(), delivery_date)
            c.execute(
                """INSERT INTO delivery_notes
                   (entity_id,delivery_number,client_name,client_address,client_email,delivery_date,line_items,status,notes,created_at,created_by,
                    billing_address,client_siren,operation_nature,due_date)
                   VALUES(?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?)""",
                (current_entity_id(), delivery_number, client_name, client_address, client_email, delivery_date,
                 json.dumps(items, ensure_ascii=False), (request.form.get('notes') or '').strip(),
                 now(), current_user()['email'], (request.form.get('billing_address') or '').strip() or None,
                 siren or None, nature, due_date),
            )
            new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.commit()
            c.close()
            log_activity('DELIVERY_NOTE_CREATED', f"Bon de livraison {delivery_number} créé")
            flash(f"Bon de livraison {delivery_number} créé.")
            return redirect(url_for('delivery_note_detail', delivery_id=new_id))
        c.close()
        return render_template('delivery_note_new.html', today=paris_today().isoformat())

    @app.route('/facturation/livraisons/<int:delivery_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def delivery_note_detail(delivery_id):
        c = cx()
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        row = c.execute('SELECT * FROM delivery_notes WHERE id=? AND entity_id IS ?', (delivery_id, eid)).fetchone()
        c.close()
        if not row: abort(404)
        items = json.loads(row['line_items'] or '[]')
        subtotal, vat_amount, total = _totals(items)
        return render_template('delivery_note_detail.html', delivery=row, items=items,
                                subtotal=subtotal, vat_amount=vat_amount, total=total)

    @app.route('/facturation/livraisons/<int:delivery_id>/livrer', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def delivery_note_deliver(delivery_id):
        c = cx()
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        row = c.execute('SELECT status FROM delivery_notes WHERE id=? AND entity_id IS ?', (delivery_id, eid)).fetchone()
        if not row: c.close(); abort(404)
        if row['status'] == 'draft':
            c.execute("UPDATE delivery_notes SET status='delivered' WHERE id=? AND entity_id IS ?", (delivery_id, eid))
            c.commit()
            log_activity('DELIVERY_NOTE_DELIVERED', f"Bon de livraison #{delivery_id} marqué livré")
            flash("Bon de livraison marqué comme livré.")
        c.close()
        return redirect(url_for('delivery_note_detail', delivery_id=delivery_id))

    @app.route('/facturation/livraisons/<int:delivery_id>/facturer', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def delivery_note_invoice(delivery_id):
        from profitos.entities import current_entity_id
        c = cx()
        eid = current_entity_id()
        row = c.execute('SELECT * FROM delivery_notes WHERE id=? AND entity_id IS ?', (delivery_id, eid)).fetchone()
        if not row: c.close(); abort(404)
        if row['linked_invoice_id']:
            c.close()
            flash("Ce bon de livraison a déjà été facturé.")
            return redirect(url_for('delivery_note_detail', delivery_id=delivery_id))
        items = json.loads(row['line_items'] or '[]')
        subtotal, vat_amount, total = _totals(items)
        # Le bon de livraison porte l'adresse de livraison ; l'adresse de facturation est
        # celle du client (reprise du bon si renseignée), la nature est celle du bon
        # (livraison de biens par défaut), la date de livraison devient la date de la vente
        # et l'échéance est celle du bon (à défaut : 30 jours, jamais déjà passée).
        due_date = _g(row, 'due_date')
        if not due_date or str(due_date)[:10] < paris_today().isoformat():
            due_date = (paris_today() + timedelta(days=30)).isoformat()
        new_invoice_id, _token = _insert_draft_invoice(c, {
            'client_name': row['client_name'], 'client_address': _g(row, 'billing_address') or row['client_address'],
            'client_email': row['client_email'], 'delivery_address': row['client_address'],
            'client_siren': _g(row, 'client_siren'), 'operation_nature': _g(row, 'operation_nature', 'biens') or 'biens',
            'due_date': due_date, 'service_date': row['delivery_date'],
            'line_items': row['line_items'], 'subtotal': subtotal, 'vat_amount': vat_amount, 'total': total,
            'entity_id': eid, 'notes': f"Facturé à partir du bon de livraison {row['delivery_number']} du {fr_date(row['delivery_date'])}"})
        c.execute("UPDATE delivery_notes SET linked_invoice_id=? WHERE id=? AND entity_id IS ?", (new_invoice_id, delivery_id, eid))
        c.commit(); c.close()
        log_activity('DELIVERY_NOTE_INVOICED', f"Bon de livraison {row['delivery_number']} facturé (brouillon n°{new_invoice_id})")
        flash(f"Facture brouillon n°{new_invoice_id} créée à partir du bon de livraison.")
        return redirect(url_for('invoicing_detail', invoice_id=new_invoice_id))

    @app.route('/facturation/livraisons/<int:delivery_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def delivery_note_pdf(delivery_id):
        c = cx()
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        row = c.execute('SELECT * FROM delivery_notes WHERE id=? AND entity_id IS ?', (delivery_id, eid)).fetchone()
        company_row = _seller_identity(c, eid)
        c.close()
        if not row: abort(404)
        pdf_bytes = _render_delivery_pdf(row, company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lancez : pip install -r requirements.txt")
            return redirect(url_for('delivery_note_detail', delivery_id=delivery_id))
        return Response(pdf_bytes, mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{row["delivery_number"]}.pdf"'})

    @app.route('/facturation/<int:invoice_id>/facturx')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_facturx(invoice_id):
        """Lot 22 — génère le PDF/A-3 Factur-X (profil EN16931). Refuse si les
        mentions obligatoires (Lot 21) ou les règles métier EN16931 (BR-*)
        ne sont pas toutes réunies."""
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        # Identité de l'entité émettrice de LA facture (filiale ou société mère).
        company_row=_seller_identity(c,inv['entity_id'])
        c.close()
        if inv['status'] not in ('sent','partially_paid','paid'):
            flash("Le Factur-X n'est disponible que pour une facture émise : émettez d'abord la facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        _,missing=_check_mandatory_mentions(inv,company_row)
        if missing:
            flash("Facture non conforme : complétez d'abord les mentions obligatoires manquantes avant de générer le Factur-X.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        items=json.loads(inv['line_items'] or '[]')
        _,rule_failures=validate_facturx_business_rules(inv,items,company_row)
        if rule_failures:
            labels=', '.join(r['label'] for r in rule_failures)
            flash(f"Règle(s) métier EN16931 non respectée(s) : {labels}")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        pdf_bytes=render_facturx_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("Le Factur-X (PDF/A-3) n'est pas disponible sur ce serveur : il nécessite fpdf2 version 2.8.7 ou ultérieure. Le PDF de la facture reste téléchargeable.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        log_activity('INVOICE_FACTURX_GENERATED',f"Factur-X généré pour la facture {inv['invoice_number']}")
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{inv["invoice_number"]}_facturx.pdf"'})

    @app.route('/facturation/<int:invoice_id>/weinvoice', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_send_weinvoice(invoice_id):
        """Lot 23.3 — transmet le Factur-X à WeInvoice avec idempotence stricte."""
        c = cx()
        try:
            inv = _current_invoice(c,invoice_id)
            if not inv: abort(404)
            company_row = _seller_identity(c,inv['entity_id'])
            settings = c.execute('SELECT weinvoice_company_id,weinvoice_kyb_status FROM weinvoice_entity_settings WHERE entity_key=?', (inv['entity_id'] or 0,)).fetchone()
            if inv['status'] == 'cancelled':
                flash("Une facture annulée ne peut pas être transmise à la plateforme de facturation électronique.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            if inv['status'] not in ('sent','partially_paid','paid'):
                # Seule une facture émise (numéro légal, date d'émission, écriture) peut
                # être transmise : un brouillon n'a pas de numéro définitif.
                flash("Seule une facture émise peut être transmise : émettez d'abord la facture (« Marquer comme émise » ou « Envoyer »).")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            if 'weinvoice_invoice_id' in inv.keys() and inv['weinvoice_invoice_id']:
                flash(f"Facture déjà transmise à WeInvoice — identifiant {inv['weinvoice_invoice_id']}.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            if not settings or not settings['weinvoice_company_id']:
                flash("WeInvoice n'est pas encore onboardé pour cette entreprise.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            if (settings['weinvoice_kyb_status'] or '').upper() != 'VALIDATED':
                flash(f"Onboarding WeInvoice non validé (statut : {settings['weinvoice_kyb_status'] or 'inconnu'}).")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            _, missing = _check_mandatory_mentions(inv, company_row)
            if missing:
                flash("Transmission impossible : complétez les mentions obligatoires manquantes (détail ci-dessous).")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            items = json.loads(inv['line_items'] or '[]')
            _, rule_failures = validate_facturx_business_rules(inv, items, company_row)
            if rule_failures:
                flash("Facture EN16931 invalide — corrigez les règles métier avant transmission WeInvoice.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            validation_errors = validate_outgoing_invoice(inv)
            if validation_errors:
                flash("Transmission bloquée : " + " ".join(validation_errors))
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            pdf_bytes = render_facturx_pdf(inv, company_row)
            if pdf_bytes is None:
                flash("Impossible de générer le Factur-X PDF/A-3 — transmission WeInvoice annulée.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            idem = (inv['weinvoice_idempotency_key'] if 'weinvoice_idempotency_key' in inv.keys() else None) or f"profitos-{session.get('org_id')}-{invoice_id}"
            c.execute('UPDATE outgoing_invoices SET weinvoice_idempotency_key=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?', (idem, invoice_id, inv['entity_id']))
            c.commit()
            try:
                data = submit_invoice_file(settings['weinvoice_company_id'], pdf_bytes, f"{inv['invoice_number']}_facturx.pdf", idem)
            except (WeInvoiceAPIError, WeInvoiceConfigError) as e:
                c.execute('UPDATE outgoing_invoices SET weinvoice_last_error=? WHERE id=? AND entity_id IS ?', (str(e)[:1500], invoice_id, inv['entity_id']))
                _record_einvoice_event(c,inv,'submission_failed',idempotency_key=idem,detail=str(e))
                c.commit(); flash(str(e))
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            remote_id = data.get('eInvoicingId') or data.get('generationId') or data.get('id') or ''
            remote_status = data.get('status') or ('GENERATION' if data.get('generationId') else 'SUBMITTED')
            if not remote_id:
                missing_remote_id_error = "WeInvoice a accepté la facture mais aucun identifiant distant exploitable n'a été renvoyé."
                c.execute('UPDATE outgoing_invoices SET weinvoice_last_error=? WHERE id=? AND entity_id IS ?', (missing_remote_id_error, invoice_id, inv['entity_id']))
                _record_einvoice_event(c,inv,'submission_failed',idempotency_key=idem,detail=missing_remote_id_error)
                c.commit(); flash("WeInvoice a accepté la facture, mais l'identifiant distant est absent — vérifiez le journal des événements avant tout nouvel envoi.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            c.execute('UPDATE outgoing_invoices SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?', (remote_id, remote_status, now(), invoice_id, inv['entity_id']))
            _record_einvoice_event(c,inv,'submitted',remote_id=remote_id,status=remote_status,idempotency_key=idem)
            _stage_ereporting_record(c,inv,'transaction')
            c.commit()
            log_activity('INVOICE_WEINVOICE_SUBMITTED', f"Facture {inv['invoice_number']} transmise à WeInvoice ({remote_id}, {remote_status})")
            flash(f"Facture transmise à WeInvoice — identifiant {remote_id}, statut : {remote_status}.")
            return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
        finally:
            c.close()

    @app.route('/facturation/<int:invoice_id>/weinvoice/synchroniser', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_sync_weinvoice(invoice_id):
        """Lot 23.4 — resynchronisation manuelle du statut via la timeline officielle."""
        c = cx()
        try:
            inv = _current_invoice(c,invoice_id)
            if not inv: abort(404)
            settings = c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?', (inv['entity_id'] or 0,)).fetchone()
            remote_id = inv['weinvoice_invoice_id'] if 'weinvoice_invoice_id' in inv.keys() else None
            if not remote_id:
                flash("Cette facture n'a pas encore d'identifiant WeInvoice.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            if not settings or not settings['weinvoice_company_id']:
                flash("Organisation WeInvoice absente — synchronisation impossible.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            try:
                data = get_invoice_timeline(settings['weinvoice_company_id'], remote_id)
                status, regulatory_code = invoice_status_from_timeline(data)

                # Les acquittements PPF sont des accusés techniques/réglementaires :
                # ils sont journalisés séparément et ne remplacent jamais le cycle de vie métier.
                ack = data.get('latestAcquittement') or {}
                ack_code = str(ack.get('code') or '').strip()
                ack_statuses = {
                    '250': 'ACK_250_ACCEPTED',
                    '251': 'ACK_251_REJECTED',
                    '500': 'ACK_500_RECEVABLE',
                    '501': 'ACK_501_INADMISSIBLE',
                    '601': 'ACK_601_REJECTED',
                }
                ack_status = ack_statuses.get(ack_code)
                if ack_status:
                    ack_detail = ' · '.join(
                        str(v) for v in (
                            ack.get('objet'),
                            ack.get('decision'),
                            ack.get('motifCode'),
                        ) if v not in (None, '')
                    )
                    _record_einvoice_event(
                        c, inv, 'acquittement',
                        remote_id=remote_id,
                        status=ack_status,
                        regulatory_code=ack_code,
                        idempotency_key=f'ppf-ack-{remote_id}-{ack_code}-{ack.get("motifCode") or ""}',
                        detail=ack_detail,
                    )
            except (WeInvoiceAPIError, WeInvoiceConfigError) as e:
                c.execute('UPDATE outgoing_invoices SET weinvoice_last_error=? WHERE id=? AND entity_id IS ?', (str(e)[:1500], invoice_id, inv['entity_id']))
                c.commit(); flash(str(e))
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            c.execute('UPDATE outgoing_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?',
                      (status, str(regulatory_code) if regulatory_code is not None else None, now(), invoice_id, inv['entity_id']))
            _record_einvoice_event(c,inv,'status_sync',remote_id=remote_id,status=status,regulatory_code=regulatory_code,
                                   idempotency_key=f'sync-{remote_id}-{status}-{regulatory_code}')
            c.commit()
            log_activity('INVOICE_WEINVOICE_SYNCED', f"Facture {inv['invoice_number']} synchronisée WeInvoice ({remote_id}, {status})")
            code_text = f" · code réglementaire {regulatory_code}" if regulatory_code is not None else ''
            flash(f"Statut WeInvoice synchronisé : {status}{code_text}.")
            return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
        finally:
            c.close()

    @app.route('/facturation/<int:invoice_id>/weinvoice/test-webhook-sandbox', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_test_weinvoice_webhook_sandbox(invoice_id):
        """Lot 23.6 : seul le webhook entrant doit modifier le statut local."""
        c = cx()
        try:
            inv = _current_invoice(c,invoice_id)
            if not inv: abort(404)
            settings = c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?', (inv['entity_id'] or 0,)).fetchone()
            remote_id = inv['weinvoice_invoice_id'] if 'weinvoice_invoice_id' in inv.keys() else None
            if not remote_id:
                flash("Cette facture n'a pas encore d'identifiant WeInvoice.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            if not settings or not settings['weinvoice_company_id']:
                flash("Organisation WeInvoice absente — test impossible.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            try:
                sandbox_force_invoice_status(remote_id, status=request.form.get('sandbox_status','213'), organization_id=settings['weinvoice_company_id'])
            except (WeInvoiceAPIError, WeInvoiceConfigError) as e:
                flash(str(e))
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            log_activity('INVOICE_WEINVOICE_SANDBOX_WEBHOOK_TEST', f"Test webhook sandbox demandé pour {inv['invoice_number']} ({remote_id}, CDV {request.form.get('sandbox_status','213')})")
            flash("Test Sandbox envoyé à WeInvoice. Attendez quelques secondes : le webhook doit mettre à jour ProfitOS automatiquement. N'utilisez « Actualiser le statut WeInvoice » qu'en diagnostic.")
            return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
        finally:
            c.close()

    @app.route('/facturation/<int:invoice_id>/envoyer',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_send(invoice_id):
        """Envoie la facture par e-mail. Un brouillon est d'abord ÉMIS (numéro, date,
        écriture) dans une transaction validée ; l'e-mail part APRÈS la validation. Un
        échec d'envoi ne défait jamais l'émission : la facture reste émise et peut être
        renvoyée."""
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status'] in ('paid','cancelled'):
            c.close()
            flash("Cette facture ne peut plus être envoyée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if not inv['client_email']:
            c.close()
            flash("Aucune adresse e-mail client n'est renseignée pour cette facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        was_draft=inv['status']=='draft'
        if was_draft:
            try:
                inv=issue_invoice(c,invoice_id,inv['entity_id'])
            except InvoiceIssueError as e:
                c.close()
                flash(str(e) + (" Détail ci-dessous." if e.missing else ""))
                return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
            deliver_webhook(c,'invoice.sent',{'id':invoice_id,'invoice_number':inv['invoice_number'],
                'client_name':inv['client_name'],'total':inv['total'],'due_date':inv['due_date']},
                entity_id=inv['entity_id'])
        org=current_org()
        base=app_base_url()
        link=f"{base}{url_for('public_invoice_view',token=inv['public_token'])}"
        html=render_template('email_transactional.html',title=f"Facture {inv['invoice_number']} — {org['name']}",
            intro=f"Voici votre facture {inv['invoice_number']} de {org['name']}, d'un montant de {fr_number(inv['total'],2)} € TTC.",
            cta_label='Consulter la facture',cta_url=link,footer='')
        try:
            result=send_email(inv['client_email'],f"Facture {inv['invoice_number']} — {org['name']}",html,on_behalf_of_org=org)
        except Exception:
            app.logger.exception('Envoi de la facture %s impossible', invoice_id)
            result={'sent':False,'error':'exception'}
        prefix=f"Facture {inv['invoice_number']} émise. " if was_draft else ''
        if result.get('dry_run'):
            flash(prefix+f"Service e-mail non configuré : la facture n'a pas été réellement envoyée à {inv['client_email']} (mode simulation).")
        elif result.get('sent'):
            c.execute("UPDATE outgoing_invoices SET sent_at=? WHERE id=? AND entity_id IS ?",(now(),invoice_id,inv['entity_id']))
            c.commit()
            log_activity('INVOICE_SENT',f"Facture {inv['invoice_number']} envoyée à {inv['client_email']}")
            flash(prefix+f"Facture envoyée à {inv['client_email']}.")
        else:
            log_ops_event('INVOICE_EMAIL_FAILED',outcome='ERROR',detail=f"facture {invoice_id}: {str(result.get('error') or '')[:200]}")
            flash(prefix+"L'e-mail n'a pas pu être envoyé. Réessayez dans quelques minutes ou transmettez la facture (PDF) par un autre moyen.")
        c.close()
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/marquer-emise',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_mark_issued(invoice_id):
        """Émet une facture brouillon transmise au client hors ProfitOS (courrier, messagerie
        personnelle, remise en main propre) : numéro légal, date d'émission, écriture de
        vente et webhook, sans e-mail. Atomique : si un contrôle ou l'écriture comptable
        échoue, la facture reste un brouillon sans numéro."""
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        try:
            issued=issue_invoice(c,invoice_id,inv['entity_id'])
        except InvoiceIssueError as e:
            c.close()
            flash(str(e) + (" Détail ci-dessous." if e.missing else ""))
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        deliver_webhook(c,'invoice.sent',{'id':invoice_id,'invoice_number':issued['invoice_number'],
            'client_name':issued['client_name'],'total':issued['total'],'due_date':issued['due_date']},
            entity_id=issued['entity_id'])
        c.close()
        log_activity('INVOICE_ISSUED_MANUALLY',f"Facture {issued['invoice_number']} émise (transmise hors ProfitOS)")
        flash(f"Facture {issued['invoice_number']} émise. Vous pouvez maintenant enregistrer ses règlements.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/relancer',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_remind(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status'] not in ('sent','partially_paid') or _display_status(inv)!='overdue':
            c.close()
            flash("Seule une facture envoyée et échue peut être relancée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        hold=_einvoice_hold(inv)
        if hold:
            c.close()
            flash(f"Cette facture est {hold} côté facturation électronique : régularisez-la avant de relancer le client.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if not inv['client_email']:
            c.close()
            flash("Aucun e-mail client renseigné pour cette facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        balance=_invoice_balance(c,inv)
        if balance <= 0.005:
            c.close()
            flash("Cette facture est entièrement réglée ou couverte par un avoir et ne peut pas être relancée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        row=c.execute("SELECT COUNT(*) AS n FROM invoice_reminders WHERE invoice_id=? AND entity_id IS ?",(invoice_id,inv['entity_id'])).fetchone()
        reminder_number=int(row['n'] or 0)+1
        org=current_org()
        base=app_base_url()
        link=f"{base}{url_for('public_invoice_view',token=inv['public_token'])}"
        html=render_template(
            'email_transactional.html',
            title=f"Relance facture {inv['invoice_number']} — {org['name']}",
            intro=(f"Sauf erreur de notre part, la facture {inv['invoice_number']} "
                   f"(montant TTC {fr_number(inv['total'],2)} €), échue le {fr_date(inv['due_date'])}, "
                   f"présente un reste à payer de {fr_number(balance,2)} €. "
                   f"Conformément à nos conditions de paiement, tout retard entraîne l'application de pénalités de retard "
                   f"et d'une indemnité forfaitaire pour frais de recouvrement de 40 € (art. L441-10 du Code de commerce). "
                   f"Si votre règlement a déjà été effectué, merci de ne pas tenir compte de cette relance."),
            cta_label='Consulter la facture',cta_url=link,footer=''
        )
        try:
            result=send_email(inv['client_email'],f"Relance facture {inv['invoice_number']} — {org['name']}",html,on_behalf_of_org=org)
        except Exception:
            app.logger.exception('Relance de la facture %s impossible', invoice_id)
            result={'sent':False,'error':'exception'}
        if result.get('dry_run'):
            flash(f"Service e-mail non configuré — relance non envoyée réellement (mode simulation) à {inv['client_email']}.")
        elif result.get('sent'):
            sent_at=now()
            c.execute("INSERT INTO invoice_reminders(entity_id,invoice_id,recipient_email,sent_at,reminder_number) VALUES(?,?,?,?,?)",
                      (inv['entity_id'],invoice_id,inv['client_email'],sent_at,reminder_number))
            c.commit()
            log_activity('INVOICE_REMINDER_SENT',f"Relance n°{reminder_number} pour {inv['invoice_number']} envoyée à {inv['client_email']}")
            flash(f"Relance n°{reminder_number} envoyée à {inv['client_email']}.")
        else:
            log_ops_event('INVOICE_REMINDER_FAILED',outcome='ERROR',detail=f"facture {invoice_id}: {str(result.get('error') or '')[:200]}")
            flash("La relance n'a pas pu être envoyée. Réessayez dans quelques minutes.")
        c.close()
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/reglement',methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_add_payment(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status'] not in ('sent','partially_paid'):
            c.close(); flash("Un règlement ne peut être saisi que sur une facture émise non soldée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        amount=round(amounts.parse_number(request.form.get('amount'),0.0),2)
        balance=_invoice_balance(c,inv)
        if amount <= 0 or amount > balance + 0.001:
            c.close(); flash(f"Montant invalide. Le reste à payer est de {fr_number(balance,2)} €.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        payment_date=(request.form.get('payment_date') or paris_today().isoformat()).strip()
        try: date.fromisoformat(payment_date)
        except ValueError:
            c.close(); flash("Date de règlement invalide.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if payment_date > paris_today().isoformat():
            c.close(); flash("La date de règlement ne peut pas être dans le futur.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if inv['issue_date'] and payment_date < str(inv['issue_date'])[:10]:
            c.close(); flash(f"La date de règlement ne peut pas précéder la date d'émission ({fr_date(inv['issue_date'])}).")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        reference=(request.form.get('reference') or '').strip()[:120]
        method=(request.form.get('payment_method') or 'bank').strip()[:30]
        idem=(request.form.get('idempotency_key') or '').strip() or str(uuid.uuid4())
        try:
            cur=c.execute("""INSERT INTO outgoing_invoice_payments(entity_id,invoice_id,amount,payment_date,payment_method,reference,idempotency_key,created_at)
                         VALUES(?,?,?,?,?,?,?,?)""",(inv['entity_id'],invoice_id,amount,payment_date,method,reference,idem,now()))
            payment_id=cur.lastrowid
            payment=c.execute("SELECT * FROM outgoing_invoice_payments WHERE id=? AND entity_id IS ?",(payment_id,inv['entity_id'])).fetchone()
            generate_sale_partial_payment_entry(c,inv,payment)
            new_status='paid' if _invoice_balance(c,inv) <= 0.005 else 'partially_paid'
            paid_at=now() if new_status=='paid' else None
            c.execute("UPDATE outgoing_invoices SET status=?,paid_at=? WHERE id=? AND entity_id IS ?",(new_status,paid_at,invoice_id,inv['entity_id']))
            _stage_payment_ereporting(c,inv,payment)
            c.commit()
            if new_status=='paid':
                deliver_webhook(c,'invoice.paid',{'id':invoice_id,'invoice_number':inv['invoice_number'],'client_name':inv['client_name'],'total':inv['total']},entity_id=inv['entity_id'])
        except dbmod.IntegrityError:
            c.rollback(); c.close(); flash("Ce règlement a déjà été enregistré.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        except AccountingError as e:
            c.rollback(); c.close(); flash(f"Règlement non enregistré : {e}")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        c.close()
        log_activity('INVOICE_PAYMENT_RECORDED',f"Règlement de {fr_number(amount,2)} € sur {inv['invoice_number']}")
        flash(f"Règlement de {fr_number(amount,2)} € enregistré.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/marquer-payee',methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_mark_paid(invoice_id):
        c=cx(); inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status'] not in ('sent','partially_paid'):
            c.close(); flash("Cette facture ne peut pas être soldée dans son état actuel.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        balance=_invoice_balance(c,inv)
        if balance <= 0.005:
            c.close(); flash("Cette facture est déjà soldée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        try:
            idem=f"manual-balance:{invoice_id}:{inv['entity_id']}:{round(balance,2)}"
            cur=c.execute("""INSERT INTO outgoing_invoice_payments(entity_id,invoice_id,amount,payment_date,payment_method,reference,idempotency_key,created_at)
                         VALUES(?,?,?,?,?,?,?,?)""",(inv['entity_id'],invoice_id,balance,paris_today().isoformat(),'bank','Solde manuel',idem,now()))
            payment=c.execute("SELECT * FROM outgoing_invoice_payments WHERE id=?",(cur.lastrowid,)).fetchone()
            generate_sale_partial_payment_entry(c,inv,payment)
            c.execute("UPDATE outgoing_invoices SET status='paid',paid_at=? WHERE id=? AND entity_id IS ?",(now(),invoice_id,inv['entity_id']))
            _stage_payment_ereporting(c,inv,payment)
            c.commit()
        except dbmod.IntegrityError:
            c.rollback(); c.close(); flash("Ce règlement a déjà été enregistré.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        except AccountingError as e:
            c.rollback(); c.close(); flash(f"Paiement non enregistré : {e}")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        deliver_webhook(c,'invoice.paid',{'id':invoice_id,'invoice_number':inv['invoice_number'],'client_name':inv['client_name'],'total':inv['total']},entity_id=inv['entity_id'])
        c.close()
        flash(f"Facture {inv['invoice_number']} soldée.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))


    @app.route('/facturation/<int:invoice_id>/modifier',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_edit(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status']!='draft':
            c.close()
            flash("Seule une facture en brouillon peut être modifiée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if request.method=='POST':
            header,error=_parse_invoice_header(request.form,paris_today().isoformat())
            header['notes']=(request.form.get('notes') or '').strip()
            items=_compute_line_items(request.form)
            if not error and (not header['client_name'] or not items):
                error='Nom du client et au moins une ligne de facture requis.'
            if not error and (_g(inv,'invoice_kind','standard') or 'standard')!='final':
                error=_line_items_error(items)
            if error:
                c.close(); flash(error)
                merged=dict(inv); merged.update(header)
                return render_template('invoicing_edit.html',inv=inv,f=merged,items=items,
                                       vat_treatment_labels=VAT_TREATMENT_LABELS), 400
            subtotal,vat_amount,total=_totals(items)
            sets=','.join(f'{k}=?' for k in INVOICE_HEADER_COLUMNS)
            c.execute(f"UPDATE outgoing_invoices SET {sets},line_items=?,subtotal=?,vat_amount=?,total=?,notes=? "
                      "WHERE id=? AND entity_id IS ? AND status='draft'",
                      [header[k] for k in INVOICE_HEADER_COLUMNS]+[json.dumps(items,ensure_ascii=False),subtotal,vat_amount,total,
                       header['notes'],invoice_id,inv['entity_id']])
            c.commit(); c.close()
            log_activity('INVOICE_UPDATED',f"Facture {invoice_label(inv)} modifiée")
            flash(f"{invoice_label(inv)} mise à jour.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        items=json.loads(inv['line_items'] or '[]')
        c.close()
        return render_template('invoicing_edit.html',inv=inv,f=inv,items=items,vat_treatment_labels=VAT_TREATMENT_LABELS)

    @app.route('/facturation/<int:invoice_id>/annuler',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_cancel(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status'] in ('sent','partially_paid','paid'):
            c.close()
            flash("Une facture émise ne peut plus être annulée directement.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if inv['status']=='cancelled':
            c.close()
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        c.execute("UPDATE outgoing_invoices SET status='cancelled' WHERE id=? AND entity_id IS ?",(invoice_id,inv['entity_id']))
        c.commit(); c.close()
        log_activity('INVOICE_CANCELLED',f"Facture {invoice_label(inv)} annulée")
        flash(f"{invoice_label(inv)} annulé : aucun numéro de facture n'a été consommé.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/avoir',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_credit_new(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        if inv['status'] not in ('sent','partially_paid','paid'):
            c.close()
            flash("Un avoir ne peut être créé que pour une facture émise.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        already=_credited_total(c,invoice_id,inv['entity_id'])
        remaining=max(0,float(inv['total'] or 0)-already)
        if remaining <= 0.005:
            c.close()
            flash("Cette facture est déjà intégralement créditée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        if request.method=='POST':
            # Sérialise la création des avoirs : deux requêtes concurrentes ne
            # peuvent pas toutes deux créditer le même solde restant.
            c.execute('BEGIN IMMEDIATE')
            inv=_current_invoice(c,invoice_id)
            if not inv:
                c.rollback(); c.close(); abort(404)
            already=_credited_total(c,invoice_id,inv['entity_id'])
            remaining=max(0,float(inv['total'] or 0)-already)
            # Factures classiques, d'acompte et finales : chacune a son type d'écriture de vente.
            sale_entry=c.execute(
                "SELECT id,source_type FROM accounting_entries WHERE source_type IN ('outgoing_invoice','customer_deposit_invoice','customer_final_invoice') AND source_id=? AND entity_id IS ? LIMIT 1",
                (invoice_id,inv['entity_id']),
            ).fetchone()
            if not sale_entry:
                c.rollback(); c.close()
                flash("Avoir impossible : l'écriture comptable de la facture d'origine est absente. Corrigez d'abord la comptabilisation de la facture.")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))
            reason=request.form.get('reason','').strip()
            try:
                amount_ttc=float(amounts.parse_number(request.form.get('amount_ttc'),0.0))
            except (TypeError,ValueError):
                amount_ttc=0
            if not reason or amount_ttc <= 0 or amount_ttc > remaining + 0.005:
                c.close()
                flash(f"Motif requis et montant TTC compris entre 0,01 € et {fr_number(remaining,2)} €.")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))

            source_items=json.loads(inv['line_items'] or '[]')
            if abs(amount_ttc-float(inv['total'] or 0))<0.005 and already<0.005:
                # Avoir total : copie exacte des lignes, mêmes totaux que la facture.
                items=credit_note_items(source_items)
            else:
                ratio=amount_ttc/float(inv['total'])
                items=_adjust_credit_items(credit_note_items(source_items,ratio),amount_ttc)
            subtotal,vat_amount,credit_total=_totals(items)
            if credit_total > remaining + 0.005:
                c.rollback(); c.close()
                flash(f"Le montant de l'avoir ({fr_number(credit_total,2)} €) dépasserait le reste créditable ({fr_number(remaining,2)} €).")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))
            from profitos.invoice_numbering import next_document_number
            issue_date=paris_today().isoformat()
            credit_number=next_document_number(c,'credit',inv['entity_id'],issue_date)
            c.execute("""INSERT INTO outgoing_credit_notes
                (entity_id,credit_number,original_invoice_id,original_invoice_number,client_name,issue_date,
                 line_items,subtotal,vat_amount,total,reason,status,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,'issued',?)""",
                (inv['entity_id'],credit_number,invoice_id,inv['invoice_number'],inv['client_name'],issue_date,
                 json.dumps(items,ensure_ascii=False),subtotal,vat_amount,credit_total,reason,now()))
            requested_ttc=amount_ttc
            amount_ttc=credit_total
            credit_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            credit=_current_credit(c,credit_id)
            try:
                # Avoir sur facture d'acompte : on annule l'acompte (419100), pas du chiffre d'affaires.
                # Les autres avoirs suivent les comptes de la facture d'origine : 707 pour les
                # biens, 706 pour les services (nature de chaque ligne).
                generate_sale_credit_entry(c,credit,
                    revenue_account='419100' if sale_entry['source_type']=='customer_deposit_invoice' else None)
                # Facture désormais soldée (encaissements + avoirs) : elle sort des créances et des relances.
                if inv['status'] in ('sent','partially_paid') and _invoice_balance(c,inv) <= 0.005:
                    c.execute("UPDATE outgoing_invoices SET status='paid',paid_at=? WHERE id=? AND entity_id IS ?",
                              (now(),invoice_id,inv['entity_id']))
                c.commit()
            except AccountingError as e:
                c.rollback()
                log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"avoir facture {invoice_id}: {e}")
                c.close()
                flash(f"Avoir non créé : {e}")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))
            c.close()
            log_activity('CREDIT_NOTE_CREATED',f"Avoir {credit_number} créé pour {inv['invoice_number']} ({fr_number(amount_ttc,2)} € TTC)")
            if abs(requested_ttc-credit_total)>=0.005:
                flash(f"Avoir {credit_number} créé pour {fr_number(credit_total,2)} € TTC : avec l'arrondi de la TVA, "
                      f"{fr_number(requested_ttc,2)} € n'est pas atteignable au centime près.")
            else:
                flash(f"Avoir {credit_number} créé.")
            return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))

        c.close()
        return render_template('invoicing_credit_new.html',inv=inv,remaining=remaining)

    @app.route('/facturation/avoir/<int:credit_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_credit_detail(credit_id):
        c=cx()
        credit=_current_credit(c,credit_id)
        c.close()
        if not credit: abort(404)
        items=json.loads(credit['line_items'] or '[]')
        return render_template('invoicing_credit_detail.html',credit=credit,items=items)

    @app.post('/facturation/e-reporting/transmettre')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_ereporting_transmit():
        from profitos.entities import current_entity_id
        eid=current_entity_id(); anchor_date=(request.form.get('anchor_date') or '').strip()
        flow_type=(request.form.get('flow_type') or '').strip()
        try: date.fromisoformat(anchor_date)
        except Exception:
            flash("Date d'ancrage e-reporting invalide."); return redirect(url_for('invoicing_list'))
        c=cx()
        try:
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',((eid or 0),)).fetchone()
            if not settings or not settings['weinvoice_company_id']:
                flash("Plateforme de facturation électronique non configurée pour cette entité."); return redirect(url_for('invoicing_list'))
            try:
                validate_ereporting_fiscal_readiness(settings['weinvoice_company_id'])
                payload=build_flux10(c,eid,anchor_date,flow_type)
            except ValueError as exc:
                flash(str(exc)); return redirect(url_for('invoicing_list'))
            if not payload:
                flash("Aucune donnée éligible pour ce flux et cette date."); return redirect(url_for('invoicing_list'))
            old=c.execute("""SELECT * FROM ereporting_transmissions WHERE entity_id IS ? AND provider='weinvoice'
                AND transmission_number=?""",(eid,payload['transmissionNumber'])).fetchone()
            if old and old['status'] in ('submitted','accepted'):
                flash("Ce flux e-reporting a déjà été transmis."); return redirect(url_for('invoicing_list'))
            stamp=now()
            c.execute("""INSERT OR IGNORE INTO ereporting_transmissions(
                entity_id,provider,transmission_number,flow_type,anchor_date,type_code,status,payload_json,created_at,updated_at)
                VALUES(?,'weinvoice',?,?,?,?, 'prepared',?,?,?)""",
                (eid,payload['transmissionNumber'],flow_type,anchor_date,payload.get('typeCode','IN'),
                 json.dumps(payload,ensure_ascii=False,separators=(',',':')),stamp,stamp)); c.commit()
            try: result=submit_ereporting_flow(settings['weinvoice_company_id'],payload)
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                c.execute("""UPDATE ereporting_transmissions SET status='error',last_error=?,updated_at=?
                    WHERE entity_id IS ? AND transmission_number=?""",(str(exc)[:2000],now(),eid,payload['transmissionNumber']))
                c.commit(); flash(str(exc)); return redirect(url_for('invoicing_list'))
            ref=str(result.get('id') or result.get('transmissionId') or result.get('reference') or '')
            c.execute("""UPDATE ereporting_transmissions SET status='submitted',provider_reference=?,late_deposit=?,
                response_json=?,last_error=NULL,submitted_at=?,updated_at=? WHERE entity_id IS ? AND transmission_number=?""",
                (ref,1 if result.get('lateDeposit') else 0,json.dumps(result,ensure_ascii=False),now(),now(),eid,payload['transmissionNumber']))
            c.commit(); log_activity('EREPORTING_SUBMITTED',f"Flux {flow_type} transmis ({anchor_date})")
            flash("Déclaration électronique transmise à la plateforme agréée."); return redirect(url_for('invoicing_list'))
        finally: c.close()

    @app.post('/facturation/e-reporting/<int:transmission_id>/synchroniser')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_ereporting_sync(transmission_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        try:
            row=c.execute('SELECT * FROM ereporting_transmissions WHERE id=? AND entity_id IS ?',
                          (transmission_id,eid)).fetchone()
            if not row: abort(404)
            if not row['provider_reference']:
                flash("Transmission sans identifiant distant."); return redirect(url_for('invoicing_list'))
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',
                               ((eid or 0),)).fetchone()
            if not settings or not settings['weinvoice_company_id']: abort(409)
            try:
                data=get_ereporting_transmission(settings['weinvoice_company_id'],row['provider_reference'])
                tx=data.get('transmission') or {}
                status=str(tx.get('status') or tx.get('fluxStatus') or row['status'])
                flux_status=str(tx.get('fluxStatus') or '')
                motifs=tx.get('rejectionMotifs') or data.get('rejectionMotifs') or []
                c.execute("""UPDATE ereporting_transmissions SET status=?,flux_status=?,
                    rejection_motifs_json=?,response_json=?,last_checked_at=?,last_error=NULL,updated_at=?
                    WHERE id=? AND entity_id IS ?""",
                    (status,flux_status,json.dumps(motifs,ensure_ascii=False),
                     json.dumps(data,ensure_ascii=False),now(),now(),transmission_id,eid))
                c.commit(); flash("Statut de la déclaration électronique actualisé.")
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                c.execute('UPDATE ereporting_transmissions SET last_error=?,last_checked_at=?,updated_at=? WHERE id=? AND entity_id IS ?',
                          (str(exc)[:2000],now(),now(),transmission_id,eid)); c.commit(); flash(str(exc))
            return redirect(url_for('invoicing_list'))
        finally: c.close()

    @app.post('/facturation/e-reporting/<int:transmission_id>/preuve')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_ereporting_proof(transmission_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        try:
            row=c.execute('SELECT * FROM ereporting_transmissions WHERE id=? AND entity_id IS ?',
                          (transmission_id,eid)).fetchone()
            if not row: abort(404)
            if not row['provider_reference']:
                flash("Preuve indisponible : transmission sans identifiant distant."); return redirect(url_for('invoicing_list'))
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',
                               ((eid or 0),)).fetchone()
            if not settings or not settings['weinvoice_company_id']: abort(409)
            try:
                proof=get_ereporting_proof(settings['weinvoice_company_id'],row['provider_reference'])
                motifs=proof.get('rejectionMotifs') or []
                c.execute("""UPDATE ereporting_transmissions SET proof_json=?,rejection_motifs_json=?,
                    flux_status=?,last_checked_at=?,updated_at=?,last_error=NULL WHERE id=? AND entity_id IS ?""",
                    (json.dumps(proof,ensure_ascii=False),json.dumps(motifs,ensure_ascii=False),
                     str(proof.get('fluxStatus') or proof.get('status') or ''),now(),now(),transmission_id,eid))
                c.commit()
                return Response(json.dumps(proof,ensure_ascii=False,indent=2),mimetype='application/json',
                    headers={'Content-Disposition':f'attachment; filename="preuve-fiscale-{transmission_id}.json"'})
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                flash(str(exc)); return redirect(url_for('invoicing_list'))
        finally: c.close()

    @app.get('/facturation/electronique/diagnostic')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_electronic_diagnostic():
        from profitos.entities import current_entity_id
        eid=current_entity_id(); local=production_readiness(); fiscal=None; fiscal_error=None
        c=cx()
        try:
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',
                               ((eid or 0),)).fetchone()
            if settings and settings['weinvoice_company_id']:
                try: fiscal=validate_ereporting_fiscal_readiness(settings['weinvoice_company_id'])
                except Exception as exc: fiscal_error=str(exc)
        finally: c.close()
        if request.args.get('format')=='json' and current_role() in ('OWNER','ADMIN'):
            return jsonify({'local':local,'fiscalSettings':fiscal,'fiscalError':fiscal_error,
                            'productionSwitchAutomatic':False})
        # Page lisible pour l'utilisateur (le JSON brut reste réservé aux administrateurs).
        return render_template('einvoice_diagnostic.html',local=local,fiscal=fiscal,fiscal_error=fiscal_error,
                               connected=bool(settings and settings['weinvoice_company_id']))

    @app.post('/facturation/avoir/<int:credit_id>/electronique/envoyer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_credit_electronic_send(credit_id):
        c=cx()
        try:
            credit=_current_credit(c,credit_id)
            if not credit: abort(404)
            original=_current_invoice(c,credit['original_invoice_id'])
            if not original: abort(404)
            if credit['weinvoice_invoice_id']:
                flash("Cet avoir a déjà été transmis électroniquement.")
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',((credit['entity_id'] or 0),)).fetchone()
            if not settings or not settings['weinvoice_company_id']:
                flash("Plateforme de facturation électronique non configurée pour cette entité.")
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            company=_seller_identity(c,credit['entity_id'])
            pdf=render_credit_facturx_pdf(credit,original,company)
            if not pdf:
                flash("Impossible de générer le Factur-X de l'avoir.")
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            idem=f"credit-{credit['entity_id'] or 0}-{credit['id']}-{credit['credit_number']}"
            try:
                data=submit_invoice_file(settings['weinvoice_company_id'],pdf,f"{credit['credit_number']}.pdf",idem)
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                c.execute('UPDATE outgoing_credit_notes SET weinvoice_last_error=?,weinvoice_last_sync_at=? WHERE id=? AND entity_id IS ?',
                          (str(exc)[:2000],now(),credit_id,credit['entity_id']))
                c.commit(); flash(str(exc))
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            remote=str(data.get('eInvoicingId') or '').strip()
            if not remote:
                flash("WeInvoice n'a pas retourné d'identifiant électronique.")
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            c.execute('UPDATE outgoing_credit_notes SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?',
                      (remote,str(data.get('status') or 'SUBMITTED'),str(data.get('regulatoryStatusCode') or ''),now(),credit_id,credit['entity_id']))
            c.commit(); log_activity('CREDIT_NOTE_EINVOICE_SUBMITTED',f"Avoir {credit['credit_number']} transmis électroniquement")
            flash("Avoir transmis à la facturation électronique.")
            return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
        finally: c.close()

    @app.post('/facturation/avoir/<int:credit_id>/electronique/synchroniser')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_credit_electronic_sync(credit_id):
        c=cx()
        try:
            credit=_current_credit(c,credit_id)
            if not credit: abort(404)
            if not credit['weinvoice_invoice_id']:
                flash("Cet avoir n'a pas encore été transmis électroniquement.")
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',((credit['entity_id'] or 0),)).fetchone()
            if not settings or not settings['weinvoice_company_id']:
                flash("Plateforme de facturation électronique non configurée pour cette entité.")
                return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            try:
                timeline=get_invoice_timeline(settings['weinvoice_company_id'],credit['weinvoice_invoice_id'])
                status,cdv=invoice_status_from_timeline(timeline)
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                flash(str(exc)); return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
            c.execute('UPDATE outgoing_credit_notes SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?',
                      (str(status or ''),str(cdv) if cdv is not None else None,now(),credit_id,credit['entity_id']))
            c.commit(); flash("Statut électronique de l'avoir actualisé.")
            return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
        finally: c.close()

    @app.route('/facturation/avoir/<int:credit_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_credit_pdf(credit_id):
        c=cx()
        credit=_current_credit(c,credit_id)
        if not credit:
            c.close(); abort(404)
        from profitos.entities import resolve_entity
        company_row=_seller_identity(c,credit['entity_id'])
        original=c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?',(credit['original_invoice_id'],credit['entity_id'])).fetchone()
        c.close()
        pdf_bytes=_render_credit_pdf(credit,company_row,original)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2'.")
            return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{credit["credit_number"]}.pdf"'})

    @app.route('/devis/<token>')
    def public_quote_view(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        
        if q:
            from profitos.entities import resolve_entity
            company_row=_seller_identity(tc,q['entity_id'])
        else:
            company_row=None
        tc.close()
        if not q: abort(404)
        return render_template('invoicing_quote_public.html',q=q,
                               items=json.loads(q['line_items'] or '[]'),
                               company=company_row,token=token,
                               status_label=_quote_status_label(q['status']))

    @app.post('/devis/<token>/accepter')
    def public_quote_accept(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        if not q:
            tc.close(); abort(404)
        if q['status']=='sent':
            tc.execute("UPDATE outgoing_quotes SET status='accepted',accepted_at=? WHERE id=?",
                       (now(),mapping['quote_local_id']))
            tc.commit()
        tc.close()
        return redirect(url_for('public_quote_view',token=token))

    @app.post('/devis/<token>/refuser')
    def public_quote_refuse(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        if not q:
            tc.close(); abort(404)
        if q['status']=='sent':
            tc.execute("UPDATE outgoing_quotes SET status='refused',refused_at=? WHERE id=?",
                       (now(),mapping['quote_local_id']))
            tc.commit()
        tc.close()
        return redirect(url_for('public_quote_view',token=token))

    @app.route('/devis/<token>/pdf')
    def public_quote_pdf(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        
        if q:
            from profitos.entities import resolve_entity
            company_row=_seller_identity(tc,q['entity_id'])
        else:
            company_row=None
        tc.close()
        if not q: abort(404)
        pdf_bytes=_render_quote_pdf(q,company_row)
        if pdf_bytes is None: abort(404)
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'inline; filename="{q["quote_number"]}.pdf"'})

    @app.route('/facture/<token>')
    def public_invoice_view(token):
        """Vue publique d'une facture émise — aucune authentification requise.
        Résolution par token via la table auth partagée (pas de dépendance à une
        session — un client externe n'en a pas), sans exposer la structure interne."""
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_invoice_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        inv=tc.execute('SELECT * FROM outgoing_invoices WHERE id=?',(mapping['invoice_local_id'],)).fetchone()
        
        if inv:
            from profitos.entities import resolve_entity
            company_row=_seller_identity(tc,inv['entity_id'])
        else:
            company_row=None
        tc.close()
        if not inv: abort(404)
        # Seule une facture émise est consultable publiquement : jamais un brouillon
        # (sans valeur de facture) ni une facture annulée.
        if inv['status'] not in ('sent','partially_paid','paid'): abort(404)
        items=json.loads(inv['line_items'] or '[]')
        return render_template('invoicing_public.html',inv=inv,items=items,company=company_row,token=token,
                               vat_breakdown=_pdf_breakdown(inv,company_row,items),
                               legal_mentions=_legal_mentions(inv,company_row,fr_date=fr_date))

    @app.route('/facture/<token>/pdf')
    def public_invoice_pdf(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_invoice_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        inv=tc.execute('SELECT * FROM outgoing_invoices WHERE id=?',(mapping['invoice_local_id'],)).fetchone()
        
        if inv:
            from profitos.entities import resolve_entity
            company_row=_seller_identity(tc,inv['entity_id'])
        else:
            company_row=None
        tc.close()
        if not inv: abort(404)
        # Seule une facture émise est consultable publiquement : jamais un brouillon
        # (sans valeur de facture) ni une facture annulée.
        if inv['status'] not in ('sent','partially_paid','paid'): abort(404)
        pdf_bytes=_render_invoice_pdf(inv,company_row)
        if pdf_bytes is None: abort(404)
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'inline; filename="{inv["invoice_number"]}.pdf"'})



    @app.route('/facturation/recurrentes',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def recurring_invoices():
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        if request.method=='POST':
            client_name=(request.form.get('client_name') or '').strip()
            client_email=(request.form.get('client_email') or '').strip()
            client_address=(request.form.get('client_address') or '').strip()
            start=(request.form.get('next_run_date') or '').strip()
            frequency=(request.form.get('frequency') or 'monthly').strip()
            if frequency not in ('weekly','monthly','quarterly','yearly'): frequency='monthly'
            try: due_days=max(0,min(365,int(request.form.get('due_days') or 30)))
            except ValueError: due_days=30
            try: interval_count=max(1,min(24,int(request.form.get('interval_count') or 1)))
            except ValueError: interval_count=1
            try: date.fromisoformat(start)
            except ValueError:
                c.close(); flash("Date de première génération invalide."); return redirect(url_for('recurring_invoices'))
            items=_compute_line_items(request.form)
            if not client_name or not items:
                c.close(); flash("Client et au moins une ligne sont requis."); return redirect(url_for('recurring_invoices'))
            items_error=_line_items_error(items)
            if items_error:
                c.close(); flash(items_error); return redirect(url_for('recurring_invoices'))
            end=(request.form.get('end_date') or '').strip() or None
            if end:
                try: date.fromisoformat(end)
                except ValueError:
                    c.close(); flash("Date de fin invalide."); return redirect(url_for('recurring_invoices'))
                if end<start:
                    c.close(); flash("La date de fin doit être postérieure à la première génération."); return redirect(url_for('recurring_invoices'))
            c.execute("""INSERT INTO recurring_invoice_templates(entity_id,client_name,client_address,client_email,client_siren,
                line_items,notes,operation_nature,vat_on_debits,delivery_address,frequency,interval_count,due_days,next_run_date,
                end_date,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (eid,client_name,client_address,client_email,(request.form.get('client_siren') or '').strip() or None,
                 json.dumps(items,ensure_ascii=False),(request.form.get('notes') or '').strip(),
                 request.form.get('operation_nature') or 'services',1 if request.form.get('vat_on_debits')=='on' else 0,
                 (request.form.get('delivery_address') or '').strip() or None,frequency,interval_count,due_days,start,end,now(),now()))
            c.commit(); c.close(); flash("Facturation récurrente créée."); return redirect(url_for('recurring_invoices'))
        rows=c.execute("SELECT * FROM recurring_invoice_templates WHERE entity_id IS ? ORDER BY id DESC",(eid,)).fetchall()
        clients=c.execute("SELECT * FROM invoicing_clients WHERE entity_id IS ? ORDER BY lower(name)",(eid,)).fetchall()
        c.close()
        return render_template('recurring_invoices.html',rows=rows,clients=clients,today=paris_today().isoformat())

    @app.route('/facturation/recurrentes/generer',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def recurring_invoices_generate():
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        try:
            created=_generate_due_recurring_invoices(c,eid)
            c.commit()
        except Exception:
            c.rollback(); c.close(); raise
        c.close()
        # Les jetons publics vivent dans auth.db : on les inscrit après commit tenant.
        if created:
            ac=auth_cx()
            try:
                for invoice_id,number,token in created:
                    ac.execute('INSERT OR IGNORE INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
                               (token,session['org_id'],invoice_id,now()))
                ac.commit()
            finally: ac.close()
        flash(f"{len(created)} facture(s) récurrente(s) générée(s) en brouillon.")
        return redirect(url_for('recurring_invoices'))

    @app.route('/facturation/recurrentes/<int:template_id>/statut',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def recurring_invoice_status(template_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); action=request.form.get('action')
        new_status={'pause':'paused','resume':'active','stop':'stopped'}.get(action)
        if not new_status: abort(400)
        c=cx(); row=c.execute("SELECT id FROM recurring_invoice_templates WHERE id=? AND entity_id IS ?",(template_id,eid)).fetchone()
        if not row: c.close(); abort(404)
        c.execute("UPDATE recurring_invoice_templates SET status=?,updated_at=? WHERE id=? AND entity_id IS ?",(new_status,now(),template_id,eid))
        c.commit(); c.close(); flash("Statut de la récurrence mis à jour.")
        return redirect(url_for('recurring_invoices'))


# --- Documents PDF (facture, avoir, devis, pro-forma, bon de livraison, Factur-X) -----------
from profitos import invoice_pdf as _pdf

DRAFT_WATERMARK = "BROUILLON — document sans valeur de facture"
CANCELLED_WATERMARK = "ANNULÉE"


def _pdf_rows(items):
    rows = []
    for it in items:
        rows.append((it.get('label') or '', amounts.fr_qty(it.get('qty', 1)),
                     amounts.fr_price(it.get('unit_price', 0)) + ' €',
                     amounts.fr_rate(it.get('vat_rate', 0)), _pdf.money(it.get('line_total', 0))))
    return rows


def _pdf_buyer(inv):
    return {'name': _g(inv, 'client_name', ''), 'address': _g(inv, 'client_address', ''),
            'siren': _g(inv, 'client_siren'), 'vat_number': _g(inv, 'client_vat_number'),
            'country': (_g(inv, 'counterparty_country', '') or 'FR').upper()}


def _pdf_breakdown(inv, company, items):
    out = []
    for b in amounts.vat_breakdown(items):
        category = _vat_category(inv, company, b['rate'])
        label = amounts.fr_rate(b['rate']) if category == 'S' else {
            'Z': '0 %', 'E': 'Exonéré', 'AE': 'Autoliquidation', 'K': 'Intracom.', 'G': 'Export'}.get(category, '0 %')
        out.append(dict(b, label=label))
    return out


def _pdf_totals(subtotal, vat, total, ttc_label='Total TTC'):
    return [('Total HT', _pdf.money(subtotal), False), ('Total TVA', _pdf.money(vat), False),
            (ttc_label, _pdf.money(total), True)]


def build_invoice_document(inv, company, proforma=False):
    """Contenu imprimable d'une facture (toutes les mentions obligatoires)."""
    items = json.loads(_g(inv, 'line_items', '[]') or '[]')
    subtotal, vat, total = amounts.totals(items)
    status = _g(inv, 'status', 'draft')
    issued = status in ('sent', 'partially_paid', 'paid')
    kind = _g(inv, 'invoice_kind', 'standard') or 'standard'
    kind_label = {'deposit': "Facture d'acompte", 'final': 'Facture finale'}.get(kind, 'Facture')
    if proforma:
        title = 'Facture pro forma'
        subtitle = f"Réf. {invoice_label(inv)} — document sans valeur comptable ni fiscale, ne constitue pas une facture."
    elif issued or (status == 'cancelled' and _g(inv, 'invoice_number')):
        title = f"{kind_label} n° {_g(inv, 'invoice_number')}"
        subtitle = None
    else:
        title = f"{kind_label} — {invoice_label(inv)}"
        subtitle = "Projet de facture : le numéro et la date d'émission sont attribués à l'émission."
    meta = [("Date d'émission", _pdf.fr_date(_g(inv, 'issue_date')) if issued else ('à l\'émission' if not proforma else _pdf.fr_date(paris_today().isoformat()))),
            ("Date d'échéance", _pdf.fr_date(_g(inv, 'due_date')))]
    nature = _g(inv, 'operation_nature', 'services') or 'services'
    sale_date = _g(inv, 'service_date') or (_g(inv, 'issue_date') if issued else None)
    details = [("Nature de l'opération", OPERATION_NATURE_LABELS.get(nature, nature)),
               ('Date de la vente / de la prestation', _pdf.fr_date(sale_date) if sale_date else None)]
    if _is_public_buyer(inv):
        details.append(('Acheteur public', 'facture à déposer sur Chorus Pro'))
    totals = _pdf_totals(subtotal, vat, total)
    if kind == 'final' and float(_g(inv, 'deposit_applied_subtotal', 0) or 0):
        applied = float(_g(inv, 'deposit_applied_subtotal', 0) or 0) + float(_g(inv, 'deposit_applied_vat', 0) or 0)
        totals.insert(0, ('Acomptes déduits (TTC)', _pdf.money(-applied), False))
    return {
        'title': title, 'subtitle': subtitle, 'seller': dict(company or {}), 'buyer': _pdf_buyer(inv),
        'delivery_address': _g(inv, 'delivery_address'), 'meta': meta, 'details': details,
        'rows': _pdf_rows(items), 'vat_breakdown': _pdf_breakdown(inv, company, items), 'totals': totals,
        'mentions': _legal_mentions(inv, company, fr_date=_pdf.fr_date, include_payment=not proforma),
        'notes': _g(inv, 'notes'), 'footer': _seller_legal_footer(company or {}),
    }


def _invoice_watermark(inv):
    status = _g(inv, 'status', 'draft')
    if status == 'draft':
        return DRAFT_WATERMARK
    if status == 'cancelled':
        return CANCELLED_WATERMARK
    return None


def _render_invoice_pdf(inv, company_row):
    """PDF d'une facture. Un brouillon porte le filigrane « BROUILLON — document sans
    valeur de facture », une facture annulée « ANNULÉE ». Retourne None sans fpdf2."""
    doc = build_invoice_document(inv, company_row)
    return _pdf.render_document(doc, watermark=_invoice_watermark(inv), title=doc['title'])


def _render_proforma_pdf(inv, company_row):
    """Pro-forma d'un brouillon : mêmes montants, explicitement sans valeur comptable ni
    fiscale ; ne consomme aucun numéro de facture."""
    doc = build_invoice_document(inv, company_row, proforma=True)
    return _pdf.render_document(doc, watermark='PRO FORMA', title=doc['title'])


def facturx_xmp(title, doc_type='INVOICE'):
    """Métadonnées XMP d'un PDF/A-3B Factur-X (schéma d'extension fx déclaré)."""
    safe_title = (title or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
   <pdfaid:part>3</pdfaid:part>
   <pdfaid:conformance>B</pdfaid:conformance>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{safe_title}</rdf:li></rdf:Alt></dc:title>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
   <pdf:Producer>ProfitOS</pdf:Producer>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
   <fx:DocumentType>{doc_type}</fx:DocumentType>
   <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
   <fx:Version>1.0</fx:Version>
   <fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/" xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
   <pdfaExtension:schemas>
    <rdf:Bag>
     <rdf:li rdf:parseType="Resource">
      <pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>
      <pdfaSchema:namespaceURI>urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#</pdfaSchema:namespaceURI>
      <pdfaSchema:prefix>fx</pdfaSchema:prefix>
      <pdfaSchema:property>
       <rdf:Seq>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentFileName</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>The name of the embedded XML document</pdfaProperty:description></rdf:li>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentType</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>The type of the hybrid document in capital letters, e.g. INVOICE or ORDER</pdfaProperty:description></rdf:li>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>Version</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>The actual version of the standard applying to the embedded XML document</pdfaProperty:description></rdf:li>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>ConformanceLevel</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>The conformance level of the embedded XML document</pdfaProperty:description></rdf:li>
       </rdf:Seq>
      </pdfaSchema:property>
     </rdf:li>
    </rdf:Bag>
   </pdfaExtension:schemas>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''


def _render_facturx(doc, xml_bytes, title, description):
    """PDF/A-3B avec factur-x.xml embarqué (relation « Alternative ») et XMP Factur-X.
    Nécessite fpdf2 ≥ 2.8.7 ; sinon None (l'appelant affiche un message)."""
    if not _pdf.pdfa_supported():
        return None
    from fpdf.enums import DocumentCompliance
    attachment = {'bytes': xml_bytes, 'basename': 'factur-x.xml', 'mime_type': 'text/xml',
                  'desc': description, 'associated_file_relationship': 'Alternative', 'compress': True,
                  # /Params /ModDate du fichier embarqué, attendu par la spécification Factur-X.
                  'modification_date': datetime.now(timezone.utc)}
    return _pdf.render_document(doc, compliance=DocumentCompliance.PDFA_3B, attachments=[attachment],
                                xmp=facturx_xmp(title), title=title)


def render_facturx_pdf(inv, company_row):
    """Factur-X (profil EN16931) : page visible identique à la facture PDF (dates
    d'émission et d'échéance, n° de TVA du vendeur, mentions) + factur-x.xml embarqué.
    Retourne None si fpdf2 est trop ancien (< 2.8.7) ou si les polices manquent.

    ATTENTION : la conformité PDF/A-3 finale n'a pas été validée contre un outil
    officiel (veraPDF, FNFE-MPE) dans cet environnement."""
    items = json.loads(inv['line_items'] or '[]')
    # Facture d'acompte : type 386 (prepayment invoice), et non 380.
    type_code = '386' if (_g(inv, 'invoice_kind', 'standard') or 'standard') == 'deposit' else '380'
    xml_bytes = generate_facturx_xml(inv, items, company_row, type_code=type_code)
    doc = build_invoice_document(inv, company_row)
    return _render_facturx(doc, xml_bytes, doc['title'], 'Factur-X invoice data (EN16931)')


def _adjust_credit_items(items, target_ttc):
    """Ajuste les lignes HT d'un avoir partiel au centime pour que le TTC (TVA arrondie
    par taux) tombe exactement sur le montant demandé. Avec plusieurs taux, corriger une
    seule ligne ne suffit pas toujours (12,74 / 12,76 € pour 12,75 € demandés) : on essaie
    des corrections d'une ligne, puis de deux lignes, de quelques centimes."""
    import itertools
    def ttc(its):
        return round(_totals(its)[2], 2)
    def apply(base, deltas):
        out = [dict(x) for x in base]
        for idx, d in deltas:
            lt = round(out[idx]['line_total'] + d / 100.0, 2)
            out[idx]['line_total'] = lt
            out[idx]['unit_price'] = abs(lt)
            out[idx]['qty'] = -1.0 if lt < 0 else 1.0
        return out
    target = round(float(target_ttc), 2)
    if not items or abs(ttc(items) - target) < 0.005:
        return items
    order = sorted(range(len(items)), key=lambda i: -abs(items[i]['line_total']))
    steps = [d for k in range(1, 6) for d in (k, -k)]
    for i in order:
        for d in steps:
            cand = apply(items, [(i, d)])
            if abs(ttc(cand) - target) < 0.005:
                return cand
    for i, j in itertools.combinations(order[:6], 2):
        for di in steps:
            for dj in steps:
                cand = apply(items, [(i, di), (j, dj)])
                if abs(ttc(cand) - target) < 0.005:
                    return cand
    # Inatteignable (cas extrême) : le plus proche sans dépasser la cible.
    best = items
    for i in order:
        for d in steps:
            cand = apply(items, [(i, d)])
            if ttc(cand) <= target + 0.005 and abs(ttc(cand) - target) < abs(ttc(best) - target):
                best = cand
    return best


def credit_note_items(original_items, ratio=None):
    """Lignes d'un avoir : copie exacte (avoir total) ou prorata par ligne (avoir partiel).
    Chaque ligne garde sa nature (biens/services) et son taux : l'avoir suit les comptes
    de la facture d'origine. Quantité × prix = total de ligne."""
    items = []
    for it in original_items:
        x = {'label': f"Avoir — {it.get('label', '')}", 'vat_rate': amounts.rate_key(it.get('vat_rate', 0))}
        if it.get('nature'):
            x['nature'] = it['nature']
        if it.get('deposit_deduction'):
            x['deposit_deduction'] = True
        if ratio is None:
            x['qty'] = float(it.get('qty', 1) or 1)
            x['unit_price'] = float(it.get('unit_price', 0) or 0)
            x['line_total'] = amounts.money(it.get('line_total', 0))
        else:
            lt = amounts.money(float(it.get('line_total', 0) or 0) * ratio)
            x['qty'] = -1.0 if lt < 0 else 1.0
            x['unit_price'] = abs(lt)
            x['line_total'] = lt
        items.append(x)
    return items


def build_credit_document(credit, original, company):
    items = json.loads(_g(credit, 'line_items', '[]') or '[]')
    subtotal, vat, total = amounts.totals(items)
    base = dict(original) if original is not None else {}
    base.update({'notes': None, 'due_date': None})
    orig_date = _pdf.fr_date(_g(original, 'issue_date')) if original is not None else ''
    # Titre réglementaire complet : l'avoir référence la facture rectifiée et sa date.
    title = (f"Facture d'avoir n° {_g(credit, 'credit_number')} — rectifie la facture n° "
             f"{_g(credit, 'original_invoice_number')}" + (f" du {orig_date}" if orig_date else ''))
    subtitle = None
    details = [('Motif', _g(credit, 'reason')),
               ("Nature de l'opération", OPERATION_NATURE_LABELS.get(_g(original, 'operation_nature', 'services') or 'services'))]
    mentions = _legal_mentions(base, company, fr_date=_pdf.fr_date, include_payment=False)
    mentions.append("Montant à déduire de la facture rectifiée ou à rembourser au client.")
    return {
        'title': title, 'title_size': 12, 'subtitle': subtitle, 'seller': dict(company or {}), 'buyer': _pdf_buyer(base or credit),
        'delivery_address': None, 'meta': [("Date d'émission", _pdf.fr_date(_g(credit, 'issue_date')))],
        'details': details, 'rows': _pdf_rows(items), 'vat_breakdown': _pdf_breakdown(base, company, items),
        'totals': _pdf_totals(subtotal, vat, total, "Total TTC de l'avoir"), 'mentions': mentions,
        'notes': None, 'footer': _seller_legal_footer(company or {}),
    }


def _render_credit_pdf(credit, company_row, original=None):
    doc = build_credit_document(credit, original, company_row)
    return _pdf.render_document(doc, title=doc['title'])


def generate_credit_facturx_xml(credit, original_invoice, company):
    """CII EN16931 d'un avoir : code 381, lignes et totaux de l'avoir, référence à la
    facture d'origine (InvoiceReferencedDocument, après la MonetarySummation)."""
    items=json.loads(credit['line_items'] or '[]')
    proxy=dict(original_invoice)
    proxy.update(invoice_number=credit['credit_number'],issue_date=credit['issue_date'],
                 due_date=None,subtotal=credit['subtotal'],vat_amount=credit['vat_amount'],total=credit['total'],
                 notes=None)
    # Un avoir n'a pas d'échéance : ses conditions indiquent l'imputation ou le
    # remboursement (BR-CO-25 : montant dû positif => conditions de paiement présentes).
    xml=generate_facturx_xml(proxy,items,company,type_code='381',
                             referenced={'number':credit['original_invoice_number'],'issue_date':original_invoice['issue_date']},
                             payment_terms_text=f"Avoir imputable sur la facture n° {credit['original_invoice_number']} ou remboursable au client.")
    root=ET.fromstring(xml.split(b'\n',1)[1]); ns=_CII_NS
    doc=root.find(f'{{{ns["rsm"]}}}ExchangedDocument')
    type_code=doc.find(f'{{{ns["ram"]}}}TypeCode') if doc is not None else None
    if type_code is None: raise ValueError("CII invalide : TypeCode absent")
    type_code.text='381'
    return b'<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(root,encoding='utf-8')


def render_credit_facturx_pdf(credit, original_invoice, company_row):
    """PDF/A-3 Factur-X d'avoir avec CII 381 embarqué (fpdf2 ≥ 2.8.7)."""
    xml_bytes=generate_credit_facturx_xml(credit,original_invoice,company_row)
    doc=build_credit_document(credit,original_invoice,company_row)
    return _render_facturx(doc,xml_bytes,doc['title'],'Factur-X credit note data (EN16931)')


def _render_quote_pdf(q, company_row):
    items = json.loads(q['line_items'] or '[]')
    subtotal, vat, total = amounts.totals(items)
    proxy = dict(q)
    meta = [('Date', _pdf.fr_date(q['issue_date'])), ("Valable jusqu'au", _pdf.fr_date(q['valid_until']))]
    nature = _g(q, 'operation_nature', 'services') or 'services'
    doc = {
        'title': f"Devis n° {q['quote_number']}", 'subtitle': None, 'seller': dict(company_row or {}),
        'buyer': _pdf_buyer(proxy), 'buyer_title': 'Client', 'delivery_address': _g(q, 'delivery_address'),
        'meta': meta, 'details': [("Nature de l'opération", OPERATION_NATURE_LABELS.get(nature, nature))],
        'rows': _pdf_rows(items), 'vat_breakdown': _pdf_breakdown(proxy, company_row, items),
        'totals': _pdf_totals(subtotal, vat, total), 'mentions': _legal_mentions(proxy, company_row, fr_date=_pdf.fr_date, include_payment=False),
        'notes': q['notes'], 'footer': _seller_legal_footer(company_row or {}),
        'signature_box': "Bon pour accord — date et signature du client :",
    }
    return _pdf.render_document(doc, title=doc['title'])


def _render_delivery_pdf(row, company_row):
    items = json.loads(row['line_items'] or '[]')
    doc = {
        'title': f"Bon de livraison n° {row['delivery_number']}", 'subtitle': None, 'seller': dict(company_row or {}),
        'buyer': {'name': row['client_name'], 'address': _g(row, 'billing_address') or row['client_address'],
                  'siren': _g(row, 'client_siren')},
        'buyer_title': 'Client', 'delivery_address': row['client_address'], 'delivery_title': 'Livré à',
        'meta': [('Date de livraison', _pdf.fr_date(row['delivery_date']))], 'details': [],
        'columns': [('Désignation', 140, 'L'), ('Quantité livrée', 40, 'R')],
        'rows': [(it.get('label') or '', amounts.fr_qty(it.get('qty', 1))) for it in items],
        'vat_breakdown': [], 'totals': [], 'mentions': [], 'notes': row['notes'],
        'footer': _seller_legal_footer(company_row or {}),
        'signature_box': "Reçu le : ____/____/________ — nom et signature du destinataire :",
    }
    return _pdf.render_document(doc, title=doc['title'])
