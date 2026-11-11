"""E-reporting par SUPER PDP (v497) : ventes aux particuliers et leurs encaissements.

La plateforme reçoit chaque opération, puis agrège et transmet elle-même à l'administration
selon le régime de TVA de l'entreprise. Veqalio pousse donc :
- une déclaration par facture émise à un particulier (périmètre ``b2c``), ventilée par taux ;
- une déclaration par encaissement d'une prestation de services à un particulier, sauf option
  pour les débits (la TVA y est exigible à l'encaissement : seul cas où le paiement se déclare).

Suivi par opération dans ``ereporting_records`` (une ligne par facture ou par paiement) :
- ``submitted`` : acceptée par la plateforme (référence conservée), jamais renvoyée ;
- ``error`` : refusée (4xx), renvoyée au prochain passage une fois la cause corrigée ;
- ``unknown`` : réponse perdue (coupure réseau). L'API n'ayant pas de clé d'idempotence, un
  renvoi automatique risquerait une double déclaration : l'opération attend une décision ;
- ``manual`` : cas que Veqalio ne sait pas déclarer seul (encaissement d'une opération mixte).

Hors périmètre : les ventes à des professionnels étrangers (le contrat d'API publié ne décrit
pas leur e-reporting).
"""
from profitos import einvoice_superpdp as ES
from profitos import superpdp as SP
from profitos.ereporting import CATEGORIES, _payment_split, _vat_buckets

VAT_REGIME_BY_FILING = {'ca3_mensuelle': 'monthly', 'ca3_trimestrielle': 'quarterly', 'ca12': 'simplified',
                        'franchise': 'vat_exemption'}
STATUSES_DONE = ('submitted', 'manual', 'unknown')
MAX_PER_RUN = 200


def _money(v):
    return f'{float(v or 0):.2f}'


def _rate(r):
    return f'{float(r):.2f}'


def vat_regime(c, eid):
    from profitos.corporate_tax import get_fiscal_profile
    company = c.execute('SELECT vat_regime, vat_on_debits FROM company WHERE id=1').fetchone()
    if company and (company['vat_regime'] or '') == 'franchise_293B':
        return 'vat_exemption', False
    filing = get_fiscal_profile(c, eid).get('vat_filing') or 'ca3_mensuelle'
    return VAT_REGIME_BY_FILING.get(filing, 'monthly'), bool(company and str(company['vat_on_debits'] or '0') == '1')


def transaction_payload(inv):
    """Déclaration d'une vente à un particulier, ventilée par taux de TVA."""
    category = inv['ereporting_category']
    if category not in CATEGORIES:
        raise ValueError("catégorie e-reporting absente ou invalide")
    buckets = _vat_buckets(inv)
    return {'date': inv['issue_date'], 'currency': 'EUR', 'role_code': 'SE', 'category_code': category,
            'tax_exclusive_amount': _money(sum(b for b, _v in buckets.values())),
            'tax_total': _money(sum(v for _b, v in buckets.values())),
            'tax_subtotals': [{'tax_percent': _rate(rate), 'taxable_amount': _money(base), 'tax_total': _money(vat)}
                              for rate, (base, vat) in sorted(buckets.items())]}


def payment_payload(inv, payment):
    return {'date': payment['payment_date'],
            'subtotals': [{'category_code': inv['ereporting_category'], 'tax_percent': _rate(rate),
                           'amount': _money(amount), 'currency_code': 'EUR'}
                          for rate, amount in _payment_split(inv, payment['amount'])]}


def _record(c, eid, record_type, source_type, source_id):
    return c.execute("""SELECT * FROM ereporting_records WHERE entity_id IS ? AND record_type=? AND source_type=?
                        AND source_id=?""", (eid, record_type, source_type, source_id)).fetchone()


