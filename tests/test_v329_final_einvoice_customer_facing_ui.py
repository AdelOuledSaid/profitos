from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DETAIL=(ROOT/'templates/invoicing_detail.html').read_text(encoding='utf-8')
CREDIT=(ROOT/'templates/invoicing_credit_detail.html').read_text(encoding='utf-8')
LISTING=(ROOT/'templates/invoicing_list.html').read_text(encoding='utf-8')
COMPANY=(ROOT/'templates/company.html').read_text(encoding='utf-8')
AGREEMENT=(ROOT/'templates/weinvoice_agreement.html').read_text(encoding='utf-8')

def test_history_translates_provider_statuses():
    assert "status_labels.get((ev.status or '')|upper" in DETAIL
    assert "ev.status or ('Erreur'" not in DETAIL

def test_credit_and_reporting_do_not_render_raw_regulatory_values():
    assert "credit.weinvoice_regulatory_code }}" not in CREDIT
    assert "{{ remote_status }}" not in LISTING

def test_setup_pages_are_customer_facing():
    for t in (COMPANY, AGREEMENT):
        assert '>WeInvoice<' not in t
    assert 'Signer et continuer' in AGREEMENT
