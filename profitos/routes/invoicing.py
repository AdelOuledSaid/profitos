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
from profitos.plan_usage import quota_state, record_usage
from profitos.feature_access import requires_paid_plan
from profitos.entities import current_entity_id
from profitos.accounting import (generate_sale_entry, generate_sale_payment_entry, generate_sale_partial_payment_entry,
    generate_sale_credit_entry, generate_customer_deposit_entry, generate_customer_final_entry, generate_purchase_entry, generate_purchase_credit_entry,
    generate_purchase_payment_entry, generate_purchase_partial_payment_entry, AccountingError)
from profitos.webhooks_outbound import deliver_webhook
from profitos.weinvoice import (submit_invoice_file, get_invoice_timeline,
    invoice_status_from_timeline, sandbox_force_invoice_status,
    WeInvoiceAPIError, WeInvoiceConfigError, list_inbound_invoices,
    get_inbound_invoice_content, download_inbound_invoice_readable, download_inbound_invoice_original, apply_inbound_lifecycle_action)


def _compute_line_items(form):
    """Lit jusqu'à 8 lignes de facture depuis le formulaire (libellé, quantité, prix
    unitaire, taux de TVA). Les lignes vides sont ignorées — pas de JS dynamique
    nécessaire, conforme à la politique de sécurité stricte de l'app (pas de script
    inline)."""
    items=[]
    for i in range(1,9):
        label=form.get(f'label_{i}','').strip()
        if not label: continue
        try: qty=float(form.get(f'qty_{i}','1').replace(',','.'))
        except ValueError: qty=1
        try: unit_price=float(form.get(f'price_{i}','0').replace(',','.'))
        except ValueError: unit_price=0
        try: vat_rate=float(form.get(f'vat_{i}','20').replace(',','.'))
        except ValueError: vat_rate=20
        line_total=qty*unit_price
        items.append({'label':label,'qty':qty,'unit_price':unit_price,'vat_rate':vat_rate,'line_total':line_total})
    return items


def _totals(items):
    subtotal=sum(i['line_total'] for i in items)
    vat_amount=sum(i['line_total']*i['vat_rate']/100 for i in items)
    return subtotal,vat_amount,subtotal+vat_amount


def _check_mandatory_mentions(inv,company):
    """Vérification des mentions obligatoires les plus courantes pour une facture
    française (Code de commerce art. L441-9, CGI). Vérification indicative — ne
    remplace pas un avis juridique ou comptable professionnel, et ne constitue pas
    une certification de conformité Factur-X."""
    checks=[]
    checks.append({'label':"Nom de l'émetteur",'ok':bool(company and company['name'])})
    checks.append({'label':"Adresse de l'émetteur",'ok':bool(company and company['address'])})
    siret=(company['siret'] if company else '') or ''
    checks.append({'label':"SIRET de l'émetteur (14 chiffres)",'ok':bool(re.fullmatch(r'\d{14}',re.sub(r'\D','',siret)))})
    checks.append({'label':"N° TVA intracommunautaire de l'émetteur",'ok':bool(company and company['vat_number'])})
    checks.append({'label':'Nom du client','ok':bool(inv['client_name'])})
    checks.append({'label':'Adresse du client','ok':bool(inv['client_address'])})
    siren=(inv['client_siren'] if 'client_siren' in inv.keys() else '') or ''
    checks.append({'label':'SIREN du client (9 chiffres)','ok':bool(re.fullmatch(r'\d{9}',siren))})
    checks.append({'label':'Numéro de facture unique','ok':bool(inv['invoice_number'])})
    checks.append({'label':"Date d'émission",'ok':bool(inv['issue_date'])})
    checks.append({'label':"Date d'échéance / conditions de règlement",'ok':bool(inv['due_date'])})
    op_nature=inv['operation_nature'] if 'operation_nature' in inv.keys() else None
    checks.append({'label':"Nature de l'opération (biens / services / mixte)",'ok':bool(op_nature)})
    if op_nature in ('biens','mixte'):
        delivery=inv['delivery_address'] if 'delivery_address' in inv.keys() else None
        checks.append({'label':'Adresse de livraison (requise pour une livraison de biens)','ok':bool(delivery)})
    try:
        items=json.loads(inv['line_items'] or '[]')
    except (json.JSONDecodeError,TypeError):
        items=[]
    checks.append({'label':'Au moins une ligne avec désignation, quantité, prix unitaire et taux de TVA','ok':bool(items)})
    checks.append({'label':'Montants HT et TTC calculés','ok':(inv['subtotal'] is not None and inv['total'] is not None)})
    missing=[c['label'] for c in checks if not c['ok']]
    return checks,missing


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


def generate_facturx_xml(inv, items, company):
    """Génère le XML Cross Industry Invoice (CII), profil EN16931, encapsulable dans
    un PDF/A-3 pour former un fichier Factur-X. Les données proviennent uniquement
    de la facture et du profil entreprise déjà enregistrés dans ProfitOS — aucune
    valeur n'est inventée. Structure vérifiée (espaces de noms, imbrication, valeurs
    XSD `decimal`/`date`) mais non testée contre un validateur officiel (FNFE-MPE,
    Chorus Pro) : une vérification externe reste nécessaire avant mise en production.
    """
    ns = _CII_NS
    for prefix, uri in ns.items():
        ET.register_namespace(prefix, uri)

    root = ET.Element(f'{{{ns["rsm"]}}}CrossIndustryInvoice')

    ctx = ET.SubElement(root, f'{{{ns["rsm"]}}}ExchangedDocumentContext')
    guideline = ET.SubElement(ctx, f'{{{ns["ram"]}}}GuidelineSpecifiedDocumentContextParameter')
    ET.SubElement(guideline, f'{{{ns["ram"]}}}ID').text = 'urn:cen.eu:en16931:2017'

    doc = ET.SubElement(root, f'{{{ns["rsm"]}}}ExchangedDocument')
    ET.SubElement(doc, f'{{{ns["ram"]}}}ID').text = inv['invoice_number'] or ''
    ET.SubElement(doc, f'{{{ns["ram"]}}}TypeCode').text = '380'  # 380 = facture commerciale
    issue = ET.SubElement(doc, f'{{{ns["ram"]}}}IssueDateTime')
    issue_str = ET.SubElement(issue, f'{{{ns["udt"]}}}DateTimeString')
    issue_str.set('format', '102')
    issue_str.text = _cii_date(inv['issue_date']) or ''

    txn = ET.SubElement(root, f'{{{ns["rsm"]}}}SupplyChainTradeTransaction')

    # --- Lignes de facture ---
    for idx, it in enumerate(items, start=1):
        line = ET.SubElement(txn, f'{{{ns["ram"]}}}IncludedSupplyChainTradeLineItem')
        doc_line = ET.SubElement(line, f'{{{ns["ram"]}}}AssociatedDocumentLineDocument')
        ET.SubElement(doc_line, f'{{{ns["ram"]}}}LineID').text = str(idx)
        product = ET.SubElement(line, f'{{{ns["ram"]}}}SpecifiedTradeProduct')
        ET.SubElement(product, f'{{{ns["ram"]}}}Name').text = it.get('label', '')
        agreement = ET.SubElement(line, f'{{{ns["ram"]}}}SpecifiedLineTradeAgreement')
        price = ET.SubElement(agreement, f'{{{ns["ram"]}}}NetPriceProductTradePrice')
        _cii_amount(price, 'ChargeAmount', it.get('unit_price', 0), ns)
        delivery = ET.SubElement(line, f'{{{ns["ram"]}}}SpecifiedLineTradeDelivery')
        qty = ET.SubElement(delivery, f'{{{ns["ram"]}}}BilledQuantity')
        qty.set('unitCode', 'C62')  # C62 = unité, pièce
        qty.text = f'{it.get("qty", 0):g}'
        settlement = ET.SubElement(line, f'{{{ns["ram"]}}}SpecifiedLineTradeSettlement')
        tax = ET.SubElement(settlement, f'{{{ns["ram"]}}}ApplicableTradeTax')
        ET.SubElement(tax, f'{{{ns["ram"]}}}TypeCode').text = 'VAT'
        ET.SubElement(tax, f'{{{ns["ram"]}}}CategoryCode').text = 'S'  # S = taux standard
        ET.SubElement(tax, f'{{{ns["ram"]}}}RateApplicablePercent').text = f'{it.get("vat_rate", 0):g}'
        line_summation = ET.SubElement(settlement, f'{{{ns["ram"]}}}SpecifiedTradeSettlementLineMonetarySummation')
        _cii_amount(line_summation, 'LineTotalAmount', it.get('line_total', 0), ns)

    # --- Vendeur / Acheteur ---
    agreement = ET.SubElement(txn, f'{{{ns["ram"]}}}ApplicableHeaderTradeAgreement')
    seller = ET.SubElement(agreement, f'{{{ns["ram"]}}}SellerTradeParty')
    ET.SubElement(seller, f'{{{ns["ram"]}}}Name').text = (company['name'] if company else '') or ''
    seller_siret = re.sub(r'\D', '', (company['siret'] if company else '') or '')
    if len(seller_siret) == 14:
        seller_org = ET.SubElement(seller, f'{{{ns["ram"]}}}SpecifiedLegalOrganization')
        seller_id = ET.SubElement(seller_org, f'{{{ns["ram"]}}}ID')
        seller_id.set('schemeID', '0002')  # 0002 = SIRENE (France)
        seller_id.text = seller_siret[:9]  # SIREN = 9 premiers chiffres du SIRET
    seller_addr = ET.SubElement(seller, f'{{{ns["ram"]}}}PostalTradeAddress')
    ET.SubElement(seller_addr, f'{{{ns["ram"]}}}LineOne').text = (company['address'] if company else '') or ''
    ET.SubElement(seller_addr, f'{{{ns["ram"]}}}CountryID').text = 'FR'
    # BT-34 — adresse électronique vendeur, pour le routage via une plateforme agréée.
    # Adresse "bare-SIREN" : schemeID 0225 (confirmé par WeInvoice, erreur BR-FR-21 —
    # ne pas confondre avec le schemeID 0002 utilisé pour SpecifiedLegalOrganization,
    # qui identifie l'entité légale, pas l'adresse de routage).
    if len(seller_siret) == 14:
        seller_uri = ET.SubElement(seller, f'{{{ns["ram"]}}}URIUniversalCommunication')
        seller_uri_id = ET.SubElement(seller_uri, f'{{{ns["ram"]}}}URIID')
        seller_uri_id.set('schemeID', '0225')
        seller_uri_id.text = seller_siret[:9]
    if company and company['vat_number']:
        seller_tax = ET.SubElement(seller, f'{{{ns["ram"]}}}SpecifiedTaxRegistration')
        seller_vat = ET.SubElement(seller_tax, f'{{{ns["ram"]}}}ID')
        seller_vat.set('schemeID', 'VA')
        seller_vat.text = company['vat_number']

    buyer = ET.SubElement(agreement, f'{{{ns["ram"]}}}BuyerTradeParty')
    ET.SubElement(buyer, f'{{{ns["ram"]}}}Name').text = inv['client_name'] or ''
    client_siren = inv['client_siren'] if 'client_siren' in inv.keys() else None
    if client_siren and re.fullmatch(r'\d{9}', client_siren):
        buyer_org = ET.SubElement(buyer, f'{{{ns["ram"]}}}SpecifiedLegalOrganization')
        buyer_id = ET.SubElement(buyer_org, f'{{{ns["ram"]}}}ID')
        buyer_id.set('schemeID', '0002')
        buyer_id.text = client_siren
    buyer_addr = ET.SubElement(buyer, f'{{{ns["ram"]}}}PostalTradeAddress')
    ET.SubElement(buyer_addr, f'{{{ns["ram"]}}}LineOne').text = inv['client_address'] or ''
    ET.SubElement(buyer_addr, f'{{{ns["ram"]}}}CountryID').text = 'FR'
    # BT-49 — adresse électronique acheteur, même logique que BT-34 côté vendeur
    # (schemeID 0225, pas 0002 — voir commentaire ci-dessus).
    if client_siren and re.fullmatch(r'\d{9}', client_siren):
        buyer_uri = ET.SubElement(buyer, f'{{{ns["ram"]}}}URIUniversalCommunication')
        buyer_uri_id = ET.SubElement(buyer_uri, f'{{{ns["ram"]}}}URIID')
        buyer_uri_id.set('schemeID', '0225')
        buyer_uri_id.text = client_siren

    # --- Livraison (uniquement si une adresse de livraison est renseignée) ---
    delivery_address = inv['delivery_address'] if 'delivery_address' in inv.keys() else None
    if delivery_address:
        delivery_hdr = ET.SubElement(txn, f'{{{ns["ram"]}}}ApplicableHeaderTradeDelivery')
        ship_to = ET.SubElement(delivery_hdr, f'{{{ns["ram"]}}}ShipToTradeParty')
        ship_addr = ET.SubElement(ship_to, f'{{{ns["ram"]}}}PostalTradeAddress')
        ET.SubElement(ship_addr, f'{{{ns["ram"]}}}LineOne').text = delivery_address
        ET.SubElement(ship_addr, f'{{{ns["ram"]}}}CountryID').text = 'FR'
    else:
        ET.SubElement(txn, f'{{{ns["ram"]}}}ApplicableHeaderTradeDelivery')

    # --- Règlement : devise, TVA par taux, échéance, totaux ---
    # Ordre imposé par le schéma CII (xs:sequence) : ApplicableTradeTax doit précéder
    # SpecifiedTradePaymentTerms, qui doit lui-même précéder MonetarySummation.
    settlement_hdr = ET.SubElement(txn, f'{{{ns["ram"]}}}ApplicableHeaderTradeSettlement')
    ET.SubElement(settlement_hdr, f'{{{ns["ram"]}}}InvoiceCurrencyCode').text = 'EUR'

    # Regroupe les lignes par taux de TVA (une ram:ApplicableTradeTax par taux distinct).
    by_rate = {}
    for it in items:
        rate = it.get('vat_rate', 0)
        by_rate.setdefault(rate, {'basis': 0.0, 'tax': 0.0})
        by_rate[rate]['basis'] += it.get('line_total', 0)
        by_rate[rate]['tax'] += it.get('line_total', 0) * rate / 100
    for rate, sums in sorted(by_rate.items()):
        tax_el = ET.SubElement(settlement_hdr, f'{{{ns["ram"]}}}ApplicableTradeTax')
        _cii_amount(tax_el, 'CalculatedAmount', sums['tax'], ns)
        ET.SubElement(tax_el, f'{{{ns["ram"]}}}TypeCode').text = 'VAT'
        _cii_amount(tax_el, 'BasisAmount', sums['basis'], ns)
        ET.SubElement(tax_el, f'{{{ns["ram"]}}}CategoryCode').text = 'S'
        ET.SubElement(tax_el, f'{{{ns["ram"]}}}RateApplicablePercent').text = f'{rate:g}'

    if inv['due_date']:
        terms = ET.SubElement(settlement_hdr, f'{{{ns["ram"]}}}SpecifiedTradePaymentTerms')
        due = ET.SubElement(terms, f'{{{ns["ram"]}}}DueDateDateTime')
        due_str = ET.SubElement(due, f'{{{ns["udt"]}}}DateTimeString')
        due_str.set('format', '102')
        due_str.text = _cii_date(inv['due_date']) or ''

    summation = ET.SubElement(settlement_hdr, f'{{{ns["ram"]}}}SpecifiedTradeSettlementHeaderMonetarySummation')
    subtotal = float(inv['subtotal'] or 0)
    vat_amount = float(inv['vat_amount'] or 0)
    total = float(inv['total'] or 0)
    _cii_amount(summation, 'LineTotalAmount', subtotal, ns)
    _cii_amount(summation, 'TaxBasisTotalAmount', subtotal, ns)
    _cii_amount(summation, 'TaxTotalAmount', vat_amount, ns, currency='EUR')
    _cii_amount(summation, 'GrandTotalAmount', total, ns)
    _cii_amount(summation, 'DuePayableAmount', total, ns)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='utf-8')
    return xml_bytes


