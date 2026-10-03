from pathlib import Path
T=Path("templates/invoicing_detail.html").read_text(encoding="utf-8")
R=Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")

def test_customer_ui_hides_sandbox_uuid_and_detail_column():
    assert "Simuler le statut Sandbox" not in T
    assert "Facturation électronique : <strong>" in T
    assert " · <code>{{ inv.weinvoice_invoice_id }}</code>" not in T
    assert "<th>Détail</th>" not in T
    assert "word-break:break-word;\">{{ ev.detail or '—' }}</td>" not in T

def test_due_date_is_not_before_issue_date_on_create_and_edit():
    assert R.count("La date d'échéance ne peut pas être antérieure à la date d'émission.") >= 2
    assert "date.fromisoformat(due_date) < date.fromisoformat(issue_date)" in R
    assert "date.fromisoformat(due_date) < date.fromisoformat(inv['issue_date'])" in R
