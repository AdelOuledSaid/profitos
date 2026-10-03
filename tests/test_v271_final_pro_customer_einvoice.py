from pathlib import Path
T=Path("templates/invoicing_detail.html").read_text(encoding="utf-8")
R=Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")

def test_final_customer_einvoice_ui():
    assert "Simuler le statut Sandbox" not in T
    assert 'name="sandbox_status"' not in T
    assert "/v1/_sandbox/" not in T
    assert "ev.detail or '—'" not in T
    assert "Transmission électronique" in T
    assert "Mise à jour du statut" in T
    assert "Facturation électronique : <strong>" in T
    assert "Actualiser le statut" in T

def test_due_date_guard_create_and_edit():
    assert R.count("La date d'échéance ne peut pas être antérieure à la date d'émission.") >= 2
    assert "date.fromisoformat(due_date) < date.fromisoformat(issue_date)" in R
    assert "date.fromisoformat(due_date) < date.fromisoformat(inv['issue_date'])" in R
