from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = (ROOT / "templates" / "purchase_detail.html").read_text(encoding="utf-8")
W = (ROOT / "profitos" / "weinvoice.py").read_text(encoding="utf-8")


def test_status_labels_cover_real_weinvoice_buyer_lifecycle():
    expected = {
        "'PARTIALLY_APPROVED':'Partiellement acceptée'",
        "'APPROVED_PARTIALLY':'Partiellement acceptée'",
        "'DISPUTED':'En litige'",
        "'IN_DISPUTE':'En litige'",
        "'SUSPENDED':'Suspendue'",
        "'COMPLETED':'Complétée'",
        "'REFUSED':'Refusée'",
        "'PAYMENT_SENT':'Paiement transmis'",
    }
    for fragment in expected:
        assert fragment in T


def test_206_exposes_dispute_and_payment_but_not_refusal():
    # Real Sandbox: 206 -> 207 and 206 -> 211 are accepted.
    assert "lifecycle_stage in ('APPROVED_PARTIALLY','PARTIALLY_APPROVED')" in T
    assert (
        "lifecycle_stage in ('TAKEN_IN_CHARGE','APPROVED','DISPUTED') "
        "or lifecycle_stage == 'IN_DISPUTE' "
        "or lifecycle_stage in ('APPROVED_PARTIALLY','PARTIALLY_APPROVED')"
    ) in T

    # The dispute option explicitly includes both names observed/used for 206.
    assert (
        "lifecycle_stage in "
        "('TAKEN_IN_CHARGE','APPROVED','APPROVED_PARTIALLY','PARTIALLY_APPROVED')"
    ) in T
    assert '<option value="dispute">Mettre en litige</option>' in T

    # Real Sandbox: 206 -> 210 is rejected. Refusal must remain limited to
    # TAKEN_IN_CHARGE / DISPUTED / IN_DISPUTE, not partial approval.
    refusal_guard = (
        "{% if (lifecycle_stage in ('TAKEN_IN_CHARGE','DISPUTED') "
        "or lifecycle_stage == 'IN_DISPUTE') %}"
        '<option value="refuse">Refuser</option>'
    )
    assert refusal_guard in T


def test_208_is_buyer_wait_state():
    # SUSPENDED is labelled, but it is deliberately absent from all buyer
    # action guards. The seller must complete before buyer processing resumes.
    assert "'SUSPENDED':'Suspendue'" in T
    action_area = T[T.index("{% set lifecycle_stage="):]
    for guarded_action in (
        "take-in-charge",
        "approve",
        "approve-partially",
        "payment-sent",
        '<option value="dispute">',
        '<option value="refuse">',
    ):
        # SUSPENDED must not be added to any condition immediately governing
        # these actions.
        assert "('SUSPENDED'" not in action_area


def test_209_completed_allows_full_and_partial_approval():
    assert "'COMPLETED':'Complétée'" in T
    # Preserve the exact legacy expression because v331 also protects it.
    assert "lifecycle_stage in ('TAKEN_IN_CHARGE','DISPUTED','COMPLETED')" in T
    # The same guard wraps both approve and approve-partially forms.
    completed_guard = (
        "{% if (lifecycle_stage in ('TAKEN_IN_CHARGE','DISPUTED','COMPLETED') "
        "or lifecycle_stage == 'IN_DISPUTE') %}"
    )
    assert completed_guard in T
    after = T[T.index(completed_guard):]
    assert 'value="approve"' in after
    assert 'value="approve-partially"' in after


def test_210_and_211_have_labels_and_no_extra_terminal_transition_guard():
    assert "'REFUSED':'Refusée'" in T
    assert "'PAYMENT_SENT':'Paiement transmis'" in T

    # Neither terminal state is admitted by the buyer action conditions.
    lifecycle_part = T[T.index("{% set lifecycle_stage="):]
    for state in ("REFUSED", "PAYMENT_SENT"):
        # Status label occurs before lifecycle_stage; it must not occur in the
        # action section.
        assert state not in lifecycle_part


def test_backend_buyer_action_catalog_is_still_expected():
    expected = {
        "'take-in-charge':(204,'TAKEN_IN_CHARGE')",
        "'approve':(205,'APPROVED')",
        "'approve-partially':(206,'PARTIALLY_APPROVED')",
        "'dispute':(207,'DISPUTED')",
        "'suspend':(208,'SUSPENDED')",
        "'refuse':(210,'REFUSED')",
        "'payment-sent':(211,'PAYMENT_SENT')",
    }
    compact = W.replace(" ", "")
    for fragment in expected:
        assert fragment.replace(" ", "") in compact

    # complete(209) is seller-side and must not become a buyer action.
    start = compact.index("_BUYER_LIFECYCLE_ACTIONS={")
    end = compact.index("}", start)
    buyer_catalog = compact[start:end]
    assert "'complete'" not in buyer_catalog
