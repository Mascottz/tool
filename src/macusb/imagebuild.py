"""Assemble bootable artifacts without macOS:

  * raw GPT disk image (.img) - for USB sticks destined for Intel Macs
      - "apple"    scheme: single HFS+ partition (+ .IABootFiles/boot.efi),
                   replicating what `createinstallmedia` produces
      - "opencore" scheme: EFI System Partition (FAT32, OpenCore) + HFS+
                   installer partition; boots on any UEFI Intel Mac
  * hybrid ISO (.iso) - via xorriso, for VMs / archival / optical boot

Nothing here modifies Apple-signed binaries; we only repackage them.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess

from .errors import MacUsbError
from .opencore import prepare_efi_partition_tree
from .payload import stage_ia_bootfiles, find_boot_efi
from .util import human, info, require_root, require_tools, run

SECTOR = 512
ALIGN_SECTORS = 2048  # 1 MiB
HFS_GPT_TYPE = "AF00"
EFI_GPT_TYPE = "EF00"
EFI_PART_SIZE = 512 * 1024 * 1024          # 512 MiB
HFS_MIN_SIZE = 4 * 1024 * 1024 * 1024      # never smaller than 4 GiB
SLACK_NUM, SLACK_DEN = 5, 4                # +25% headroom
EXTRA_HEADROOM = 512 * 1024 * 1024         # +512 MiB for journal/metadata


def dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def hfs_partition_size(tree_size: int, app_size: int = 0) -> int:
    content = tree_size + app_size
    size = content * SLACK_NUM // SLACK_DEN + EXTRA_HEADROOM
    return max(size, HFS_MIN_SIZE)


def _align_up(n: int, align: int) -> int:
    return int(math.ceil(n / align) * align)


def plan_layout(hfs_size: int, boot_scheme: str) -> list[dict]:
    """Return [{name,type,start(sectors),size(sectors)}] aligned to 1MiB."""
    layout = []
    cursor = ALIGN_SECTORS
    if boot_scheme == "opencore":
        efi_sectors = EFI_PART_SIZE // SECTOR
        layout.append({"name": "EFI", "type": EFI_GPT_TYPE,
                       "start": cursor, "size": efi_sectors})
        cursor += efi_sectors
        cursor = _align_up(cursor, ALIGN_SECTORS)
    hfs_sectors = _align_up(hfs_size, SECTOR) // SECTOR
    layout.append({"name": "Install macOS", "type": HFS_GPT_TYPE,
                   "start": cursor, "size": hfs_sectors})
    return layout


def image_total_size(layout: list[dict]) -> int:
    end = max(p["start"] + p["size"] for p in layout)
    return (end + ALIGN_SECTORS) * SECTOR  # backup GPT + slack


# --------------------------------------------------------------------------

def _write_fat_partition_image(efi_src_tree: str, dest_img: str, size: int,
                               volume_label: str = "EFI") -> None:
    """Create a populated FAT32 image *without* root, using mtools."""
    require_tools(["mformat", "mcopy"])
    with open(dest_img, "wb") as fh:
        fh.truncate(size)
    run(["mformat", "-i", dest_img, "-F", f"::{volume_label}"])
    # Copy everything under efi_src_tree into the image root.
    for item in os.listdir(efi_src_tree):
        run(["mcopy", "-i", dest_img, "-s", os.path.join(efi_src_tree, item),
             f"::{item}"])


def _write_hfs_partition_image(tree_root: str, app_dir: str | None,
                               dest_img: str, size: int, label: str) -> None:
    """Create and populate an HFS+ partition image. Needs root (loop mount)."""
    require_root("HFS+ image creation (kernel HFS+ driver + loop device)")
    require_tools(["mkfs.hfsplus", "mount", "umount", "losetup"])
    with open(dest_img, "wb") as fh:
        fh.truncate(size)
    run(["mkfs.hfsplus", "-v", label, dest_img])
    mnt = dest_img + ".mnt"
    os.makedirs(mnt, exist_ok=True)
    loop = None
    try:
        loop_out = run(["losetup", "--find", "--show", dest_img], capture=True)
        loop = loop_out.stdout.decode().strip()
        mounted = False
        for opts in ("rw", "force,rw"):
            cp = run(["mount", "-t", "hfsplus", "-o", opts, loop, mnt],
                     check=False, capture=True)
            if cp.returncode == 0:
                mounted = True
                break
        if not mounted:
            raise MacUsbError(
                "Could not mount the HFS+ image read-write. The kernel HFS+ "
                "driver may be missing (`modprobe hfsplus`) or the filesystem "
                "is journaled. Try: `sudo modprobe hfsplus` and re-run."
            )
        # Probe writability: freshly-made HFS+ can still end up read-only.
        probe = os.path.join(mnt, ".macusb-write-test")
        try:
            with open(probe, "wb") as fh:
                fh.write(b"ok")
            os.remove(probe)
        except OSError as exc:
            raise MacUsbError(
                f"HFS+ volume mounted read-only ({exc}). The kernel may not "
                "support read-write HFS+ here. Try `sudo modprobe hfsplus`; if it "
                "keeps failing, build on a machine with writable HFS+ (or macOS)."
            )
        info(f"Populating HFS+ volume at {mnt} ...")
        for item in os.listdir(tree_root):
            src = os.path.join(tree_root, item)
            dst = os.path.join(mnt, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        if app_dir and os.path.isdir(app_dir):
            name = os.path.basename(app_dir.rstrip("/"))
            info(f"Copying installer application: {name}")
            shutil.copytree(app_dir, os.path.join(mnt, name), symlinks=True)
        boot_src = find_boot_efi(tree_root)
        if boot_src:
            stage_ia_bootfiles(mnt, boot_src)
            info("Staged .IABootFiles/boot.efi (what createinstallmedia does)")
        else:
            info("No boot.efi found in payload tree; skipping .IABootFiles")
        run(["umount", mnt])
        mounted = False
    finally:
        if mounted:
            run(["umount", mnt], check=False)
        if loop:
            run(["losetup", "-d", loop], check=False)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def build_disk_image(*, tree_root: str, app_dir: str | None, out_img: str,
                     boot_scheme: str = "apple", label: str = "Install macOS",
                     workdir: str, opencore_workdir: str | None = None) -> dict:
    """Build the raw GPT image. Returns build metadata (for the manifest)."""
    if boot_scheme not in ("apple", "opencore"):
        raise MacUsbError(f"Unknown boot scheme: {boot_scheme}")

    require_tools(["sgdisk", "dd"])
    hfs_size = hfs_partition_size(dir_size(tree_root), dir_size(app_dir) if app_dir else 0)
    layout = plan_layout(hfs_size, boot_scheme)
    total = image_total_size(layout)
    info(f"HFS+ partition: {human(hfs_size)} | image size: {human(total)}")

    hfs_img = os.path.join(workdir, "partition-hfs.img")
    efi_img = os.path.join(workdir, "partition-efi.img")

    if boot_scheme == "opencore":
        efi_tree_root = os.path.join(opencore_workdir or workdir, "efi-tree")
        if os.path.isdir(efi_tree_root):
            shutil.rmtree(efi_tree_root)
        os.makedirs(efi_tree_root)
        prepare_efi_partition_tree(efi_tree_root, os.path.join(workdir, "opencore-work"))
        _write_fat_partition_image(efi_tree_root, efi_img, EFI_PART_SIZE)

    _write_hfs_partition_image(tree_root, app_dir, hfs_img, hfs_size, label)

    with open(out_img, "wb") as fh:
        fh.truncate(total)
    run(["sgdisk", "--zap-all", out_img], check=False)  # fine on a fresh sparse file
    part_num = 0
    for p in layout:
        part_num += 1
        start = p["start"]
        end = p["start"] + p["size"] - 1
        run(["sgdisk", "-n", f"{part_num}:{start}:{end}", "-t",
             f"{part_num}:{p['type']}", "-c", f"{part_num}:{p['name']}", out_img])
    for p, src in zip(layout, ([efi_img, hfs_img] if boot_scheme == "opencore" else [hfs_img])):
        offset_bytes = p["start"] * SECTOR
        run(["dd", f"if={src}", f"of={out_img}", "bs=1M",
             f"seek={offset_bytes // (1024 * 1024)}", "conv=notrunc", "status=none"])

    info(f"Disk image written: {out_img} ({human(total)})")
    return {
        "image": out_img,
        "size": total,
        "boot_scheme": boot_scheme,
        "partitions": [
            {"name": p["name"], "type": p["type"],
             "start_sector": p["start"], "size_sectors": p["size"]}
            for p in layout
        ],
    }


def build_iso(*, tree_root: str, app_dir: str | None, out_iso: str,
              volume_id: str = "MACOS_INSTALL") -> str:
    """Pack the extracted tree into a hybrid ISO9660/HFS+ image via xorriso.

    Useful for VMs (QEMU/KVM) and archival. Intel Macs generally will NOT boot
    this from USB; use the raw .img for real hardware.
    """
    require_tools(["xorriso"])
    stage = out_iso + ".staging"
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    shutil.copytree(tree_root, stage, symlinks=True)
    if app_dir and os.path.isdir(app_dir):
        shutil.copytree(app_dir, os.path.join(stage, os.path.basename(app_dir.rstrip("/"))),
                        symlinks=True)
    boot_src = find_boot_efi(tree_root)
    if boot_src:
        stage_ia_bootfiles(stage, boot_src)
    base_cmd = ["xorriso", "-as", "mkisofs", "-o", out_iso,
                "-iso-level", "3", "-J", "-R", "-l", "-V", volume_id]
    try:
        run(base_cmd + ["-hfs", "-hfs-volume-id", "Install macOS",
                        "-apm-block-size", "2048", stage])
        info("Built hybrid ISO (ISO9660 + HFS+/APM)")
    except MacUsbError:
        info("xorriso lacks HFS+ support here; building plain ISO9660/UDF instead "
             "(fine for VMs, not for optical macOS boot)")
        run(base_cmd + stage)
    shutil.rmtree(stage, ignore_errors=True)
    info(f"ISO written: {out_iso}")
    return out_iso


def write_manifest(path: str, manifest: dict) -> None:
    with open(path, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
