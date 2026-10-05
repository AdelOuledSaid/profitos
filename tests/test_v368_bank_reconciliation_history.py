from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
TPL=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_v368_customer_history_is_explicit():
    assert "Rapprochements clients confirmés" in TPL
    assert "invoice_number" in TPL
    assert "client_name" in TPL
    assert "matched_amount" in TPL

def test_v368_supplier_history_query_is_entity_scoped():
    b=BANK.split("purchase_reconciliations = c.execute",1)[1].split("categories =",1)[0]
    assert "FROM bank_purchase_allocations r" in b
    assert "JOIN bank_transactions t ON t.id=r.bank_transaction_id" in b
    assert "JOIN purchase_invoices p ON p.id=r.purchase_invoice_id" in b
    assert "WHERE r.{ef}" in b

def test_v368_supplier_history_is_passed_to_template():
    assert "purchase_reconciliations=purchase_reconciliations" in BANK

def test_v368_supplier_history_ui_has_audit_fields():
    assert "Rapprochements fournisseurs confirmés" in TPL
    assert "Fournisseur" in TPL
    assert "Montant affecté" in TPL
    assert "r.transaction_date" in TPL
    assert "r.supplier_name" in TPL
    assert "r.matched_amount" in TPL

def test_v368_history_supports_multiple_allocations():
    b=BANK.split("purchase_reconciliations = c.execute",1)[1].split("categories =",1)[0]
    assert "ORDER BY r.id DESC LIMIT 50" in b
    assert "GROUP BY" not in b
    assert "DISTINCT" not in b

def test_v368_preserves_v367b_matching_and_v366_workflow():
    assert "def _token_typo_match" in BANK
    assert "bank_transaction_workflow" in BANK
    assert "tx['transaction_date']" in BANK
    assert "tx['booking_date']" not in BANK
