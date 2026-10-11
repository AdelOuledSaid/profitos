from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S = (ROOT / "profitos" / "swan_baas.py").read_text(encoding="utf-8")
R = (ROOT / "profitos" / "routes" / "swan_baas.py").read_text(encoding="utf-8")
T = (ROOT / "templates" / "swan_settings.html").read_text(encoding="utf-8")

def test_swan_account_query_reads_balances_and_recent_transactions():
    assert "balances {" in S
    assert "available { value currency }" in S
    assert "booked { value currency }" in S
    assert "transactions(first: 10)" in S
    assert "amount { value currency }" in S
    assert "reference" in S
    assert "statusInfo { status }" in S

def test_sandbox_accounts_visible_but_live_stays_locally_scoped():
    assert "if current_environment() == 'sandbox':" in R
    assert "remote_accounts = all_remote_accounts" in R
    assert "allowed_remote_ids" in R

def test_ui_shows_account_balance_transactions_and_refresh():
    assert "Mon compte Swan" in T
    assert "Solde disponible" in T
    assert "Dernières transactions" in T
    assert "Actualiser" in T
    assert "a.available_balance.get('value','—')" in T
    assert "tx.reference" in T
