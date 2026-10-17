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
    # v421 : contrôle commun à la création et à la modification (_parse_invoice_header).
    assert "La date d'échéance ne peut pas être antérieure à la date d'émission." in R
    assert "v['due_date'] < str(issue_date)[:10]" in R
    assert R.count("_parse_invoice_header(request.form,paris_today().isoformat())") >= 2
