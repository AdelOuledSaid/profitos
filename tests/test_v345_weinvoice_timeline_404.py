import pytest

import profitos.weinvoice as weinvoice


class _Fake404Response:
    status_code = 404
    text = '{"error":"einvoicing_not_found"}'

    def json(self):
        return {"error": "einvoicing_not_found"}


def test_v345_timeline_404_is_raised_cleanly_without_fabricating_status(monkeypatch):
    """Un 404 WeInvoice doit rester une erreur de synchronisation explicite.

    Le helper timeline est en lecture seule : il ne doit ni fabriquer un statut,
    ni transformer le 404 en succès silencieux.
    """
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
        return _Fake404Response()

    monkeypatch.setattr(weinvoice.requests, "get", fake_get)

    with pytest.raises(weinvoice.WeInvoiceAPIError) as exc:
        weinvoice.get_invoice_timeline(
            organization_id="org-test",
            e_invoicing_id="00000000-0000-0000-0000-000000000000",
            timeout=7,
        )

    message = str(exc.value)
    assert "404" in message
    assert "einvoicing_not_found" in message
    assert captured["url"].endswith(
        "/v1/invoice-queries/00000000-0000-0000-0000-000000000000/timeline"
    )
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["X-Org-Id"] == "org-test"
    assert captured["timeout"] == 7
