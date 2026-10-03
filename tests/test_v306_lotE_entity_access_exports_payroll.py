from pathlib import Path
P=Path("profitos/routes/payroll.py").read_text(encoding="utf-8")
A=Path("profitos/routes/accounting.py").read_text(encoding="utf-8")

def test_payroll_routes_enforce_entity_access_server_side():
    assert P.count("user_can_access_entity(c, session.get('user_id'), eid)") >= 2
    assert P.count("c.close(); abort(403)") >= 2

def test_fec_export_rejects_crafted_inaccessible_entity():
    block=A.split("def accounting_fec_export():",1)[1].split("def accounting_fec_send_accountant():",1)[0]
    assert "user_can_access_entity(c, session.get('user_id'), entity_id)" in block
    assert "accessible_entities(c, session.get('user_id'))" in block
    assert "list_all_entities(c)" not in block

def test_fec_email_route_rejects_inaccessible_entity():
    block=A.split("def accounting_fec_send_accountant():",1)[1].split("def accounting_fec_download(token):",1)[0]
    assert "user_can_access_entity(c2, session.get('user_id'), entity_id)" in block
    assert "Vous n'avez pas accès à cette entité." in block
