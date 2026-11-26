import plistlib
from datetime import datetime, timezone

import pytest


@pytest.fixture
def fake_apple_cdn(tmp_path):
    """Build a synthetic Apple CDN tree: sucatalog + distribution plists + payloads.

    Returns dict with catalog_url and the payload file URLs.
    """
    cdn = tmp_path / "cdn"
    cdn.mkdir()

    def w(name, data: bytes) -> str:
        p = cdn / name
        p.write_bytes(data)
        return p.as_uri()

    recovery_dmg = w("BaseSystem.dmg", b"FAKE-RECOVERY-" + b"\xAA" * 64)
    full_pkg = w("InstallAssistant.pkg", b"FAKE-FULL-INSTALLER-" + b"\xBB" * 64)
    old_recovery_dmg = w("BaseSystem-old.dmg", b"OLD-" + b"\xCC" * 32)

    dist_rec = {
        "Title": "Install macOS Sonoma",
        "InstallInfo": {"OSes": [
            {"OSInstallation": recovery_dmg, "OSVersion": "14.7.1", "Build": "23H222"},
        ]},
    }
    dist_full = {
        "Title": "Install macOS Sonoma (full)",
        "InstallInfo": {"OSes": [
            {"OSInstallation": full_pkg, "OSVersion": "14.7.1", "Build": "23H222"},
        ]},
    }
    dist_old = {
        "Title": "Install macOS Ventura",
        "InstallInfo": {"OSes": [
            {"OSInstallation": old_recovery_dmg, "OSVersion": "13.6.9", "Build": "22G830"},
        ]},
    }
    dist_rec_url = w("dist-rec.dist", plistlib.dumps(dist_rec))
    dist_full_url = w("dist-full.dist", plistlib.dumps(dist_full))
    dist_old_url = w("dist-old.dist", plistlib.dumps(dist_old))

    catalog = {
        "CatalogVersion": "2",
        "GeneratedDate": datetime(2026, 11, 20, tzinfo=timezone.utc),
        "Products": [
            {"PostDate": datetime(2026, 11, 5, tzinfo=timezone.utc),
             "Distributions": {"English": dist_rec_url, "Russian": dist_rec_url}},
            {"PostDate": datetime(2026, 11, 5, tzinfo=timezone.utc),
             "Distributions": {"English": dist_full_url}},
            {"PostDate": datetime(2024, 5, 1, tzinfo=timezone.utc),
             "Distributions": {"English": dist_old_url}},
            {"PostDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
             "Distributions": {"English": "file:///nonexistent.dist"}},  # broken, must be skipped
        ],
    }
    catalog_url = w("index.merged-1.sucatalog", plistlib.dumps(catalog))
    return {
        "catalog_url": catalog_url,
        "recovery_dmg": recovery_dmg,
        "full_pkg": full_pkg,
        "old_recovery_dmg": old_recovery_dmg,
    }
