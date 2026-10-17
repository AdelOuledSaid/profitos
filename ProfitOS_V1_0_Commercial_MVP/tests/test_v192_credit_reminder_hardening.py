from pathlib import Path
import sqlite3, sys, importlib.util

ROOT=Path(__file__).resolve().parents[1]
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RUN=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
ACC=(ROOT/'profitos/accounting.py').read_text(encoding='utf-8')


def test_reminders_are_entity_scoped_in_detail_and_send_flow():
    assert 'invoice_id=? AND entity_id IS ? ORDER BY reminder_number DESC' in INV
    assert 'COUNT(*) AS n FROM invoice_reminders WHERE invoice_id=? AND entity_id IS ?' in INV
    assert 'INSERT INTO invoice_reminders(entity_id,invoice_id,recipient_email,sent_at,reminder_number)' in INV
    assert "('invoice_reminders','entity_id')" in RUN


def test_credit_note_has_accounting_generator_and_entity_idempotence():
    assert 'def generate_sale_credit_entry(conn, credit, revenue_account=' in ACC
    assert "_entry_already_exists(conn, 'outgoing_credit_note', credit['id'], entity_id)" in ACC
    assert "source_type='outgoing_credit_note', source_id=credit['id']" in ACC
    assert 'entity_id=entity_id' in ACC
    # v4xx : produits ventilés 707 (biens) / 706 (services) ou 419100 (acompte) ; la TVA
    # encore en attente (445740) est annulée avant la TVA collectée (445710).
    assert "account = revenue_account or REVENUE_ACCOUNT_BY_NATURE.get(g['nature'], SERVICES_REVENUE_ACCOUNT)" in ACC
    assert "{'account_code': VAT_PENDING_ACCOUNT, 'debit': moved['vat']" in ACC
    assert "{'account_code': VAT_COLLECTED_ACCOUNT, 'debit': rest" in ACC
    assert "'account_code': '411000', 'credit': credit['total']" in ACC


def test_credit_creation_is_atomic_with_accounting():
    block=INV[INV.index("def invoicing_credit_new(invoice_id):"):INV.index("@app.route('/facturation/avoir/<int:credit_id>')")]
    assert 'generate_sale_credit_entry(c,credit,' in block
    assert "source_type IN ('outgoing_invoice','customer_deposit_invoice','customer_final_invoice') AND source_id=? AND entity_id IS ?" in block
    assert "l'écriture comptable de la facture d'origine est absente" in block
    assert 'except AccountingError as e:' in block
    assert 'c.rollback()' in block
    # no commit is allowed between the credit INSERT and accounting generation
    tail=block[block.index('INSERT INTO outgoing_credit_notes'):block.index('generate_sale_credit_entry(c,credit,')]
    assert 'c.commit()' not in tail


def test_no_customer_recurring_invoice_engine_is_falsely_claimed():
    # Existing recurring feature is supplier/purchase detection, not customer invoice scheduling.
    assert 'def _detect_recurring_suppliers' in INV
    assert "@app.route('/facturation/achats/recurrents')" in INV
