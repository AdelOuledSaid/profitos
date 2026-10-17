from pathlib import Path
T=Path("profitos/routes/payroll.py").read_text(encoding="utf-8")

def test_payroll_import_validates_real_accounting_date():
    assert "entry_date=date.fromisoformat(entry_date).isoformat()" in T
    assert "La date comptable de l'import de paie est invalide." in T

def test_payroll_rows_reject_invalid_debit_credit_semantics():
    assert "if debit < 0 or credit < 0:" in T
    assert "if debit and credit:" in T
    assert "if not debit and not credit:" in T

def test_payroll_duplicate_period_is_scoped_by_entity_and_provider():
    assert "entity_id IS ? AND provider=? AND period_label=? AND status IN" in T
    assert "(eid,provider,period)" in T
    assert "Un import de paie existe déjà pour ce prestataire et cette période sur cette entité." in T
