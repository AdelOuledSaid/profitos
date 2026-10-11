"""Stockage persistant des documents clients (disque Render)."""
import re
from pathlib import Path

import pytest

from profitos import storage
import profitos.runtime as rt

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_up_is_storage_root():
    assert rt.UP == storage.UPLOAD_ROOT


def test_storage_path_layout_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path)
    p = storage.storage_path('purchase_documents', 12, 'abc.pdf')
    assert p == tmp_path / 'purchase_documents' / '12' / 'abc.pdf'
    assert p.parent.is_dir()
    assert storage.upload_dir(12, 'imports') == tmp_path / 'org_12' / 'imports'
    for bad in ('../x.pdf', 'a/b.pdf', '', '.hidden'):
        with pytest.raises(ValueError):
            storage.storage_path('purchase_documents', 12, bad)
    with pytest.raises(ValueError):
        storage.storage_dir('../etc', 1)


def test_write_read_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path)
    storage.write_bytes('expense_receipts', 3, 'r.pdf', b'%PDF-1')
    assert storage.read_bytes('expense_receipts', 3, 'r.pdf') == b'%PDF-1'
    assert not list((tmp_path / 'expense_receipts' / '3').glob('*.part'))
    assert storage.org_directories(3) == [tmp_path / 'expense_receipts' / '3']
    storage.delete('expense_receipts', 3, 'r.pdf')
    assert not (tmp_path / 'expense_receipts' / '3' / 'r.pdf').exists()


def test_every_document_writer_uses_upload_root():
    """Aucun écrivain de documents clients ne doit écrire sous BASE en dur."""
    offenders = []
    for path in (ROOT / 'profitos').rglob('*.py'):
        if path.name == 'storage.py':
            continue  # seul module autorisé à définir le repli de développement
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r"BASE\s*/\s*['\"](uploads|documents|receipts)", text):
            offenders.append(f'{path.name}:{m.group(0)}')
        if path.name != 'storage.py' and "'uploads'" in text and 'require_area' not in text:
            offenders.append(f"{path.name}: 'uploads'")
    assert offenders == []
    for rel, marker in (('routes/invoicing.py', 'UP / "purchase_documents"'),
                        ('routes/expenses.py', 'UP / "expense_receipts"'),
                        ('routes/cabinet.py', 'UP / "cabinet_documents"')):
        assert marker in (ROOT / 'profitos' / rel).read_text(encoding='utf-8')
