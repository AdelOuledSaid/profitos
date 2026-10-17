from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profitos.routes.decision_simulator import (
    _inflow_is_available,
    _simulate_decision,
    _simulate_strategy,
    _optimize_decision,
)


def _cash(value=100.0):
    vals = [float(value)] * 91
    return {
        'curves': [{'mode': 'probable', 'values': vals, 'minimum': min(vals), 'end_90': vals[-1]}],
        'monthly_burn': 0.0,
    }


def test_inflow_is_not_available_on_its_announced_day():
    assert _inflow_is_available(14, 15) is False
    assert _inflow_is_available(15, 15) is False
    assert _inflow_is_available(16, 15) is True


def test_same_day_inflow_does_not_fund_same_day_payment_strategy():
    r = _simulate_strategy(
        _cash(100), 'investment', 100,
        decision_day=15, expected_inflow=50, inflow_day=15, installments=1,
    )
    # At J+15: 100 - 100 = 0. The +50 becomes available only at J+16.
    assert r['values'][15] == 0.0
    assert r['values'][16] == 50.0
    assert r['minimum'] == 0.0
    assert r['min_day'] == 15


def test_public_decision_simulation_uses_same_conservative_rule():
    r = _simulate_decision(
        _cash(100), 'investment', 100,
        decision_day=15, expected_inflow=50, inflow_day=15,
    )
    assert r['values'][15] == 0.0
    assert r['values'][16] == 50.0
    assert r['minimum'] == 0.0


def test_optimizer_boundary_moves_from_j15_to_j16():
    kwargs = dict(
        cash=_cash(100), kind='investment', amount=100,
        decision_day=0, expected_inflow=50, inflow_day=15,
        reserve=50, max_financing=0, deadline=20,
        allow_delay=True, allow_installments=False, max_installments=1,
        allow_financing=False,
    )

    at_15 = _optimize_decision(max_delay=15, **kwargs)
    assert at_15['constraints_met'] is False

    at_16 = _optimize_decision(max_delay=16, **kwargs)
    assert at_16['constraints_met'] is True
    assert at_16['best']['decision_day'] == 16
    assert at_16['best']['minimum'] == 50.0


def test_template_explains_next_day_availability():
    t = (ROOT / 'templates' / 'decision_simulator.html').read_text(encoding='utf-8')
    assert 'disponible à partir de J+{{ simulation.inflow_day + 1 }}' in t
    assert "un encaissement prévu à J+n est considéré disponible à partir de J+(n+1)" in t