def _mark(c, eid, record_type, source_type, source_id, period, status, *, reference=None, error=None, amounts=(0, 0, 0, 0),
          nature=None):
    from profitos.runtime import now
    row = _record(c, eid, record_type, source_type, source_id)
    if row:
        c.execute('UPDATE ereporting_records SET status=?, provider_reference=?, last_error=?, updated_at=? WHERE id=?',
                  (status, reference, (error or None) and str(error)[:1000], now(), row['id']))
    else:
        c.execute("""INSERT INTO ereporting_records(entity_id,record_type,source_type,source_id,period_date,amount_ht,
                     vat_amount,amount_ttc,payment_amount,operation_nature,status,provider_reference,last_error,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (eid, record_type, source_type, source_id, period, *amounts, nature, status, reference,
                   (error or None) and str(error)[:1000], now(), now()))


def _pending_invoices(c, eid, limit):
    return c.execute(f"""SELECT i.* FROM outgoing_invoices i WHERE i.entity_id IS ? AND i.ereporting_scope='b2c'
        AND i.status IN ('sent','partially_paid','paid')
        AND NOT EXISTS (SELECT 1 FROM ereporting_records r WHERE r.entity_id IS ? AND r.record_type='transaction'
                        AND r.source_type='outgoing_invoice' AND r.source_id=i.id
                        AND r.status IN ({','.join('?' * len(STATUSES_DONE))}))
        ORDER BY i.issue_date, i.id LIMIT ?""", (eid, eid, *STATUSES_DONE, limit)).fetchall()


def _pending_payments(c, eid, limit):
    return c.execute(f"""SELECT p.* FROM outgoing_invoice_payments p JOIN outgoing_invoices i ON i.id=p.invoice_id
        WHERE p.entity_id IS ? AND i.entity_id IS ? AND i.ereporting_scope='b2c'
        AND NOT EXISTS (SELECT 1 FROM ereporting_records r WHERE r.entity_id IS ? AND r.record_type='payment'
                        AND r.source_type='outgoing_invoice_payment' AND r.source_id=p.id
                        AND r.status IN ({','.join('?' * len(STATUSES_DONE))}))
        ORDER BY p.payment_date, p.id LIMIT ?""", (eid, eid, eid, *STATUSES_DONE, limit)).fetchall()


def _push(c, eid, call, payload, mark):
    """Envoie une opération et note son sort. Retourne 'submitted', 'error' ou 'unknown'."""
    try:
        created = call([payload])
    except SP.SuperPdpError as exc:
        status = 'error' if exc.status and exc.status < 500 else 'unknown'
        mark(status, error=("Réponse de la plateforme non reçue : vérifiez dans SUPER PDP que l'opération n'y figure "
                            "pas avant de la renvoyer." if status == 'unknown' else exc))
        c.commit()
        return status
    reference = str((created[0] or {}).get('id') or '') if created else ''
    if not reference:
        mark('unknown', error="La plateforme a répondu sans identifiant : vérifiez dans SUPER PDP avant tout renvoi.")
        c.commit()
        return 'unknown'
    mark('submitted', reference=reference)
    c.commit()
    return 'submitted'


def declare_pending(c, eid, org_id, http=None, limit=MAX_PER_RUN):
    """Déclare les ventes aux particuliers et les encaissements pas encore déclarés.
    Retourne {'submitted', 'error', 'unknown', 'manual', 'skipped'}."""
    row = ES.connection(c, eid)
    if not row or row['status'] != 'connected' or not row['verified']:
        raise SP.SuperPdpConfigError("Facture électronique non reliée : reliez l'entreprise depuis le profil entreprise.")
    out = {'submitted': 0, 'error': 0, 'unknown': 0, 'manual': 0, 'skipped': 0}
    invoices = _pending_invoices(c, eid, limit)
    payments = _pending_payments(c, eid, limit)
    if not invoices and not payments:
        return out
    client = ES.client_for(c, eid, org_id, http=http)
    regime, on_debits = vat_regime(c, eid)
    client.update_vat_regime(regime, has_vat_on_debits=on_debits)        # calendrier de transmission
    for inv in invoices:
        amounts = (float(inv['subtotal'] or 0), float(inv['vat_amount'] or 0), float(inv['total'] or 0), 0.0)
        mark = lambda status, reference=None, error=None, inv=inv, amounts=amounts: _mark(
            c, eid, 'transaction', 'outgoing_invoice', inv['id'], inv['issue_date'], status, reference=reference,
            error=error, amounts=amounts, nature=inv['operation_nature'])
        try:
            payload = transaction_payload(inv)
        except ValueError as exc:
            mark('error', error=exc)
            c.commit()
            out['error'] += 1
            continue
        out[_push(c, eid, client.create_b2c_transactions, payload, mark)] += 1
    for pay in payments:
        inv = c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?', (pay['invoice_id'], eid)).fetchone()
        mark = lambda status, reference=None, error=None, pay=pay, inv=inv: _mark(
            c, eid, 'payment', 'outgoing_invoice_payment', pay['id'], pay['payment_date'], status, reference=reference,
            error=error, amounts=(0.0, 0.0, 0.0, float(pay['amount'] or 0)), nature=inv['operation_nature'])
        category = inv['ereporting_category']
        if on_debits or int(inv['vat_on_debits'] or 0) or category in ('TLB1', 'TNT1'):
            # Livraison de biens, opération non taxable ou option pour les débits : la TVA n'est
            # pas exigible à l'encaissement, le paiement ne se déclare pas.
            mark('manual', error="Encaissement hors e-reporting de paiement (biens, opération non taxable ou TVA sur les débits).")
            c.commit()
            out['skipped'] += 1
            continue
        if category != 'TPS1':
            mark('manual', error="Opération mixte : la part de services de cet encaissement est à déclarer avec votre "
                                 "expert-comptable.")
            c.commit()
            out['manual'] += 1
            continue
        try:
            payload = payment_payload(inv, pay)
        except ValueError as exc:
            mark('error', error=exc)
            c.commit()
            out['error'] += 1
            continue
        out[_push(c, eid, client.create_b2c_payments, payload, mark)] += 1
    return out


def summary(c, eid):
    """Compteurs pour l'écran de facturation."""
    rows = c.execute("""SELECT r.status, COUNT(*) AS n FROM ereporting_records r WHERE r.entity_id IS ?
                        AND r.source_type IN ('outgoing_invoice','outgoing_invoice_payment')
                        AND (r.provider_reference IS NOT NULL OR r.status IN ('error','unknown','manual'))
                        GROUP BY r.status""", (eid,)).fetchall()
    counts = {r['status']: int(r['n']) for r in rows}
    counts['pending'] = len(_pending_invoices(c, eid, 1000)) + len(_pending_payments(c, eid, 1000)) - counts.get('error', 0)
    counts['pending'] = max(0, counts['pending'])
    blocked = c.execute("""SELECT * FROM ereporting_records WHERE entity_id IS ? AND status IN ('error','unknown')
                           ORDER BY updated_at DESC LIMIT 10""", (eid,)).fetchall()
    return counts, blocked


def release_unknown(c, eid, record_id):
    """Après vérification par l'utilisateur dans SUPER PDP : remet une opération au sort inconnu
    dans la file (elle sera renvoyée) — ou la confirme comme déjà déclarée."""
    from profitos.runtime import now
    c.execute("UPDATE ereporting_records SET status='error', last_error=?, updated_at=? WHERE id=? AND entity_id IS ? "
              "AND status='unknown'", ("Renvoi demandé après vérification.", now(), record_id, eid))
    c.commit()


def confirm_unknown(c, eid, record_id):
    from profitos.runtime import now
    c.execute("UPDATE ereporting_records SET status='submitted', last_error=NULL, updated_at=? WHERE id=? AND entity_id IS ? "
              "AND status='unknown'", (now(), record_id, eid))
    c.commit()
