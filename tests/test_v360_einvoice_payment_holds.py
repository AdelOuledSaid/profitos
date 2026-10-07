"""Facturation électronique : un refus, un litige, une suspension ou un rejet retient le paiement
(achats, lot SEPA) et la relance (ventes), et un retour négatif du PPF reste visible sous le titre
sans jamais écraser le statut du cycle de vie."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
WEINVOICE = (ROOT / 'profitos/weinvoice.py').read_text(encoding='utf-8')
DETAIL = (ROOT / 'templates/invoicing_detail.html').read_text(encoding='utf-8')
PURCHASE = (ROOT / 'templates/purchase_detail.html').read_text(encoding='utf-8')
SEPA_TPL = (ROOT / 'templates/purchase_sepa_batch.html').read_text(encoding='utf-8')
RECEIVABLES = (ROOT / 'templates/invoicing_receivables.html').read_text(encoding='utf-8')


def _hold(row):
    from profitos.routes.invoicing import _einvoice_hold
    return _einvoice_hold(row)


def test_adverse_statuses_hold_payment_and_reminders():
    assert _hold({'weinvoice_status': 'REFUSED'}) == 'refusée'
    assert _hold({'weinvoice_status': 'refused'}) == 'refusée'
    assert _hold({'weinvoice_status': 'IN_DISPUTE'}) == 'en litige'
    assert _hold({'weinvoice_status': 'DISPUTED'}) == 'en litige'
    assert _hold({'weinvoice_status': 'SUSPENDED'}) == 'suspendue'
    assert _hold({'weinvoice_status': 'REJECTED'}) == 'rejetée'


def test_normal_statuses_are_never_held():
    for status in ('APPROVED', 'APPROVED_PARTIALLY', 'MADE_AVAILABLE', 'RECEIVED', 'PAYMENT_SENT', '', None):
        assert _hold({'weinvoice_status': status}) is None
    assert _hold({}) is None


def test_textual_status_wins_over_a_stale_regulatory_code():
    assert _hold({'weinvoice_status': 'APPROVED', 'weinvoice_regulatory_code': '210'}) is None


def test_regulatory_code_is_only_a_fallback_when_status_is_missing():
    assert _hold({'weinvoice_status': None, 'weinvoice_regulatory_code': 208}) == 'suspendue'
    assert _hold({'weinvoice_status': '', 'weinvoice_regulatory_code': '210'}) == 'refusée'
    assert _hold({'weinvoice_status': None, 'weinvoice_regulatory_code': '205'}) is None


def test_sepa_batch_never_exports_held_invoices():
    assert "held_rows = [(r, _einvoice_hold(r)) for r in rows if _einvoice_hold(r)]" in INV
    assert "rows = [r for r in rows if not _einvoice_hold(r)]" in INV
    assert "ready, blocked, held = [], [], []" in INV
    assert "held=held," in INV
    assert "Retenues — statut de facturation électronique" in SEPA_TPL


def test_reminders_are_blocked_for_held_invoices():
    assert "hold=_einvoice_hold(inv)" in INV
    assert "côté facturation électronique : régularise-la avant de relancer le client." in INV


def test_held_invoices_stay_visible_in_receivables():
    # Volontairement signalées, jamais retirées du total : la somme n'est pas régularisée pour autant.
    assert "'einvoice_hold':_einvoice_hold(r)," in INV
    assert "relance suspendue" in RECEIVABLES


def test_purchase_and_sales_detail_pages_receive_the_hold():
    assert "payment_hold=_einvoice_hold(p)" in INV
    assert "einvoice_hold=_einvoice_hold(inv)," in INV
    assert "exclue des virements SEPA groupés" in PURCHASE


def test_headline_is_red_for_held_statuses():
    assert "'purchase-alert-text' if (wi_rejected or einvoice_hold) else 'purchase-ok-text'" in DETAIL
    assert "Le client a refusé cette facture" in DETAIL


def test_ppf_acknowledgement_is_shown_separately_from_the_lifecycle_status():
    assert "Données réglementaires (PPF)" in DETAIL
    assert "ACK_251_REJECTED" in DETAIL and "ACK_501_INADMISSIBLE" in DETAIL
    # Garde-fou de conception : l'indicateur PPF reste distinct, le cycle de vie n'est jamais réécrit.
    assert "Un acquittement PPF est distinct du cycle de vie" in WEINVOICE
