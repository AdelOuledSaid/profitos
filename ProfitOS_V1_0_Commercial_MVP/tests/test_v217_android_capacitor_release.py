from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "android-capacitor.yml").read_text(encoding="utf-8")
README = (ROOT / "mobile" / "README.md").read_text(encoding="utf-8")
PLAY = (ROOT / "mobile" / "GOOGLE_PLAY.md").read_text(encoding="utf-8")


def test_debug_apk_is_always_built_and_uploaded():
    assert "./gradlew assembleDebug --no-daemon" in WORKFLOW
    assert "ProfitOS-Android-Debug-APK" in WORKFLOW
    assert "app-debug.apk" in WORKFLOW


def test_release_requires_all_four_signing_secrets():
    for name in (
        "ANDROID_KEYSTORE_BASE64",
        "ANDROID_KEYSTORE_PASSWORD",
        "ANDROID_KEY_ALIAS",
        "ANDROID_KEY_PASSWORD",
    ):
        assert name in WORKFLOW
    assert 'configured" -ne 4' in WORKFLOW
    assert "Incomplete Android signing configuration" in WORKFLOW


def test_keystore_and_alias_are_verified_before_release():
    verify_pos = WORKFLOW.index("keytool -list")
    bundle_pos = WORKFLOW.index("./gradlew bundleRelease --no-daemon")
    assert verify_pos < bundle_pos
    assert "-alias \"$KEY_ALIAS\"" in WORKFLOW
    assert "test -s android/app/profitos-release.keystore" in WORKFLOW


def test_signed_aab_is_checked_and_uploaded():
    assert "Verify release AAB exists" in WORKFLOW
    assert "app/build/outputs/bundle/release/app-release.aab" in WORKFLOW
    assert "ProfitOS-Android-Release-AAB" in WORKFLOW


def test_cloud_build_is_documented_without_local_node_requirement():
    assert "aucun Node.js ni Android Studio" in README
    assert "Actions > Build ProfitOS Android > Run workflow" in README
    assert "quatre secrets GitHub" in README


def test_docs_do_not_claim_generated_aab_is_published():
    assert "ne signifie pas que l'application est publiée" in README
    assert "ne vaut ni publication ni validation" in PLAY
