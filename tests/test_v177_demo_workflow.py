from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_demo_workflow_has_admin_and_customer_emails():
    t=(ROOT/'profitos/routes/main.py').read_text(encoding='utf-8')
    assert "email_demo_admin.html" in t
    assert "email_demo_confirmation.html" in t
    assert "DEMO_REPLY_EMAIL" in t
    assert "reply_to=email" in t
    assert "reply_to=reply_email" in t

def test_send_email_supports_reply_to():
    t=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert 'def send_email(to_email, subject, html, dry_run=None, reply_to=None):' in t
    assert "params['reply_to'] = reply_to" in t
    assert "msg['Reply-To']=reply_to" in t

def test_demo_confirmation_is_professional():
    t=(ROOT/'templates/demo_thanks.html').read_text(encoding='utf-8')
    assert 'sous 1 jour ouvré' in t
    assert "email de confirmation" in t
