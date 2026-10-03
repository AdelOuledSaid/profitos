from pathlib import Path
T=Path("profitos/routes/ecommerce.py").read_text(encoding="utf-8")

def test_v296_preserves_v291_entity_scope_and_v295_domain_validation():
    b=T.split("def ecommerce_sync(platform):",1)[1]
    assert "WHERE platform=? AND entity_id IS ?" in b
    assert "SELECT COUNT(*) n FROM outgoing_invoices WHERE entity_id IS ?" in b
    connect=T.split("def ecommerce_connect(platform):",1)[1].split("\n    @app.",1)[0]
    assert "shop_domain = normalize_shop_domain(shop_domain, platform)" in connect
