from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def _block():
    return B.split("def _bootstrap_accounting_suggestion(tx):",1)[1].split("def _accounting_suggestion",1)[0]

def test_bootstrap_is_direction_aware():
    assert "direction='debit' if amount < 0 else 'credit'" in _block()

def test_bootstrap_never_posts_or_auto_validates():
    b=_block()
    assert "automation_eligible=False" in b
    assert "execute(" not in b and "commit(" not in b

def test_bootstrap_does_not_guess_vat():
    assert "vat_rate=None" in _block()

def test_conservative_common_expense_accounts():
    b=_block()
    for account in ("641000","616000","613000","606100","626000"):
        assert account in b

def test_existing_category_mapping_has_priority_over_bootstrap():
    final=B.split("category=tx['category'] or apply_categorization_rule",1)[1]
    assert final.index("if account:") < final.index("bootstrap=_bootstrap_accounting_suggestion(tx)")

def test_no_match_remains_no_suggestion():
    assert "reason='Aucune habitude suffisamment fiable'" in B
