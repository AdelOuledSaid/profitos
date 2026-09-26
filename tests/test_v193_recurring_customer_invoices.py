from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
LIST=(ROOT/'templates/invoicing_list.html').read_text(encoding='utf-8')
TPL=(ROOT/'templates/recurring_invoices.html').read_text(encoding='utf-8')

def test_recurring_schema_and_unique_cycle_guard_exist():
    assert 'CREATE TABLE IF NOT EXISTS recurring_invoice_templates' in RUNTIME
    assert 'CREATE TABLE IF NOT EXISTS recurring_invoice_runs' in RUNTIME
    assert 'UNIQUE(template_id,entity_key,run_date)' in RUNTIME
    assert 'ux_outgoing_invoice_recurring_run' in RUNTIME

def test_generator_is_entity_scoped_and_creates_drafts():
    block=INV[INV.index('def _generate_due_recurring_invoices'):INV.index('def register(app):')]
    assert "WHERE entity_id IS ? AND status='active'" in block
    assert "'draft'" in block
    assert 'recurring_template_id' in block and 'recurring_run_date' in block
    assert 'recurring_invoice_runs' in block

def test_recurring_routes_are_paid_plan_and_entity_scoped():
    assert "@app.route('/facturation/recurrentes',methods=['GET','POST'])" in INV
    assert "@app.route('/facturation/recurrentes/generer',methods=['POST'])" in INV
    assert "@app.route('/facturation/recurrentes/<int:template_id>/statut',methods=['POST'])" in INV
    assert INV.count('@requires_paid_plan') >= 3
    assert "WHERE id=? AND entity_id IS ?" in INV

def test_pause_resume_stop_and_due_generation_are_supported():
    assert "{'pause':'paused','resume':'active','stop':'stopped'}" in INV
    assert "frequency not in ('weekly','monthly','quarterly','yearly')" in INV
    assert "next_run_date<=?" in INV
    assert "status='completed'" in INV

def test_ui_exposes_recurring_invoices():
    assert "url_for('recurring_invoices')" in LIST
    assert 'Facturation récurrente' in TPL
    assert "url_for('recurring_invoices_generate')" in TPL