# Sous-ensemble des règles métier EN16931 (BR-*) les plus fréquemment citées comme
# cause de rejet en pratique — la couche Schematron, distincte de la simple validité
# XSD. Ce n'est PAS l'intégralité des ~140 règles officielles du CEN/TC 434 : c'est
# une vérification raisonnable, pas une garantie de conformité complète.
def validate_facturx_business_rules(inv, items, company):
    """Vérifie un sous-ensemble de règles métier EN16931 directement sur les données
    de la facture (avant génération XML). Retourne une liste de dicts
    {code, label, ok}. Écrite et testée entièrement en Python pur — sans dépendance
    à fpdf2 ni à un validateur externe, donc vérifiable dans n'importe quel
    environnement, contrairement à l'encapsulation PDF/A-3."""
    rules = []

    def check(code, label, ok):
        rules.append({'code': code, 'label': label, 'ok': bool(ok)})

    check('BR-01', "Identifiant de spécification présent (profil EN16931)", True)  # toujours injecté par generate_facturx_xml
    check('BR-02', 'Numéro de facture renseigné (BT-1)', bool(inv['invoice_number']))
    check('BR-03', "Date d'émission renseignée (BT-2)", bool(inv['issue_date']))
    check('BR-05', 'Code devise renseigné (BT-5 = EUR)', True)
    check('BR-06', 'Nom du vendeur renseigné (BT-27)', bool(company and company['name']))
    check('BR-07', 'Nom de l\'acheteur renseigné (BT-44)', bool(inv['client_name']))
    check('BR-08', "Code pays vendeur renseigné (adresse postale)", bool(company and company['address']))
    check('BR-09', "Code pays acheteur renseigné (adresse postale)", bool(inv['client_address']))
    check('BR-16', 'Au moins une ligne de facture (BG-25)', bool(items))

    # BR-CO-10 : la somme des montants nets de ligne doit correspondre au sous-total HT.
    line_sum = round(sum(it.get('line_total', 0) for it in items), 2)
    subtotal = round(float(inv['subtotal'] or 0), 2)
    check('BR-CO-10', f'Somme des lignes HT ({line_sum:.2f} €) = sous-total facture ({subtotal:.2f} €)',
          abs(line_sum - subtotal) < 0.01)

    # BR-CO-14 : la TVA totale doit correspondre à la somme des TVA par taux.
    vat_sum = round(sum(it.get('line_total', 0) * it.get('vat_rate', 0) / 100 for it in items), 2)
    vat_amount = round(float(inv['vat_amount'] or 0), 2)
    check('BR-CO-14', f'TVA calculée depuis les lignes ({vat_sum:.2f} €) = TVA facture ({vat_amount:.2f} €)',
          abs(vat_sum - vat_amount) < 0.01)

    # BR-CO-15 : total TTC = total HT + TVA.
    total = round(float(inv['total'] or 0), 2)
    check('BR-CO-15', f'Total TTC ({total:.2f} €) = HT + TVA ({subtotal + vat_amount:.2f} €)',
          abs(total - round(subtotal + vat_amount, 2)) < 0.01)

    # BR-CO-16 : montant dû = total TTC (aucun acompte/arrondi géré dans cette version).
    check('BR-CO-16', 'Montant dû cohérent avec le total TTC (aucun acompte)', True)

    # BR-S-* : si une ligne applique un taux de TVA standard (catégorie S), le taux
    # doit être strictement positif — un taux à 0% doit utiliser une autre catégorie
    # (exonération, autoliquidation...), non gérée par cette version simplifiée.
    zero_rate_as_standard = any(it.get('vat_rate', 0) == 0 for it in items)
    check('BR-S-08', "Aucune ligne à 0% de TVA classée par erreur en taux standard", not zero_rate_as_standard)

    missing = [r for r in rules if not r['ok']]
    return rules, missing



def _display_status(inv):
    status=inv['status']
    if status in ('sent','partially_paid') and inv['due_date']:
        try:
            if date.fromisoformat(inv['due_date']) < date.today():
                return 'overdue'
        except (TypeError,ValueError):
            pass
    return status


