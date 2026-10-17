"""Rapports hebdomadaires ProfitOS — compatibilité.

Le rapport est désormais envoyé automatiquement chaque lundi par la tâche
planifiée ``weekly_reports`` (profitos/jobs.py, cron Render
``scripts/run_job.py --due``). Ce script reste utilisable à la main :

  python send_weekly_reports.py            # = python scripts/run_job.py weekly_reports
  python send_weekly_reports.py --force    # renvoie même si déjà envoyé cette semaine
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))

import run_job  # noqa: E402


if __name__ == '__main__':
    args = ['weekly_reports'] + [a for a in sys.argv[1:] if a in ('--force', '--local')]
    sys.exit(run_job.main(args))
