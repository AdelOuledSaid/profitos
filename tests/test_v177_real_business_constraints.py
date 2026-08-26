from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from profitos.routes.decision_simulator import _optimize_decision


def _cash(value=20000.0):
    vals=[value]*91
    return {'curves':[{'mode':'probable','values':vals,'minimum':value,'end_90':value}], 'monthly_burn':1000}


def test_impossible_investment_stays_impossible_when_no_levers_allowed():
    o=_optimize_decision(
        _cash(), 'investment', 100000, reserve=5000, max_financing=10000, deadline=90,
        allow_delay=False, max_delay=0, allow_installments=False, max_installments=1,
        allow_financing=True)
    assert o['constraints_met'] is False
    assert o['best']['decision_day'] == 0
    assert o['best']['installments'] == 1
    assert o['best']['financing'] > 10000
    assert 'Aucune solution soutenable trouvée' in o['explanation']


def test_optimizer_never_uses_unapproved_delay_or_installments():
    o=_optimize_decision(
        _cash(60000), 'investment', 50000, reserve=5000, max_financing=0, deadline=90,
        allow_delay=False, max_delay=0, allow_installments=False, max_installments=1,
        allow_financing=False)
    assert o['constraints_met'] is True
    assert o['best']['decision_day'] == 0
    assert o['best']['installments'] == 1


def test_authorized_installments_can_make_plan_feasible():
    # Flat baseline: paying 40k once from 20k is impossible, but 2 installments
    # are still impossible within a flat baseline. Use a rising known cash curve.
    vals=[26000 + 1000*d for d in range(91)]
    cash={'curves':[{'mode':'probable','values':vals,'minimum':min(vals),'end_90':vals[-1]}], 'monthly_burn':1000}
    o=_optimize_decision(
        cash, 'investment', 40000, reserve=5000, max_financing=0, deadline=90,
        allow_delay=False, max_delay=0, allow_installments=True, max_installments=2,
        allow_financing=False)
    assert o['constraints_met'] is True
    assert o['best']['installments'] == 2
    assert o['requires_negotiation'] is True


def test_hire_never_gets_installment_language_from_engine():
    o=_optimize_decision(
        _cash(30000), 'hire', 0, monthly_cost=3000, reserve=5000, max_financing=0,
        allow_delay=True, max_delay=30, allow_installments=True, max_installments=4,
        allow_financing=False)
    assert all(c['installments'] == 1 for c in o['alternatives'])
    assert 'payer en' not in o['label']


def test_v177_ui_has_explicit_real_constraints():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    for text in [
        'AI CFO · EXPLAINABLE PLANNER · V1.7.7',
        'CONTRAINTES RÉELLES',
        'Report négociable',
        'Paiement fractionnable',
        'Financement externe autorisé',
        'Aucune solution soutenable trouvée.',
        'À confirmer avec le fournisseur ou le partenaire.',
    ]:
        assert text in t
