import os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
if not os.environ.get('DATABASE_URL'):
    raise SystemExit('Définissez DATABASE_URL avant ce test.')
from profitos.runtime import init_auth_db, database_readiness
from profitos import db as dbmod
init_auth_db()
ok,backend,error=database_readiness()
print({'ok':ok,'backend':backend,'error':error})
if not ok or backend!='postgresql': raise SystemExit(1)
