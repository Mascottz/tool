"""Resolution of macOS installer payloads from Apple's public software catalog.

Apple publishes merged "sucatalog" plists listing every software update. Inside
each product there are per-locale "Distributions" plists, which in turn list
"InstallInfo -> OSes" with URLs to the actual payloads:

  * recovery images:  .../BaseSystem.dmg        (internet recovery installer)
  * full installers:  .../InstallAssistant.pkg  (offline installer, macOS 11+;
                        older releases shipped as "Install macOS X.dmg")

This mirrors what gibMacOS and OpenCore's macrecovery.py do. URLs are
verified server-side signed requirements; we only download, never modify.
"""
from __future__ import annotations

import plistlib
import re
import ssl
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from .errors import MacUsbError

USER_AGENT = "macusb/0.1 (+https://github.com/Mascottz/tool)"

# Ordered by preference; both Apple CDN fronts serve the same content.
DEFAULT_CATALOGS = [
    "https://swscan.apple.com/content/catalogs/others/index-10.10-10.12-10.13-10.14-10.15-11-12-13-14-15.merged-1.sucatalog",
    "https://swcdn.apple.com/content/catalogs/others/index-10.10-10.12-10.13-10.14-10.15-11-12-13-14-15.merged-1.sucatalog",
    "https://swscan.apple.com/content/catalogs/others/index-10.15-10.14-10.13-10.12-10.11-10.10.merged-1.sucatalog",
]

RECOVERY = "recovery"
FULL = "full"
UNKNOWN = "unknown"


@dataclass
class PayloadEntry:
    kind: str            # recovery | full | unknown
    name: str            # e.g. "Install macOS Sonoma" or "macOS Recovery"
    version: str         # e.g. "14.7.1"
    build: str           # e.g. "23H222" (may be empty)
    post_date: Optional[datetime]
    url: str
    distribution_url: str = ""

    @property
    def sort_key(self):
        # version tuple, then date; robust to missing values
        try:
            ver = tuple(int(p) for p in re.split(r"[.\-]", self.version) if p.isdigit())
        except ValueError:
            ver = ()
        return (ver, self.post_date or datetime.min)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "build": self.build,
            "post_date": self.post_date.isoformat() if self.post_date else None,
            "url": self.url,
        }


def _urlopen(url: str, timeout: int = 60):
    # file:// URLs (used by tests and offline fixtures) skip SSL handling.
    if url.startswith("file://"):
        return urllib.request.urlopen(url, timeout=timeout)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())


def fetch_plist(url: str) -> dict:
    with _urlopen(url) as resp:
        raw = resp.read()
    try:
        data = plistlib.loads(raw)
    except Exception as exc:  # noqa: BLE001 - plistlib raises several exception types
        raise MacUsbError(f"Failed to parse plist from {url}: {exc}")
    if not isinstance(data, dict):
        raise MacUsbError(f"Unexpected plist shape from {url}")
    return data


def classify_payload(url: str) -> str:
    u = url.lower()
    if u.endswith(".dmg"):
        return RECOVERY
    if u.endswith(".pkg"):
        return FULL
    return UNKNOWN


def _iter_distribution_urls(dist) -> Iterable[str]:
    if isinstance(dist, str):
        yield dist
        return
    if isinstance(dist, dict):
        for v in dist.values():
            if isinstance(v, str):
                yield v


def _name_from_dist(dist: dict, url: str) -> str:
    # Distribution plists expose <key>Title</key> or we derive from the URL.
    for key in ("Title", "title"):
        v = dist.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    base = url.rsplit("/", 1)[-1]
    m = re.search(r"(Install[ _-]?macOS[^/]*)", base, re.IGNORECASE)
    return m.group(1) if m else "macOS installer"


def harvest_products(catalog: dict, distribution_fetcher=fetch_plist) -> list[PayloadEntry]:
    """Walk catalog -> distributions -> OS entries. Network-heavy; failures are
    skipped with a warning rather than aborting the whole listing."""
    entries: list[PayloadEntry] = []
    seen: set[tuple[str, str, str]] = set()
    products = catalog.get("Products", [])
    if not isinstance(products, list):
        raise MacUsbError("Catalog has no Products list; format may have changed.")

    from .util import warn  # local import to keep this module importable without side effects

    for product in products:
        if not isinstance(product, dict):
            continue
        post = product.get("PostDate")
        for dist_url in _iter_distribution_urls(product.get("Distributions")):
            try:
                dist = distribution_fetcher(dist_url)
            except Exception as exc:  # noqa: BLE001
                warn(f"skipping distribution {dist_url}: {exc}")
                continue
            for os_entry in _iter_os_entries(dist):
                url = str(os_entry.get("OSInstallation") or "").strip()
                if not url:
                    continue
                kind = classify_payload(url)
                if kind == UNKNOWN:
                    continue
                version = str(os_entry.get("OSVersion") or "").strip()
                build = str(os_entry.get("Build") or "").strip()
                # Some dists embed the post date per OS; fall back to product date.
                post_date = os_entry.get("PostDate") or post
                if isinstance(post_date, str):
                    try:
                        post_date = datetime.fromisoformat(post_date.replace("Z", "+00:00"))
                    except ValueError:
                        post_date = None
                elif not isinstance(post_date, datetime):
                    post_date = None
                key = (url, version, build)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    PayloadEntry(
                        kind=kind,
                        name=_name_from_dist(dist, url),
                        version=version or "unknown",
                        build=build,
                        post_date=post_date,
                        url=url,
                        distribution_url=dist_url,
                    )
                )
    entries.sort(key=lambda e: e.sort_key, reverse=True)
    return entries


def _iter_os_entries(dist: dict):
    install_info = dist.get("InstallInfo")
    if isinstance(install_info, dict):
        oses = install_info.get("OSes")
        if isinstance(oses, list):
            for o in oses:
                if isinstance(o, dict):
                    yield o


def list_catalog(catalog_url: Optional[str] = None) -> list[PayloadEntry]:
    urls = [catalog_url] if catalog_url else DEFAULT_CATALOGS
    last_err: Exception | None = None
    for url in urls:
        try:
            catalog = fetch_plist(url)
            return harvest_products(catalog)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise MacUsbError(
        "Could not fetch any Apple sucatalog.\n"
        f"Last error: {last_err}\n"
        "Check your network, or pass --catalog-url (or --url for a direct payload)."
    )


def version_matches(want: str, have: str) -> bool:
    """'14' matches '14.7.1'; '14.7' matches '14.7.1'; exact otherwise."""
    want_parts = [p for p in re.split(r"[.\-]", want.strip()) if p]
    have_parts = [p for p in re.split(r"[.\-]", have.strip()) if p]
    if not want_parts:
        return True
    return have_parts[: len(want_parts)] == want_parts


def select_entries(
    entries: list[PayloadEntry],
    *,
    version: Optional[str] = None,
    kind: Optional[str] = None,
    latest: bool = False,
) -> list[PayloadEntry]:
    out = entries
    if kind:
        out = [e for e in out if e.kind == kind]
    if version:
        out = [e for e in out if version_matches(version, e.version)]
    if latest:
        return out[:1]
    return out


def entries_from_url(url: str) -> list[PayloadEntry]:
    kind = classify_payload(url)
    return [
        PayloadEntry(
            kind=kind,
            name=url.rsplit("/", 1)[-1],
            version="custom",
            build="",
            post_date=None,
            url=url,
        )
    ]
