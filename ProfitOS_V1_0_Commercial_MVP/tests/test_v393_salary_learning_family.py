from pathlib import Path

T = Path('profitos/routes/bank_sync.py').read_text(encoding='utf-8')


def test_salary_names_share_one_canonical_signature():
    assert "if words and words[0] == 'salaire':" in T
    assert "base='salaire'" in T
    assert "return f\"{direction}:{base}\"[:120]" in T


def test_existing_named_salary_rules_are_reused_without_db_migration():
    assert 'def _learning_family_patterns' in T
    assert "canonical + ' '" in T
    assert ".startswith(family_prefix)" in T
    assert 'confirmations_total=sum' in T


def test_salary_family_keeps_direction_and_blocks_conflicting_accounts():
    assert "canonical in ('debit:salaire', 'credit:salaire')" in T
    assert "choices={(r['account_code'], r['vat_rate']) for r in family}" in T
    assert "if len(choices) != 1:" in T
    assert "source='famille salaire'" in T
