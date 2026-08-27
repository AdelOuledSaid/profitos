from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from profitos.routes.decision_simulator import _optimize_decision


def test_optimizer_checks_each_day_and_keeps_first_feasible_date():
    # Baseline rises by 1 EUR/day. With a 103 EUR purchase and a 5 EUR reserve,
    # the first feasible payment day is exactly J+8: 100 + 8 - 103 = 5.
    vals=[100.0 + d for d in range(91)]
    cash={
        'curves':[{'mode':'probable','values':vals,'minimum':min(vals),'end_90':vals[-1]}],
        'monthly_burn':0.0,
    }
    o=_optimize_decision(
        cash,'investment',103.0,decision_day=0,reserve=5.0,
        max_financing=0.0,deadline=10,allow_delay=True,max_delay=10,
        allow_installments=False,max_installments=1,allow_financing=False,
    )
    assert o['constraints_met'] is True
    assert o['best']['decision_day'] == 8
    assert o['best']['financing'] == 0.0
    assert o['best']['minimum'] == 5.0
