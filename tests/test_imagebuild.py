import os

from macusb.imagebuild import (ALIGN_SECTORS, EFI_PART_SIZE, SECTOR,
                               hfs_partition_size, image_total_size,
                               plan_layout)
from macusb.payload import stage_ia_bootfiles


def test_hfs_partition_size_minimums():
    assert hfs_partition_size(0) >= 4 * 1024**3
    big = hfs_partition_size(14 * 1024**3)
    assert big > 14 * 1024**3  # slack applied


def test_plan_layout_apple_scheme():
    layout = plan_layout(6 * 1024**3, "apple")
    assert len(layout) == 1
    assert layout[0]["start"] == ALIGN_SECTORS
    assert layout[0]["type"] == "AF00"


def test_plan_layout_opencore_scheme_alignment():
    layout = plan_layout(6 * 1024**3, "opencore")
    assert [p["type"] for p in layout] == ["EF00", "AF00"]
    efi, hfs = layout
    assert efi["start"] == ALIGN_SECTORS
    assert efi["size"] * SECTOR == EFI_PART_SIZE
    assert hfs["start"] % ALIGN_SECTORS == 0
    total = image_total_size(layout)
    assert total > (hfs["start"] + hfs["size"]) * SECTOR


def test_stage_ia_bootfiles(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    efi = tmp_path / "boot.efi"
    efi.write_bytes(b"\x4d\x5a" + b"\x00" * 100)
    dest = stage_ia_bootfiles(str(vol), str(efi))
    assert os.path.isfile(dest)
    assert os.path.basename(os.path.dirname(dest)) == ".IABootFiles"
    assert os.stat(dest).st_mode & 0o111
