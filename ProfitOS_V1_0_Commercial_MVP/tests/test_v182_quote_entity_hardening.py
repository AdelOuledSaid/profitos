from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV = (ROOT / 'profitos' / 'routes' / 'invoicing.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos' / 'runtime.py').read_text(encoding='utf-8')


def test_quotes_are_migrated_to_entity_scope():
    assert "('outgoing_quotes','entity_id')" in RUNTIME


def test_quote_routes_scope_reads_and_writes_by_entity():
    assert "SELECT * FROM outgoing_quotes WHERE id=? AND entity_id IS ?" in INV
    assert "UPDATE outgoing_quotes SET status='sent',sent_at=? WHERE id=? AND entity_id IS ?" in INV
    assert "UPDATE outgoing_quotes SET status='accepted',accepted_at=? WHERE id=? AND entity_id IS ?" in INV
    assert "UPDATE outgoing_quotes SET status='refused',refused_at=? WHERE id=? AND entity_id IS ?" in INV


def test_quote_conversion_propagates_entity_to_invoice():
    # v421 : brouillon sans numéro (attribué à l'émission), entité et champs du devis repris.
    block=INV[INV.index('def invoicing_quote_convert'):INV.index("@app.route('/facturation/devis/<int:quote_id>/pdf')")]
    assert "base=_quote_invoice_base(q,eid)" in block
    assert "_insert_draft_invoice(c,base)" in block
    assert "'entity_id':eid" in INV


def test_quote_numbering_is_separate_for_sub_entities():
    assert "def _next_quote_number(c, entity_id=None):" in INV
    assert 'f"DEV-E{entity_id}-{year}-"' in INV


def test_quote_pdf_uses_document_entity_identity():
    assert "company_row=_seller_identity(c,eid) if q else None" in INV
    assert "company_row=_seller_identity(tc,q['entity_id'])" in INV


def test_explicit_invoice_entity_checks_user_authorization():
    assert "user_can_access_entity(c, session.get('user_id'), entity_id)" in INV
    assert "abort(403)" in INV
