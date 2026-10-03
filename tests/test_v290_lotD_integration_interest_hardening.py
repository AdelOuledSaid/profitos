from pathlib import Path
T=Path("profitos/routes/account.py").read_text(encoding="utf-8")

def _block():
    return T.split("def integrations():",1)[1].split("\n    @app.",1)[0]

def test_integration_interest_backend_rejects_unknown_provider():
    b=_block()
    assert "allowed_providers={'Pennylane','QuickBooks','Sage'}" in b
    assert "if provider not in allowed_providers:" in b
    assert "abort(400)" in b

def test_integration_interest_is_idempotent_per_organization_and_provider():
    b=_block()
    assert "SELECT 1 FROM integration_interest WHERE organization_id=? AND provider=?" in b
    assert "if not existing:" in b

def test_integration_interest_does_not_accept_spoofed_email_from_form():
    b=_block()
    assert "email=current_user()['email']" in b
    assert "request.form.get('email'" not in b
