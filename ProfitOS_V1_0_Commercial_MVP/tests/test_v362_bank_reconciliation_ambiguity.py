from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANK = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")


def _client_block():
    return BANK.split("def _reconciliation_suggestions", 1)[1].split("def register", 1)[0]


def _supplier_block():
    return BANK.split("def _purchase_reconciliation_suggestions", 1)[1].split("def _bank_allocated_total", 1)[0]


def test_client_equal_score_ambiguity_is_not_arbitrarily_proposed():
    block = _client_block()
    assert "second=scored[1][0] if len(scored)>1 else None" in block
    assert "best[0]-second < 15" in block
    assert "continue" in block


def test_client_explicit_invoice_number_gets_strong_priority_signal():
    block = BANK.split("def _bank_match_score", 1)[1].split("def _reconciliation_suggestions", 1)[0]
    assert "if ref and ref in label:" in block
    assert "score += 35" in block
    assert "n° facture" in block


def test_supplier_same_amount_candidates_remain_manual_choices():
    block = _supplier_block()
    assert "out.append(" in block
    assert "out.sort(" in block
    assert "return out" in block
    # No automatic payment/commit is allowed in the suggestion engine.
    assert "INSERT INTO purchase_invoice_payments" not in block
    assert ".commit()" not in block


def test_reconciliation_queries_are_entity_scoped():
    client = _client_block()
    supplier = _supplier_block()
    assert "WHERE entity_id IS ?" in client
    assert "WHERE p.entity_id IS ?" in supplier
    assert "pp.entity_id IS ?" in supplier
