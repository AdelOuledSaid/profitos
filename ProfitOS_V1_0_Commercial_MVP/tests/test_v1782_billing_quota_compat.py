from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_billing_quota_variables_are_explicit():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    for x in [
        'quota_usage.imports.used',
        'quota_usage.reports.used',
        'quota_usage.team.used',
        'quota_usage.organizations.used',
        'UTILISATION CE MOIS-CI',
        'Illimité',
    ]:
        assert x in t
