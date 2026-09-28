from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING = ROOT / "profitos" / "routes" / "accounting.py"


def test_accounting_has_no_postgresql_incompatible_two_arg_round_on_real_sums():
    src = ACCOUNTING.read_text(encoding="utf-8")
    assert "ROUND(SUM(l.debit) - SUM(l.credit), 2)" not in src
    assert "ROUND(SUM(l.debit)-SUM(l.credit),2)" not in src


def test_closure_balance_check_casts_aggregate_to_numeric():
    src = ACCOUNTING.read_text(encoding="utf-8")
    assert "HAVING ABS(CAST(SUM(l.debit) - SUM(l.credit) AS NUMERIC)) >= 0.005" in src


def test_vat_balance_check_is_postgresql_safe_and_does_not_use_select_alias_in_having():
    src = ACCOUNTING.read_text(encoding="utf-8")
    assert "HAVING ABS(CAST(SUM(l.debit)-SUM(l.credit) AS NUMERIC))>0.01" in src
    assert "HAVING ABS(diff)>0.01" not in src
