"""Réception des factures fournisseurs par SUPER PDP (v496).

- ``sync_inbound`` : importe les factures (et avoirs) reçus sur le compte SUPER PDP de
  l'entité comme factures d'achat Veqalio, une seule fois chacune (identifiant distant +
  ``einvoice_provider='superpdp'``), avec le fichier d'origine comme justificatif et
  l'écriture comptable dans la même transaction — même comportement que la réception
  WeInvoice (routes/invoicing.sync_weinvoice_inbound) ;
- ``buyer_action`` : statuts de l'acheteur renvoyés à la plateforme (prise en charge,
  acceptation, litige, refus motivé, paiement) ;
- ``sync_purchase_status`` : relit le cycle de vie d'une facture reçue.

Le contenu d'une facture reçue est lu dans ``en_invoice`` (modèle EN 16931 de l'API) ; s'il
est absent, il est lu dans le fichier d'origine (XML CII/UBL, ou XML embarqué d'un Factur-X).
"""
import hashlib
import io
import math
import xml.etree.ElementTree as ET

from profitos import superpdp as SP
from profitos import einvoice_superpdp as ES

CREDIT_NOTE_TYPES = {'261', '262', '381', '396', '502', '503'}
# Action de l'écran facture d'achat → (code poussé, statut Veqalio).
BUYER_ACTIONS = {
    'take-in-charge': ('fr:204', 'TAKEN_IN_CHARGE'), 'approve': ('fr:205', 'APPROVED'),
    'approve-partially': ('fr:206', 'PARTIALLY_APPROVED'), 'dispute': ('fr:207', 'DISPUTED'),
    'suspend': ('fr:208', 'SUSPENDED'), 'refuse': ('fr:210', 'REFUSED'), 'payment-sent': ('fr:211', 'PAYMENT_SENT'),
}
# Code motif générique accepté pour un refus (liste BR-FR-CDV-CL-09) ; la phrase de
# l'utilisateur part dans ``notes``.
REFUSAL_REASON_CODE = 'NON_CONFORME'


class NotConnected(Exception):
    """L'entité n'est pas reliée (ou pas encore vérifiée) chez SUPER PDP."""


# ------------------------------------------------------------------ lecture du contenu

def _num(value):
    try:
        n = float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _date(value):
    text = str(value or '').strip()
    if len(text) == 8 and text.isdigit():                     # CII : 20261109
        return f'{text[:4]}-{text[4:6]}-{text[6:]}'
    return text[:10] or None


def from_en_invoice(en):
    """Champs utiles d'un ``en_invoice`` (None si l'objet est absent ou inexploitable)."""
    if not isinstance(en, dict) or not en.get('number'):
        return None
    totals = en.get('totals') or {}
    seller = en.get('seller') or {}
    refs = en.get('preceding_invoice_references') or []
    ht, vat, ttc = _num(totals.get('total_without_vat')), _num(totals.get('total_vat_amount')), _num(totals.get('total_with_vat'))
    if ht is None and ttc is None:
        return None
    return {'number': str(en['number']), 'type_code': str(en.get('type_code') or '380'),
            'issue_date': _date(en.get('issue_date')), 'due_date': _date(en.get('payment_due_date')),
            'supplier': seller.get('name') or seller.get('trading_name'),
            'subtotal': ht, 'vat': vat, 'total': ttc,
            'original_number': (refs[0] or {}).get('reference') if refs else None}


def _local(el):
    return el.tag.rsplit('}', 1)[-1]


def _first(root, *path):
    """Texte du premier élément dont la suite de noms locaux (ancêtre → descendant) correspond."""
    nodes = [root]
    for name in path:
        nodes = [child for node in nodes for child in node.iter() if _local(child) == name and child is not node]
        if not nodes:
            return None
    return (nodes[0].text or '').strip() or None


