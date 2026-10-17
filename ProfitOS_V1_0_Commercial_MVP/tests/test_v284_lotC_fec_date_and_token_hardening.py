from pathlib import Path
T=Path("profitos/routes/accounting.py").read_text(encoding="utf-8")

def test_fec_export_validates_real_calendar_dates():
    block=T.split("def accounting_fec_export():",1)[1].split("\n    @app.",1)[0]
    assert "date.fromisoformat(date_from)" in block
    assert "date.fromisoformat(date_to)" in block
    assert 'error = "Les dates FEC sont invalides."' in block

def test_accountant_fec_link_dates_are_validated():
    block=T.split("def accounting_fec_send_accountant():",1)[1].split("\n    @app.",1)[0]
    assert "date.fromisoformat(date_from)" in block
    assert "parsed_date_from > parsed_date_to" in block

def test_public_fec_token_expires_after_seven_days():
    block=T.split("def accounting_fec_download(token):",1)[1].split("\n    @app.",1)[0]
    assert "datetime.fromisoformat" in block
    assert "timedelta(days=7)" in block
    assert "abort(410)" in block
    assert "Ce lien est valable 7 jours" in T
