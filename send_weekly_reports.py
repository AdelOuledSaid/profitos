"""Weekly ProfitOS reports for all active organizations."""
import sys
from profitos import create_app
from profitos.runtime import auth_cx, compute_weekly_digest, send_weekly_email


def main():
    dry_run = '--dry-run' in sys.argv
    flask_app = create_app()
    with flask_app.test_request_context():
        c = auth_cx()
        orgs = c.execute("SELECT * FROM organizations WHERE status='ACTIVE'").fetchall()
        c.close()
        print(f"{len(orgs)} organisation(s) active(s) trouvée(s).")
        for org in orgs:
            digest = compute_weekly_digest(org['id'])
            result = send_weekly_email(org, digest, dry_run=dry_run if dry_run else None)
            if result.get('sent'):
                print(f"[OK]   {org['name']} -> envoyé à {result['to']}")
            elif result.get('dry_run'):
                print(f"[SKIP] {org['name']} -> simulation")
            else:
                print(f"[ERR]  {org['name']} -> {result.get('reason','échec inconnu')}")


if __name__ == '__main__':
    main()
