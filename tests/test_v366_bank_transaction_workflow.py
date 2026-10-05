from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
TPL=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_v366_workflow_registry_is_entity_scoped_and_unique():
    assert "CREATE TABLE IF NOT EXISTS bank_transaction_workflow" in BANK
    assert "UNIQUE(entity_id,bank_transaction_id)" in BANK

def test_v366_states_cover_pending_reconciled_ignored():
    b=BANK.split("def _bank_transaction_states",1)[1].split('@app.route("/banking")',1)[0]
    assert "state='ignored'" in b
    assert "state='reconciled'" in b
    assert "state='pending'" in b
    assert "allocated >= total-.005" in b

def test_v366_ignored_and_reconciled_do_not_get_matching_suggestions():
    assert "active_transactions = [t for t in transactions if transaction_states[t['id']]['state']=='pending']" in BANK
    assert "_reconciliation_suggestions(c, active_transactions)" in BANK
    assert "for tx in active_transactions:" in BANK

def test_v366_ignore_and_restore_are_post_csrf_ui_actions():
    assert '@app.post("/banking/transaction/<int:tx_id>/ignore")' in BANK
    assert '@app.post("/banking/transaction/<int:tx_id>/restore")' in BANK
    assert "csrf_token" in TPL
    assert "banking_transaction_ignore" in TPL
    assert "banking_transaction_restore" in TPL

def test_v366_ignore_and_restore_are_entity_scoped():
    for fn in ("def banking_transaction_ignore","def banking_transaction_restore"):
        b=BANK.split(fn,1)[1].split("return redirect(url_for('banking'))",1)[0]
        assert "entity_id IS ?" in b

def test_v366_ui_has_three_workflow_labels():
    assert "À traiter" in TPL
    assert "Rapprochée" in TPL
    assert "Ignorée" in TPL
    assert "Remettre à traiter" in TPL

def test_v366_preserves_v365_and_v364_guards():
    assert "remaining_after <= 5.00" in BANK
    assert "paiement groupé possible" in BANK
    assert "tx['transaction_date']" in BANK
    assert "tx['booking_date']" not in BANK
