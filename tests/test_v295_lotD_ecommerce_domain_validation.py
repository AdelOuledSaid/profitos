import pytest
from profitos.ecommerce import normalize_shop_domain

def test_shopify_domain_is_normalized_and_restricted():
    assert normalize_shop_domain(" Store.MYSHOPIFY.com. ", "shopify") == "store.myshopify.com"
    with pytest.raises(ValueError):
        normalize_shop_domain("example.com", "shopify")

@pytest.mark.parametrize("host", [
    "https://shop.example.com",
    "shop.example.com/path",
    "user@shop.example.com",
    "127.0.0.1",
    "localhost",
    "shop.example.com:8443",
])
def test_ecommerce_domain_rejects_non_hostname_inputs(host):
    with pytest.raises(ValueError):
        normalize_shop_domain(host, "woocommerce")

def test_woocommerce_accepts_normal_public_hostname_shape():
    assert normalize_shop_domain("Boutique.Example.FR", "woocommerce") == "boutique.example.fr"
