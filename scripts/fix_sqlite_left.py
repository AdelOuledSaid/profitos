
from pathlib import Path
import ast
import shutil

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "profitos" / "db.py"
BACKUP = ROOT / "profitos" / "db.py.before_left_fix.bak"

def quote_sqlite_left(sql):
    """Quote LEFT function calls outside SQL literals and comments."""
    out = []
    i = 0
    n = len(sql)
    state = "normal"

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if state == "normal":
            if ch == "'":
                state = "single"
            elif ch == '"':
                state = "double"
            elif ch == "`":
                state = "backtick"
            elif ch == "[":
                state = "bracket"
            elif ch == "-" and nxt == "-":
                state = "line_comment"
                out.append("--")
                i += 2
                continue
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                out.append("/*")
                i += 2
                continue
            elif sql[i:i + 4].upper() == "LEFT":
                before = sql[i - 1] if i else ""
                after = sql[i + 4:i + 5]
                if not (before.isalnum() or before in "_.") and not (
                    after and (after.isalnum() or after == "_")
                ):
                    j = i + 4
                    while j < n and sql[j].isspace():
                        j += 1
                    if j < n and sql[j] == "(":
                        out.append('"LEFT"')
                        i += 4
                        continue

        elif state in ("single", "double", "backtick"):
            delimiter = {"single": "'", "double": '"', "backtick": "`"}[state]
            if ch == delimiter:
                if nxt == delimiter:
                    out.append(ch + nxt)
                    i += 2
                    continue
                state = "normal"

        elif state == "bracket" and ch == "]":
            state = "normal"

        elif state == "line_comment" and ch == "\n":
            state = "normal"

        elif state == "block_comment" and ch == "*" and nxt == "/":
            out.append("*/")
            i += 2
            state = "normal"
            continue

        out.append(ch)
        i += 1

    return "".join(out)


source = DB.read_text(encoding="utf-8")

# Insert the helper before SQLiteConnection, not in PostgreSQL code.
anchor = "class SQLiteConnection:"
assert source.count(anchor) == 1, "SQLiteConnection introuvable"

# Change only the SQLiteConnection.execute method.
start = source.index(anchor)
prefix, sqlite_class = source[:start], source[start:]

old = (
    "    def execute(self, sql, params=()):\n"
    "        cur = self._conn.cursor()\n"
    "        cur.execute(sql, params)\n"
)
new = (
    "    def execute(self, sql, params=()):\n"
    "        cur = self._conn.cursor()\n"
    "        sql = _quote_sqlite_left(sql)\n"
    "        cur.execute(sql, params)\n"
)

assert sqlite_class.count(old) == 1, "Bloc SQLite.execute inattendu"
assert "def _quote_sqlite_left(" not in source, "Correction deja presente"

helper_source = (
    "\n\n"
    + "def _quote_sqlite_left(sql):\n"
    + "    return _SQLITE_LEFT_REWRITER(sql)\n\n\n"
)

# Embed the actual implementation under a dedicated name.
import inspect

# Use the function source in this script as the implementation.
function_source = Path(__file__).read_text(encoding="utf-8")
function_ast = ast.parse(function_source)
function_node = next(
    node for node in function_ast.body
    if isinstance(node, ast.FunctionDef) and node.name == "quote_sqlite_left"
)
implementation = ast.get_source_segment(function_source, function_node)
implementation = implementation.replace(
    "def quote_sqlite_left(", "def _quote_sqlite_left(", 1
)

updated = (
    prefix
    + "\n\n" + implementation + "\n\n"
    + sqlite_class.replace(old, new, 1)
)

# Verify the generated Python before changing the original file.
ast.parse(updated, filename=str(DB))

# Check expected behavior.
assert quote_sqlite_left("SELECT LEFT('411000',3)") == (
    'SELECT "LEFT"(\'411000\',3)'
)
assert quote_sqlite_left("SELECT 'LEFT(411000)'") == (
    "SELECT 'LEFT(411000)'"
)
assert quote_sqlite_left("SELECT LEFT (col, 3) FROM t") == (
    'SELECT "LEFT" (col, 3) FROM t'
)
assert quote_sqlite_left("SELECT 1 -- LEFT(x,3)\n") == (
    "SELECT 1 -- LEFT(x,3)\n"
)
assert quote_sqlite_left("SELECT 1 /* LEFT(x,3) */") == (
    "SELECT 1 /* LEFT(x,3) */"
)

shutil.copy2(DB, BACKUP)
DB.write_text(updated, encoding="utf-8")

print("Correction appliquee avec succes.")
print("Sauvegarde :", BACKUP)
