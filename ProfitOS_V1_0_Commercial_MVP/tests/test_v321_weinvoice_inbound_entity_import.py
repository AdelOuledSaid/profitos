from pathlib import Path
import ast

I = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")

def test_weinvoice_inbound_route_imports_entity_helper():
    assert "from profitos.entities import current_entity_id" in I
    block = I[I.index("def purchase_weinvoice_sync():"):]
    assert "eid=current_entity_id()" in block

def test_invoicing_module_parses():
    ast.parse(I)
