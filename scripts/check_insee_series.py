"""Contrôle individuel des séries INSEE de la veille (v479), à lancer dans le shell du
service web Render (l'INSEE doit être joignable) :

    python scripts/check_insee_series.py                  # les dix indices de chiffre d'affaires
    python scripts/check_insee_series.py --all            # tout le catalogue INSEE
    python scripts/check_insee_series.py CA_HOTELS BT01   # séries choisies
    python scripts/check_insee_series.py --save           # enregistre aussi le résultat

Pour chaque série : identifiant, flux et clé SDMX trouvés dans le fichier officiel
« correspondance_idbank_dimension », dimensions officielles (CORRECTION, NAF, base),
intitulé, correction retenue (CVS-CJO / BRUT / INCONNU) et sa source, contrôle de
l'activité, dernière valeur, et les anomalies qui suspendraient l'indicateur.
Lecture seule sans --save. Code de sortie : 0 si aucune série n'est suspendue, 1 sinon.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check(codes, http=None, download=None, out=print):
    """Contrôle les séries ; retourne [{code, status, ...}]. http / download : injection (tests)."""
    from profitos import market_watch as MW
    defs = [MW.definition(c) for c in codes]
    defs = [d for d in defs if d and d['source'] == 'insee']
    try:
        mapping, url = MW.download_idbank_mapping([d['idbank'] for d in defs], download)
        out(f'Fichier de correspondance : {url}')
    except Exception as exc:
        mapping = {}
        out(f'⚠ Fichier de correspondance indisponible ({exc}) : contrôle sur les intitulés seulement.')
    results = []
    for d in defs:
        r = {'code': d['code'], 'idbank': d['idbank'], 'label': d['label']}
        dims = None
        if d['idbank'] in mapping:
            flow, key = mapping[d['idbank']]
            r.update(dataflow=flow, series_key=key)
            try:
                dims = MW.fetch_dimensions(flow, key, d['idbank'], http)
            except Exception as exc:
                r['dims_error'] = str(exc)
        else:
            r['dims_error'] = 'absent du fichier de correspondance' if mapping else 'fichier indisponible'
        r['dimensions'] = dims or {}
        try:
            data = MW.fetch(dict(d, dimensions=dims or {}), http)
        except Exception as exc:
            r.update(status='suspendue' if MW.SUSPENDED in str(exc) else 'erreur', error=str(exc))
        else:
            last = data['obs'][-1]
            r.update(status='ok', title=data['title'], adjustment=data['adjustment'],
                     adjustment_source=data['adjustment_source'], naf_check=data['naf_check'],
                     last=f'{MW.period_label(last[0])} : {last[1]:g}', obs=data['obs'], data=data)
        results.append(r)
        _print(r, out)
    return results


def _print(r, out):
    mark = {'ok': '✓', 'suspendue': '✗', 'erreur': '⚠'}[r['status']]
    out(f"\n{mark} {r['code']} — {r['label']} (idbank {r['idbank']})")
    if r.get('dataflow'):
        out(f"    flux {r['dataflow']}, clé {r['series_key']}")
    if r.get('dimensions'):
        out('    dimensions : ' + ', '.join(f'{k}={v}' for k, v in sorted(r['dimensions'].items())))
    elif r.get('dims_error'):
        out(f"    dimensions : non disponibles ({r['dims_error']})")
    if r['status'] == 'ok':
        out(f"    intitulé : {r['title']}")
        src = {'dimensions': 'dimensions officielles', 'titre': 'intitulé'}.get(r['adjustment_source'], 'non confirmée')
        out(f"    correction : {r['adjustment']} ({src})")
        if r.get('naf_check'):
            out(f"    activité : {r['naf_check']}")
        out(f"    dernière valeur : {r['last']}")
    else:
        out(f"    {r['status'].upper()} : {r['error']}")


def main(argv=None):
    from profitos import market_watch as MW
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('codes', nargs='*')
    ap.add_argument('--all', action='store_true', help='tout le catalogue INSEE')
    ap.add_argument('--save', action='store_true', help='enregistre dimensions et valeurs dans la base')
    a = ap.parse_args(argv)
    if a.codes:
        codes = a.codes
    elif a.all:
        codes = [c for c, d in MW.CATALOGUE.items() if d['source'] == 'insee']
    else:
        codes = [c for c, d in MW.CATALOGUE.items() if d.get('kind') == 'activite']
    unknown = [c for c in codes if not MW.definition(c)]
    if unknown:
        print('Codes inconnus : ' + ', '.join(unknown))
        return 2
    if a.save:
        from profitos.runtime import auth_cx
        ac = auth_cx()
        try:
            MW.refresh_dimensions(ac, codes, force=True)
            report = MW.refresh(ac, codes)
        finally:
            ac.close()
        print('Enregistré : ' + ', '.join(f'{k}={v}' for k, v in report.items()))
    results = check(codes)
    bad = [r for r in results if r['status'] != 'ok']
    print(f"\n{len(results) - len(bad)}/{len(results)} série(s) valides.")
    return 1 if any(r['status'] == 'suspendue' for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
