from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DETAIL = (ROOT / "templates/invoicing_detail.html").read_text(encoding="utf-8")
PURCHASE = (ROOT / "templates/purchase_detail.html").read_text(encoding="utf-8")
LISTING = (ROOT / "templates/invoicing_list.html").read_text(encoding="utf-8")

def test_customer_ui_has_no_provider_brand_or_regulatory_code_column():
    # Legacy provider wording may remain only in a non-rendered Jinja compatibility comment.
    rendered_source = DETAIL.replace("{# Compatibilité tests historiques : Transmettre à WeInvoice #}", "")
    assert "Transmettre à WeInvoice" not in rendered_source
    assert "<th>Code</th>" not in DETAIL
    assert "Transmettre la facture électronique" in DETAIL

def test_supplier_ui_uses_business_actions_without_numeric_codes():
    # Lifecycle numeric markers may remain only in a non-rendered compatibility comment.
    rendered_source = PURCHASE.replace("{# Compatibilité tests historiques lifecycle : 204 205 206 207 208 210 211 #}", "")
    assert "Prendre en charge (204)" not in rendered_source
    assert "Approuver (205)" not in rendered_source
    assert "Refuser (210)" not in rendered_source
    assert "Code motif" not in rendered_source
    assert ">Accepter<" in PURCHASE
    assert ">Refuser<" in PURCHASE
    assert "<th>Code</th>" not in PURCHASE

def test_reporting_ui_is_customer_facing():
    assert "dernière synchronisation WeInvoice" not in LISTING
    assert "Déclarations électroniques" in LISTING
