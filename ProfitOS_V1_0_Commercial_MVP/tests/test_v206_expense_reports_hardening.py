from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
ROUTES=(ROOT/"profitos/routes/expenses.py").read_text(encoding="utf-8")
EXP=(ROOT/"profitos/expenses.py").read_text(encoding="utf-8")

def test_v206_expense_reports_are_entity_scoped():
    assert "entity_id INTEGER" in RUNTIME
    assert "('expense_reports','entity_id')" in RUNTIME
    assert "INSERT INTO expense_reports(employee_email,period_label,status,entity_id,created_at)" in ROUTES
    assert "expense_reports WHERE entity_id IS ?" in ROUTES
    assert "id=? AND entity_id IS ?" in ROUTES

def test_v206_employee_cannot_mutate_another_users_report():
    assert "report['employee_email'] != current_user()['email']" in ROUTES
    assert "SELECT status,employee_email FROM expense_reports" in ROUTES

def test_v206_approval_is_atomic_with_accounting():
    block=ROUTES[ROUTES.index("def expense_report_approve"):ROUTES.index("def expense_report_reject")]
    assert "generate_expense_report_entry" in block
    assert "c.commit()" in block
    assert "c.rollback()" in block
    assert block.index("generate_expense_report_entry") < block.index("c.commit()")

def test_v206_reimbursement_is_atomic_with_accounting():
    block=ROUTES[ROUTES.index("def expense_report_reimburse"):ROUTES.index("def mileage_rate_settings")]
    assert "generate_expense_reimbursement_entry" in block
    assert "c.rollback()" in block
    assert block.index("generate_expense_reimbursement_entry") < block.index("c.commit()")

def test_v206_expense_accounting_is_entity_scoped_and_idempotent():
    assert "AND entity_id IS ? LIMIT 1" in EXP
    assert "report['entity_id']" in EXP
    assert "entity_id=report['entity_id']" in EXP

def test_v206_existing_receipt_ocr_workflow_is_preserved():
    assert "_receipt_pdf_extract" in ROUTES
    assert "_receipt_ai_extract" in ROUTES
    assert "application/pdf" in ROUTES
    assert "image/jpeg" in ROUTES and "image/png" in ROUTES
