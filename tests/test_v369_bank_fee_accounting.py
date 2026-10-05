from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
TPL=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_v369_fee_route_is_guarded():
    b=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    assert "current_entity_id()" in b
    assert "float(tx['amount'] or 0) >= 0" in b
    assert "residual > 5.00" in b and "reference*0.02" in b

def test_v369_balanced_fee_entry():
    b=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    assert "code LIKE '627%'" in b and "code LIKE '512%'" in b
    assert "residual,0.0" in b and "0.0,residual" in b
    assert "'bank_fee'" in b

def test_v369_idempotent_by_transaction():
    b=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    assert "source_type='bank_fee' AND source_id=?" in b
    assert "déjà comptabilisés" in b

def test_v369_fee_closes_workflow_residual():
    b=BANK.split("def _bank_transaction_states",1)[1].split('@app.route("/banking")',1)[0]
    assert "float(customer)+float(supplier)+float(fee)" in b
    assert "source_type='bank_fee'" in b

def test_v369_manual_ui_and_csrf():
    assert "Comptabiliser en frais bancaire" in TPL
    assert "banking_transaction_book_fee" in TPL
    assert 'name="csrf_token"' in TPL

def test_v369_preserves_prior_features():
    assert "def _token_typo_match" in BANK
    assert "bank_transaction_workflow" in BANK
    assert "tx['booking_date']" not in BANK
    assert "Rapprochements clients confirmés" in TPL
    assert "Rapprochements fournisseurs confirmés" in TPL
