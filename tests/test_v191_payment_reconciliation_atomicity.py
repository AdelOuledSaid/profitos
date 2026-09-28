from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING = (ROOT / 'profitos/accounting.py').read_text(encoding='utf-8')
INVOICING = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
BANKING = (ROOT / 'profitos/routes/bank_sync.py').read_text(encoding='utf-8')


def test_payment_entries_are_idempotent_per_entity():
    assert "_entry_already_exists(conn, source_type, invoice['id'], entity_id)" in ACCOUNTING
    assert "source_id=invoice['id'], entity_id=entity_id" in ACCOUNTING
    assert "_entry_already_exists(conn, source_type, purchase['id'], entity_id)" in ACCOUNTING
    assert "source_id=purchase['id'], entity_id=entity_id" in ACCOUNTING


def test_auto_lettering_cannot_cross_entities():
    assert "def _letter_pair(conn, account_code, original_source_type, original_source_id, new_entry_id, entity_id=None):" in ACCOUNTING
    assert "e.source_id=? AND e.entity_id IS ? AND l.account_code=?" in ACCOUNTING
    assert "invoice['id'], entry_id, entity_id" in ACCOUNTING
    assert "purchase['id'], entry_id, entity_id" in ACCOUNTING


def test_manual_invoice_payment_rolls_back_business_status_on_accounting_failure():
    block = INVOICING[INVOICING.index('def invoicing_mark_paid'):INVOICING.index("@app.route('/facturation/<int:invoice_id>/modifier")]
    assert "generate_sale_partial_payment_entry(c,inv,payment)" in block
    assert "except AccountingError as e:" in block
    assert "c.rollback()" in block
    assert "Paiement non enregistré" in block
    assert block.index("generate_sale_partial_payment_entry(c,inv,payment)") < block.index("c.commit()")


def test_manual_purchase_payment_rolls_back_business_status_on_accounting_failure():
    block = INVOICING[INVOICING.index('def purchase_mark_paid'):INVOICING.index("def purchase_sepa_batch")]
    assert "INSERT INTO purchase_invoice_payments" in block
    assert "generate_purchase_partial_payment_entry(c,p,payment)" in block
    assert "c.rollback()" in block
    assert "Paiement non enregistré" in block
    assert block.index("generate_purchase_partial_payment_entry(c,p,payment)") < block.index("c.commit()")


def test_bank_customer_reconciliation_is_atomic_with_accounting():
    block = BANKING[BANKING.index('def banking_reconcile'):BANKING.index('def banking_sync')]
    assert "INSERT INTO outgoing_invoice_payments" in block
    assert "INSERT INTO bank_invoice_allocations" in block
    assert "generate_sale_partial_payment_entry(c,inv,payment)" in block
    assert "c.rollback()" in block
    assert "Rapprochement annulé" in block
    assert block.index("generate_sale_partial_payment_entry(c,inv,payment)") < block.index("c.commit()")


def test_bank_supplier_reconciliation_is_atomic_with_accounting():
    block = BANKING[BANKING.index('def confirm_purchase_reconciliation'):BANKING.index("@app.route('/banking/regles'")]
    assert "INSERT INTO purchase_invoice_payments" in block
    assert "INSERT INTO bank_purchase_allocations" in block
    assert "generate_purchase_partial_payment_entry(c,p,payment)" in block
    assert "c.rollback()" in block
    assert "Rapprochement annulé" in block
    assert block.index("generate_purchase_partial_payment_entry(c,p,payment)") < block.index("c.commit()")


def test_sepa_export_is_traced_but_does_not_create_unconfirmed_payment():
    block = INVOICING[INVOICING.index('def purchase_sepa_batch'):INVOICING.index("@app.route('/facturation/commandes')")]
    # Pass 33: producing a pain.001 file is not proof that the bank executed it.
    assert "INSERT INTO sepa_export_batches" in block
    assert "INSERT INTO sepa_export_items" in block
    assert "INSERT INTO purchase_invoice_payments" not in block
    assert "generate_purchase_partial_payment_entry(c,r,payment)" not in block
    assert "UPDATE purchase_invoices SET status='paid'" not in block
    assert "c.rollback()" in block
    assert "Export SEPA interrompu" in block
