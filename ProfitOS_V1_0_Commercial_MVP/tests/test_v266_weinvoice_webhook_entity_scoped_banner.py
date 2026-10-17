from pathlib import Path
W=Path("profitos/weinvoice.py").read_text(encoding="utf-8")
def test_entity_scope_preserved():
    assert "SELECT id,entity_id FROM outgoing_invoices WHERE weinvoice_invoice_id=?" in W
    assert "WHERE id=? AND entity_id IS ?" in W
    assert "(str(status), str(cdv) if cdv is not None else None, now(), last_error, row['id'], row['entity_id'])" in W
    assert "rowcount" in W
