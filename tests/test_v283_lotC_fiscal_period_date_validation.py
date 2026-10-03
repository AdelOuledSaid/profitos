from pathlib import Path
T = Path("profitos/routes/accounting.py").read_text(encoding="utf-8")

def test_vat_period_uses_real_iso_calendar_validation():
    block=T.split("def vat_summary():",1)[1].split("\n    @app.",1)[0]
    assert "parsed_date_from = date.fromisoformat(date_from)" in block
    assert "parsed_date_to = date.fromisoformat(date_to)" in block
    assert "parsed_date_from > parsed_date_to" in block

def test_fiscal_workpaper_period_uses_real_iso_calendar_validation():
    block=T.split("def fiscal_workpapers():",1)[1].split("\n    @app.",1)[0]
    assert "parsed_date_from = date.fromisoformat(date_from)" in block
    assert "parsed_date_to = date.fromisoformat(date_to)" in block
    assert "parsed_date_from > parsed_date_to" in block
