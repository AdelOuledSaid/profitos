from pathlib import Path
from jinja2 import Environment
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
TPL=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_v378_template_parses():
    Environment().parse(TPL)

def test_v378_history_is_visible_and_combined():
    assert "Historique des rapprochements" in TPL
    assert ">Clients<" in TPL
    assert ">Fournisseurs<" in TPL
    assert "purchase_reconciliations" in TPL
    assert "(r.matched_amount or 0)|fr_number(2)" in TPL

def test_v378_history_backend_is_entity_scoped_and_passed():
    assert "purchase_reconciliations = c.execute" in BANK
    assert "FROM bank_purchase_allocations r" in BANK
    assert "WHERE r.{ef}" in BANK
    assert "purchase_reconciliations=purchase_reconciliations" in BANK

def test_v378_hides_amount_only_supplier_noise():
    assert "if score <= 30:" in BANK
    assert "continue" in BANK.split("if score <= 30:",1)[1][:80]
    assert "montant exact ou un indice fournisseur/facture" in TPL

def test_v378_keeps_manual_confirmation():
    assert "Confirmer le paiement fournisseur" in TPL
