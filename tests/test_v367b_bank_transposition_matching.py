from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def load():
    tree=ast.parse(BANK)
    wanted={"_norm_text","_token_typo_match","_name_similarity"}
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.Import,ast.ImportFrom))
           and (not isinstance(n,ast.FunctionDef) or n.name in wanted)]
    ns={}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),"<matching>","exec"),ns)
    return ns

def test_v367b_adjacent_transposition_only():
    f=load()["_token_typo_match"]
    assert f("amazon","amazno") is True
    assert f("budget","budegt") is True
    assert f("amazon","amnoaz") is False

def test_v367b_still_rejects_two_unrelated_substitutions():
    f=load()["_token_typo_match"]
    assert f("budget","buxxet") is False
