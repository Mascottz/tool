"""Fetch OpenCore (acidanthera) from GitHub releases and stage a real-Mac EFI.

Why this exists: on real Intel Macs the *faithful replica* scheme (HFS+ volume
+ .IABootFiles/boot.efi) usually boots, but firmware scanning is model- and
bless-dependent. Intel Mac firmware is plain UEFI, so a FAT32 EFI partition
holding BOOTx64.EFI (OpenCore) is a reliable boot path for the installer.
OpenCore itself is BSD-3-Clause and downloaded at runtime; nothing is
redistributed in this repo.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import urllib.request
import zipfile

from .errors import MacUsbError
from .util import info

API_LATEST = "https://api.github.com/repos/acidanthera/OpenCorePkg/releases/latest"
USER_AGENT = "macusb/0.1 (+https://github.com/Mascottz/tool)"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def latest_opencore_release() -> dict:
    return _get_json(API_LATEST)


def find_release_asset(release: dict) -> str:
    assets = release.get("assets", [])
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name.startswith("opencore-") and name.endswith("-release.zip"):
            return asset["browser_download_url"]
    names = ", ".join(a.get("name", "?") for a in assets)
    raise MacUsbError(f"No OpenCore RELEASE zip in latest GitHub release. Assets: {names}")


def stage_opencore(dest_dir: str) -> str:
    """Download + unzip OpenCore. Returns path to the extracted bundle root."""
    release = latest_opencore_release()
    url = find_release_asset(release)
    version = release.get("tag_name", "unknown")
    info(f"Downloading OpenCore {version} from GitHub...")
    os.makedirs(dest_dir, exist_ok=True)
    zip_path = os.path.join(dest_dir, "opencore.zip")
    _download(url, zip_path)
    extract_dir = os.path.join(dest_dir, "opencore-src")
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def find_efi_bundle(root: str) -> str:
    """Locate the x64 EFI directory inside the extracted release."""
    for dirpath, dirs, _files in os.walk(root):
        if os.path.basename(dirpath) == "EFI" and os.path.isdir(os.path.join(dirpath, "BOOT")):
            return dirpath
        for d in list(dirs):
            if d == "EFI" and os.path.isdir(os.path.join(dirpath, d)):
                cand = os.path.join(dirpath, d)
                if os.path.isdir(os.path.join(cand, "BOOT")):
                    return cand
    raise MacUsbError("Could not locate EFI bundle inside OpenCore release zip")


def find_hfs_driver(oc_dir: str) -> str | None:
    drivers = os.path.join(oc_dir, "OC", "Drivers")
    if not os.path.isdir(drivers):
        return None
    for name in os.listdir(drivers):
        if "hfs" in name.lower():
            return name
    return None


def _find_sample_plist(root: str) -> str | None:
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name in ("Sample.plist", "SampleFull.plist"):
                return os.path.join(dirpath, name)
    return None


def write_config_plist(extract_root: str, oc_dir: str) -> str:
    """Produce EFI/OC/config.plist tuned for a *real* Intel Mac.

    Strategy: start from the Sample.plist shipped in the release (always matches
    the release's schema), then patch minimally:
      * UEFI -> Drivers: enable only the HFS+ driver (plus bundled helpers)
      * PlatformInfo: do NOT spoof SMBIOS (this is real Apple hardware);
        leave Update* flags off so the firmware's real SMBIOS is kept
      * Misc -> Boot: short picker timeout, verbose boot args for debuggability
    """
    sample = _find_sample_plist(extract_root)
    config_path = os.path.join(oc_dir, "OC", "config.plist")

    if sample:
        with open(sample, "rb") as fh:
            cfg = plistlib.load(fh)
    else:
        cfg = {}

    hfs = find_hfs_driver(oc_dir)
    drivers = []
    bundled = []
    drivers_dir = os.path.join(oc_dir, "OC", "Drivers")
    if os.path.isdir(drivers_dir):
        bundled = sorted(os.listdir(drivers_dir))
    if hfs:
        drivers.append(hfs)
    for keep in ("OpenUsbKbDxe.efi", "OpenPartitionDxe.efi", "ResetNvramEntry.efi"):
        if keep in bundled and keep not in drivers:
            drivers.append(keep)

    uefi = cfg.setdefault("UEFI", {})
    if drivers:
        uefi["Drivers"] = drivers

    pi = cfg.setdefault("PlatformInfo", {})
    pi["Automatic"] = False
    pi["UpdateDataHub"] = False
    pi["UpdateNVRAM"] = False
    pi["UpdateSMBIOS"] = False
    pi["UpdateSMBIOSMode"] = "Custom"
    generic = pi.setdefault("Generic", {})
    # Placeholders only; they are NOT applied because UpdateSMBIOS=false.
    generic.setdefault("SystemProductName", "iMacPro1,1")
    generic.setdefault("SystemSerialNumber", "W00000000000")
    generic.setdefault("SystemUUID", "00000000-0000-1000-8000-000000000000")
    generic.setdefault("MLB", "M0000000000000000")
    generic.setdefault("ROM", b"\x00" * 6)

    misc = cfg.setdefault("Misc", {})
    boot = misc.setdefault("Boot", {})
    boot["ShowPicker"] = True
    boot["PickerTimeout"] = 5
    debug = misc.setdefault("Debug", {})
    debug["Target"] = 67  # console + file logging: invaluable when a Mac says "nope"

    nvram = cfg.setdefault("NVRAM", {})
    nvram_vars = nvram.setdefault("Add", {})
    nvram_vars["7C436110-AB2A-4BBB-A880-FE41995C9F82"] = {"boot-args": "-v keepsyms=1"}

    with open(config_path, "wb") as fh:
        plistlib.dump(cfg, fh)
    return config_path


def prepare_efi_partition_tree(dest_root: str, workdir: str) -> str:
    """Download OpenCore and lay out an EFI/ tree ready to copy to FAT32."""
    extract_root = stage_opencore(os.path.join(workdir, "opencore-dl"))
    efi_src = find_efi_bundle(extract_root)
    efi_dst = os.path.join(dest_root, "EFI")
    if os.path.isdir(efi_dst):
        shutil.rmtree(efi_dst)
    shutil.copytree(efi_src, efi_dst)
    oc_dir = efi_dst
    write_config_plist(extract_root, oc_dir)
    if find_hfs_driver(oc_dir) is None:
        info("NOTE: OpenCore release ships no HFS+ driver; the HFS+ installer "
             "volume will rely on firmware drivers instead.")
    return dest_root