def from_xml(raw):
    """Champs utiles d'une facture XML CII (Factur-X) ou UBL. None si illisible."""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return None
    kind = _local(root)
    if kind == 'CrossIndustryInvoice':
        data = {'number': _first(root, 'ExchangedDocument', 'ID'), 'type_code': _first(root, 'ExchangedDocument', 'TypeCode') or '380',
                'issue_date': _date(_first(root, 'ExchangedDocument', 'IssueDateTime', 'DateTimeString')),
                'due_date': _date(_first(root, 'SpecifiedTradePaymentTerms', 'DueDateDateTime', 'DateTimeString')),
                'supplier': _first(root, 'SellerTradeParty', 'Name'),
                'subtotal': _num(_first(root, 'SpecifiedTradeSettlementHeaderMonetarySummation', 'TaxBasisTotalAmount')),
                'vat': _num(_first(root, 'SpecifiedTradeSettlementHeaderMonetarySummation', 'TaxTotalAmount')),
                'total': _num(_first(root, 'SpecifiedTradeSettlementHeaderMonetarySummation', 'GrandTotalAmount')),
                'original_number': _first(root, 'InvoiceReferencedDocument', 'IssuerAssignedID')}
    elif kind in ('Invoice', 'CreditNote'):
        ids = [c for c in root if _local(c) == 'ID']
        data = {'number': (ids[0].text or '').strip() if ids else None,
                'type_code': next(((c.text or '').strip() for c in root if _local(c) in ('InvoiceTypeCode', 'CreditNoteTypeCode')),
                                  '381' if kind == 'CreditNote' else '380'),
                'issue_date': _date(next(((c.text or '') for c in root if _local(c) == 'IssueDate'), None)),
                'due_date': _date(next(((c.text or '') for c in root if _local(c) == 'DueDate'), None)),
                'supplier': _first(root, 'AccountingSupplierParty', 'RegistrationName') or _first(root, 'AccountingSupplierParty', 'Name'),
                'subtotal': _num(_first(root, 'LegalMonetaryTotal', 'TaxExclusiveAmount')),
                'vat': _num(next(((a.text or '') for t in root if _local(t) == 'TaxTotal' for a in t if _local(a) == 'TaxAmount'), None)),
                'total': _num(_first(root, 'LegalMonetaryTotal', 'TaxInclusiveAmount')),
                'original_number': _first(root, 'BillingReference', 'ID')}
    else:
        return None
    if not data['number'] or (data['subtotal'] is None and data['total'] is None):
        return None
    return data


def embedded_xml(pdf_bytes):
    """XML embarqué d'un Factur-X (pièce jointe ``factur-x.xml`` ou premier .xml), ou None."""
    try:
        from pypdf import PdfReader
        attachments = PdfReader(io.BytesIO(pdf_bytes)).attachments
    except Exception:
        return None
    names = sorted(attachments, key=lambda n: (n.lower() != 'factur-x.xml', n))
    for name in names:
        if name.lower().endswith('.xml') and attachments[name]:
            return attachments[name][0]
    return None


def read_invoice(item, content, ctype):
    """Contenu d'une facture reçue : ``en_invoice`` d'abord, sinon le fichier d'origine."""
    data = from_en_invoice((item or {}).get('en_invoice'))
    if data:
        return data
    if not content:
        return None
    if content[:4] == b'%PDF' or 'pdf' in (ctype or ''):
        xml = embedded_xml(content)
        return from_xml(xml) if xml else None
    return from_xml(content)


def _complete(data):
    """Complète les montants manquants et vérifie qu'ils sont cohérents."""
    ht, vat, ttc = data['subtotal'], data['vat'], data['total']
    if vat is None and ht is not None and ttc is not None:
        vat = round(ttc - ht, 2)
    if ht is None and ttc is not None:
        ht = round(ttc - (vat or 0), 2)
    if ttc is None:
        ttc = round((ht or 0) + (vat or 0), 2)
    vat = vat or 0.0
    if not all(v is not None and math.isfinite(v) and v >= 0 for v in (ht, vat, ttc)):
        raise ValueError('montants non valides')
    return ht, vat, ttc


# ------------------------------------------------------------------ import

def _known(c, eid, remote_id):
    for table in ('purchase_invoices', 'purchase_credit_notes'):
        if c.execute(f"SELECT 1 FROM {table} WHERE entity_id IS ? AND weinvoice_invoice_id=? AND einvoice_provider=?",
                     (eid, remote_id, ES.PROVIDER)).fetchone():
            return True
    return False


