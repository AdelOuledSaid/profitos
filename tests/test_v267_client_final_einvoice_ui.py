from pathlib import Path
T=Path("templates/invoicing_detail.html").read_text(encoding="utf-8")

def test_customer_ui_hides_sandbox_controls():
    assert "Simuler le statut Sandbox" not in T
    assert "sandbox_status" not in T
    assert "/v1/_sandbox/einvoicing/" not in T
    assert "Actualiser le statut" in T
    assert "Historique de facturation électronique" in T
    assert "Historique de transmission" in T
