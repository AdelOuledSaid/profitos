import pytest
import requests
import profitos.weinvoice as weinvoice


def test_v346_timeline_timeout_is_controlled_and_never_returns_fake_status(monkeypatch):
    """Un timeout WeInvoice ne doit jamais être interprété comme un statut facture."""
    monkeypatch.setattr(
        weinvoice,
        "fetch_access_token",
        lambda credential_set="management": "test-token",
    )

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        raise requests.Timeout("simulated WeInvoice timeout")

    monkeypatch.setattr(weinvoice.requests, "get", fake_get)

    with pytest.raises(Exception) as exc:
        weinvoice.get_invoice_timeline(
            organization_id="org-test",
            e_invoicing_id="TEST-TIMEOUT",
            timeout=7,
        )

    message = str(exc.value).lower()
    assert "timeout" in message or "timed out" in message
    assert captured["url"].endswith("/v1/invoice-queries/TEST-TIMEOUT/timeline")
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["X-Org-Id"] == "org-test"
    assert captured["timeout"] == 7