def sync_inbound(c, eid, org_id, pdf_dir, max_items=None, http=None):
    """Importe les factures reçues par SUPER PDP pour une entité. Retourne le même bilan que
    la réception WeInvoice : created / skipped / ignored / failed / remaining."""
    from profitos.accounting import generate_purchase_credit_entry, generate_purchase_entry
    from profitos.runtime import log_ops_event, now
    row = ES.connection(c, eid)
    if not row or row['status'] != 'connected' or not row['verified']:
        raise NotConnected()
    client = ES.client_for(c, eid, org_id, http=http)
    created = skipped = ignored = remaining = processed = 0
    failed = []
    cursor = row['inbound_cursor']
    pages = 0
    while True:
        page = client.received(starting_after_id=cursor) or {}
        items = page.get('data') or []
        pages += 1
        stop = False
        for item in items:
            remote_id = str(item.get('id') or '').strip()
            if not remote_id or str(item.get('direction') or 'in') != 'in':
                continue
            if _known(c, eid, remote_id):
                skipped += 1
                if not stop:                     # après une facture laissée pour plus tard, le curseur est figé
                    cursor = int(remote_id)
                continue
            if max_items is not None and processed >= max_items:
                remaining += 1
                stop = True                      # le curseur n'avance pas : reprise au prochain passage
                continue
            processed += 1
            code, _label, _reason = SP.current_status(item)
            if code == SP.REJECTED:              # rejet de la plateforme : pas une facture à payer
                ignored += 1
                cursor = int(remote_id)
                continue
            stored = None
            try:
                content, ctype = client.download(remote_id)
                data = read_invoice(item, content, ctype)
                if not data:
                    raise ValueError('contenu de la facture illisible')
                subtotal, vat, total = _complete(data)
                number = data['number'][:100]
                supplier = (data['supplier'] or 'Fournisseur électronique')[:255]
                status = ES.STATUS_BY_CODE.get(code, 'RECEIVED')
                reg = code.split(':')[-1] if code else None
                ext = 'pdf' if content[:4] == b'%PDF' else 'xml'
                digest = hashlib.sha256(f'superpdp:{eid}:{remote_id}'.encode()).hexdigest()[:24]
                if data['type_code'] in CREDIT_NOTE_TYPES:
                    if not data['original_number']:
                        raise ValueError("avoir fournisseur sans référence à la facture d'origine")
                    original = c.execute('SELECT * FROM purchase_invoices WHERE entity_id IS ? AND invoice_number=? '
                                         'ORDER BY id DESC LIMIT 1', (eid, data['original_number'])).fetchone()
                    if not original:
                        raise ValueError("facture fournisseur d'origine introuvable")
                    stored = f'superpdp_credit_{digest}.{ext}'
                    (pdf_dir / stored).write_bytes(content)
                    cur = c.execute("""INSERT INTO purchase_credit_notes(
                        entity_id,original_purchase_id,original_invoice_number,credit_number,supplier_name,issue_date,
                        subtotal,vat_amount,total,category,notes,document_path,weinvoice_invoice_id,weinvoice_status,
                        weinvoice_regulatory_code,weinvoice_last_sync_at,created_at,einvoice_provider)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (eid, original['id'], data['original_number'], number, supplier, data['issue_date'], subtotal, vat, total,
                         original['category'] or 'autre', 'Avoir reçu par facturation électronique SUPER PDP', stored,
                         remote_id, status, reg, now(), now(), ES.PROVIDER))
                    credit = c.execute('SELECT * FROM purchase_credit_notes WHERE id=?', (cur.lastrowid,)).fetchone()
                    generate_purchase_credit_entry(c, credit, commit=False)
                else:
                    stored = f'superpdp_{digest}.{ext}'
                    (pdf_dir / stored).write_bytes(content)
                    setting = c.execute('SELECT require_purchase_validation FROM app_settings WHERE id=1').fetchone()
                    validation = 'pending' if (setting and setting['require_purchase_validation']) else 'approved'
                    cur = c.execute("""INSERT INTO purchase_invoices(
                        supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,
                        notes,created_at,document_path,category,validation_status,entity_id,
                        weinvoice_invoice_id,weinvoice_status,weinvoice_regulatory_code,weinvoice_last_sync_at,einvoice_provider)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (supplier, number, data['issue_date'], data['due_date'], subtotal, vat, total, 'unpaid',
                         'Reçue par facturation électronique SUPER PDP', now(), stored, 'autre', validation, eid,
                         remote_id, status, reg, now(), ES.PROVIDER))
                    if validation == 'approved':
                        purchase = c.execute('SELECT * FROM purchase_invoices WHERE id=?', (cur.lastrowid,)).fetchone()
                        generate_purchase_entry(c, purchase, commit=False)
                ES._save(c, eid, inbound_cursor=int(remote_id))
                c.commit()
                created += 1
                cursor = int(remote_id)
            except Exception as exc:
                c.rollback()
                try:
                    if stored:
                        (pdf_dir / stored).unlink(missing_ok=True)
                except Exception:
                    pass
                failed.append(f'{remote_id}: {exc if isinstance(exc, ValueError) else type(exc).__name__}')
                log_ops_event('SUPERPDP_INBOUND_IMPORT_FAILED', 'ERROR', detail=f'id={remote_id} error={type(exc).__name__}')
                stop = True                      # on ne saute pas une facture en échec : elle sera retentée
                break
        if stop or not items or not page.get('has_after') or pages > 500:
            break
    if cursor != row['inbound_cursor'] and not failed:
        ES._save(c, eid, inbound_cursor=cursor)
        c.commit()
    log_ops_event('SUPERPDP_INBOUND_SYNC', 'INFO' if not failed else 'WARNING',
                  detail=f'entity={eid} created={created} skipped={skipped} ignored={ignored} failed={len(failed)}')
    return {'created': created, 'skipped': skipped, 'ignored': ignored, 'failed': failed, 'remaining': remaining}


