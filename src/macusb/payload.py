"""Turn downloaded Apple payloads into an extracted installer tree.

Layouts differ by payload type / macOS era:

  recovery (BaseSystem.dmg):
      dmg2img -> raw HFS+ image -> extract/mount -> { System/, .IABootFiles?, ... }
  full installer (InstallAssistant.pkg, macOS 11+):
      deep-extract xar -> cpio payload -> "Install macOS X.app/"
      app -> Contents/SharedSupport/SharedSupport.dmg (Big Sur+) or InstallESD.dmg

Older full installers shipped as "Install macOS X.dmg" containing the .app
directly; we handle that too.
"""
from __future__ import annotations

import glob
import os
import shutil

from .errors import MacUsbError
from .util import info, require_tools, run

# ---------------------------------------------------------------- dmg handling

def dmg_to_raw(dmg_path: str, raw_path: str) -> str:
    """Convert an (unencrypted, UDZO/UDIF) DMG to a raw image."""
    require_tools(["dmg2img"])
    if os.path.exists(raw_path):
        os.remove(raw_path)
    run(["dmg2img", "-i", dmg_path, "-o", raw_path])
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        raise MacUsbError(f"dmg2img produced no image for {dmg_path}")
    return raw_path


# -------------------------------------------------------------- pkg handling

def _deep_extract(pkg_path: str, dest_dir: str, depth: int = 4) -> str:
    """Recursively extract xar/pkg/cpio archives into dest_dir.

    bsdtar understands xar and cpio(.gz). 7z is the fallback. We stop when no
    more archives remain or depth is exhausted.
    """
    os.makedirs(dest_dir, exist_ok=True)
    current = pkg_path
    for level in range(depth):
        level_dir = os.path.join(dest_dir, f"level{level}")
        os.makedirs(level_dir, exist_ok=True)
        ok = False
        for tool in (("bsdtar", ["bsdtar", "-xf", current, "-C", level_dir]),
                     ("7z", ["7z", "x", "-y", f"-o{level_dir}", current])):
            name, cmd = tool
            if shutil.which(name):
                try:
                    run(cmd, capture=True)
                    ok = True
                    break
                except MacUsbError:
                    continue
        if not ok:
            raise MacUsbError(
                f"Could not extract {pkg_path}. Install bsdtar (`libarchive-tools`) "
                f"or `p7zip-full` and retry."
            )
        found = _find_first_archive(level_dir)
        if found is None:
            return level_dir
        current = found
    return dest_dir


_ARCHIVE_EXTS = (".pkg", ".payload", "pay load", "")


def _find_first_archive(root: str) -> str | None:
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            lower = name.lower()
            if lower in ("payload",) or lower.endswith((".pkg", ".payload")):
                return os.path.join(dirpath, name)
    return None


def extract_full_installer(pkg_path: str, dest_dir: str) -> str:
    """Extract InstallAssistant.pkg -> find the 'Install macOS*.app' bundle."""
    final_dir = _deep_extract(pkg_path, os.path.join(dest_dir, "pkg-extract"))
    app = find_app(final_dir)
    if app is None:
        # The .app may itself be nested deeper than our walk-found archives reached.
        app = find_app(dest_dir)
    if app is None:
        raise MacUsbError(
            f"Extracted {pkg_path} but found no 'Install macOS*.app' inside. "
            "Apple may have changed the package layout (please open an issue "
            "with the swcdn URL you used)."
        )
    return app


def find_app(root: str) -> str | None:
    best = None
    for dirpath, dirs, _files in os.walk(root):
        for d in dirs:
            if d.lower().startswith("install macos") and d.endswith(".app"):
                cand = os.path.join(dirpath, d)
                if best is None or len(cand) < len(best):
                    best = cand
    return best


def find_support_dmg(app_path: str) -> str:
    shared = os.path.join(app_path, "Contents", "SharedSupport")
    for name in ("InstallESD.dmg", "SharedSupport.dmg", "BaseSystem.dmg"):
        cand = os.path.join(shared, name)
        if os.path.exists(cand):
            return cand
    raise MacUsbError(f"No SharedSupport DMG found in {app_path}")


# ---------------------------------------------------- extraction of hfs trees

def extract_dmg_tree(dmg_path: str, dest_dir: str) -> str:
    """Extract the *contents* of a DMG containing an HFS+ filesystem.

    Preferred: dmg2img -> 7z/unar extract of the raw HFS+ image (works without
    root). Falls back to 'mount -o loop,ro' when available and permitted.
    """
    raw = dmg_path.rsplit(".dmg", 1)[0] + ".img"
    dmg_to_raw(dmg_path, raw)
    os.makedirs(dest_dir, exist_ok=True)
    # 7z can read raw HFS+ images directly in most builds.
    if shutil.which("7z"):
        try:
            run(["7z", "x", "-y", f"-o{dest_dir}", raw], capture=True)
            if os.listdir(dest_dir):
                info(f"Extracted {os.path.basename(raw)} with 7z")
                return dest_dir
        except MacUsbError:
            info("7z could not read the HFS+ image; falling back to loop mount")
    # Fallback: loop mount (needs root and kernel hfs+ support)
    if shutil.which("mount"):
        mnt = dest_dir.rstrip("/") + ".mnt"
        os.makedirs(mnt, exist_ok=True)
        try:
            run(["mount", "-o", "loop,ro", raw, mnt])
        except MacUsbError as exc:
            raise MacUsbError(
                f"Could not extract {dmg_path}. Install 7z (`p7zip-full`) for "
                f"userspace extraction, or re-run as root so the image can be "
                f"loop-mounted read-only. Underlying error: {exc}"
            )
        try:
            for item in os.listdir(mnt):
                shutil.move(os.path.join(mnt, item), os.path.join(dest_dir, item))
        finally:
            run(["umount", mnt], check=False)
        return dest_dir
    raise MacUsbError(
        f"Could not extract {dmg_path}: neither 7z nor mount is available."
    )


# ------------------------------------------------------------- boot artifacts

IA_BOOTFILES = ".IABootFiles"
BOOT_EFI_CANDIDATES = [
    os.path.join("System", "Library", "CoreServices", "boot.efi"),
    os.path.join("usr", "standalone", "i386", "boot.efi"),
    os.path.join("EFI", "BOOT", "BOOTx64.EFI"),
]


def find_boot_efi(tree_root: str) -> str | None:
    for rel in BOOT_EFI_CANDIDATES:
        cand = os.path.join(tree_root, rel)
        if os.path.exists(cand):
            return cand
    return None


def stage_ia_bootfiles(volume_root: str, boot_efi_src: str) -> str:
    """Create <volume_root>/.IABootFiles/boot.efi (what createinstallmedia does)."""
    dest_dir = os.path.join(volume_root, IA_BOOTFILES)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "boot.efi")
    shutil.copy2(boot_efi_src, dest)
    os.chmod(dest, 0o755)
    return dest
