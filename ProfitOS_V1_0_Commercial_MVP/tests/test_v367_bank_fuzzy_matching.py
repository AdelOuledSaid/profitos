from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
def _load_matching():
    tree=ast.parse(BANK); wanted={"_norm_text","_token_typo_match","_name_similarity"}
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.Import,ast.ImportFrom)) and (not isinstance(n,ast.FunctionDef) or n.name in wanted)]
    ns={}; exec(compile(ast.Module(body=nodes,type_ignores=[]),"<matching>","exec"),ns); return ns
def test_v367_exact_names_keep_full_score():
    assert _load_matching()["_name_similarity"]("BUDGET INSIGHT","VIR BUDGET INSIGHT PARIS")==30
def test_v367_one_typo_on_long_supplier_token_is_tolerated():
    ns=_load_matching()
    assert ns["_name_similarity"]("MICROSOFT","PRLV MICROSOFTT FRANCE")==30
    assert ns["_name_similarity"]("AMAZON","CB AMAZNO MARKETPLACE")==30
def test_v367_short_tokens_are_not_fuzzy_matched():
    ns=_load_matching()
    assert ns["_token_typo_match"]("abc","abd") is False
    assert ns["_token_typo_match"]("uber","ubfr") is False
def test_v367_multiple_edits_are_rejected():
    assert _load_matching()["_token_typo_match"]("budget","buxxet") is False
def test_v367_legal_form_noise_is_ignored():
    assert _load_matching()["_name_similarity"]("ACME SAS","VIREMENT ACME SARL")==30
def test_v367_score_scale_stays_compatible_with_v361():
    p=BANK.split("def _purchase_reconciliation_suggestions",1)[1].split("def _bank_allocated_total",1)[0]
    assert "if sim >= 30:" in p and "elif sim >= 20:" in p and "elif sim >= 10:" in p
def test_v367_preserves_ambiguity_guard():
    b=BANK.split("def _reconciliation_suggestions",1)[1].split("def _accounting_suggestion",1)[0]
    assert "best[0] < 80" in b and "best[0]-second < 15" in b
def test_v367_preserves_v366_and_v365():
    assert "bank_transaction_workflow" in BANK
    assert "remaining_after <= 5.00" in BANK
    assert "tx['transaction_date']" in BANK and "tx['booking_date']" not in BANK
