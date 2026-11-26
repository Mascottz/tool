from datetime import datetime

from macusb import catalog as cat


def test_classify_payload():
    assert cat.classify_payload("https://x/BaseSystem.dmg") == cat.RECOVERY
    assert cat.classify_payload("https://x/InstallAssistant.pkg") == cat.FULL
    assert cat.classify_payload("https://x/AppleDocumentation.pdf") == cat.UNKNOWN


def test_version_matches():
    assert cat.version_matches("14", "14.7.1")
    assert cat.version_matches("14.7", "14.7.1")
    assert cat.version_matches("14.7.1", "14.7.1")
    assert not cat.version_matches("15", "14.7.1")
    assert not cat.version_matches("14.8", "14.7.1")


def test_harvest_and_select(fake_apple_cdn):
    entries = cat.list_catalog(fake_apple_cdn["catalog_url"])
    kinds = {e.kind for e in entries}
    assert kinds == {cat.RECOVERY, cat.FULL}
    # broken distribution was skipped, 3 valid payloads remain (dedupe by url)
    urls = {e.url for e in entries}
    assert fake_apple_cdn["recovery_dmg"] in urls
    assert fake_apple_cdn["full_pkg"] in urls
    assert fake_apple_cdn["old_recovery_dmg"] in urls

    rec = cat.select_entries(entries, kind=cat.RECOVERY, latest=True)
    assert len(rec) == 1 and rec[0].version == "14.7.1"

    v13 = cat.select_entries(entries, kind=cat.RECOVERY, version="13")
    assert len(v13) == 1 and v13[0].version == "13.6.9"

    full = cat.select_entries(entries, kind=cat.FULL, version="14.7")
    assert len(full) == 1 and full[0].url == fake_apple_cdn["full_pkg"]


def test_entries_from_url():
    entries = cat.entries_from_url("https://swcdn.apple.com/x/InstallAssistant.pkg")
    assert entries[0].kind == cat.FULL
    entries = cat.entries_from_url("https://swcdn.apple.com/x/BaseSystem.dmg")
    assert entries[0].kind == cat.RECOVERY