def _next_invoice_number(c, entity_id=None):
    year=datetime.now(timezone.utc).year
    # Comportement inchangé pour la société mère (entity_id=None) — aucune
    # facture existante ne change de format. Une filiale reçoit sa propre
    # séquence, indépendante, avec un préfixe distinct (obligation légale :
    # chaque entité doit avoir sa propre numérotation continue de factures).
    prefix=f"FA-{year}-" if not entity_id else f"FA-E{entity_id}-{year}-"
    rows=c.execute("SELECT invoice_number FROM outgoing_invoices WHERE invoice_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['invoice_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    candidate=highest+1
    while c.execute("SELECT 1 FROM outgoing_invoices WHERE invoice_number=?",(f"{prefix}{candidate:03d}",)).fetchone():
        candidate+=1
    return f"{prefix}{candidate:03d}"


def _next_credit_number(c, entity_id=None):
    year=datetime.now(timezone.utc).year
    prefix=f"AV-{year}-" if not entity_id else f"AV-E{entity_id}-{year}-"
    rows=c.execute("SELECT credit_number FROM outgoing_credit_notes WHERE credit_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['credit_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    return f"{prefix}{highest+1:03d}"


def _next_delivery_number(c):
    year=datetime.now(timezone.utc).year
    prefix=f"BL-{year}-"
    rows=c.execute("SELECT delivery_number FROM delivery_notes WHERE delivery_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['delivery_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    return f"{prefix}{highest+1:03d}"


def _credited_total(c, invoice_id, entity_id=None):
    row=c.execute("SELECT COALESCE(SUM(total),0) AS n FROM outgoing_credit_notes WHERE original_invoice_id=? AND entity_id IS ? AND status='issued'",(invoice_id,entity_id)).fetchone()
    return float(row['n'] or 0)

def _invoice_paid_total(c, invoice_id, entity_id=None):
    row=c.execute("SELECT COALESCE(SUM(amount),0) AS n FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ?",(invoice_id,entity_id)).fetchone()
    return round(float(row['n'] or 0),2)

def _invoice_balance(c, invoice):
    return max(0.0, round(float(invoice['total'] or 0)-_invoice_paid_total(c,invoice['id'],invoice['entity_id']),2))


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
              (invoice['entity_id'],record_type,'outgoing_invoice',invoice['id'],invoice['issue_date'] or date.today().isoformat(),
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
    year=datetime.now(timezone.utc).year
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
            "Ce document n'a pas de texte exploitable et l'extraction par IA n'est "
            "pas configurée (ANTHROPIC_API_KEY absente côté serveur). Saisis la "
            "facture manuellement, ou configure la clé pour activer l'extraction "
            "automatique des PDF scannés et des photos."
        )
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
        raise ValueError("Réponse d'extraction IA illisible — réessaie ou saisis la facture manuellement.") from e

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
        raise ValueError("Champs non détectés par l'IA : " + ", ".join(missing) + ". Saisis la facture manuellement.")
    try:
        subtotal, vat_amount, total = round(float(subtotal), 2), round(float(vat_amount), 2), round(float(total), 2)
    except (TypeError, ValueError):
        raise ValueError("Montants détectés par l'IA illisibles. Saisis la facture manuellement.")
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
    through=through_date or date.today().isoformat()
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
                number=_next_invoice_number(c,entity_id)
                due=(date.fromisoformat(run_date)+timedelta(days=max(0,int(tpl['due_days'] or 0)))).isoformat()
                token=secrets.token_urlsafe(20)
                c.execute("""INSERT INTO outgoing_invoices(
                    invoice_number,client_name,client_address,client_email,issue_date,due_date,line_items,
                    subtotal,vat_amount,total,notes,status,public_token,created_at,client_siren,operation_nature,
                    vat_on_debits,delivery_address,entity_id,recurring_template_id,recurring_run_date)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)""",
                    (number,tpl['client_name'],tpl['client_address'],tpl['client_email'],run_date,due,tpl['line_items'],
                     subtotal,vat_amount,total,tpl['notes'],token,now(),tpl['client_siren'],tpl['operation_nature'],
                     tpl['vat_on_debits'],tpl['delivery_address'],entity_id,tpl['id'],run_date))
                invoice_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
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
                flash('Le SIREN doit comporter exactement 9 chiffres (laisse vide si inconnu).')
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
                c.close(); flash('Le SIREN doit comporter exactement 9 chiffres (laisse vide si inconnu).')
                return redirect(url_for('invoicing_client_detail',client_id=client_id))
            c.execute("""UPDATE invoicing_clients SET name=?,email=?,address=?,siret=?,vat_number=?,phone=?,notes=?,updated_at=?,siren=? WHERE id=? AND entity_id IS ?""",
              (name,request.form.get('email','').strip(),request.form.get('address','').strip(),
               request.form.get('siret','').strip(),request.form.get('vat_number','').strip(),
               request.form.get('phone','').strip(),request.form.get('notes','').strip(),now(),siren or None,client_id,eid))
            c.commit(); client=c.execute('SELECT * FROM invoicing_clients WHERE id=? AND entity_id IS ?',(client_id,eid)).fetchone()
            flash("Fiche client mise à jour.")
        invoices=c.execute("SELECT * FROM outgoing_invoices WHERE lower(client_name)=lower(?) AND entity_id IS ? ORDER BY id DESC",(client['name'],eid)).fetchall()
        credits=c.execute("SELECT * FROM outgoing_credit_notes WHERE lower(client_name)=lower(?) ORDER BY id DESC",(client['name'],)).fetchall()
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
        clients=c.execute("SELECT * FROM invoicing_clients ORDER BY lower(name)").fetchall()
        if request.method=='POST':
            client_name=request.form.get('client_name','').strip()
            if not client_name:
                c.close(); flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_quote_new'))
            items=_compute_line_items(request.form)
            if not items:
                c.close(); flash("Au moins une ligne de devis est requise.")
                return redirect(url_for('invoicing_quote_new'))
            subtotal=round(sum(x['line_total'] for x in items),2)
            vat_amount=round(sum(x['line_total']*x['vat_rate']/100 for x in items),2)
            total=round(subtotal+vat_amount,2)
            from profitos.entities import current_entity_id
            entity_id=current_entity_id()
            quote_number=_next_quote_number(c,entity_id)
            c.execute("""INSERT INTO outgoing_quotes
              (quote_number,client_name,client_address,client_email,issue_date,valid_until,line_items,
               subtotal,vat_amount,total,notes,status,created_at,entity_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
              (quote_number,client_name,request.form.get('client_address','').strip(),
               request.form.get('client_email','').strip(),date.today().isoformat(),
               request.form.get('valid_until') or None,json.dumps(items,ensure_ascii=False),
               subtotal,vat_amount,total,request.form.get('notes','').strip(),now(),entity_id))
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
        return render_template('invoicing_quote_new.html',company=company_row,clients=clients,today=date.today().isoformat())

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
            flash("Aucun email client renseigné pour ce devis.")
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
        base=os.environ.get('APP_BASE_URL',request.host_url.rstrip('/'))
        link=f"{base}{url_for('public_quote_view',token=quote_token)}"
        html=render_template(
            'email_transactional.html',
            title=f"Devis {q['quote_number']} — {org['name']}",
            intro=f"Voici votre devis {q['quote_number']} de {org['name']}, d'un montant de {fr_number(q['total'],2)} € TTC.",
            cta_label='Consulter et répondre au devis',
            cta_url=link,
            footer="Vous pouvez accepter ou refuser ce devis depuis la page sécurisée."
        )
        result=send_email(q['client_email'],f"Devis {q['quote_number']} — {org['name']}",html)

        if result.get('dry_run'):
            flash(f"Service email non configuré — devis non envoyé réellement (mode simulation) à {q['client_email']}.")
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
        if q['status']!='sent':
            c.close()
            flash("La réponse à ce devis est déjà enregistrée et verrouillée.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
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
        if q['status']!='sent':
            c.close()
            flash("La réponse à ce devis est déjà enregistrée et verrouillée.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
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
            x=dict(it); x['qty']=float(it.get('qty',1)); x['unit_price']=round(float(it.get('unit_price',0))*percent/100,6); x['line_total']=round(float(it.get('line_total',0))*percent/100,2); items.append(x)
        subtotal,vat_amount,total=_totals(items); number=_next_invoice_number(c,eid); token=secrets.token_urlsafe(24); due=(date.today()+timedelta(days=30)).isoformat()
        c.execute("""INSERT INTO outgoing_invoices(invoice_number,client_name,client_address,client_email,issue_date,due_date,line_items,subtotal,vat_amount,total,notes,status,public_token,created_at,entity_id,invoice_kind,source_quote_id,deposit_percent) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?)""",
          (number,q['client_name'],q['client_address'],q['client_email'],date.today().isoformat(),due,json.dumps(items,ensure_ascii=False),subtotal,vat_amount,total,f"Facture d'acompte de {percent:g}% sur le devis {q['quote_number']}.",token,now(),eid,'deposit',quote_id,percent))
        invoice_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close()
        log_activity('DEPOSIT_INVOICE_CREATED',f"Acompte {number} créé depuis {q['quote_number']}"); flash(f"Facture d'acompte {number} créée en brouillon.")
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
                final_items.append({'label':f'Déduction acomptes déjà facturés — TVA {rate:g}%','qty':1.0,'unit_price':-round(amount,2),'vat_rate':rate,'line_total':-round(amount,2)})
        if net_total< -0.01:
            c.close(); flash("Les acomptes dépassent le montant du devis."); return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        number=_next_invoice_number(c,eid); token=secrets.token_urlsafe(24); due=(date.today()+timedelta(days=30)).isoformat()
        notes=f"Facture finale du devis {q['quote_number']}. Acomptes déjà facturés : {dep_ht+dep_vat:.2f} € TTC."
        c.execute("""INSERT INTO outgoing_invoices(invoice_number,client_name,client_address,client_email,issue_date,due_date,line_items,subtotal,vat_amount,total,notes,status,public_token,created_at,entity_id,invoice_kind,source_quote_id,deposit_applied_subtotal,deposit_applied_vat) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?)""",
          (number,q['client_name'],q['client_address'],q['client_email'],date.today().isoformat(),due,json.dumps(final_items,ensure_ascii=False),net_ht,net_vat,net_total,notes,token,now(),eid,'final',quote_id,dep_ht,dep_vat))
        invoice_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("UPDATE outgoing_quotes SET status='converted',converted_invoice_id=? WHERE id=? AND entity_id IS ?",(invoice_id,quote_id,eid)); c.commit(); c.close()
        log_activity('FINAL_INVOICE_CREATED',f"Facture finale {number} créée depuis {q['quote_number']}"); flash(f"Facture finale {number} créée en brouillon.")
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
        invoice_number=_next_invoice_number(c,eid)
        token=secrets.token_urlsafe(24)
        due=(date.today()+timedelta(days=30)).isoformat()
        c.execute("""INSERT INTO outgoing_invoices
          (invoice_number,client_name,client_address,client_email,issue_date,due_date,line_items,
           subtotal,vat_amount,total,notes,status,public_token,created_at,entity_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)""",
          (invoice_number,q['client_name'],q['client_address'],q['client_email'],date.today().isoformat(),due,
           q['line_items'],q['subtotal'],q['vat_amount'],q['total'],
           f"Créée depuis le devis {q['quote_number']}." + (("\\n"+q['notes']) if q['notes'] else ""),
           token,now(),eid))
        c.commit()
        invoice_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("UPDATE outgoing_quotes SET status='converted',converted_invoice_id=? WHERE id=? AND entity_id IS ?",(invoice_id,quote_id,eid))
        c.commit(); c.close()
        log_activity('QUOTE_CONVERTED',f"Devis {q['quote_number']} converti en {invoice_number}")
        flash(f"Devis {q['quote_number']} converti en facture {invoice_number}.")
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
        company_row=resolve_entity(c,eid) if q else None
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
        c=cx()
        from profitos.entities import current_entity_id
        eid=current_entity_id()
        rows=c.execute("""
            SELECT i.*,
                   COALESCE((SELECT SUM(cn.total) FROM outgoing_credit_notes cn
                             WHERE cn.original_invoice_id=i.id AND cn.entity_id IS i.entity_id AND cn.status='issued'),0) AS credited_total,
                   COALESCE((SELECT SUM(p.amount) FROM outgoing_invoice_payments p
                             WHERE p.invoice_id=i.id AND p.entity_id IS i.entity_id),0) AS paid_total
            FROM outgoing_invoices i
            WHERE i.status IN ('sent','partially_paid') AND i.entity_id IS ?
            ORDER BY i.due_date ASC, i.id DESC
        """,(eid,)).fetchall()

        today=date.today()
        invoices=[]
        by_client={}
        total_due=0.0
        overdue_due=0.0

        for r in rows:
            total=float(r['total'] or 0)
            credited=float(r['credited_total'] or 0)
            remaining=max(0.0,total-credited-float(r['paid_total'] or 0))
            if remaining <= 0.01:
                continue

            due=None
            try:
                due=datetime.strptime(r['due_date'],'%Y-%m-%d').date() if r['due_date'] else None
            except Exception:
                due=None
            overdue=bool(due and due < today)
            days_overdue=(today-due).days if overdue else 0
            total_due += remaining
            if overdue:
                overdue_due += remaining

            item={
                'id':r['id'],
                'invoice_number':r['invoice_number'],
                'client_name':r['client_name'],
                'client_email':r['client_email'],
                'due_date':r['due_date'],
                'remaining':remaining,
                'overdue':overdue,
                'days_overdue':days_overdue,
            }
            invoices.append(item)

            client=(r['client_name'] or 'Client sans nom').strip()
            agg=by_client.setdefault(client,{'client_name':client,'total':0.0,'overdue':0.0,'count':0})
            agg['total'] += remaining
            agg['count'] += 1
            if overdue:
                agg['overdue'] += remaining

        clients=sorted(by_client.values(),key=lambda x:(x['overdue'],x['total']),reverse=True)
        c.close()
        return render_template(
            'invoicing_receivables.html',
            invoices=invoices,
            clients=clients,
            total_due=total_due,
            overdue_due=overdue_due,
            unpaid_count=len(invoices),
            overdue_count=sum(1 for x in invoices if x['overdue'])
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
        purchases=c.execute("SELECT * FROM purchase_invoices WHERE entity_id IS ? ORDER BY due_date ASC, id DESC",(eid,)).fetchall()
        suppliers=c.execute("SELECT * FROM suppliers WHERE entity_id IS ? ORDER BY name ASC",(eid,)).fetchall()
        today=date.today()
        total_unpaid=0.0
        total_overdue=0.0
        view=[]
        for p in purchases:
            overdue=False
            if p['status']=='unpaid' and p['due_date']:
                try:
                    overdue=datetime.strptime(p['due_date'],'%Y-%m-%d').date() < today
                except Exception:
                    overdue=False
            if p['status']=='unpaid':
                total_unpaid += float(p['total'] or 0)
                if overdue:
                    total_overdue += float(p['total'] or 0)
            view.append({'row':p,'overdue':overdue})
        c.close()
        return render_template('purchase_list.html',purchases=view,suppliers=suppliers,
                               total_unpaid=total_unpaid,total_overdue=total_overdue)

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
                    amount=max(0.0,float(raw)) if raw else 0.0
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
            month=date.today().strftime('%Y-%m')

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
               "Date d'émission":p['issue_date'] or '',"Date d'échéance":p['due_date'] or '',
               'Montant HT (€)':round(p['subtotal'] or 0,2),'TVA (€)':round(p['vat_amount'] or 0,2),
               'Montant TTC (€)':round(p['total'] or 0,2),
               'Catégorie':PURCHASE_CATEGORY_LABELS.get(p['category'] or 'autre','Autre'),
               'Statut':'Payée' if p['status']=='paid' else 'À payer'} for p in purchases]
        record_usage('reports_per_month',organization_id=org['id'])
        return export_response(data,'profitos-achats-fournisseurs')

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
            flash("IBAN invalide — vérifie la saisie (format et somme de contrôle incorrects).")
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
        uploaded=request.files.get('document')
        used_ai=False
        try:
            stored,mime=_save_purchase_document(uploaded)
            if not stored:
                raise ValueError("Sélectionne un fichier PDF ou une photo.")
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
        open_orders=c.execute("SELECT id,order_number,supplier_name FROM purchase_orders WHERE status IN ('sent','partially_received','received') ORDER BY order_date DESC").fetchall()
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
            "SELECT * FROM purchase_invoices WHERE notes LIKE 'Reçu par email%' AND entity_id IS ? ORDER BY id DESC LIMIT 20",
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
            new_id = tc.execute('SELECT last_insert_rowid()').fetchone()[0]
            try:
                purchase_row = tc.execute('SELECT * FROM purchase_invoices WHERE id=?', (new_id,)).fetchone()
                generate_purchase_entry(tc, purchase_row)
            except AccountingError as e:
                log_ops_event('ACCOUNTING_ENTRY_FAILED', outcome='ERROR', detail=f"achat email {new_id}: {e}")
            tc.close()
            created.append(detected['invoice_number'])

        log_ops_event('SUPPLIER_INBOX_RECEIVED', outcome='INFO' if created else 'WARNING',
                       detail=f"org={org_id} entity={entity_id} créées={created} échecs={failed}")
        return jsonify({'created': created, 'failed': failed}), 200

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
            settings=c.execute('SELECT weinvoice_company_id FROM weinvoice_entity_settings WHERE entity_key=?',(eid or 0,)).fetchone()
            if not settings or not settings['weinvoice_company_id']:
                flash("Plateforme de facturation électronique non configurée pour cette entité.")
                return redirect(url_for('purchase_list'))
            org_remote=settings['weinvoice_company_id']
            # Parcourt toutes les pages WeInvoice. La limite de 100 est celle
            # d'une page API, pas celle d'une synchronisation ProfitOS.
            inbound_items=[]; page=1; page_size=100
            try:
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
            except (WeInvoiceAPIError,WeInvoiceConfigError) as exc:
                flash(str(exc)); return redirect(url_for('purchase_list'))
            created=0; skipped=0; ignored=0; failed=[]
            for item in inbound_items:
                stored=None
                remote_id=str(item.get('id') or '').strip()
                if not remote_id or str(item.get('direction') or '').upper()!='INBOUND':
                    continue
                if (c.execute('SELECT 1 FROM purchase_invoices WHERE entity_id IS ? AND weinvoice_invoice_id=?',(eid,remote_id)).fetchone()
                    or c.execute('SELECT 1 FROM purchase_credit_notes WHERE entity_id IS ? AND weinvoice_invoice_id=?',(eid,remote_id)).fetchone()):
                    skipped+=1; continue
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
                            (_purchase_pdf_dir()/stored).write_bytes(pdf)
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
                        (_purchase_pdf_dir()/stored).write_bytes(pdf)
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
                    purchase_row=c.execute('SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?',(new_id,eid)).fetchone()
                    generate_purchase_entry(c,purchase_row,commit=False)
                    c.commit(); created+=1
                except Exception as exc:
                    c.rollback()
                    try:
                        if stored: (_purchase_pdf_dir()/stored).unlink(missing_ok=True)
                    except Exception: pass
                    failed.append(f"{remote_id}: {type(exc).__name__}")
                    log_ops_event('WEINVOICE_INBOUND_IMPORT_FAILED','ERROR',detail=f'eInvoicingId={remote_id} error={type(exc).__name__}')
            log_ops_event('WEINVOICE_INBOUND_SYNC','INFO' if not failed else 'WARNING',detail=f'entity={eid} created={created} skipped={skipped} ignored={ignored} failed={len(failed)}')
            flash(f"Factures électroniques reçues : {created} importée(s), {skipped} déjà présente(s), {ignored} rejetée(s) ignorée(s), {len(failed)} échec(s).")
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
            try:
                subtotal=float(request.form.get('subtotal') or 0)
                vat=float(request.form.get('vat_amount') or 0)
            except ValueError:
                c.close()
                flash("Montants invalides.")
                return redirect(url_for('purchase_new'))
            total=round(subtotal+vat,2)
            if not supplier_name or not number or subtotal < 0 or vat < 0:
                c.close()
                flash("Fournisseur, numéro et montants valides sont obligatoires.")
                return redirect(url_for('purchase_new'))
            pending_document=(request.form.get('pending_document') or '').strip()
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
                       request.form.get('issue_date') or None,
                       request.form.get('due_date') or None,
                       subtotal,vat,total,'unpaid',
                       (request.form.get('notes') or '').strip(),now(),pending_document or None,category,validation_status,purchase_order_id,entity_id))
            c.commit()
            new_purchase_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            try:
                purchase_row=c.execute('SELECT * FROM purchase_invoices WHERE id=?',(new_purchase_id,)).fetchone()
                generate_purchase_entry(c,purchase_row)
            except AccountingError as e:
                log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"achat {new_purchase_id}: {e}")
            deliver_webhook(c,'purchase.created',{'id':new_purchase_id,'invoice_number':number,
                'supplier_name':supplier_name,'total':total},entity_id=eid)
            c.close()
            flash("Facture fournisseur enregistrée.")
            return redirect(url_for('purchase_list'))
        open_orders=c.execute("SELECT id,order_number,supplier_name FROM purchase_orders WHERE status IN ('sent','partially_received','received') ORDER BY order_date DESC").fetchall()
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
        unpaid=sum(float(p['total'] or 0) for p in purchases if p['status']=='unpaid')
        c.close()
        return render_template('supplier_detail.html',supplier=supplier,purchases=purchases,total=total,unpaid=unpaid)


    @app.get('/facturation/dettes-fournisseurs')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_debts():
        today = date.today()
        d7 = today + timedelta(days=7)
        d30 = today + timedelta(days=30)
        c = cx()
        rows = c.execute("""
                SELECT id, supplier_id, supplier_name, invoice_number,
                       issue_date, due_date, total, status, created_at
                FROM purchase_invoices
                WHERE COALESCE(status, 'unpaid') != 'paid'
                ORDER BY CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
                         due_date ASC, id DESC
            """).fetchall()
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
        today = date.today()
        d7 = today + timedelta(days=7)
        d30 = today + timedelta(days=30)

        c = cx()
        rows = c.execute("""
            SELECT id, supplier_id, supplier_name, invoice_number,
                   issue_date, due_date, total, status, created_at
            FROM purchase_invoices
            WHERE COALESCE(status, 'unpaid') != 'paid'
            ORDER BY
              CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
              due_date ASC, id DESC
        """).fetchall()
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
        return render_template('purchase_detail.html',p=p,supplier=supplier,einvoice_events=einvoice_events)

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
                subtotal=float(request.form.get('subtotal') or 0)
                vat=float(request.form.get('vat_amount') or 0)
            except ValueError:
                c.close(); flash("Montants invalides.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            if not supplier_name or not number or subtotal<0 or vat<0:
                c.close(); flash("Fournisseur, numéro et montants valides sont obligatoires.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            category=request.form.get('category','autre')
            if category not in PURCHASE_CATEGORY_LABELS: category='autre'
            c.execute("""UPDATE purchase_invoices SET supplier_id=?,supplier_name=?,invoice_number=?,
                         issue_date=?,due_date=?,subtotal=?,vat_amount=?,total=?,notes=?,category=? WHERE id=? AND entity_id IS ?""",
                      (supplier_id,supplier_name,number,request.form.get('issue_date') or None,
                       request.form.get('due_date') or None,subtotal,vat,round(subtotal+vat,2),
                       (request.form.get('notes') or '').strip(),category,purchase_id,eid))
            c.commit(); c.close()
            flash("Facture fournisseur mise à jour.")
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
        key=f"manual-full:{eid}:{purchase_id}:{balance:.2f}"
        try:
            c.execute("INSERT INTO purchase_invoice_payments(entity_id,purchase_invoice_id,amount,payment_date,payment_method,reference,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",(eid,purchase_id,balance,date.today().isoformat(),'manual','Solde manuel',key,now()))
            payment=c.execute("SELECT * FROM purchase_invoice_payments WHERE entity_id IS ? AND idempotency_key=?",(eid,key)).fetchone()
            generate_purchase_partial_payment_entry(c,p,payment)
            c.execute("UPDATE purchase_invoices SET status='paid',paid_at=? WHERE id=? AND entity_id IS ?",(now(),purchase_id,eid))
            c.commit()
        except (AccountingError, sqlite3.IntegrityError) as e:
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
                flash("Sélectionne au moins une facture à inclure dans le virement groupé.")
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
                          (entity_id,msg_id,date.today().isoformat(),total,len(payable_rows),file_sha256,now(),session.get('user_email')))
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
            except (AccountingError, sqlite3.IntegrityError) as e:
                c.rollback(); c.close()
                log_ops_event('SEPA_EXPORT_FAILED', outcome='ERROR', detail=f"lot {msg_id}: {e}")
                flash(f"Export SEPA interrompu : {e}")
                return redirect(url_for('purchase_sepa_batch', entity_id=entity_id or ''))
            c.close()
            log_activity('SEPA_BATCH_GENERATED', f"Lot SEPA {msg_id} ({debtor_identity['name']}) : {len(payable_rows)} virement(s), {fr_number(total, 2)} €")
            filename = f"virements_{date.today().isoformat()}.xml"
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
        ready, blocked = [], []
        for r in candidates:
            if r['supplier_iban'] and validate_iban(r['supplier_iban']) and r['supplier_bic']:
                ready.append(r)
            else:
                blocked.append(r)
        company_ready = bool(debtor_identity['iban'] and validate_iban(debtor_identity['iban']) and debtor_identity['bic'])
        entities = list_all_entities(c)
        c.close()
        return render_template('purchase_sepa_batch.html', ready=ready, blocked=blocked,
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
            order_date = request.form.get('order_date') or date.today().isoformat()
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
                flash("Ajoute au moins une ligne avec une quantité positive.")
                return redirect(url_for('purchase_order_new'))

            year = date.today().year
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
                    received = float(request.form.get(key) or 0)
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
        c.close()
        totals={'draft':0,'sent':0,'overdue':0,'paid':0,'cancelled':0}
        display_statuses={}
        for r in rows:
            status=_display_status(r)
            display_statuses[r['id']]=status
            if status in totals: totals[status]+=r['total'] or 0
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
            flash("Complète d'abord ton profil entreprise (nom, adresse, SIRET) avant de créer une facture.")
            return redirect(url_for('company'))

        if request.method=='POST':
            client_name=request.form.get('client_name','').strip()
            client_address=request.form.get('client_address','').strip()
            client_email=request.form.get('client_email','').strip()
            client_siren=re.sub(r'\D','',request.form.get('client_siren','').strip())[:9]
            operation_nature=request.form.get('operation_nature','services')
            if operation_nature not in ('biens','services','mixte'): operation_nature='services'
            vat_on_debits=1 if request.form.get('vat_on_debits')=='on' else 0
            delivery_address=request.form.get('delivery_address','').strip()
            ereporting_scope=(request.form.get('ereporting_scope') or '').strip()
            ereporting_category=(request.form.get('ereporting_category') or '').strip()
            counterparty_country=(request.form.get('counterparty_country') or '').strip().upper()
            client_vat_number=(request.form.get('client_vat_number') or '').strip()
            if ereporting_scope not in ('','b2c','international_b2b'):
                flash("Périmètre e-reporting invalide."); return redirect(url_for('invoicing_new'))
            if ereporting_scope=='b2c' and ereporting_category not in ('TLB1','TPS1','TNT1','TMA1'):
                flash("Catégorie e-reporting B2C requise."); return redirect(url_for('invoicing_new'))
            if ereporting_scope=='international_b2b' and (len(counterparty_country)!=2 or counterparty_country=='FR'):
                flash("Pays étranger ISO alpha-2 requis pour le B2B international."); return redirect(url_for('invoicing_new'))
            due_date=request.form.get('due_date','').strip()
            notes=request.form.get('notes','').strip()
            items=_compute_line_items(request.form)
            issue_date=date.today().isoformat()
            if due_date:
                try:
                    if date.fromisoformat(due_date) < date.fromisoformat(issue_date):
                        c.close()
                        flash("La date d'échéance ne peut pas être antérieure à la date d'émission.")
                        return redirect(url_for('invoicing_new'))
                except ValueError:
                    c.close()
                    flash("Date d'échéance invalide.")
                    return redirect(url_for('invoicing_new'))

            entity_id_raw=request.form.get('entity_id')
            entity_id=int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None
            if entity_id is not None:
                from profitos.entities import resolve_entity, user_can_access_entity
                try:
                    resolve_entity(c,entity_id)
                except ValueError:
                    c.close()
                    flash("Entité sélectionnée introuvable.")
                    return redirect(url_for('invoicing_new'))
                if not user_can_access_entity(c, session.get('user_id'), entity_id):
                    c.close()
                    abort(403)

            if not client_name or not items:
                c.close()
                flash('Nom du client et au moins une ligne de facture requis.')
                return redirect(url_for('invoicing_new'))
            if client_siren and len(client_siren)!=9:
                c.close()
                flash('Le SIREN client doit comporter exactement 9 chiffres (laisse vide si inconnu).')
                return redirect(url_for('invoicing_new'))

            subtotal,vat_amount,total=_totals(items)
            invoice_number=_next_invoice_number(c,entity_id)
            token=secrets.token_urlsafe(20)

            c.execute('''INSERT INTO outgoing_invoices(invoice_number,client_name,client_address,client_email,issue_date,due_date,
                         line_items,subtotal,vat_amount,total,notes,status,public_token,created_at,
                         client_siren,operation_nature,vat_on_debits,delivery_address,entity_id,
                         ereporting_scope,ereporting_category,counterparty_country,client_vat_number)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?)''',
                (invoice_number,client_name,client_address,client_email,issue_date,due_date or None,
                 json.dumps(items,ensure_ascii=False),subtotal,vat_amount,total,notes,token,now(),
                 client_siren or None,operation_nature,vat_on_debits,delivery_address or None,entity_id,
                 ereporting_scope or None,ereporting_category or None,counterparty_country or None,client_vat_number or None))
            c.commit()
            new_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            ac=auth_cx()
            ac.execute('INSERT INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
                (token,session['org_id'],new_id,now())); ac.commit(); ac.close()
            log_activity('INVOICE_CREATED',f'Facture {invoice_number} créée ({fr_number(total)} € TTC)')
            flash(f'Facture {invoice_number} créée en brouillon.')
            return redirect(url_for('invoicing_detail',invoice_id=new_id))

        clients=c.execute("SELECT * FROM invoicing_clients ORDER BY lower(name)").fetchall()
        from profitos.entities import list_all_entities
        entities=list_all_entities(c)
        c.close()
        return render_template('invoicing_new.html',company=company_row,today=date.today().isoformat(),clients=clients,entities=entities)

    @app.route('/facturation/<int:invoice_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_detail(invoice_id):
        c=cx()
        inv=_current_invoice(c,invoice_id)
        if not inv:
            c.close(); abort(404)
        from profitos.entities import resolve_entity
        company_row=resolve_entity(c,inv['entity_id'])
        c.close()
        items=json.loads(inv['line_items'] or '[]')
        mention_checks,missing_mentions=_check_mandatory_mentions(inv,company_row)
        emitter_missing_count=sum(1 for m in missing_mentions if 'émetteur' in m)
        c=cx()
        credits=c.execute("SELECT * FROM outgoing_credit_notes WHERE original_invoice_id=? AND entity_id IS ? ORDER BY id DESC",(invoice_id,inv['entity_id'])).fetchall()
        credited_total=_credited_total(c,invoice_id,inv['entity_id'])
        reminders=c.execute("SELECT * FROM invoice_reminders WHERE invoice_id=? AND entity_id IS ? ORDER BY reminder_number DESC",(invoice_id,inv['entity_id'])).fetchall()
        payments=c.execute("SELECT * FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ? ORDER BY payment_date,id",(invoice_id,inv['entity_id'])).fetchall()
        paid_total=_invoice_paid_total(c,invoice_id,inv['entity_id'])
        balance_due=max(0.0,round(float(inv['total'] or 0)-paid_total,2))
        einvoice_events=c.execute("SELECT * FROM einvoice_events WHERE invoice_id=? AND entity_id IS ? ORDER BY id DESC LIMIT 50",(invoice_id,inv['entity_id'])).fetchall()
        c.close()
        return render_template('invoicing_detail.html',inv=inv,items=items,display_status=_display_status(inv),
                               credits=credits,credited_total=credited_total,reminders=reminders,payments=payments,
                               paid_total=paid_total,balance_due=balance_due,
                               creditable_total=max(0,float(inv['total'] or 0)-credited_total),
                               mention_checks=mention_checks,missing_mentions=missing_mentions,
                               emitter_missing_count=emitter_missing_count,einvoice_events=einvoice_events)

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
        company_row=resolve_entity(c,inv['entity_id'])
        c.close()
        pdf_bytes=_render_invoice_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lance : pip install -r requirements.txt")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{inv["invoice_number"]}.pdf"'})

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
        company_row=resolve_entity(c,eid)
        c.close()
        if not inv: abort(404)
        if inv['status']!='draft':
            flash("Le pro-forma n'a de sens que pour une facture encore au statut brouillon — celle-ci est déjà émise.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        pdf_bytes=_render_proforma_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lance : pip install -r requirements.txt")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        log_activity('PROFORMA_GENERATED', f"Pro-forma généré pour la facture brouillon {inv['invoice_number']}")
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="proforma-{inv["invoice_number"]}.pdf"'})

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
            delivery_date = request.form.get('delivery_date') or date.today().isoformat()
            items = _compute_line_items(request.form)
            if not client_name or not items:
                c.close()
                flash("Nom du client et au moins une ligne sont requis.")
                return redirect(url_for('delivery_note_new'))
            delivery_number = _next_delivery_number(c)
            c.execute(
                """INSERT INTO delivery_notes
                   (entity_id,delivery_number,client_name,client_address,client_email,delivery_date,line_items,status,notes,created_at,created_by)
                   VALUES(?,?,?,?,?,?,?,'draft',?,?,?)""",
                (current_entity_id(), delivery_number, client_name, client_address, client_email, delivery_date,
                 json.dumps(items, ensure_ascii=False), (request.form.get('notes') or '').strip(),
                 now(), current_user()['email']),
            )
            c.commit()
            new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            log_activity('DELIVERY_NOTE_CREATED', f"Bon de livraison {delivery_number} créé")
            flash(f"Bon de livraison {delivery_number} créé.")
            return redirect(url_for('delivery_note_detail', delivery_id=new_id))
        c.close()
        return render_template('delivery_note_new.html', today=date.today().isoformat())

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
        invoice_number = _next_invoice_number(c, current_entity_id())
        token = secrets.token_urlsafe(20)
        c.execute(
            """INSERT INTO outgoing_invoices(invoice_number,client_name,client_address,client_email,issue_date,
               line_items,subtotal,vat_amount,total,status,public_token,created_at,entity_id,notes)
               VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?,?,?)""",
            (invoice_number, row['client_name'], row['client_address'], row['client_email'], date.today().isoformat(),
             row['line_items'], subtotal, vat_amount, total, token, now(), current_entity_id(),
             f"Facturé à partir du bon de livraison {row['delivery_number']}"),
        )
        c.commit()
        new_invoice_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        ac = auth_cx()
        ac.execute('INSERT INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
            (token, session['org_id'], new_invoice_id, now())); ac.commit(); ac.close()
        c.execute("UPDATE delivery_notes SET linked_invoice_id=? WHERE id=? AND entity_id IS ?", (new_invoice_id, delivery_id, current_entity_id()))
        c.commit(); c.close()
        log_activity('DELIVERY_NOTE_INVOICED', f"Bon de livraison {row['delivery_number']} facturé ({invoice_number})")
        flash(f"Facture {invoice_number} créée à partir du bon de livraison.")
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
        from profitos.entities import resolve_entity
        company_row = resolve_entity(c, eid)
        c.close()
        if not row: abort(404)
        try:
            from fpdf import FPDF
        except ImportError:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lance : pip install -r requirements.txt")
            return redirect(url_for('delivery_note_detail', delivery_id=delivery_id))

        def safe(text):
            if text is None: return ''
            text=str(text)
            repl={'—':'-','–':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"','…':'...','\xa0':' ','€':'EUR'}
            for a,b in repl.items(): text=text.replace(a,b)
            return text.encode('latin-1',errors='replace').decode('latin-1')

        items = json.loads(row['line_items'] or '[]')
        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.add_page()
        pdf.set_font('Helvetica','B',20); pdf.set_text_color(17,24,39)
        pdf.cell(0,12,safe(f"Bon de livraison {row['delivery_number']}"),ln=1)
        pdf.set_font('Helvetica','',11); pdf.set_text_color(107,114,128)
        if company_row:
            pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        pdf.ln(4)
        pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',12)
        pdf.cell(0,7,'Livré à :',ln=1)
        pdf.set_font('Helvetica','',11)
        pdf.cell(0,6,safe(row['client_name']),ln=1)
        if row['client_address']: pdf.cell(0,6,safe(row['client_address']),ln=1)
        pdf.ln(4)
        pdf.set_font('Helvetica','',10); pdf.set_text_color(107,114,128)
        pdf.cell(0,6,safe(f"Date de livraison : {row['delivery_date']}"),ln=1)
        pdf.ln(8)
        pdf.set_fill_color(243,244,246); pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',10)
        pdf.cell(100,8,'Description',border=0,fill=True)
        pdf.cell(30,8,'Qté livrée',border=0,fill=True,align='R')
        pdf.cell(50,8,'',border=0,fill=True,ln=1)
        pdf.set_font('Helvetica','',10)
        for it in items:
            pdf.cell(100,7,safe(it['label']))
            pdf.cell(30,7,safe(f"{it['qty']:g}"),align='R')
            pdf.cell(50,7,'',ln=1)
        pdf.ln(10); pdf.set_font('Helvetica','I',8); pdf.set_text_color(150,150,150)
        pdf.multi_cell(0,4,safe("Document généré via ProfitOS — bon de livraison, à faire signer par le destinataire pour valoir accusé de réception."))
        pdf_bytes = bytes(pdf.output(dest='S'))
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
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        c.close()
        if not inv: abort(404)
        _,missing=_check_mandatory_mentions(inv,company_row)
        if missing:
            flash("Facture non conforme — complète d'abord les mentions manquantes avant de générer le Factur-X.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        items=json.loads(inv['line_items'] or '[]')
        _,rule_failures=validate_facturx_business_rules(inv,items,company_row)
        if rule_failures:
            labels=', '.join(r['label'] for r in rule_failures)
            flash(f"Règle(s) métier EN16931 non respectée(s) : {labels}")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        pdf_bytes=render_facturx_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("La génération Factur-X nécessite fpdf2 ≥ 2.8.7 ET les polices à embarquer dans static/fonts/ (DejaVuSans.ttf, DejaVuSans-Bold.ttf) — vérifie les deux.")
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
            from profitos.entities import resolve_entity
            company_row = resolve_entity(c,inv['entity_id'])
            settings = c.execute('SELECT weinvoice_company_id,weinvoice_kyb_status FROM weinvoice_entity_settings WHERE entity_key=?', (inv['entity_id'] or 0,)).fetchone()
            if not inv: abort(404)
            if inv['status'] == 'cancelled':
                flash("Une facture annulée ne peut pas être transmise à WeInvoice.")
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
                flash("Facture non conforme — complète les mentions obligatoires avant transmission WeInvoice.")
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            items = json.loads(inv['line_items'] or '[]')
            _, rule_failures = validate_facturx_business_rules(inv, items, company_row)
            if rule_failures:
                flash("Facture EN16931 invalide — corrige les règles métier avant transmission WeInvoice.")
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
                c.execute('UPDATE outgoing_invoices SET weinvoice_last_error=? WHERE id=? AND entity_id IS ?', ("WeInvoice a accepté la facture mais aucun identifiant distant exploitable n'a été renvoyé.", invoice_id, inv['entity_id']))
                c.commit(); flash("WeInvoice a accepté la facture, mais l'identifiant distant est absent — vérifie les logs avant tout nouvel envoi.")
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
            except (WeInvoiceAPIError, WeInvoiceConfigError) as e:
                c.execute('UPDATE outgoing_invoices SET weinvoice_last_error=? WHERE id=? AND entity_id IS ?', (str(e)[:1500], invoice_id, inv['entity_id']))
                c.commit(); flash(str(e))
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
            c.execute('UPDATE outgoing_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?',
                      (status, str(regulatory_code) if regulatory_code is not None else None, now(), invoice_id, inv['entity_id']))
            _record_einvoice_event(c,inv,'status_sync',remote_id=remote_id,status=status,regulatory_code=regulatory_code,
                                   idempotency_key=f'sync-{remote_id}-{status}-{regulatory_code}')

            # Les acquittements PPF (250/251/500/501/601) sont distincts du
            # statut métier de la facture. On les journalise séparément afin
            # qu'un 601, par exemple, ne transforme jamais RECEIVED en rejet.
            ack = data.get('latestAcquittement') if isinstance(data, dict) else None
            if isinstance(ack, dict) and ack.get('code') is not None:
                ack_code = str(ack.get('code')).strip()
                ack_decision = str(ack.get('decision') or '').strip().upper()
                ack_status_map = {
                    '250': 'ACK_250_ACCEPTED',
                    '251': 'ACK_251_REJECTED',
                    '500': 'ACK_500_RECEVABLE',
                    '501': 'ACK_501_INADMISSIBLE',
                    '601': 'ACK_601_REJECTED',
                }
                ack_status = ack_status_map.get(ack_code, f'ACK_{ack_code}_{ack_decision or "UNKNOWN"}')
                ack_detail = json.dumps({
                    'code': ack_code,
                    'statut': ack.get('statut'),
                    'objet': ack.get('objet'),
                    'decision': ack.get('decision'),
                    'motifCode': ack.get('motifCode'),
                    'motifTexte': ack.get('motifTexte'),
                }, ensure_ascii=False)
                _record_einvoice_event(
                    c, inv, 'acquittement', remote_id=remote_id, status=ack_status,
                    regulatory_code=None,
                    idempotency_key=f'ack-{remote_id}-{ack_code}-{ack.get("motifCode") or ""}',
                    detail=ack_detail,
                )
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
            flash("Aucun email client renseigné pour cette facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        org=current_org()
        base=os.environ.get('APP_BASE_URL',request.host_url.rstrip('/'))
        link=f"{base}{url_for('public_invoice_view',token=inv['public_token'])}"
        html=render_template('email_transactional.html',title=f"Facture {inv['invoice_number']} — {org['name']}",
            intro=f"Voici votre facture {inv['invoice_number']} de {org['name']}, d'un montant de {fr_number(inv['total'],2)} € TTC.",
            cta_label='Consulter la facture',cta_url=link,footer='')
        result=send_email(inv['client_email'],f"Facture {inv['invoice_number']} — {org['name']}",html)

        if result.get('dry_run'):
            flash(f"Service email non configuré — facture non envoyée réellement (mode simulation) à {inv['client_email']}.")
        elif result.get('sent'):
            issue_date = date.today().isoformat() if inv['status']=='draft' else inv['issue_date']
            c.execute("UPDATE outgoing_invoices SET status='sent',sent_at=?,issue_date=? WHERE id=? AND entity_id IS ?",
                      (now(),issue_date,invoice_id,inv['entity_id']))
            c.commit()
            if inv['status']=='draft':
                try:
                    inv_updated=_current_invoice(c,invoice_id)
                    invoice_kind=inv_updated['invoice_kind'] if 'invoice_kind' in inv_updated.keys() else 'standard'
                    if invoice_kind=='deposit':
                        generate_customer_deposit_entry(c,inv_updated)
                    elif invoice_kind=='final':
                        generate_customer_final_entry(c,inv_updated)
                    else:
                        generate_sale_entry(c,inv_updated)
                except AccountingError as e:
                    log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"vente facture {invoice_id}: {e}")
                deliver_webhook(c,'invoice.sent',{'id':invoice_id,'invoice_number':inv['invoice_number'],
                    'client_name':inv['client_name'],'total':inv['total'],'due_date':inv['due_date']},
                    entity_id=inv['entity_id'])
            log_activity('INVOICE_SENT',f"Facture {inv['invoice_number']} envoyée à {inv['client_email']}")
            flash(f"Facture envoyée à {inv['client_email']}.")
        else:
            flash(f"Échec de l'envoi : {result.get('error','erreur inconnue')}")
        c.close()
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
        if not inv['client_email']:
            c.close()
            flash("Aucun email client renseigné pour cette facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if _credited_total(c,invoice_id,inv['entity_id']) >= float(inv['total'] or 0)-0.01:
            c.close()
            flash("Cette facture est entièrement couverte par un avoir et ne peut pas être relancée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        row=c.execute("SELECT COUNT(*) AS n FROM invoice_reminders WHERE invoice_id=? AND entity_id IS ?",(invoice_id,inv['entity_id'])).fetchone()
        reminder_number=int(row['n'] or 0)+1
        org=current_org()
        base=os.environ.get('APP_BASE_URL',request.host_url.rstrip('/'))
        link=f"{base}{url_for('public_invoice_view',token=inv['public_token'])}"
        html=render_template(
            'email_transactional.html',
            title=f"Relance facture {inv['invoice_number']} — {org['name']}",
            intro=(f"Sauf erreur de notre part, la facture {inv['invoice_number']} "
                   f"d'un montant de {fr_number(inv['total'],2)} € TTC, échue le {inv['due_date']}, "
                   f"reste impayée. Si votre règlement a déjà été effectué, merci de ne pas tenir compte de cette relance."),
            cta_label='Consulter la facture',cta_url=link,footer=''
        )
        result=send_email(inv['client_email'],f"Relance facture {inv['invoice_number']} — {org['name']}",html)
        if result.get('dry_run'):
            flash(f"Service email non configuré — relance non envoyée réellement (mode simulation) à {inv['client_email']}.")
        elif result.get('sent'):
            sent_at=now()
            c.execute("INSERT INTO invoice_reminders(entity_id,invoice_id,recipient_email,sent_at,reminder_number) VALUES(?,?,?,?,?)",
                      (inv['entity_id'],invoice_id,inv['client_email'],sent_at,reminder_number))
            c.commit()
            log_activity('INVOICE_REMINDER_SENT',f"Relance n°{reminder_number} pour {inv['invoice_number']} envoyée à {inv['client_email']}")
            flash(f"Relance n°{reminder_number} envoyée à {inv['client_email']}.")
        else:
            flash(f"Échec de la relance : {result.get('error','erreur inconnue')}")
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
        try:
            amount=round(float((request.form.get('amount') or '0').replace(',','.')),2)
        except ValueError:
            amount=0
        balance=_invoice_balance(c,inv)
        if amount <= 0 or amount > balance + 0.001:
            c.close(); flash(f"Montant invalide. Le reste à payer est de {fr_number(balance,2)} €.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        payment_date=(request.form.get('payment_date') or date.today().isoformat()).strip()
        try: date.fromisoformat(payment_date)
        except ValueError:
            c.close(); flash("Date de règlement invalide.")
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
            paid_total=_invoice_paid_total(c,invoice_id,inv['entity_id'])
            new_status='paid' if paid_total >= float(inv['total'] or 0)-0.005 else 'partially_paid'
            paid_at=now() if new_status=='paid' else None
            c.execute("UPDATE outgoing_invoices SET status=?,paid_at=? WHERE id=? AND entity_id IS ?",(new_status,paid_at,invoice_id,inv['entity_id']))
            _stage_payment_ereporting(c,inv,payment)
            c.commit()
            if new_status=='paid':
                deliver_webhook(c,'invoice.paid',{'id':invoice_id,'invoice_number':inv['invoice_number'],'client_name':inv['client_name'],'total':inv['total']},entity_id=inv['entity_id'])
        except sqlite3.IntegrityError:
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
                         VALUES(?,?,?,?,?,?,?,?)""",(inv['entity_id'],invoice_id,balance,date.today().isoformat(),'bank','Solde manuel',idem,now()))
            payment=c.execute("SELECT * FROM outgoing_invoice_payments WHERE id=?",(cur.lastrowid,)).fetchone()
            generate_sale_partial_payment_entry(c,inv,payment)
            c.execute("UPDATE outgoing_invoices SET status='paid',paid_at=? WHERE id=? AND entity_id IS ?",(now(),invoice_id,inv['entity_id']))
            _stage_payment_ereporting(c,inv,payment)
            c.commit()
        except sqlite3.IntegrityError:
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
            client_name=request.form.get('client_name','').strip()
            client_address=request.form.get('client_address','').strip()
            client_email=request.form.get('client_email','').strip()
            due_date=request.form.get('due_date','').strip()
            notes=request.form.get('notes','').strip()
            items=_compute_line_items(request.form)
            if due_date:
                try:
                    if date.fromisoformat(due_date) < date.fromisoformat(inv['issue_date']):
                        c.close()
                        flash("La date d'échéance ne peut pas être antérieure à la date d'émission.")
                        return redirect(url_for('invoicing_edit',invoice_id=invoice_id))
                except (ValueError, TypeError):
                    c.close()
                    flash("Date d'échéance invalide.")
                    return redirect(url_for('invoicing_edit',invoice_id=invoice_id))
            if not client_name or not items:
                c.close()
                flash('Nom du client et au moins une ligne de facture requis.')
                return redirect(url_for('invoicing_edit',invoice_id=invoice_id))
            subtotal,vat_amount,total=_totals(items)
            c.execute("UPDATE outgoing_invoices SET client_name=?,client_address=?,client_email=?,due_date=?,line_items=?,subtotal=?,vat_amount=?,total=?,notes=? WHERE id=? AND entity_id IS ? AND status='draft'",
                (client_name,client_address,client_email,due_date or None,json.dumps(items,ensure_ascii=False),subtotal,vat_amount,total,notes,invoice_id,inv['entity_id']))
            c.commit(); c.close()
            log_activity('INVOICE_UPDATED',f"Facture {inv['invoice_number']} modifiée")
            flash(f"Facture {inv['invoice_number']} mise à jour.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        items=json.loads(inv['line_items'] or '[]')
        c.close()
        return render_template('invoicing_edit.html',inv=inv,items=items)

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
        log_activity('INVOICE_CANCELLED',f"Facture {inv['invoice_number']} annulée")
        flash(f"Facture {inv['invoice_number']} annulée.")
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
            sale_entry=c.execute(
                "SELECT id FROM accounting_entries WHERE source_type='outgoing_invoice' AND source_id=? AND entity_id IS ? LIMIT 1",
                (invoice_id,inv['entity_id']),
            ).fetchone()
            if not sale_entry:
                c.rollback(); c.close()
                flash("Avoir impossible : l'écriture comptable de la facture d'origine est absente. Corrigez d'abord la comptabilisation de la facture.")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))
            reason=request.form.get('reason','').strip()
            try:
                amount_ttc=float(request.form.get('amount_ttc','0').replace(',','.'))
            except ValueError:
                amount_ttc=0
            if not reason or amount_ttc <= 0 or amount_ttc > remaining + 0.005:
                c.close()
                flash(f"Motif requis et montant TTC compris entre 0,01 € et {remaining:.2f} €.")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))

            ratio=amount_ttc/float(inv['total'])
            source_items=json.loads(inv['line_items'] or '[]')
            items=[]
            for it in source_items:
                line_total=round(float(it['line_total'])*ratio,2)
                items.append({'label':f"Avoir — {it['label']}",'qty':1.0,
                              'unit_price':line_total,'vat_rate':float(it['vat_rate']),
                              'line_total':line_total})
            subtotal=round(float(inv['subtotal'])*ratio,2)
            vat_amount=round(amount_ttc-subtotal,2)
            credit_number=_next_credit_number(c,inv['entity_id'])
            c.execute("""INSERT INTO outgoing_credit_notes
                (entity_id,credit_number,original_invoice_id,original_invoice_number,client_name,issue_date,
                 line_items,subtotal,vat_amount,total,reason,status,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,'issued',?)""",
                (inv['entity_id'],credit_number,invoice_id,inv['invoice_number'],inv['client_name'],date.today().isoformat(),
                 json.dumps(items,ensure_ascii=False),subtotal,vat_amount,round(amount_ttc,2),reason,now()))
            credit_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            credit=_current_credit(c,credit_id)
            try:
                generate_sale_credit_entry(c,credit)
                c.commit()
            except AccountingError as e:
                c.rollback()
                log_ops_event('ACCOUNTING_ENTRY_FAILED',outcome='ERROR',detail=f"avoir facture {invoice_id}: {e}")
                c.close()
                flash(f"Avoir non créé : {e}")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))
            c.close()
            log_activity('CREDIT_NOTE_CREATED',f"Avoir {credit_number} créé pour {inv['invoice_number']} ({amount_ttc:.2f} € TTC)")
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
        return jsonify({'local':local,'fiscalSettings':fiscal,'fiscalError':fiscal_error,
                        'productionSwitchAutomatic':False})

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
            from profitos.entities import resolve_entity
            company=resolve_entity(c,credit['entity_id'])
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
        company_row=resolve_entity(c,credit['entity_id'])
        c.close()
        pdf_bytes=_render_credit_pdf(credit,company_row)
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
            company_row=resolve_entity(tc,q['entity_id'])
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
            company_row=resolve_entity(tc,q['entity_id'])
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
            company_row=resolve_entity(tc,inv['entity_id'])
        else:
            company_row=None
        tc.close()
        if not inv: abort(404)
        items=json.loads(inv['line_items'] or '[]')
        return render_template('invoicing_public.html',inv=inv,items=items,company=company_row,token=token)

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
            company_row=resolve_entity(tc,inv['entity_id'])
        else:
            company_row=None
        tc.close()
        if not inv: abort(404)
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
        return render_template('recurring_invoices.html',rows=rows,clients=clients,today=date.today().isoformat())

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


