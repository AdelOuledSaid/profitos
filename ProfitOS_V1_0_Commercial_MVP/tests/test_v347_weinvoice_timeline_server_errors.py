import pytest
import profitos.weinvoice as weinvoice


class _FakeServerErrorResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = '{"error":"temporary_provider_failure"}'

    def json(self):
        return {"error": "temporary_provider_failure"}


@pytest.mark.parametrize("status_code", [500, 503])
def test_v347_timeline_server_error_never_becomes_invoice_status(monkeypatch, status_code):
    """Une panne WeInvoice 500/503 doit rester une erreur, jamais un faux statut."""
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
        return _FakeServerErrorResponse(status_code)

    monkeypatch.setattr(weinvoice.requests, "get", fake_get)

    with pytest.raises(weinvoice.WeInvoiceAPIError) as exc:
        weinvoice.get_invoice_timeline(
            organization_id="org-test",
            e_invoicing_id="TEST-SERVER-ERROR",
            timeout=7,
        )

    message = str(exc.value)
    assert str(status_code) in message
    assert "temporary_provider_failure" in message
    assert captured["url"].endswith(
        "/v1/invoice-queries/TEST-SERVER-ERROR/timeline"
    )
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["X-Org-Id"] == "org-test"
    assert captured["timeout"] == 7
