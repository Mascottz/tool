"""Safe enumeration and writing of USB sticks. Destructive; guarded hard."""
from __future__ import annotations

import json
import os
import time

from .errors import MacUsbError
from .util import (base_block_device, device_size_bytes, human, info,
                   is_removable, mounted_partitions, require_root, require_tools, run)

GPT_MAGIC = b"EFI PART"


def list_removable() -> list[dict]:
    cp = run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,TRAN,RM,MODEL,VENDOR"],
             capture=True, check=False)
    try:
        data = json.loads(cp.stdout or b"{}")
    except json.JSONDecodeError:
        raise MacUsbError("Could not parse `lsblk` output")
    out = []

    def walk(node):
        if node.get("type") == "disk":
            path = f"/dev/{node.get('name')}"
            rm = node.get("rm") in (1, "1", True)
            tran = (node.get("tran") or "").lower()
            if rm or tran == "usb":
                out.append({
                    "device": path,
                    "size": node.get("size"),
                    "tran": tran,
                    "model": (node.get("model") or "").strip(),
                    "vendor": (node.get("vendor") or "").strip(),
                })
        for child in node.get("children") or []:
            walk(child)

    for dev in data.get("blockdevices", []):
        walk(dev)
    return out


def guard_target(device: str, *, allow_internal: bool = False,
                 assume_yes: bool = False, interactive: bool = True) -> str:
    """Validate that `device` is a whole-disk, removable, unmounted target."""
    if not os.path.exists(device):
        raise MacUsbError(f"{device} does not exist")
    base = base_block_device(device)
    if base != device.rstrip("/"):
        info(f"Using whole disk {base} (not partition {device})")
    if base == "/dev/sda" or base.startswith("/dev/nvme0"):
        # Not an absolute ban (some systems do boot from USB on sda), but demand explicit consent.
        if not allow_internal:
            raise MacUsbError(
                f"{base} looks like a primary internal disk. Refusing. "
                "Pass --allow-internal only if you are absolutely sure."
            )
    if not is_removable(base) and not allow_internal:
        raise MacUsbError(
            f"{base} is not flagged as removable/USB. Refusing. "
            "Pass --allow-internal to override (dangerous)."
        )
    mounts = mounted_partitions(base)
    if mounts:
        raise MacUsbError(
            "Refusing to write: device (or partitions of it) is mounted:\n  "
            + "\n  ".join(mounts)
            + "\nUnmount first: `sudo umount <part>` (or pass --umount to do it automatically)."
        )
    size = device_size_bytes(base)
    if assume_yes:
        return base
    print()
    print(f"  Device : {base}")
    print(f"  Size   : {human(size) if size else 'unknown'}")
    print(f"  Model  : {_model(base)}")
    print()
    if interactive and os.isatty(0):
        reply = input(f"ALL DATA on {base} will be DESTROYED. Type the device name to continue: ")
        if reply.strip() != os.path.basename(base):
            raise MacUsbError("Confirmation did not match; aborting.")
    else:
        raise MacUsbError(
            "Refusing without confirmation: pass --yes to confirm destruction, "
            "or run in a terminal to type the device name."
        )
    return base


def _model(base: str) -> str:
    name = os.path.basename(base)
    try:
        with open(f"/sys/class/block/{name}/device/model") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


def unmount_all(device: str) -> None:
    cp = run(["lsblk", "-J", "-o", "NAME,MOUNTPOINT", base_block_device(device)],
             capture=True, check=False)
    try:
        data = json.loads(cp.stdout or b"{}")
    except json.JSONDecodeError:
        return
    parts = []

    def walk(node):
        mp = node.get("mountpoint")
        if mp:
            parts.append(f"/dev/{node['name']}")
        for child in node.get("children") or []:
            walk(child)

    for dev in data.get("blockdevices", []):
        walk(dev)
    for p in parts:
        info(f"Unmounting {p}")
        run(["umount", p])


def write_image(device: str, image: str, *, verify: bool = True) -> None:
    require_root("Writing to a block device")
    require_tools(["dd"])
    size = device_size_bytes(device)
    img_size = os.path.getsize(image)
    if size and img_size > size:
        raise MacUsbError(
            f"Image ({human(img_size)}) is larger than the target device ({human(size)})."
        )
    info(f"Writing {human(img_size)} to {device} ...")
    t0 = time.time()
    run(["dd", f"if={image}", f"of={device}", "bs=4M", "conv=fsync",
         "status=progress"])
    info(f"Write finished in {time.time() - t0:.0f}s")
    run(["sync"], check=False)
    if verify:
        with open(device, "rb") as fh:
            fh.seek(SECTOR_OFFSET)
            magic = fh.read(8)
        if magic != GPT_MAGIC:
            raise MacUsbError(
                "Post-write check failed: 'EFI PART' signature not found at LBA1. "
                "The device may be faulty or the write was intercepted."
            )
        info("Post-write verification passed (GPT signature present)")


SECTOR_OFFSET = 512  # GPT header is at LBA 1
