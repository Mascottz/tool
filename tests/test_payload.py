import os

from macusb.payload import (find_app, find_boot_efi, find_support_dmg,
                            IA_BOOTFILES)


def _fake_app(tmp_path, name="Install macOS Sonoma.app", dmg="SharedSupport.dmg"):
    app = tmp_path / name
    shared = app / "Contents" / "SharedSupport"
    shared.mkdir(parents=True)
    (shared / dmg).write_bytes(b"FAKE-DMG")
    return str(app)


def test_find_app_and_support_dmg(tmp_path):
    app = _fake_app(tmp_path)
    found = find_app(str(tmp_path))
    assert found == app
    dmg = find_support_dmg(found)
    assert dmg.endswith(os.path.join("Contents", "SharedSupport", "SharedSupport.dmg"))


def test_find_app_none(tmp_path):
    (tmp_path / "random.app").mkdir()
    assert find_app(str(tmp_path)) is None


def test_find_boot_efi(tmp_path):
    tree = tmp_path / "tree"
    efi = tree / "System" / "Library" / "CoreServices" / "boot.efi"
    efi.parent.mkdir(parents=True)
    efi.write_bytes(b"\x4d\x5a")
    assert find_boot_efi(str(tree)) == str(efi)


def test_find_boot_efi_missing(tmp_path):
    assert find_boot_efi(str(tmp_path)) is None


def test_ia_bootfiles_constant():
    assert IA_BOOTFILES == ".IABootFiles"
