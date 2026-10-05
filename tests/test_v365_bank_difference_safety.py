from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
TPL=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def supplier():
    return BANK.split("def _purchase_reconciliation_suggestions",1)[1].split("def _bank_allocated_total",1)[0]

def confirm():
    return BANK.split("def confirm_purchase_reconciliation",1)[1].split("@app.route('/banking/regles'",1)[0]

def test_v365_difference_is_metadata_not_invoice_payment():
    b=supplier()
    assert "remaining_after=max(0.0,round(target-suggested_amount,2))" in b
    assert "'remaining_after':remaining_after" in b
    assert "'small_difference':small_difference" in b

def test_v365_small_difference_is_conservative():
    b=supplier()
    assert "remaining_after <= 5.00" in b
    assert "round(total*0.02,2)" in b

def test_v365_ui_posts_only_suggested_invoice_amount():
    assert 'name="matched_amount"' in TPL
    assert "'%.2f'|format(s.amount)" in TPL

def test_v365_ui_warns_residual_must_be_handled_separately():
    assert "resteront à traiter sur le mouvement bancaire" in TPL
    assert "Petit écart / frais bancaire possible — à comptabiliser séparément." in TPL
    assert "Ce reliquat peut correspondre à une autre facture." in TPL

def test_v365_confirmation_still_forbids_overallocation():
    b=confirm()
    assert "amount>balance+.001" in b
    assert "amount>available+.001" in b
    assert "new_balance=round(balance-amount,2)" in b

def test_v365_preserves_v364_remaining_bank_amount():
    b=supplier()
    assert "abs(amount)-float(allocated)" in b
    assert "paiement groupé possible" in b
    assert "paiement partiel possible" in b

def test_v365_preserves_transaction_date_fix():
    assert "tx['transaction_date']" in BANK
    assert "tx['booking_date']" not in BANK
