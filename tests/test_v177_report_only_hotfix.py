from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from profitos.routes.decision_simulator import _optimize_decision


def test_report_only_can_find_later_feasible_date_without_financing_or_split():
    vals=[20000 + 2000*d for d in range(91)]
    cash={"curves":[{"mode":"probable","values":vals,"minimum":min(vals),"end_90":vals[-1]}],"monthly_burn":1000}
    o=_optimize_decision(cash,"investment",100000,decision_day=0,reserve=5000,max_financing=None,deadline=90,
        allow_delay=True,max_delay=90,allow_installments=False,max_installments=1,allow_financing=False)
    assert o["constraints_met"] is True
    assert o["best"]["decision_day"] > 0
    assert o["best"]["installments"] == 1
    assert o["best"]["financing"] == 0


def test_constraint_fields_are_always_submitted_and_theoretical_plan_is_labelled():
    t=(ROOT/"templates"/"decision_simulator.html").read_text(encoding="utf-8")
    assert 'name="max_delay" value="{{ inputs.max_delay }}" disabled' not in t
    assert 'name="max_installments" disabled' not in t
    assert 'SOLUTION THÉORIQUE HORS CONTRAINTES' in t
