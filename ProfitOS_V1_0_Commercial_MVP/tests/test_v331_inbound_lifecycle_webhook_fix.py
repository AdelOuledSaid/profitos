from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
W=(ROOT/"profitos/weinvoice.py").read_text(encoding="utf-8")
R=(ROOT/"profitos/routes/invoicing.py").read_text(encoding="utf-8")
T=(ROOT/"templates/purchase_detail.html").read_text(encoding="utf-8")
def test_purchase_webhook_event_never_uses_null_invoice_id():
 b=W[W.index("purchase_webhook_status")-300:W.index("purchase_webhook_status")+500]; assert "VALUES(? ,0,'weinvoice','purchase_webhook_status'" in b; assert "VALUES(? ,NULL,'weinvoice','purchase_webhook_status'" not in b
def test_purchase_buyer_action_event_never_uses_null_invoice_id():
 b=R[R.index("purchase_buyer_action")-300:R.index("purchase_buyer_action")+500]; assert "VALUES(?,0,'weinvoice','purchase_buyer_action'" in b; assert "VALUES(?,NULL,'weinvoice','purchase_buyer_action'" not in b
def test_actions_are_lifecycle_stage_aware():
 assert "lifecycle_stage in ('RECEIVED','MADE_AVAILABLE')" in T; assert "lifecycle_stage in ('TAKEN_IN_CHARGE','DISPUTED','COMPLETED')" in T; assert "lifecycle_stage in ('TAKEN_IN_CHARGE','APPROVED','DISPUTED')" in T
