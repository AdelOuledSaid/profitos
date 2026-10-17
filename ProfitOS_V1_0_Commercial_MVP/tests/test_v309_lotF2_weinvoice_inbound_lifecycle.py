from pathlib import Path
from unittest.mock import Mock, patch
import pytest
import profitos.weinvoice as w


def test_buyer_lifecycle_endpoints_match_openapi():
    assert w._BUYER_LIFECYCLE_ACTIONS == {
        'take-in-charge': (204,'TAKEN_IN_CHARGE'), 'approve': (205,'APPROVED'),
        'approve-partially': (206,'PARTIALLY_APPROVED'), 'dispute': (207,'DISPUTED'),
        'suspend': (208,'SUSPENDED'), 'refuse': (210,'REFUSED'), 'payment-sent': (211,'PAYMENT_SENT')}


def test_refuse_requires_reason_code():
    with pytest.raises(w.WeInvoiceConfigError):
        w.apply_inbound_lifecycle_action('org','inv','refuse')


def test_buyer_action_calls_documented_endpoint():
    resp=Mock(status_code=200,content=b'{}'); resp.json.return_value={}
    with patch.object(w,'fetch_access_token',return_value='tok'), patch.object(w.requests,'post',return_value=resp) as post:
        out=w.apply_inbound_lifecycle_action('org-1','inv-1','approve')
    assert post.call_args.args[0].endswith('/v1/invoice-lifecycle/inv-1/approve')
    assert post.call_args.kwargs['headers']['X-Org-Id']=='org-1'
    assert out['regulatoryStatusCode']==205


def test_dispute_sends_reason_payload():
    resp=Mock(status_code=202,content=b'{}'); resp.json.return_value={}
    with patch.object(w,'fetch_access_token',return_value='tok'), patch.object(w.requests,'post',return_value=resp) as post:
        w.apply_inbound_lifecycle_action('org','inv','dispute',reason_code='OTHER',reason_label='Écart de montant')
    assert post.call_args.kwargs['json']=={'reasonCode':'OTHER','reasonLabel':'Écart de montant'}


def test_purchase_routes_are_entity_scoped_and_ui_exposes_lifecycle():
    root=Path(__file__).resolve().parents[1]
    route=(root/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
    tpl=(root/'templates/purchase_detail.html').read_text(encoding='utf-8')
    assert "WHERE id=? AND entity_id IS ?" in route
    assert 'purchase_weinvoice_action' in route and 'purchase_weinvoice_status_sync' in route
    for code in ('204','205','206','207','208','210','211'):
        assert code in tpl


def test_webhook_lookup_supports_purchase_invoice():
    src=Path(w.__file__).read_text(encoding='utf-8')
    assert "'purchase' AS invoice_kind FROM purchase_invoices" in src
    assert "purchase_webhook_status" in src
