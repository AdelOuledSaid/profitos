"""Point d'entrée des tâches planifiées ProfitOS (cron Render).

Usage :
  python scripts/run_job.py --due            # cron horaire : tâches dues (heure de Paris)
  python scripts/run_job.py weekly_reports   # une tâche précise
  python scripts/run_job.py bank_sync --org 12 --force
  python scripts/run_job.py --list

Les tâches marquées « disque » (documents clients) sont déléguées au service
web, seul à monter le disque persistant : POST {JOBS_TRIGGER_URL ou
APP_BASE_URL}/api/v1/internal/jobs/<nom> avec « Authorization: Bearer
$JOBS_TRIGGER_TOKEN ». --local force l'exécution dans ce process (dev, shell
du service web).

Code de sortie : 0 si tout a réussi, 1 si au moins une tâche/organisation a
échoué (Render signale alors l'exécution du cron en échec).
"""
import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _production():
    return (os.environ.get('PROFITOS_ENV') or 'development').strip().lower() == 'production'


def delegate_to_web(name, force=False, timeout=15):
    """Demande au service web d'exécuter une tâche « disque ». Retourne True si acceptée."""
    import requests
    from profitos.jobs import trigger_token
    base = (os.environ.get('JOBS_TRIGGER_URL') or os.environ.get('APP_BASE_URL') or '').rstrip('/')
    token = trigger_token()
    if not base or not token:
        print(f'[ProfitOS] ERREUR: {name} nécessite le disque du service web, '
              'mais JOBS_TRIGGER_TOKEN / APP_BASE_URL ne sont pas configurés.')
        return False
    url = f'{base}/api/v1/internal/jobs/{name}' + ('?force=1' if force else '')
    try:
        resp = requests.post(url, headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
    except requests.RequestException as exc:
        print(f'[ProfitOS] ERREUR: déclenchement de {name} impossible ({type(exc).__name__}).')
        return False
    if resp.status_code in (202, 409):
        print(f"[ProfitOS] {name}: {'démarrée' if resp.status_code == 202 else 'déjà en cours'} dans le service web")
        return True
    print(f'[ProfitOS] ERREUR: le service web a refusé {name} (HTTP {resp.status_code}).')
    return False


def _should_delegate(job, local):
    if local or not job.needs_disk:
        return False
    # En développement sans jeton, tout s'exécute localement.
    return _production() or bool(os.environ.get('JOBS_TRIGGER_TOKEN'))


def _print_report(report):
    print(f"[ProfitOS] {report['job']} ({report['period']}) : {len(report['ok'])} ok, "
          f"{len(report['skipped'])} ignorée(s), {len(report['already'])} déjà faite(s), "
          f"{len(report['failed'])} échec(s)")
    for org_id, err in sorted(report['failed'].items()):
        print(f'[ProfitOS]   ÉCHEC org_{org_id}: {err}')


def main(argv=None, app=None):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('job', nargs='?')
    ap.add_argument('--due', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--local', action='store_true')
    ap.add_argument('--org', type=int, action='append')
    args = ap.parse_args(argv)

    from profitos import jobs
    if args.list:
        for j in jobs.JOBS.values():
            when = 'toutes les heures' if j.cadence == 'hourly' else (
                f"chaque jour à {j.hour} h" if j.cadence == 'daily' else f"chaque semaine (jour {j.weekday}) à {j.hour} h")
            print(f"{j.name:28} {when:32} {'[disque] ' if j.needs_disk else ''}{j.description}")
        return 0
    if not args.due and args.job not in jobs.JOBS:
        print(f"[ProfitOS] ERREUR: tâche inconnue {args.job!r}. Voir --list.")
        return 2

    if app is None:
        from profitos import create_app
        app = create_app()
    selected = jobs.due_jobs() if args.due else [jobs.JOBS[args.job]]
    failures = 0
    for j in selected:
        if _should_delegate(j, args.local):
            failures += 0 if delegate_to_web(j.name, args.force) else 1
            continue
        try:
            report = jobs.run_job(app, j.name, force=args.force, org_ids=set(args.org) if args.org else None)
        except Exception as exc:  # isolation entre tâches du dispatcher
            logging.getLogger('profitos.jobs').exception('Tâche %s interrompue', j.name)
            print(f'[ProfitOS] ERREUR: {j.name} interrompue ({type(exc).__name__})')
            failures += 1
            continue
        _print_report(report)
        if report['failed']:
            failures += 1
        if args.due and not args.org and jobs.period_complete(report):
            jobs.mark_period_done(j.name)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
