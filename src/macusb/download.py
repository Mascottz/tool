"""Resumable, integrity-checked downloader for Apple CDN payloads."""
from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from dataclasses import dataclass

from .errors import MacUsbError
from .util import human, info

USER_AGENT = "macusb/0.1 (+https://github.com/Mascottz/tool)"
_CHUNK = 1024 * 1024  # 1 MiB


@dataclass
class DownloadResult:
    path: str
    bytes_written: int
    resumed: bool
    sha256: str


def _expected_size(resp) -> int | None:
    for h in ("Content-Length", "content-length"):
        v = resp.headers.get(h) if hasattr(resp, "headers") else None
        if v:
            try:
                return int(v)
            except ValueError:
                pass
    return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: str, sha256: str | None = None, resume: bool = True,
             timeout: int = 120) -> DownloadResult:
    """Download url -> dest with resume via `<dest>.part` and atomic rename.

    file:// URLs are supported (used by tests and offline fixtures); Range is
    ignored there, in which case we restart from scratch rather than corrupt.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    part = dest + ".part"
    is_file_url = url.startswith("file://")

    part_size = os.path.getsize(part) if (resume and os.path.exists(part)) else 0
    headers = {"User-Agent": USER_AGENT}
    mode = "wb"
    resumed = False
    if part_size > 0 and not is_file_url:
        headers["Range"] = f"bytes={part_size}-"
        mode = "ab"
        resumed = True
    else:
        part_size = 0  # restart cleanly (no resume for file:// or missing .part)

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise MacUsbError(f"Download failed for {url}: {exc}") from exc

    with resp:
        status = getattr(resp, "status", 200)
        if resumed and status != 206:
            # Server ignored Range; restart cleanly.
            mode = "wb"
            resumed = False
            part_size = 0
        total = _expected_size(resp)
        if total is not None and mode == "ab":
            total += part_size
        written = part_size
        last_log = time.time()
        with open(part, mode) as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last_log >= 2.0:
                    pct = f" ({written / total * 100:.0f}%)" if total else ""
                    info(f"downloaded {human(written)}{pct}...")
                    last_log = now

    if total is not None and written != total:
        raise MacUsbError(
            f"Truncated download: got {human(written)}, expected {human(total)}. "
            f"Re-run the same command to resume from the .part file."
        )

    digest = sha256_file(part)
    if sha256 and digest.lower() != sha256.lower():
        raise MacUsbError(
            f"SHA-256 mismatch for {os.path.basename(dest)}: got {digest}, expected {sha256}. "
            f"The .part file was kept at {part} for inspection; delete it to retry."
        )
    os.replace(part, dest)
    return DownloadResult(dest, written, resumed, digest)
