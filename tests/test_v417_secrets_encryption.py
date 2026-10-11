"""v417 — Secrets de tiers chiffrés au repos (AES-GCM, sous-clé HKDF par
organisation), migration transparente des valeurs historiques en clair."""
import pytest

from support_v4xx import app, auth, post, signup, tenant  # noqa: F401


def test_roundtrip_prefix_and_org_binding(monkeypatch):
    from profitos import crypto
    monkeypatch.setenv('PROFITOS_DATA_KEY', 'k' * 48)
    enc = crypto.encrypt_secret('shpat_secret', org_id=1)
    assert enc.startswith('enc:v1:') and 'shpat_secret' not in enc
    assert crypto.encrypt_secret('shpat_secret', org_id=1) != enc  # nonce aléatoire
    assert crypto.decrypt_secret(enc, org_id=1) == 'shpat_secret'
    with pytest.raises(ValueError):
        crypto.decrypt_secret(enc, org_id=2)  # copié dans une autre organisation
    tampered = enc[:-4] + ('AAAA' if not enc.endswith('AAAA') else 'BBBB')
    with pytest.raises(ValueError):
        crypto.decrypt_secret(tampered, org_id=1)


def test_legacy_plaintext_is_read_transparently():
    from profitos import crypto
    assert crypto.decrypt_secret('ancien-jeton-en-clair', org_id=1) == 'ancien-jeton-en-clair'
    assert crypto.reencrypt_legacy_value('ancien', org_id=1).startswith('enc:v1:')


def test_hkdf_matches_rfc5869_vector():
    from profitos.crypto import _hkdf_sha256
    okm = _hkdf_sha256(bytes.fromhex('0b' * 22), bytes.fromhex('000102030405060708090a0b0c'),
                       bytes.fromhex('f0f1f2f3f4f5f6f7f8f9'), 42)
    assert okm.hex() == ('3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf'
                         '34007208d5b887185865')


def test_production_without_data_key_refuses(monkeypatch):
    from profitos import crypto
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.delenv('PROFITOS_DATA_KEY', raising=False)
    with pytest.raises(crypto.DataKeyMissing):
        crypto.encrypt_secret('x', org_id=1)


def test_ecommerce_credentials_stored_encrypted(app):
    client, _, org_id, _ = signup(app, 'Boutique SAS', plan='BUSINESS')
    post(client, '/integrations/ecommerce/woocommerce/connecter',
         {'shop_domain': 'boutique.example.fr', 'credential_1': 'ck_clair', 'credential_2': 'cs_clair'})
    tc = tenant(org_id)
    row = tc.execute("SELECT credential_1,credential_2 FROM ecommerce_connections WHERE platform='woocommerce'").fetchone()
    tc.close()
    assert row['credential_1'].startswith('enc:v1:') and row['credential_2'].startswith('enc:v1:')
    from profitos.ecommerce import get_connection
    tc = tenant(org_id)
    conn = get_connection(tc, 'woocommerce', org_id=org_id)
    tc.close()
    assert conn['credential_1'] == 'ck_clair' and conn['credential_2'] == 'cs_clair'


def test_fec_tokens_are_hashed():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'profitos/routes/accounting.py').read_text(encoding='utf-8')
    assert "(token_digest(token), org['id'], date_from, date_to, now(), entity_id)" in src
    assert "WHERE token=?', (token_digest(token),)" in src


def test_cloud_tokens_encrypted_in_callback_and_refresh():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    route = (root / 'profitos/routes/cloud_storage.py').read_text(encoding='utf-8')
    module = (root / 'profitos/cloud_storage.py').read_text(encoding='utf-8')
    assert "(provider, access_enc, refresh_enc, tokens['expires_at']," in route
    assert "encrypt_secret(refreshed['access_token'])" in module and "decrypt_secret(connection['access_token'])" in module
