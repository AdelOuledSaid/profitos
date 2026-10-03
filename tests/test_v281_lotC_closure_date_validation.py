from pathlib import Path

T = Path("profitos/routes/accounting.py").read_text(encoding="utf-8")

def test_accounting_closure_rejects_malformed_calendar_dates():
    assert "date.fromisoformat(closed_until)" in T
    assert "parsed_closed_until is None" in T
    assert 'error = "La date de clôture est invalide."' in T

def test_accounting_closure_keeps_entity_scoped_locking():
    assert '"UPDATE accounting_entries SET is_locked=1 WHERE entity_id IS ? AND entry_date<=?"' in T
    assert "'SELECT closed_until FROM accounting_entity_closure WHERE entity_key=?'" in T
