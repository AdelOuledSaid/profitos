from pathlib import Path

SRC = Path('profitos/weinvoice.py').read_text(encoding='utf-8')


def test_regulatory_accepted_webhook_is_supported():
    assert "'invoice.regulatory.accepted': ('ACK_250_ACCEPTED', '250')" in SRC
    assert "'invoice.regulatory.rejected': ('ACK_251_REJECTED', '251')" in SRC
    assert "not event_name.startswith('invoice.status.') and not regulatory_ack" in SRC


def test_ppf_ack_does_not_replace_business_status():
    block = SRC.split('if regulatory_ack:', 1)[1].split("is_rejected=(event_name == 'invoice.status.rejected')", 1)[0]
    assert 'SET weinvoice_status=' not in block
    assert "SET weinvoice_last_sync_at=?" in block
    assert "'acquittement'" in block


def test_ppf_ack_has_semantic_deduplication():
    block = SRC.split('if regulatory_ack:', 1)[1].split("is_rejected=(event_name == 'invoice.status.rejected')", 1)[0]
    assert 'existing_ack' in block
    assert "AND status=? LIMIT 1" in block
    assert 'WEINVOICE_PPF_ACK_DUPLICATE' in block
