from pathlib import Path
T=Path("profitos/routes/api.py").read_text(encoding="utf-8")

def _block(name, next_name):
    return T.split("def "+name+"():",1)[1].split("def "+next_name+"():",1)[0]

def test_purchase_api_rejects_nonfinite_and_negative_amounts():
    b=_block("api_create_purchase_invoice","api_create_expense_report")
    assert "math.isfinite(subtotal)" in b
    assert "math.isfinite(vat_amount)" in b
    assert "subtotal < 0" in b and "vat_amount < 0" in b

def test_expense_api_rejects_nonfinite_amount():
    b=_block("api_create_expense_report","api_create_invoice")
    assert "not math.isfinite(amount) or amount <= 0" in b

def test_invoice_api_rejects_nonfinite_or_invalid_line_numbers():
    b=T.split("def api_create_invoice():",1)[1].split("# ------------------------------------------------------------------",1)[0]
    assert "not math.isfinite(qty)" in b
    assert "not math.isfinite(price)" in b
    assert "not math.isfinite(vat_rate)" in b
    assert "qty <= 0 or price < 0 or vat_rate < 0" in b