def _render_invoice_pdf(inv,company_row):
    """Génère le PDF d'une facture émise. Retourne None si fpdf2 n'est pas installé
    (dégradation propre, jamais d'erreur 500)."""
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    def safe(text):
        if text is None: return ''
        text=str(text)
        repl={'—':'-','–':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"','…':'...','\xa0':' ','€':'EUR'}
        for a,b in repl.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')

    items=json.loads(inv['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4')
    pdf.set_auto_page_break(auto=True,margin=18)
    pdf.add_page()

    pdf.set_font('Helvetica','B',20); pdf.set_text_color(17,24,39)
    pdf.cell(0,12,safe(f"Facture {inv['invoice_number']}"),ln=1)
    pdf.set_font('Helvetica','',11); pdf.set_text_color(107,114,128)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['address']: pdf.cell(0,6,safe(company_row['address']),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
        if company_row['vat_number']: pdf.cell(0,6,safe(f"TVA : {company_row['vat_number']}"),ln=1)
    pdf.ln(6)

    pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',12)
    pdf.cell(0,7,'Facturé à :',ln=1)
    pdf.set_font('Helvetica','',11)
    pdf.cell(0,6,safe(inv['client_name']),ln=1)
    if inv['client_address']: pdf.cell(0,6,safe(inv['client_address']),ln=1)
    pdf.ln(4)
    pdf.set_font('Helvetica','',10); pdf.set_text_color(107,114,128)
    pdf.cell(0,6,safe(f"Date d'émission : {inv['issue_date']}"),ln=1)
    if inv['due_date']: pdf.cell(0,6,safe(f"Échéance : {inv['due_date']}"),ln=1)
    pdf.ln(8)

    pdf.set_fill_color(243,244,246); pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',10)
    pdf.cell(80,8,'Description',border=0,fill=True)
    pdf.cell(20,8,'Qté',border=0,fill=True,align='R')
    pdf.cell(30,8,'Prix unit.',border=0,fill=True,align='R')
    pdf.cell(20,8,'TVA',border=0,fill=True,align='R')
    pdf.cell(30,8,'Total HT',border=0,fill=True,align='R',ln=1)
    pdf.set_font('Helvetica','',10)
    for it in items:
        pdf.cell(80,7,safe(it['label']))
        pdf.cell(20,7,safe(f"{it['qty']:g}"),align='R')
        pdf.cell(30,7,safe(f"{fr_number(it['unit_price'],2)} EUR"),align='R')
        pdf.cell(20,7,safe(f"{it['vat_rate']:g}%"),align='R')
        pdf.cell(30,7,safe(f"{fr_number(it['line_total'],2)} EUR"),align='R',ln=1)
    pdf.ln(6)

    pdf.set_font('Helvetica','',11)
    pdf.cell(150,7,'Sous-total HT',align='R')
    pdf.cell(30,7,safe(f"{fr_number(inv['subtotal'],2)} EUR"),align='R',ln=1)
    pdf.cell(150,7,'TVA',align='R')
    pdf.cell(30,7,safe(f"{fr_number(inv['vat_amount'],2)} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(150,9,'Total TTC',align='R')
    pdf.cell(30,9,safe(f"{fr_number(inv['total'],2)} EUR"),align='R',ln=1)

    if inv['notes']:
        pdf.ln(8); pdf.set_font('Helvetica','',9); pdf.set_text_color(107,114,128)
        pdf.multi_cell(0,5,safe(inv['notes']))

    pdf.ln(10); pdf.set_font('Helvetica','I',8); pdf.set_text_color(150,150,150)
    pdf.multi_cell(0,4,safe("Document genere via ProfitOS. Ce document n'est pas emis via une Plateforme Agreee DGFiP au sens de la reforme de facturation electronique."))

    return bytes(pdf.output(dest='S'))


def _render_proforma_pdf(inv, company_row):
    """Génère un pro-forma — présentation alternative d'une facture encore
    au statut brouillon, mêmes montants, mais explicitement sans valeur
    comptable ni fiscale (utile pour une douane, un acompte, ou avant
    accord définitif). Ne modifie jamais la facture elle-même, ne
    consomme aucun numéro de séquence — un pro-forma n'est jamais une
    vraie facture. Retourne None si fpdf2 n'est pas installé, même
    convention que _render_invoice_pdf."""
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    def safe(text):
        if text is None: return ''
        text=str(text)
        repl={'—':'-','–':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"','…':'...','\xa0':' ','€':'EUR'}
        for a,b in repl.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')

    items=json.loads(inv['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4')
    pdf.set_auto_page_break(auto=True,margin=18)
    pdf.add_page()

    pdf.set_font('Helvetica','B',20); pdf.set_text_color(17,24,39)
    pdf.cell(0,12,safe(f"FACTURE PRO FORMA — {inv['invoice_number']}"),ln=1)
    pdf.set_font('Helvetica','I',10); pdf.set_text_color(220,38,38)
    pdf.cell(0,7,safe("Document sans valeur comptable ni fiscale — ne constitue pas une facture."),ln=1)
    pdf.set_font('Helvetica','',11); pdf.set_text_color(107,114,128)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['address']: pdf.cell(0,6,safe(company_row['address']),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
        if company_row['vat_number']: pdf.cell(0,6,safe(f"TVA : {company_row['vat_number']}"),ln=1)
    pdf.ln(6)

    pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',12)
    pdf.cell(0,7,'Destinataire :',ln=1)
    pdf.set_font('Helvetica','',11)
    pdf.cell(0,6,safe(inv['client_name']),ln=1)
    if inv['client_address']: pdf.cell(0,6,safe(inv['client_address']),ln=1)
    pdf.ln(4)
    pdf.set_font('Helvetica','',10); pdf.set_text_color(107,114,128)
    pdf.cell(0,6,safe(f"Date d'émission : {inv['issue_date']}"),ln=1)
    if inv['due_date']: pdf.cell(0,6,safe(f"Échéance indicative : {inv['due_date']}"),ln=1)
    pdf.ln(8)

    pdf.set_fill_color(243,244,246); pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',10)
    pdf.cell(80,8,'Description',border=0,fill=True)
    pdf.cell(20,8,'Qté',border=0,fill=True,align='R')
    pdf.cell(30,8,'Prix unit.',border=0,fill=True,align='R')
    pdf.cell(20,8,'TVA',border=0,fill=True,align='R')
    pdf.cell(30,8,'Total HT',border=0,fill=True,align='R',ln=1)
    pdf.set_font('Helvetica','',10)
    for it in items:
        pdf.cell(80,7,safe(it['label']))
        pdf.cell(20,7,safe(f"{it['qty']:g}"),align='R')
        pdf.cell(30,7,safe(f"{fr_number(it['unit_price'],2)} EUR"),align='R')
        pdf.cell(20,7,safe(f"{it['vat_rate']:g}%"),align='R')
        pdf.cell(30,7,safe(f"{fr_number(it['line_total'],2)} EUR"),align='R',ln=1)
    pdf.ln(6)

    pdf.set_font('Helvetica','',11)
    pdf.cell(150,7,'Sous-total HT',align='R')
    pdf.cell(30,7,safe(f"{fr_number(inv['subtotal'],2)} EUR"),align='R',ln=1)
    pdf.cell(150,7,'TVA',align='R')
    pdf.cell(30,7,safe(f"{fr_number(inv['vat_amount'],2)} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(150,9,'Total TTC indicatif',align='R')
    pdf.cell(30,9,safe(f"{fr_number(inv['total'],2)} EUR"),align='R',ln=1)

    pdf.ln(10); pdf.set_font('Helvetica','I',8); pdf.set_text_color(150,150,150)
    pdf.multi_cell(0,4,safe(
        "Document généré via ProfitOS à titre de pro-forma — présentation indicative avant facturation "
        "définitive. Ne constitue ni une facture, ni un document comptable ou fiscal opposable."
    ))

    return bytes(pdf.output(dest='S'))


def render_facturx_pdf(inv, company_row):
    """Génère le PDF/A-3 Factur-X (facture visible + factur-x.xml embarqué), profil
    EN16931. Retourne None si fpdf2 est absent/trop ancien, ou si les fichiers de
    police à embarquer sont introuvables (voir static/fonts/ — nécessaires car
    PDF/A-3B interdit les polices "de base" non incorporées comme Helvetica).

    ATTENTION : la conformité PDF/A-3 finale et l'intégration Factur-X n'ont pas été
    validées contre un outil officiel (FNFE-MPE, Chorus Pro, Mustangproject...).
    """
    try:
        from fpdf import FPDF
        from fpdf.enums import DocumentCompliance
    except ImportError:
        return None
    if not hasattr(DocumentCompliance, 'PDFA_3B'):
        return None  # version de fpdf2 trop ancienne pour le support PDF/A-3

    font_regular = BASE / 'static' / 'fonts' / 'DejaVuSans.ttf'
    font_bold = BASE / 'static' / 'fonts' / 'DejaVuSans-Bold.ttf'
    if not font_regular.is_file() or not font_bold.is_file():
        return None  # polices à embarquer absentes — voir static/fonts/README.txt

    items = json.loads(inv['line_items'] or '[]')
    xml_bytes = generate_facturx_xml(inv, items, company_row)

    def safe(text):
        return '' if text is None else str(text)

    pdf = FPDF(orientation='P', unit='mm', format='A4', enforce_compliance=DocumentCompliance.PDFA_3B)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_title(f"Facture {inv['invoice_number']}")
    pdf.add_font('DejaVu', '', str(font_regular))
    pdf.add_font('DejaVu', 'B', str(font_bold))
    pdf.add_page()

    pdf.set_font('DejaVu', 'B', 20); pdf.set_text_color(17, 24, 39)
    pdf.cell(0, 12, safe(f"Facture {inv['invoice_number']}"), ln=1)
    pdf.set_font('DejaVu', '', 11); pdf.set_text_color(107, 114, 128)
    if company_row:
        pdf.cell(0, 6, safe(company_row['name'] or ''), ln=1)
        if company_row['address']: pdf.cell(0, 6, safe(company_row['address']), ln=1)
        if company_row['siret']: pdf.cell(0, 6, safe(f"SIRET : {company_row['siret']}"), ln=1)
    pdf.ln(6)
    pdf.set_text_color(17, 24, 39); pdf.set_font('DejaVu', 'B', 12)
    pdf.cell(0, 7, 'Facturé à :', ln=1)
    pdf.set_font('DejaVu', '', 11)
    pdf.cell(0, 6, safe(inv['client_name']), ln=1)
    if inv['client_address']: pdf.cell(0, 6, safe(inv['client_address']), ln=1)
    pdf.ln(8)

    pdf.set_fill_color(243, 244, 246); pdf.set_font('DejaVu', 'B', 10)
    pdf.cell(90, 8, 'Description', fill=True)
    pdf.cell(20, 8, 'Qté', fill=True, align='R')
    pdf.cell(30, 8, 'Prix unit.', fill=True, align='R')
    pdf.cell(20, 8, 'TVA', fill=True, align='R')
    pdf.cell(30, 8, 'Total HT', fill=True, align='R', ln=1)
    pdf.set_font('DejaVu', '', 10)
    for it in items:
        pdf.cell(90, 7, safe(it['label']))
        pdf.cell(20, 7, safe(f"{it['qty']:g}"), align='R')
        pdf.cell(30, 7, safe(f"{fr_number(it['unit_price'],2)} €"), align='R')
        pdf.cell(20, 7, safe(f"{it['vat_rate']:g}%"), align='R')
        pdf.cell(30, 7, safe(f"{fr_number(it['line_total'],2)} €"), align='R', ln=1)
    pdf.ln(6)
    pdf.set_font('DejaVu', '', 11)
    pdf.cell(160, 7, 'Sous-total HT', align='R')
    pdf.cell(30, 7, safe(f"{fr_number(inv['subtotal'],2)} €"), align='R', ln=1)
    pdf.cell(160, 7, 'TVA', align='R')
    pdf.cell(30, 7, safe(f"{fr_number(inv['vat_amount'],2)} €"), align='R', ln=1)
    pdf.set_font('DejaVu', 'B', 13)
    pdf.cell(160, 9, 'Total TTC', align='R')
    pdf.cell(30, 9, safe(f"{fr_number(inv['total'],2)} €"), align='R', ln=1)

    pdf.ln(10); pdf.set_font('DejaVu', '', 8); pdf.set_text_color(150, 150, 150)
    pdf.multi_cell(0, 4, safe(
        "Facture électronique Factur-X (profil EN16931). Contient un fichier XML structuré "
        "factur-x.xml. Document généré via ProfitOS ; transmission via une Plateforme Agréée "
        "DGFiP non encore réalisée par cette version."))

    pdf.embed_file(
        bytes=xml_bytes,
        basename='factur-x.xml',
        mime_type='application/xml',
        desc='Factur-X invoice data (EN16931)',
        associated_file_relationship='Alternative',
        compress=True,
    )
    return bytes(pdf.output(dest='S'))



def generate_credit_facturx_xml(credit, original_invoice, company):
    """CII EN16931 d'un avoir : code 381 + référence à la facture d'origine."""
    items=json.loads(credit['line_items'] or '[]')
    proxy=dict(original_invoice)
    proxy.update(invoice_number=credit['credit_number'],issue_date=credit['issue_date'],
                 due_date=None,subtotal=credit['subtotal'],vat_amount=credit['vat_amount'],total=credit['total'])
    xml=generate_facturx_xml(proxy,items,company)
    root=ET.fromstring(xml.split(b'\n',1)[1]); ns=_CII_NS
    doc=root.find(f'{{{ns["rsm"]}}}ExchangedDocument')
    type_code=doc.find(f'{{{ns["ram"]}}}TypeCode') if doc is not None else None
    if type_code is None: raise ValueError("CII invalide : TypeCode absent")
    type_code.text='381'
    txn=root.find(f'{{{ns["rsm"]}}}SupplyChainTradeTransaction')
    settlement=txn.find(f'{{{ns["ram"]}}}ApplicableHeaderTradeSettlement') if txn is not None else None
    if settlement is None: raise ValueError("CII invalide : règlement absent")
    ref=ET.Element(f'{{{ns["ram"]}}}InvoiceReferencedDocument')
    ET.SubElement(ref,f'{{{ns["ram"]}}}IssuerAssignedID').text=credit['original_invoice_number']
    formatted=ET.SubElement(ref,f'{{{ns["ram"]}}}FormattedIssueDateTime')
    d=ET.SubElement(formatted,f'{{{ns["qdt"]}}}DateTimeString')
    d.set('format','102'); d.text=_cii_date(original_invoice['issue_date']) or ''
    monetary=settlement.find(f'{{{ns["ram"]}}}SpecifiedTradeSettlementHeaderMonetarySummation')
    children=list(settlement); pos=children.index(monetary) if monetary is not None else len(children)
    settlement.insert(pos,ref)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(root,encoding='utf-8')

def render_credit_facturx_pdf(credit, original_invoice, company_row):
    """PDF/A-3 Factur-X d'avoir avec CII 381 embarqué."""
    try:
        from fpdf import FPDF
        from fpdf.enums import DocumentCompliance
    except ImportError:
        return None
    if not hasattr(DocumentCompliance,'PDFA_3B'): return None
    regular=BASE/'static'/'fonts'/'DejaVuSans.ttf'; bold=BASE/'static'/'fonts'/'DejaVuSans-Bold.ttf'
    if not regular.is_file() or not bold.is_file(): return None
    xml_bytes=generate_credit_facturx_xml(credit,original_invoice,company_row)
    items=json.loads(credit['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4',enforce_compliance=DocumentCompliance.PDFA_3B)
    pdf.set_auto_page_break(auto=True,margin=18); pdf.set_title(f"Avoir {credit['credit_number']}")
    pdf.add_font('DejaVu','',str(regular)); pdf.add_font('DejaVu','B',str(bold)); pdf.add_page()
    pdf.set_font('DejaVu','B',20); pdf.cell(0,12,f"Avoir {credit['credit_number']}",ln=1)
    pdf.set_font('DejaVu','',10)
    if company_row:
        pdf.cell(0,6,str(company_row['name'] or ''),ln=1)
        if company_row['siret']: pdf.cell(0,6,f"SIRET : {company_row['siret']}",ln=1)
    pdf.ln(4); pdf.set_font('DejaVu','B',11); pdf.cell(0,7,f"Facture d'origine : {credit['original_invoice_number']}",ln=1)
    pdf.set_font('DejaVu','',10); pdf.cell(0,6,f"Client : {credit['client_name'] or ''}",ln=1)
    pdf.cell(0,6,f"Date : {credit['issue_date']}",ln=1); pdf.multi_cell(0,6,f"Motif : {credit['reason'] or ''}"); pdf.ln(5)
    for it in items:
        pdf.cell(135,7,str(it.get('label') or '')); pdf.cell(45,7,f"-{fr_number(it.get('line_total',0),2)} EUR",align='R',ln=1)
    pdf.ln(5); pdf.set_font('DejaVu','B',12); pdf.cell(135,8,'Total TTC avoir',align='R')
    pdf.cell(45,8,f"-{fr_number(credit['total'],2)} EUR",align='R',ln=1)
    pdf.ln(8); pdf.set_font('DejaVu','',8)
    pdf.multi_cell(0,4,"Avoir électronique Factur-X EN16931 — code document 381 et référence à la facture d'origine.")
    pdf.embed_file(bytes=xml_bytes,basename='factur-x.xml',mime_type='application/xml',
                   desc='Factur-X credit note data (EN16931)',associated_file_relationship='Alternative',compress=True)
    return bytes(pdf.output(dest='S'))


def _render_credit_pdf(credit,company_row):
    try:
        from fpdf import FPDF
    except ImportError:
        return None
    def safe(text):
        if text is None: return ''
        text=str(text)
        repl={'—':'-','–':'-','€':'EUR','\xa0':' '}
        for a,b in repl.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')
    items=json.loads(credit['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4'); pdf.add_page()
    pdf.set_font('Helvetica','B',20)
    pdf.cell(0,12,safe(f"Avoir {credit['credit_number']}"),ln=1)
    pdf.set_font('Helvetica','',10)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
    pdf.ln(5)
    pdf.set_font('Helvetica','B',11)
    pdf.cell(0,7,safe(f"Facture d'origine : {credit['original_invoice_number']}"),ln=1)
    pdf.set_font('Helvetica','',10)
    pdf.cell(0,6,safe(f"Client : {credit['client_name']}"),ln=1)
    pdf.cell(0,6,safe(f"Date : {credit['issue_date']}"),ln=1)
    pdf.multi_cell(0,6,safe(f"Motif : {credit['reason']}"))
    pdf.ln(5)
    for it in items:
        pdf.cell(130,7,safe(it['label']))
        pdf.cell(50,7,safe(f"-{fr_number(it['line_total'],2)} EUR"),align='R',ln=1)
    pdf.ln(5); pdf.set_font('Helvetica','',11)
    pdf.cell(140,7,'Sous-total HT',align='R'); pdf.cell(40,7,safe(f"-{fr_number(credit['subtotal'],2)} EUR"),align='R',ln=1)
    pdf.cell(140,7,'TVA',align='R'); pdf.cell(40,7,safe(f"-{fr_number(credit['vat_amount'],2)} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(140,9,'Total TTC avoir',align='R'); pdf.cell(40,9,safe(f"-{fr_number(credit['total'],2)} EUR"),align='R',ln=1)
    return bytes(pdf.output(dest='S'))


def _render_quote_pdf(q,company_row):
    try:
        from fpdf import FPDF
    except ImportError:
        return None
    def safe(text):
        if text is None: return ''
        text=str(text)
        for a,b in {'—':'-','–':'-','€':'EUR','\xa0':' '}.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')
    items=json.loads(q['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4'); pdf.add_page()
    pdf.set_font('Helvetica','B',20); pdf.cell(0,12,safe(f"Devis {q['quote_number']}"),ln=1)
    pdf.set_font('Helvetica','',10)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
    pdf.ln(4); pdf.cell(0,6,safe(f"Client : {q['client_name']}"),ln=1)
    if q['client_address']: pdf.multi_cell(0,6,safe(q['client_address']))
    pdf.cell(0,6,safe(f"Date : {q['issue_date']}"),ln=1)
    if q['valid_until']: pdf.cell(0,6,safe(f"Valable jusqu'au : {q['valid_until']}"),ln=1)
    pdf.ln(5)
    for it in items:
        pdf.cell(130,7,safe(f"{it['label']} ({it['qty']} x {it['unit_price']:.2f} EUR, TVA {it['vat_rate']:.1f}%)"))
        pdf.cell(50,7,safe(f"{fr_number(it['line_total'],2)} EUR"),align='R',ln=1)
    pdf.ln(5)
    pdf.cell(140,7,'Sous-total HT',align='R'); pdf.cell(40,7,safe(f"{fr_number(q['subtotal'],2)} EUR"),align='R',ln=1)
    pdf.cell(140,7,'TVA',align='R'); pdf.cell(40,7,safe(f"{fr_number(q['vat_amount'],2)} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(140,9,'Total TTC',align='R'); pdf.cell(40,9,safe(f"{fr_number(q['total'],2)} EUR"),align='R',ln=1)
    if q['notes']:
        pdf.ln(5); pdf.set_font('Helvetica','',10); pdf.multi_cell(0,6,safe(q['notes']))
    return bytes(pdf.output(dest='S'))
