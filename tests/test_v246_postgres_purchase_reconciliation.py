from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')

def test_purchase_reconciliation_uses_parameterized_null_safe_entity_scope():
    assert 'pp.entity_id IS p.entity_id' not in BANK
    assert 'WHERE pp.purchase_invoice_id=p.id AND pp.entity_id IS ?' in BANK
    assert 'WHERE p.entity_id IS ?' in BANK
    assert '""",(eid,eid)).fetchall()' in BANK
