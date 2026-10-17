from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")
ast.parse(B)

def test_v395_major_families_are_present():
    for token in ("urssaf","dgfip","assurance","loyer","carburant","telephone",
                  "google","microsoft","adobe","openai","frais bancaire"):
        assert token in B

def test_v395_never_infers_vat_from_bank_label():
    block=B.split("def _bootstrap_accounting_suggestion(tx):",1)[1].split("\ndef ",1)[0]
    assert "vat_rate=None" in block
    assert "automation_eligible=False" in block
    assert "Validation humaine requise." in block

def test_v395_tax_account_stays_unforced():
    assert "('debit',('dgfip',),'Impôts et taxes',None" in B
    assert "('debit',('impot',),'Impôts et taxes',None" in B
    assert "('debit',('tresor public',),'Impôts et taxes',None" in B

def test_v395_known_safe_pcg_mappings():
    assert "'Charges sociales / URSSAF','645000'" in B
    assert "'Salaires et paie','641000'" in B
    assert "'Assurances','616000'" in B
    assert "'Loyers','613000'" in B
    assert "'Télécom','626000'" in B
    assert "'Logiciels / Abonnements','628100'" in B
    assert "'Frais bancaires','627000'" in B

def test_v395_keeps_stable_learning_safety():
    assert "confirmations >= 4 and score >= 90" in B
    assert "source in ('directionnelle', 'famille salaire')" in B