# ------------------------------------------------------------------ statuts de l'acheteur

def buyer_action(c, purchase, org_id, action, note=None, http=None):
    """Pousse le statut choisi par l'acheteur et met à jour la facture d'achat."""
    from profitos.runtime import now
    if action not in BUYER_ACTIONS:
        raise SP.SuperPdpError("Action non autorisée.")
    code, status = BUYER_ACTIONS[action]
    note = (note or '').strip()
    if action in ('refuse', 'dispute') and not note:
        raise SP.SuperPdpError("Un motif est obligatoire pour un refus ou un litige.")
    client = ES.client_for(c, purchase['entity_id'], org_id, http=http)
    client.push_status(purchase['weinvoice_invoice_id'], code,
                       reason_code=REFUSAL_REASON_CODE if action == 'refuse' else None, notes=note or None)
    reg = code.split(':')[-1]
    c.execute('UPDATE purchase_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=? WHERE id=?',
              (status, reg, now(), purchase['id']))
    c.execute("""INSERT INTO einvoice_events(entity_id,invoice_id,provider,event_type,remote_id,status,regulatory_code,
                 idempotency_key,detail,occurred_at) VALUES(?,0,?,?,?,?,?,?,?,?)""",
              (purchase['entity_id'], ES.PROVIDER, 'purchase_buyer_action', purchase['weinvoice_invoice_id'], status, reg,
               f"buyer-{purchase['id']}-{action}-{now()}", (f'{action} : {note}' if note else action)[:2000], now()))
    c.commit()
    return status


def sync_purchase_status(c, purchase, org_id, http=None):
    from profitos.runtime import now
    data = ES.client_for(c, purchase['entity_id'], org_id, http=http).get_invoice(purchase['weinvoice_invoice_id'])
    code, _label, _reason = SP.current_status(data)
    status = ES.STATUS_BY_CODE.get(code, purchase['weinvoice_status'] or 'RECEIVED')
    c.execute('UPDATE purchase_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?,weinvoice_last_sync_at=? WHERE id=?',
              (status, code.split(':')[-1] if code else None, now(), purchase['id']))
    c.commit()
    return status
