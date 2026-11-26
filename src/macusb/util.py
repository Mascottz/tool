"""Shared helpers: process execution, dependency detection, device safety."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .errors import MacUsbError


def info(msg: str) -> None:
    print(f"[macusb] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[macusb] WARNING: {msg}", file=sys.stderr, flush=True)


def human(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TiB"  # pragma: no cover


def run(cmd, check: bool = True, capture: bool = False, env=None):
    """Run a command, returning CompletedProcess. Raises MacUsbError on failure."""
    try:
        cp = subprocess.run(
            list(cmd),
            check=False,
            stdout=(subprocess.PIPE if capture else None),
            stderr=(subprocess.PIPE if capture else None),
            env=env,
        )
    except FileNotFoundError:
        raise MacUsbError(
            f"Required tool not found: {cmd[0]}. Run `macusb doctor` for install hints."
        )
    if check and cp.returncode != 0:
        tail = ""
        if capture:
            tail = (cp.stderr or cp.stdout or b"").decode(errors="replace").strip()
            tail = f"\n{cmd[0]} said: {tail[-2000:]}" if tail else ""
        raise MacUsbError(f"Command failed ({cp.returncode}): {' '.join(map(str, cmd))}{tail}")
    return cp


APT = "sudo apt-get install -y {p}"
DNF = "sudo dnf install -y {p}"
PACMAN = "sudo pacman -S {p}"

# tool -> (apt/dnf/pacman package hint)
TOOL_HINTS = {
    "dmg2img": ("dmg2img", "dmg2img", "dmg2img"),
    "sgdisk": ("gdisk", "gdisk", "gptfdisk"),
    "xorriso": ("xorriso", "xorriso", "libisoburn"),
    "mkfs.hfsplus": ("hfsprogs", "hfsprogs", "hfsprogs"),
    "mkfs.vfat": ("dosfstools", "dosfstools", "dosfstools"),
    "mformat": ("mtools", "mtools", "mtools"),
    "mcopy": ("mtools", "mtools", "mtools"),
    "bsdtar": ("libarchive-tools", "libarchive", "libarchive"),
    "7z": ("p7zip-full", "p7zip", "p7zip"),
    "cpio": ("cpio", "cpio", "cpio"),
    "losetup": ("util-linux", "util-linux", "util-linux"),
    "dd": ("coreutils", "coreutils", "coreutils"),
    "openssl": ("openssl", "openssl", "openssl"),
}


def find_tool(name: str) -> str | None:
    return shutil.which(name)


@dataclass
class DepStatus:
    name: str
    found: bool
    path: str | None
    hint: str


def check_tools(names) -> list[DepStatus]:
    out = []
    for n in names:
        path = find_tool(n)
        apt, dnf, pac = TOOL_HINTS.get(n, (n, n, n))
        hint = f"Debian/Ubuntu: `{APT.format(p=apt)}` | Fedora: `{DNF.format(p=dnf)}` | Arch: `{PACMAN.format(p=pac)}`"
        out.append(DepStatus(n, bool(path), path, hint))
    return out


def require_tools(names) -> None:
    missing = [d for d in check_tools(names) if not d.found]
    if missing:
        lines = [f"Missing required tools: {', '.join(d.name for d in missing)}"]
        lines += [f"  {d.name}: {d.hint}" for d in missing]
        raise MacUsbError("\n".join(lines))


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_root(what: str) -> None:
    if not is_root():
        raise MacUsbError(
            f"{what} needs root (loop devices / raw device writes). Re-run with sudo."
        )


def base_block_device(path: str) -> str:
    """/dev/sdb1 -> /dev/sdb ; leaves non-partition devices alone."""
    name = os.path.basename(path.rstrip("/"))
    sys_path = f"/sys/class/block/{name}"
    try:
        real = os.path.realpath(os.path.join(sys_path))
    except OSError:
        raise MacUsbError(f"{path}: not a block device")
    # If this is a partition, its sysfs path contains "<disk>/<partition>"
    disk = os.path.basename(os.path.dirname(real))
    parent = os.path.join("/sys/class/block", disk)
    if os.path.exists(os.path.join(real, "partition")) and os.path.exists(parent):
        return f"/dev/{disk}"
    return f"/dev/{name}"


def disk_sysfs_int(path: str, key: str):
    name = os.path.basename(path.rstrip("/"))
    try:
        with open(f"/sys/class/block/{name}/{key}") as fh:
            return int(fh.read().strip())
    except OSError:
        return None


def is_removable(path: str) -> bool:
    base = base_block_device(path)
    name = os.path.basename(base)
    rm = disk_sysfs_int(base, "removable")
    if rm == 1:
        return True
    # Walk up the sysfs tree: a usb-storage ancestor means removable.
    cur = f"/sys/class/block/{name}/device"
    for _ in range(8):
        uevent = os.path.join(cur, "uevent")
        if os.path.exists(uevent):
            try:
                data = open(uevent).read()
                if "DRIVER=usb-storage" in data or "DEVTYPE=usb_device" in data:
                    return True
            except OSError:
                pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


def device_size_bytes(path: str):
    base = base_block_device(path)
    name = os.path.basename(base)
    sz = disk_sysfs_int(base, "size")
    return sz * 512 if sz else None


def mounted_partitions(path: str) -> list[str]:
    import json

    base = base_block_device(path)
    cp = run(["lsblk", "-J", "-b", "-o", "NAME,MOUNTPOINT", base], capture=True, check=False)
    try:
        data = json.loads(cp.stdout or b"{}")
    except json.JSONDecodeError:
        return []
    mounts: list[str] = []

    def walk(node):
        mp = node.get("mountpoint")
        if mp:
            mounts.append(f"/dev/{node.get('name')} -> {mp}")
        for child in node.get("children") or []:
            walk(child)

    for dev in data.get("blockdevices", []):
        walk(dev)
    return mounts
