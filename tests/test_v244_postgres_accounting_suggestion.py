from pathlib import Path

S = Path('profitos/routes/bank_sync.py').read_text(encoding='utf-8')
T = Path('templates/banking.html').read_text(encoding='utf-8')


def test_learning_rule_query_has_no_literal_like_percent_wildcards():
    assert "? LIKE '%' || pattern || '%'" not in S
    assert "WHERE entity_id IS ?" in S
    # v384 uses exact direction-aware consensus instead of legacy substring matching.
    assert "(r['pattern'] or '') == pattern" in S
    assert "consensus(directional)" in S


def test_learning_rule_matching_stays_entity_scoped_and_human_confirmed():
    assert "SELECT * FROM bank_accounting_learning_rules" in S
    assert "WHERE entity_id IS ?" in S
    assert "Habitudes contradictoires : aucune proposition automatique" in S
    assert "Validation humaine requise." in T
