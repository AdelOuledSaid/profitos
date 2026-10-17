from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")

def _list_accounts_block():
    return SERVICE.split("def list_accounts(",1)[1].split("def request_new_account(",1)[0]

def test_swan_accounts_use_current_status_info_schema():
    block = _list_accounts_block()
    assert "statusInfo {" in block
    assert "status\n" in block
    assert "\n            status\n" not in block

def test_swan_account_status_is_normalized_for_existing_ui():
    block = _list_accounts_block()
    assert "node['status'] = (node.get('statusInfo') or {}).get('status')" in block

def test_connection_remains_read_only():
    block = _list_accounts_block()
    assert "query ProfitOSListAccounts" in block
    assert "mutation " not in block
