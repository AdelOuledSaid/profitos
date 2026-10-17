from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def web_templates():
    return [p for p in TEMPLATES.rglob("*.html") if not p.name.startswith("email_")]


def test_no_inline_event_handlers_in_web_templates():
    pattern = re.compile(r"\son[a-z]+\s*=", re.I)
    offenders = []
    for path in web_templates():
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_no_inline_script_blocks_in_web_templates():
    pattern = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>", re.I)
    offenders = []
    for path in web_templates():
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_csp_disallows_inline_javascript():
    source = (ROOT / "profitos" / "__init__.py").read_text(encoding="utf-8")
    assert "script-src 'self';" in source
    assert "script-src-attr 'none';" in source
    assert "script-src 'self' 'unsafe-inline'" not in source
