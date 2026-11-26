import os

import pytest

from macusb.download import download
from macusb.errors import MacUsbError


def test_download_file_url(tmp_path):
    src = tmp_path / "src.bin"
    payload = b"hello-macusb" * 1000
    src.write_bytes(payload)
    dest = tmp_path / "out" / "dst.bin"
    res = download(src.as_uri(), str(dest))
    assert res.bytes_written == len(payload)
    assert dest.read_bytes() == payload
    assert not os.path.exists(str(dest) + ".part")


def test_resume_ignores_garbage_for_file_urls(tmp_path):
    src = tmp_path / "src.bin"
    payload = b"\x42" * 4096
    src.write_bytes(payload)
    dest = tmp_path / "dst.bin"
    # Pre-seed a corrupt partial: for file:// we must restart, not append.
    open(str(dest) + ".part", "wb").write(b"GARBAGE")
    res = download(src.as_uri(), str(dest), resume=True)
    assert dest.read_bytes() == payload


def test_sha256_mismatch_keeps_part(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    dest = tmp_path / "dst.bin"
    with pytest.raises(MacUsbError):
        download(src.as_uri(), str(dest), sha256="0" * 64)
    assert os.path.exists(str(dest) + ".part")
    assert not dest.exists()
