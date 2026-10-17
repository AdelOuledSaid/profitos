from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from profitos.routes.decision_simulator import _optimize_decision


def _cash(values):
    return {"curves":[{"mode":"probable","values":values,"minimum":min(values),"end_90":values[-1]}],"monthly_burn":1000}

def test_financed_minimum_respects_reserve():
    cash=_cash([35422.90]*91)
    o=_optimize_decision(cash,"investment",35211.45,reserve=10000,max_financing=9788.55,deadline=90)
    assert o["constraints_met"] is True
    assert o["best"]["minimum_before_financing"] < 10000
    assert o["best"]["minimum_after_financing"] >= 10000 - .01
    assert o["best"]["financing"] <= 9788.55 + .01

def test_plan_rejected_when_cap_cannot_preserve_reserve():
    cash=_cash([35422.90]*91)
    o=_optimize_decision(cash,"investment",40000,reserve=10000,max_financing=5000,deadline=90)
    assert o["constraints_met"] is False

