from pathlib import Path

SRC = Path('profitos/weinvoice.py').read_text(encoding='utf-8')


def test_v344_supports_inadmissible_and_all_regulatory_events():
    assert "'invoice.regulatory.inadmissible': ('ACK_501_INADMISSIBLE', '501')" in SRC
    assert "is_regulatory_event = event_name.startswith('invoice.regulatory.')" in SRC


def test_v344_uses_latest_acquittement_as_source_of_truth():
    assert "timeline.get('latestAcquittement')" in SRC
    assert "'500': ('ACK_500_RECEVABLE', '500')" in SRC
    assert "'501': ('ACK_501_INADMISSIBLE', '501')" in SRC
    assert "'250': ('ACK_250_ACCEPTED', '250')" in SRC
    assert "'251': ('ACK_251_REJECTED', '251')" in SRC


def test_v344_transmitted_without_ack_does_not_create_fake_history():
    assert 'if not regulatory_ack:' in SRC
    assert "WEINVOICE_PPF_TIMELINE_LOOKUP_FAILED" in SRC


def test_v344_preserves_ppf_ack_semantic_deduplication():
    assert "AND status=? LIMIT 1" in SRC
    assert "WEINVOICE_PPF_ACK_DUPLICATE" in SRC
