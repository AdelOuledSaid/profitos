"""Synchronisations longues : plafonnées dans le worker web, complètes en job."""
from pathlib import Path

import pytest

import profitos.runtime as rt
import profitos.routes.cloud_storage as cs

ROOT = Path(__file__).resolve().parents[1]
INV = (ROOT / 'profitos' / 'routes' / 'invoicing.py').read_text(encoding='utf-8')


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    rt.migrate_tenant(4242, force=True)
    c = rt.tenant_cx_direct(4242)
    c.execute("INSERT INTO cloud_storage_connections(provider,access_token,folder_path,connected_at) VALUES('google_drive','t','f',?)", (rt.now(),))
    c.commit()
    conn = c.execute('SELECT * FROM cloud_storage_connections').fetchone()
    yield c, conn
    c.close()


def test_manual_cloud_sync_is_capped_and_reports_remaining(tenant, monkeypatch):
    c, conn = tenant
    files = [{'file_id': f'id{i}', 'name': f'f{i}.bin'} for i in range(15)]
    monkeypatch.setattr(cs, 'list_folder_files', lambda c_, conn_: files)
    downloads = []
    monkeypatch.setattr(cs, 'download_file', lambda c_, conn_, fid: downloads.append(fid) or b'not a pdf')
    created, failed, remaining = cs.sync_cloud_folder(c, conn, 'google_drive', None, 4242, max_files=10)
    assert len(downloads) == 10 and remaining == 5
    assert created == [] and len(failed) == 10
    # sans plafond (job planifié) : tout est traité
    downloads.clear()
    created, failed, remaining = cs.sync_cloud_folder(c, conn, 'google_drive', None, 4242)
    assert len(downloads) == 15 and remaining == 0


def test_cloud_sync_time_budget(tenant, monkeypatch):
    c, conn = tenant
    files = [{'file_id': f'id{i}', 'name': f'f{i}.bin'} for i in range(5)]
    monkeypatch.setattr(cs, 'list_folder_files', lambda c_, conn_: files)
    monkeypatch.setattr(cs, 'download_file', lambda *a: b'x')
    _, _, remaining = cs.sync_cloud_folder(c, conn, 'google_drive', None, 4242, time_budget_s=-1)
    assert remaining == 5


def test_route_uses_caps_and_french_message():
    src = (ROOT / 'profitos' / 'routes' / 'cloud_storage.py').read_text(encoding='utf-8')
    assert 'max_files=CLOUD_MANUAL_SYNC_MAX_FILES' in src
    assert "Il reste {pluralize(remaining, 'fichier')}" in src
    assert 'CLOUD_MANUAL_SYNC_MAX_FILES = 10' in src


def test_weinvoice_manual_sync_is_capped():
    assert 'WEINVOICE_MANUAL_SYNC_MAX=15' in INV
    assert 'max_items=WEINVOICE_MANUAL_SYNC_MAX' in INV
    assert "Il reste {result['remaining']} facture(s)" in INV
    core = INV[INV.index('def sync_weinvoice_inbound('):INV.index('def register(app):')]
    assert '_purchase_pdf_dir()' not in core  # utilisable hors session (job)
    assert '_purchase_pdf_dir_for_org(org_id)' in core
