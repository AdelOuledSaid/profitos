from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DETAIL = (ROOT / "templates/invoicing_detail.html").read_text(encoding="utf-8")
PURCHASE = (ROOT / "templates/purchase_detail.html").read_text(encoding="utf-8")
LISTING = (ROOT / "templates/invoicing_list.html").read_text(encoding="utf-8")

# v519 — Les commentaires « Compatibilité tests historiques » que ces contrôles neutralisaient
# n'existent plus : ils n'avaient été ajoutés aux gabarits que pour satisfaire d'anciens tests
# de libellés, eux-mêmes réécrits. Les assertions portent donc directement sur le gabarit.
def test_customer_ui_has_no_provider_brand_or_regulatory_code_column():
    assert "Transmettre à WeInvoice" not in DETAIL
    assert "<th>Code</th>" not in DETAIL
    assert "Transmettre la facture électronique" in DETAIL

def test_supplier_ui_uses_business_actions_without_numeric_codes():
    assert "Prendre en charge (204)" not in PURCHASE
    assert "Approuver (205)" not in PURCHASE
    assert "Refuser (210)" not in PURCHASE
    assert "Code motif" not in PURCHASE
    assert ">Accepter<" in PURCHASE
    assert ">Refuser<" in PURCHASE
    assert "<th>Code</th>" not in PURCHASE

def test_reporting_ui_is_customer_facing():
    assert "dernière synchronisation WeInvoice" not in LISTING
    assert "Déclarations électroniques" in LISTING
