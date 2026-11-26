# tool
# macusb

Create **bootable macOS USB installers for Intel Macs** on Linux — no Mac required to *create* the media (you still need an Intel Mac to *run* it; see "Target reality check" below).

`macusb` fetches the official installer payloads **from Apple's own CDN at run time**, converts them, and writes a bootable raw GPT image to a USB stick:

1. `macusb list` — see which macOS versions Apple currently publishes (recovery images & full offline installers).
2. `macusb fetch` — download the payload(s) (resumable, SHA-256 logged, never redistributed).
3. `macusb build-image` — assemble a raw GPT image:
   - **`apple` scheme** (default): single HFS+ partition replicating what `createinstallmedia` produces — payload tree + installer `.app` + `.IABootFiles/boot.efi`.
   - **`opencore` scheme** (fallback): FAT32 EFI partition with [OpenCore](https://github.com/acidanthera/OpenCorePkg) (fetched from GitHub, BSD-3-Clause) + the same HFS+ installer partition. Intel Mac firmware is plain UEFI, so this path is the most predictable fallback when the volume doesn't show up in the Option-boot picker.
4. `macusb write-usb` — write it to a stick, with heavy safety guard rails.

Also produces a **hybrid ISO** (`macusb build-iso`) for VMs (QEMU/KVM) and archival. Note: real Intel Macs boot USB from raw GPT images, **not** ISOs; and `.dmg` output is not produced — Linux can't create Apple UDIF disk images reliably, and a raw `.img` (`dd`-able, mountable on macOS) is what you actually want for sticks.

## Target reality check

| Target | Status |
|---|---|
| **Intel Mac, internet recovery install** | Works. `BaseSystem.dmg` (≈2–3 GB) + network during install. The most reliable payload. |
| **Intel Mac, full offline installer** | Should work. Replicates `createinstallmedia` layout. Use a **≥32 GB** stick (payload tree + `.app` coexist). For ≤ Catalina the layout is well understood; for Big Sur+ the layout changed (SharedSupport.dmg) and this path is *less proven on real hardware* — test and report. |
| **Apple Silicon Mac** | ❌ Not possible from Linux. Requires a Mac + Apple Configurator 2 / `createinstallmedia`. Out of scope, by design. |
| **Hackintosh** | The same images work with OpenCore/Clover in the usual way; not the primary target and not legally endorsed by Apple. |

> **Honest limitation:** this tool cannot bless HFS+ volumes the way macOS does (there is no `bless` on Linux, and Apple's boot-picker scanning details are undocumented). The `apple` scheme relies on the firmware finding `.IABootFiles/boot.efi` / standard paths, which is how real installers are commonly replicated off-Mac. If your Mac's Option-boot picker doesn't show the stick, rebuild with `--boot-scheme opencore`. Neither scheme has been verified by the author on physical hardware — **you are the test bench.** Start with recovery, which is far more forgiving.

## Legal

- `macusb` **never ships or redistributes Apple software**. It downloads payloads from Apple's CDN on *your* machine, for your own use, exactly like gibMacOS / `macrecovery.py`.
- Apple's SLA licenses macOS for Apple hardware. Using it elsewhere (Hackintosh) violates that SLA; that's between you and Apple.
- OpenCore is fetched from its public GitHub releases under its own (BSD-3-Clause) license.
- No warranty. Erasing a USB stick is intentional; erasing the wrong disk is prevented by guard rails — but they are not magic. Double-check `macusb devices` output.

## Requirements

Linux (x86_64), Python ≥ 3.9, and these packages:

| Need | Debian/Ubuntu | Fedora | Arch |
|---|---|---|---|
| Everything below | `dmg2img gdisk xorriso hfsprogs mtools libarchive-tools p7zip-full cpio dosfstools util-linux` | `dmg2img gdisk xorriso hfsprogs mtools libarchive cpio dosfstools util-linux` | `dmg2img gptfdisk libisoburn hfsprogs mtools libarchive cpio dosfstools util-linux` |

Plus: `root` for building the image and writing the stick, kernel HFS+ support (`sudo modprobe hfsplus`), and a ≥16–32 GB USB 3 stick.

Quick check: `macusb doctor`

Install itself: `pip install -e .` (or `pipx install .`), then `macusb --help`.

## Usage

```bash
# What's available? (kind: recovery | full)
macusb list
macusb list --kind full --json

# Fetch newest of everything (or pin: --version 15 --payload full)
macusb fetch --version 15.1 --payload full
# -> ./macusb-work/downloads/macOS-full-15.1-<build>.pkg
# -> ./macusb-work/fetch-latest/manifest.json  (paths + sha256)

# Build the raw USB image (apple scheme)
sudo modprobe hfsplus
sudo macusb build-image --manifest ./macusb-work/fetch-latest \
    --boot-scheme apple --out ~/macos-sonoma.img

# …or the OpenCore fallback scheme
sudo macusb build-image --manifest ./macusb-work/fetch-latest \
    --boot-scheme opencore --out ~/macos-sonoma-oc.img

# Write it (destructive!)
macusb devices                      # confirm WHICH /dev/sdX is the stick
sudo macusb write-usb --image ~/macos-sonoma.img --device /dev/sdb

# All-in-one:
sudo macusb install-usb --version 15 --payload full \
    --device /dev/sdb --boot-scheme apple
```

On the Mac: **power off → insert stick → hold Option (Alt) while powering on → pick the macOS installer.** If nothing appears, try the `opencore` scheme image. To restore/refresh macOS on an Intel Mac without any stick, you can also boot internet recovery (Cmd+R) directly on the machine.

### Recovery vs full

- **recovery** (`BaseSystem.dmg`, 2–3 GB): small, fast, always current for your model's compatibility; the Mac downloads everything else during install. Best first attempt.
- **full** (`InstallAssistant.pkg`, ~12–13 GB, macOS 11+): self-contained offline install. Needs extraction of the pkg (`bsdtar` or `7z`), and a ≥32 GB stick after repackaging.

### Overrides for when Apple moves the goalposts

```bash
macusb list --catalog-url https://swscan.apple.com/content/catalogs/others/<current-file>.sucatalog
macusb fetch --url https://swcdn.apple.com/.../InstallAssistant.pkg --payload full
```

Catalog filenames and CDN endpoints change over time (gibMacOS tracks them); both are overridable so the tool survives rot. If `list` breaks for everyone, the default URLs in `src/macusb/catalog.py` need a bump.

## How it works / repo layout

```
src/macusb/
  catalog.py     # sucatalog discovery: Products -> Distributions -> InstallInfo.OSes
  download.py    # resumable (.part), atomic, sha256-logged downloads
  payload.py     # dmg2img, InstallAssistant.pkg deep-extract, .app discovery, .IABootFiles
  imagebuild.py  # GPT layout math, FAT32 (mtools) + HFS+ (loop mount) partition images,
                 # sparse raw .img assembly via sgdisk+dd, hybrid ISO via xorriso
  opencore.py    # OpenCore GitHub release fetch, real-Mac config.plist generation
  usb.py         # removable-device detection, umount guard, dd + GPT signature verify
  cli.py         # argparse CLI: doctor/list/fetch/build-iso/build-image/write-usb/install-usb/devices
tests/           # pytest, fully offline (fixtures + file:// URLs); no network or root required
```

## Development

```bash
pip install -e . pytest
python3 -m pytest -q        # 22 tests, offline
macusb doctor
```

What the unit tests **don't** cover: actual Apple CDN reachability, real `InstallAssistant.pkg` layouts (Apple changes them), HFS+ mount behavior on every kernel, and — obviously — whether a given Intel Mac displays the stick in the Option-boot picker. Those need real machines; PRs with findings welcome.

## Known limitations & roadmap

- No `.dmg` output (UDIF creation isn't sane on Linux); `.img`/ISO only — better fit for sticks anyway.
- No Apple Silicon support (see table). No macOS-only binaries anywhere in the pipeline.
- No `bless`-equivalent: relies on firmware auto-scan; use `--boot-scheme opencore` when in doubt.
- Full-installer layout for Big Sur+ needs on-hardware validation.
- Catalog default URLs will drift; `--catalog-url` / `--url` are the escape hatches.
- Roadmap: APFS output (needs `apfs-fuse`/future tooling), signed-installer preflight checks, `--verify-payload` (Apple provides no public hashes, so this can only mean structural checks), Linux→VM one-shot runner.

## Credits

- [gibMacOS](https://github.com/CorpNewt/gibMacOS) — the reference for catalog parsing.
- OpenCore & [`macrecovery.py`](https://github.com/acidanthera/opencorepkg) — recovery payload discovery and OpenCore itself.
- [OSX-KVM](https://github.com/kholia/OSX-KVM) — proof that Linux-side repackaging works at all.
