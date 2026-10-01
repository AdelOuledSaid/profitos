from pathlib import Path

S = Path('profitos/routes/bank_sync.py').read_text(encoding='utf-8')


def test_learning_rule_query_has_no_literal_like_percent_wildcards():
    assert "? LIKE '%' || pattern || '%'" not in S
    assert "WHERE entity_id IS ?" in S
    assert "learned=next((r for r in rules" in S


def test_learning_rule_matching_stays_entity_scoped_and_human_confirmed():
    assert "SELECT * FROM bank_accounting_learning_rules" in S
    assert "validation humaine reste obligatoire" in S
