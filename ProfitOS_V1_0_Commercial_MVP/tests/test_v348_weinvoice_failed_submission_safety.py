from pathlib import Path


INV = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _send_block():
    start = INV.index("def invoicing_send_weinvoice(")
    end = INV.index("def invoicing_sync_weinvoice(", start)
    return INV[start:end]


def test_v348_failed_submission_keeps_invoice_retryable_without_fake_remote_id():
    block = _send_block()

    # Une facture n'est bloquée comme "déjà transmise" que si un vrai ID distant existe.
    assert "if 'weinvoice_invoice_id' in inv.keys() and inv['weinvoice_invoice_id']:" in block

    # L'appel fournisseur est protégé par la gestion des erreurs WeInvoice.
    assert "data = submit_invoice_file(" in block
    assert "except (WeInvoiceAPIError, WeInvoiceConfigError) as e:" in block

    error_block = block[
        block.index("except (WeInvoiceAPIError, WeInvoiceConfigError) as e:"):
        block.index("remote_id = data.get(", block.index("except (WeInvoiceAPIError, WeInvoiceConfigError) as e:"))
    ]

    # En cas d'échec, on conserve l'erreur et un événement d'audit.
    assert "weinvoice_last_error=?" in error_block
    assert "_record_einvoice_event(c,inv,'submission_failed'" in error_block
    assert "c.commit()" in error_block
    assert "return redirect(" in error_block

    # Surtout : aucun faux ID/statut distant n'est écrit dans le chemin d'échec.
    assert "weinvoice_invoice_id=?" not in error_block
    assert "weinvoice_status=?" not in error_block
    assert "'submitted'" not in error_block


def test_v348_remote_id_is_persisted_only_after_successful_provider_response():
    block = _send_block()
    remote = block.index("remote_id = data.get(")
    persist = block.index(
        "UPDATE outgoing_invoices SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?",
        remote,
    )
    submitted_event = block.index("_record_einvoice_event(c,inv,'submitted'", persist)

    assert remote < persist < submitted_event
    assert "if not remote_id:" in block[remote:persist]
