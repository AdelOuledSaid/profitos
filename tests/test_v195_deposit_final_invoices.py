from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
ACC=(ROOT/'profitos/accounting.py').read_text(encoding='utf-8')
RUN=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/invoicing_quote_detail.html').read_text(encoding='utf-8')

def test_invoice_schema_has_deposit_metadata():
    for token in ('invoice_kind','source_quote_id','deposit_percent','deposit_applied_subtotal','deposit_applied_vat'):
        assert token in RUN

def test_deposit_accounting_uses_4191_and_is_entity_idempotent():
    assert "('419100', 'Clients - Avances et acomptes reçus sur commandes'" in ACC
    block=ACC[ACC.index('def generate_customer_deposit_entry'):ACC.index('def generate_customer_final_entry')]
    assert "'419100'" in block and "'customer_deposit_invoice'" in block and 'entity_id=entity_id' in block
    assert "'706000'" not in block

def test_final_accounting_releases_deposit_and_recognizes_revenue_once():
    block=ACC[ACC.index('def generate_customer_final_entry'):ACC.index('def generate_sale_credit_entry')]
    assert "'419100','debit'" in block
    assert "'706000','credit'" in block
    assert "'customer_final_invoice'" in block

def test_quote_routes_enforce_entity_and_no_duplicate_final():
    assert "def invoicing_quote_deposit" in INV
    assert "def invoicing_quote_final_invoice" in INV
    assert "source_quote_id=? AND entity_id IS ? AND invoice_kind='final'" in INV
    assert "Le cumul des acomptes dépasserait le montant du devis" in INV

def test_final_invoice_contains_explicit_deposit_deductions():
    assert "Déduction acomptes déjà facturés" in INV
    assert "deposit_by_rate" in INV
    assert "json.dumps(final_items,ensure_ascii=False)" in INV

def test_send_dispatches_special_accounting_generators():
    assert "invoice_kind=='deposit'" in INV and 'generate_customer_deposit_entry(c,inv_updated)' in INV
    assert "invoice_kind=='final'" in INV and 'generate_customer_final_entry(c,inv_updated)' in INV

def test_quote_ui_exposes_deposit_and_final_actions():
    assert "invoicing_quote_deposit" in TPL
    assert "invoicing_quote_final_invoice" in TPL
    assert 'deposit_percent' in TPL

def test_final_invoice_refuses_draft_deposits():
    assert "invoice_kind='deposit' AND status='draft'" in INV
    assert "Émettez ou annulez les factures d'acompte en brouillon" in INV
