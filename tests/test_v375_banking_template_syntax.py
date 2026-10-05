from pathlib import Path
from jinja2 import Environment

T=(Path(__file__).resolve().parents[1]/"templates"/"banking.html").read_text(encoding="utf-8")

def test_v375_banking_template_compiles():
    Environment().parse(T)

def test_v375_customer_reconciliation_loop_is_closed_correctly():
    b=T.split("{% for m in reconciliation_suggestions %}",1)[1].split("{% endfor %}",1)[0]
    assert "{% else %}" not in b
    assert "{% endif %}" not in b
    assert "banking_reconcile" in b
