from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/banking.html').read_text(encoding='utf-8')
def test_learning_registry_is_entity_scoped():
    assert 'bank_accounting_learning_rules' in RUNTIME and 'bank_accounting_validations' in RUNTIME
    assert 'UNIQUE(entity_id,bank_transaction_id)' in RUNTIME
def test_suggestion_never_posts_accounting_entry():
    body=BANK[BANK.index('def _accounting_suggestion'):BANK.index('def _cfg')]
    assert 'generate_' not in body and 'INSERT INTO accounting_entries' not in body and 'confidence_score' in body
def test_learning_is_entity_scoped_and_human_validated():
    assert "WHERE entity_id IS ? AND ? LIKE" in BANK and "a.entity_id IS ?" in BANK
    assert 'bank_accounting_validations' in BANK and 'bank_accounting_learning_rules' in BANK
def test_account_code_is_checked_before_learning():
    assert 'accounting_chart_of_accounts WHERE code=?' in BANK and 'Compte comptable invalide pour cette entité.' in BANK
def test_ui_shows_score_and_requires_explicit_validation():
    assert 'Suggestion {{ sug.confidence_score }} %' in TPL and 'Validation humaine requise.' in TPL
    assert 'name="account_code"' in TPL and 'name="vat_rate"' in TPL and '>Valider</button>' in TPL
