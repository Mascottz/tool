import json
import os

import pytest

from macusb.cli import main


def test_doctor_runs(capsys):
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "macusb" in out
    assert rc in (0, 1)  # tool availability varies by host


def test_list_with_fixture_catalog(fake_apple_cdn, capsys):
    rc = main(["list", "--catalog-url", fake_apple_cdn["catalog_url"]])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recovery" in out and "full" in out


def test_list_json(fake_apple_cdn, capsys):
    rc = main(["list", "--catalog-url", fake_apple_cdn["catalog_url"], "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3


def test_fetch_via_url(tmp_path, capsys):
    src = tmp_path / "BaseSystem.dmg"
    src.write_bytes(b"FAKE" * 1024)
    wd = tmp_path / "work"
    rc = main(["fetch", "--url", src.as_uri(), "--workdir", str(wd)])
    assert rc == 0
    man = wd / "fetch-latest" / "manifest.json"
    assert man.exists()
    manifest = json.loads(man.read_text())
    assert manifest["payloads"][0]["kind"] == "recovery"
    assert os.path.isfile(manifest["payloads"][0]["path"])


def test_write_usb_refuses_bogus_device(tmp_path, capsys):
    rc = main(["write-usb", "--device", "/dev/definitely-not-a-device-xyz",
               "--image", __file__])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_unknown_command_errors():
    with pytest.raises(SystemExit):
        main(["frobnicate"])
