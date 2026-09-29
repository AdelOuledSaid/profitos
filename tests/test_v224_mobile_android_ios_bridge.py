from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')
BRIDGE = (ROOT / 'static' / 'js' / 'mobile-capacitor.js').read_text(encoding='utf-8')
PUSH = (ROOT / 'profitos' / 'routes' / 'push_notifications.py').read_text(encoding='utf-8')
IOS = (ROOT / '.github' / 'workflows' / 'ios-capacitor.yml').read_text(encoding='utf-8')
IOS_DOC = (ROOT / 'mobile' / 'IOS.md').read_text(encoding='utf-8')
PACKAGE = (ROOT / 'mobile' / 'package.json').read_text(encoding='utf-8')


def test_remote_web_app_loads_native_capacitor_bridge():
    assert "js/mobile-capacitor.js" in BASE
    assert 'isNativePlatform' in BRIDGE
    assert "p === 'ios' || p === 'android'" in BRIDGE


def test_push_client_registers_real_capacitor_token_with_csrf():
    assert 'PushNotifications' in BRIDGE
    assert "push.addListener('registration'" in BRIDGE
    assert '/api/mobile/csrf-token' in BRIDGE
    assert '/api/mobile/register-push-token' in BRIDGE
    assert "'X-CSRF-Token': csrf" in BRIDGE
    assert 'push.requestPermissions()' in BRIDGE
    assert 'push.register()' in BRIDGE
    assert '@capacitor/push-notifications' in PACKAGE


def test_push_action_only_navigates_to_local_profitos_path():
    assert "target.startsWith('/')" in BRIDGE
    assert "target.startsWith('//')" in BRIDGE
    assert 'window.location.assign(target)' in BRIDGE


def test_mobile_csrf_endpoint_always_materializes_token():
    assert "return jsonify({'csrf_token': csrf_token()})" in PUSH


def test_ios_cloud_build_generates_syncs_and_compiles_simulator():
    assert 'runs-on: macos-latest' in IOS
    assert 'npx cap add ios' in IOS
    assert 'npx cap sync ios' in IOS
    assert 'xcodebuild' in IOS
    assert '-sdk iphonesimulator' in IOS
    assert 'CODE_SIGNING_ALLOWED=NO' in IOS
    assert 'ProfitOS-iOS-Simulator' in IOS


def test_ios_docs_do_not_claim_app_store_readiness():
    assert 'ni un IPA distribuable, ni une publication App Store' in IOS_DOC
    assert 'compte Apple Developer' in IOS_DOC
    assert 'APNs' in IOS_DOC
