"""macusb command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import __version__
from . import catalog as cat
from .download import download
from .errors import MacUsbError
from .imagebuild import build_disk_image, build_iso, write_manifest
from .payload import (extract_dmg_tree, extract_full_installer,
                      find_support_dmg)
from .usb import guard_target, list_removable, unmount_all, write_image
from .util import check_tools, human, info, is_root, require_root


# ---------------------------------------------------------------- helpers

def default_workdir() -> str:
    return os.path.abspath(os.environ.get("MACUSB_WORKDIR", "./macusb-work"))


def _add_source_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("installer sources (pick one style)")
    g.add_argument("--tree", help="Pre-extracted installer tree (contains System/ ...). Skips all conversion")
    g.add_argument("--basesystem-dmg", help="Path to BaseSystem.dmg / InstallESD.dmg (recovery or Catalina-)")
    g.add_argument("--pkg", help="Path to InstallAssistant.pkg (full offline installer, macOS 11+)")
    g.add_argument("--app", help="Path to an extracted 'Install macOS X.app' bundle")
    g.add_argument("--manifest", help="Path to a fetch() manifest dir (use after `macusb fetch`)")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workdir", default=default_workdir(),
                   help="Scratch dir for downloads and intermediate files [default: ./macusb-work or $MACUSB_WORKDIR]")


def _resolve_tree(args) -> tuple[str, str | None, dict]:
    """Turn CLI source flags into (tree_root, app_dir|None, metadata)."""
    meta: dict = {"sources": {}}
    wd = args.workdir
    os.makedirs(wd, exist_ok=True)

    srcs = [bool(getattr(args, k, None)) for k in ("tree", "basesystem_dmg", "pkg", "app", "manifest")]
    if sum(srcs) > 1:
        raise MacUsbError("Pass only one of --tree, --basesystem-dmg, --pkg, --app, --manifest")
    if not any(srcs):
        raise MacUsbError("No installer source given. Pass one of --tree/--basesystem-dmg/--pkg/--app/--manifest "
                          "(or run `macusb fetch` first and pass its directory via --manifest).")

    if args.tree:
        info(f"Using pre-extracted tree: {args.tree}")
        return os.path.abspath(args.tree), None, meta

    if args.manifest:
        man_dir = os.path.abspath(args.manifest)
        man_path = os.path.join(man_dir, "manifest.json")
        if not os.path.isfile(man_path):
            raise MacUsbError(f"No manifest.json in {man_dir}")
        with open(man_path) as fh:
            man = json.load(fh)
        rec = next((p for p in man.get("payloads", []) if p["kind"] == cat.RECOVERY), None)
        full = next((p for p in man.get("payloads", []) if p["kind"] == cat.FULL), None)
        if full:
            args.pkg = full["path"]
        elif rec:
            args.basesystem_dmg = rec["path"]
        else:
            raise MacUsbError("manifest.json lists no payloads")
        meta["sources"]["manifest"] = man_path

    if args.pkg:
        info("Extracting InstallAssistant.pkg ...")
        app = extract_full_installer(os.path.abspath(args.pkg), wd)
        info(f"Found installer app: {app}")
        args.app = app

    if args.app:
        app_dir = os.path.abspath(args.app)
        dmg = find_support_dmg(app_dir)
        info(f"Found SharedSupport image: {dmg}")
        tree = extract_dmg_tree(dmg, os.path.join(wd, "tree"))
        meta["sources"]["app"] = app_dir
        return tree, app_dir, meta

    if args.basesystem_dmg:
        tree = extract_dmg_tree(os.path.abspath(args.basesystem_dmg), os.path.join(wd, "tree"))
        meta["sources"]["basesystem_dmg"] = os.path.abspath(args.basesystem_dmg)
        return tree, None, meta

    raise MacUsbError("Internal error resolving sources")  # pragma: no cover


# ------------------------------------------------------------- commands

def cmd_doctor(_args) -> int:
    print(f"macusb {__version__}")
    print(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
    print(f"root:   {'yes' if is_root() else 'no (needed for image build / USB write)'}")
    hfsplus = os.path.exists("/sys/module/hfsplus")
    print(f"hfs+ kernel module loaded: {'yes' if hfsplus else 'unknown/not loaded'} (`sudo modprobe hfsplus`)")
    print()
    statuses = check_tools(["dmg2img", "sgdisk", "xorriso", "mkfs.hfsplus", "mformat",
                            "mcopy", "bsdtar", "7z", "mount", "losetup", "lsblk", "dd", "sync", "umount"])
    missing = 0
    for s in statuses:
        mark = "ok " if s.found else "MISSING"
        if not s.found:
            missing += 1
        print(f"[{mark}] {s.name:14s} {s.path or ''}")
        if not s.found:
            print(f"        install: {s.hint}")
    print()
    if missing:
        print(f"{missing} tool(s) missing. Core download/list works without them; building images does not.")
        return 1
    print("All tools present.")
    return 0


def cmd_list(args) -> int:
    entries = cat.list_catalog(args.catalog_url)
    if args.kind:
        entries = [e for e in entries if e.kind == args.kind]
    if args.json:
        print(json.dumps([e.as_dict() for e in entries], indent=2))
        return 0
    if not entries:
        print("No entries found (catalog reachable but nothing matched).")
        return 0
    hdr = f"{'kind':9} {'version':10} {'build':9} {'post-date':11} url"
    print(hdr)
    print("-" * len(hdr))
    for e in entries:
        pd = e.post_date.date().isoformat() if e.post_date else "?"
        print(f"{e.kind:9} {e.version:10} {e.build:9} {pd:11} {e.url}")
    return 0


def _select_for_fetch(args) -> list:
    if args.url:
        return cat.entries_from_url(args.url)
    entries = cat.list_catalog(args.catalog_url)
    kinds = ["recovery", "full"] if args.payload == "both" else [args.payload]
    picked: list = []
    for kind in kinds:
        subset = [e for e in entries if e.kind == kind]
        subset = cat.select_entries(subset, version=args.version,
                                    latest=not args.version)
        picked.extend(subset)
    if not picked:
        raise MacUsbError(
            f"No payload matched (payload={args.payload}, version={args.version or 'any'}). "
            "Run `macusb list` to see what Apple publishes, or pass --url with a direct link."
        )
    return picked


def cmd_fetch(args) -> int:
    _common = args.workdir
    dl_dir = os.path.join(_common, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    entries = _select_for_fetch(args)
    wanted: dict[str, object] = {}
    for e in entries:
        wanted.setdefault(e.kind, e)  # entries are sorted newest-first
    picked = list(wanted.values())

    results = []
    for e in picked:
        ext = ".dmg" if e.kind == cat.RECOVERY else ".pkg"
        safe_ver = e.version.replace("/", "_")
        fname = f"macOS-{e.kind}-{safe_ver}{('-' + e.build) if e.build else ''}{ext}"
        dest = os.path.join(dl_dir, fname)
        info(f"Fetching {e.kind} payload: {e.url}")
        res = download(e.url, dest)
        info(f"Saved {dest} ({human(res.bytes_written)}) sha256={res.sha256}")
        d = e.as_dict()
        d.update({"path": dest, "sha256": res.sha256, "bytes": res.bytes_written})
        results.append(d)

    manifest = {
        "created": datetime.now(timezone.utc).isoformat(),
        "macusb_version": __version__,
        "payloads": results,
    }
    man_dir = os.path.join(_common, "fetch-latest")
    os.makedirs(man_dir, exist_ok=True)
    man_path = os.path.join(man_dir, "manifest.json")
    write_manifest(man_path, manifest)
    info(f"Manifest: {man_path}")
    info("Next steps: `macusb build-image --manifest ./macusb-work/fetch-latest --boot-scheme apple`")
    return 0


def cmd_build_iso(args) -> int:
    tree, app_dir, meta = _resolve_tree(args)
    out = args.out or os.path.join(args.workdir, "macos-installer.iso")
    build_iso(tree_root=tree, app_dir=app_dir, out_iso=os.path.abspath(out),
              volume_id=args.volume_id)
    write_manifest(out + ".manifest.json", {"created": datetime.now(timezone.utc).isoformat(),
                                            **meta})
    return 0


def cmd_build_image(args) -> int:
    tree, app_dir, meta = _resolve_tree(args)
    out = args.out or os.path.join(args.workdir, f"macos-installer-{args.boot_scheme}.img")
    require_root("Building the HFS+ disk image")
    res = build_disk_image(tree_root=tree, app_dir=app_dir,
                           out_img=os.path.abspath(out),
                           boot_scheme=args.boot_scheme,
                           label=args.label,
                           workdir=os.path.join(args.workdir, "image-build"))
    res.update({"created": datetime.now(timezone.utc).isoformat(), **meta})
    write_manifest(out + ".manifest.json", res)
    info(f"Done. Write it with: sudo macusb write-usb --image {out} --device /dev/sdX --yes")
    return 0


def cmd_write_usb(args) -> int:
    if args.image:
        image = os.path.abspath(args.image)
        if not os.path.isfile(image):
            raise MacUsbError(f"No such image: {image}")
    else:
        tree, app_dir, _meta = _resolve_tree(args)
        image = os.path.join(args.workdir, f"macos-installer-{args.boot_scheme}.img")
        require_root("Building the HFS+ disk image")
        build_disk_image(tree_root=tree, app_dir=app_dir, out_img=image,
                         boot_scheme=args.boot_scheme, label=args.label,
                         workdir=os.path.join(args.workdir, "image-build"))
    if args.umount:
        unmount_all(args.device)
    device = guard_target(args.device, allow_internal=args.allow_internal,
                          assume_yes=args.yes)
    write_image(device, image, verify=not args.no_verify)
    return 0


def cmd_install_usb(args) -> int:
    # fetch -> build -> write, in one shot
    rc = cmd_fetch(args)
    if rc != 0:
        return rc
    args.manifest = os.path.join(args.workdir, "fetch-latest")
    args.image = None
    args.basesystem_dmg = args.pkg = args.app = args.tree = None
    return cmd_write_usb(args)


def cmd_devices(_args) -> int:
    devs = list_removable()
    if not devs:
        print("No removable/USB disks found.")
        return 0
    for d in devs:
        print(f"{d['device']:10} {human(d['size']) if d['size'] else '?':>10}  "
              f"{d['tran']:5} {d['vendor']} {d['model']}".rstrip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="macusb",
        description="Create bootable macOS USB installers (Intel Macs) on Linux. "
                    "Fetches payloads from Apple's CDN at run time; never ships Apple binaries.",
    )
    p.add_argument("--version", action="version", version=f"macusb {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="Check tools, root, and kernel HFS+ support")
    d.set_defaults(func=cmd_doctor)

    d = sub.add_parser("list", help="List macOS payloads published in Apple's catalog")
    d.add_argument("--catalog-url", help="Override the sucatalog URL (debugging / mirrors)")
    d.add_argument("--kind", choices=["recovery", "full"], help="Only show one payload kind")
    d.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    d.set_defaults(func=cmd_list)

    d = sub.add_parser("fetch", help="Download the macOS installer payload(s) from Apple")
    d.add_argument("--version", dest="version", help="macOS version prefix, e.g. '14', '14.7', '15.1'. Default: newest")
    d.add_argument("--payload", choices=["recovery", "full", "both"], default="both")
    d.add_argument("--url", help="Direct payload URL (bypasses catalog discovery)")
    d.add_argument("--catalog-url", help="Override the sucatalog URL")
    _add_common(d)
    d.set_defaults(func=cmd_fetch)

    d = sub.add_parser("build-iso", help="Build a hybrid ISO (VMs / archival)")
    _add_source_args(d)
    _add_common(d)
    d.add_argument("--out", help="Output .iso path [default: workdir/macos-installer.iso]")
    d.add_argument("--volume-id", default="MACOS_INSTALL")
    d.set_defaults(func=cmd_build_iso)

    d = sub.add_parser("build-image", help="Build a raw GPT image for USB (HFS+ [+ EFI])")
    _add_source_args(d)
    _add_common(d)
    d.add_argument("--out", help="Output .img path [default: workdir/macos-installer-<scheme>.img]")
    d.add_argument("--boot-scheme", choices=["apple", "opencore"], default="apple",
                   help="apple = createinstallmedia-style HFS+ volume (default). "
                        "opencore = FAT32 EFI partition booting OpenCore + HFS+ installer (fallback for stubborn UEFI).")
    d.add_argument("--label", default="Install macOS", help="HFS+ volume label")
    d.set_defaults(func=cmd_build_image)

    d = sub.add_parser("write-usb", help="Build (or take --image) and write to a USB stick")
    _add_source_args(d)
    _add_common(d)
    d.add_argument("--image", help="Prebuilt .img to write (skips building)")
    d.add_argument("--boot-scheme", choices=["apple", "opencore"], default="apple")
    d.add_argument("--label", default="Install macOS")
    d.add_argument("--device", required=True, help="Whole-disk device, e.g. /dev/sdb (NOT a partition)")
    d.add_argument("--yes", action="store_true", help="Skip the interactive device-name confirmation (automation)")
    d.add_argument("--umount", action="store_true", help="Unmount all partitions of the device before writing")
    d.add_argument("--allow-internal", action="store_true", help="Dangerous: allow non-removable targets")
    d.add_argument("--no-verify", action="store_true", help="Skip post-write GPT signature check")
    d.set_defaults(func=cmd_write_usb)

    d = sub.add_parser("install-usb", help="fetch + build + write in one shot")
    _add_source_args(d)
    _add_common(d)
    d.add_argument("--version", dest="version", help="macOS version prefix. Default: newest")
    d.add_argument("--payload", choices=["recovery", "full", "both"], default="full")
    d.add_argument("--url", help="Direct payload URL (bypasses catalog discovery)")
    d.add_argument("--catalog-url", help="Override the sucatalog URL")
    d.add_argument("--image", help=argparse.SUPPRESS)  # set internally
    d.add_argument("--boot-scheme", choices=["apple", "opencore"], default="apple")
    d.add_argument("--label", default="Install macOS")
    d.add_argument("--device", required=True)
    d.add_argument("--yes", action="store_true")
    d.add_argument("--umount", action="store_true")
    d.add_argument("--allow-internal", action="store_true")
    d.add_argument("--no-verify", action="store_true")
    d.add_argument("--volume-id", default="MACOS_INSTALL")
    d.set_defaults(func=cmd_install_usb)

    d = sub.add_parser("devices", help="List removable/USB disks")
    d.set_defaults(func=cmd_devices)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MacUsbError as exc:
        print(f"\nmacusb: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nmacusb: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
