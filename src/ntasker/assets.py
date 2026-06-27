"""Vendor-asset manifest + URL/SRI helpers.

ntasker's UI depends on a small set of third-party static assets
(Tabler core CSS, Tabler-Icons webfont, Alpine.js). Two delivery modes
are supported:

* ``cdn``    -- assets are loaded from jsDelivr at runtime; SRI hashes
  pinned in :data:`MANIFEST` guarantee bit-identical content. This is
  the **default** and keeps the wheel small (no vendored binaries).
* ``local``  -- assets are served from a user-data directory under
  ``platformdirs.user_data_dir("nTasker") / "vendor"`` and mounted
  inside the FastAPI app. Populated via ``ntasker assets fetch``.
* ``auto``   -- resolves to ``local`` if every manifest entry exists in
  ``assets_dir()``, else ``cdn``. The default ``assets_mode`` value.

Adding/upgrading an asset = one entry in :data:`MANIFEST`. The SRI hash
must be regenerated whenever the upstream version is bumped::

    openssl dgst -sha384 -binary <file> | openssl base64 -A

CDN bytes are byte-for-byte verified against the SRI hash by
``ntasker assets fetch`` (Zero-Trust: never trust the CDN, always
verify). A mismatch deletes the file and aborts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import platformdirs

from ntasker.paths import APP_NAME

Mode = Literal["cdn", "local", "auto"]
ResolvedMode = Literal["cdn", "local"]

VALID_MODES: frozenset[str] = frozenset({"cdn", "local", "auto"})
DEFAULT_MODE: Mode = "auto"


@dataclass(frozen=True)
class AssetSpec:
    """A single vendor asset.

    ``local_path`` is relative to :func:`assets_dir`; the same relative
    layout is what ``assets fetch`` writes to disk and what the FastAPI
    static mount serves under ``/static/vendor/<local_path>``.
    """

    name: str
    cdn_url: str
    sri: str
    local_path: str


# ---------------------------------------------------------------------------
# Manifest -- pinned versions + SRI hashes
# ---------------------------------------------------------------------------
# SRI hashes were computed via:
#   openssl dgst -sha384 -binary <local-vendored-file> | openssl base64 -A
# and verified to match the bytes served by jsDelivr at the URLs below.
# Bumping a pinned version REQUIRES regenerating the matching SRI hash.

MANIFEST: tuple[AssetSpec, ...] = (
    AssetSpec(
        name="tabler-css",
        cdn_url="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/css/tabler.min.css",
        sri="sha384-GgnF119bh9fxkKuWHRQYSgEe1rSp5jB0EJ2W8eMf8mjowfwhZP2H1u8n8xJUW3FQ",
        local_path="tabler/tabler.min.css",
    ),
    AssetSpec(
        name="tabler-icons-css",
        cdn_url=(
            "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/"
            "dist/tabler-icons.min.css"
        ),
        sri="sha384-hs5SINUk7GPohxRis+rS7grpSWEbtOIJ3sRoseBKg3CDPIKpG55RfSenbvA6ALOt",
        local_path="tabler-icons/tabler-icons.min.css",
    ),
    # The icon webfont CSS references its font files via relative
    # ``./fonts/...`` URLs. When loaded from the CDN they resolve against
    # the CDN; when served from the user-data dir they resolve against
    # the local layout below -- which is why ``assets fetch`` must write
    # the woff2/woff/ttf files into ``tabler-icons/fonts/`` next to the
    # CSS. The font files themselves are not <link>'d directly from a
    # template, so they have no SRI string in any HTML, but we still
    # verify their bytes after download for tamper-detection.
    AssetSpec(
        name="tabler-icons-woff2",
        cdn_url=(
            "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/"
            "dist/fonts/tabler-icons.woff2"
        ),
        sri="sha384-vRpSinO+OBaOfZqP3rEMGBIwNKWpk+usLEn45iJ9nHx4GmVvd/PlqsDsd3vhdcfk",
        local_path="tabler-icons/fonts/tabler-icons.woff2",
    ),
    AssetSpec(
        name="tabler-icons-woff",
        cdn_url=(
            "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/"
            "dist/fonts/tabler-icons.woff"
        ),
        sri="sha384-SaX6PVgL5bb1Tuf+XYT56UPTfMJAKsqhg3JdkFSxr6k+Se6iss2WBdAfa7YJ+k3q",
        local_path="tabler-icons/fonts/tabler-icons.woff",
    ),
    AssetSpec(
        name="tabler-icons-ttf",
        cdn_url=(
            "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/"
            "dist/fonts/tabler-icons.ttf"
        ),
        sri="sha384-5k+TndrkWQ94e3sLspQivYGP/ArF/CBKywiyxmk7xy06PEOzh34JTPLlQUiXAoa7",
        local_path="tabler-icons/fonts/tabler-icons.ttf",
    ),
    AssetSpec(
        name="alpine-js",
        cdn_url="https://cdn.jsdelivr.net/npm/alpinejs@3.14.3/dist/cdn.min.js",
        sri="sha384-iZD2X8o1Zdq0HR5H/7oa8W30WS4No+zWCKUPD7fHRay9I1Gf+C4F8sVmw7zec1wW",
        local_path="alpine/alpine.min.js",
    ),
    # xterm.js -- terminal emulator for the interactive "Run with Claude" view.
    # The classic ``xterm`` package exposes ``window.Terminal`` /
    # ``window.FitAddon`` for plain <script> use (no bundler).
    AssetSpec(
        name="xterm-css",
        cdn_url="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css",
        sri="sha384-LJcOxlx9IMbNXDqJ2axpfEQKkAYbFjJfhXexLfiRJhjDU81mzgkiQq8rkV0j6dVh",
        local_path="xterm/xterm.css",
    ),
    AssetSpec(
        name="xterm-js",
        cdn_url="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js",
        sri="sha384-/nfmYPUzWMS6v2atn8hbljz7NE0EI1iGx34lJaNzyVjWGDzMv+ciUZUeJpKA3Glc",
        local_path="xterm/xterm.js",
    ),
    AssetSpec(
        name="xterm-addon-fit",
        cdn_url="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js",
        sri="sha384-AQLWHRKAgdTxkolJcLOELg4E9rE89CPE2xMy3tIRFn08NcGKPTsELdvKomqji+DL",
        local_path="xterm/xterm-addon-fit.js",
    ),
)

_MANIFEST_BY_NAME: dict[str, AssetSpec] = {spec.name: spec for spec in MANIFEST}

# URL-prefix under which local assets are mounted in FastAPI. Templates
# build full URLs as f"{LOCAL_URL_PREFIX}/{spec.local_path}".
LOCAL_URL_PREFIX = "/static/vendor"


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def assets_dir() -> Path:
    """Return the user-data directory that holds local vendor assets.

    Linux: ``~/.local/share/nTasker/vendor``. The directory is *not*
    created on import; ``assets fetch`` creates it on demand.
    """
    return Path(platformdirs.user_data_dir(APP_NAME)) / "vendor"


def local_path_for(spec: AssetSpec) -> Path:
    """Absolute on-disk path for a single asset under :func:`assets_dir`."""
    return assets_dir() / spec.local_path


def local_assets_complete() -> bool:
    """Return ``True`` iff every manifest entry exists on disk.

    This is the gate for ``mode=auto`` -> ``local``. We check existence
    only -- byte-level integrity is the responsibility of ``assets fetch``
    (which deletes any file that fails SRI verification before this
    function gets a chance to see it).
    """
    return all(local_path_for(spec).is_file() for spec in MANIFEST)


# ---------------------------------------------------------------------------
# Mode resolution + URL/SRI helpers
# ---------------------------------------------------------------------------


def resolve_mode(setting_value: str | None) -> ResolvedMode:
    """Resolve ``cdn`` / ``local`` / ``auto`` (or ``None``) to a concrete mode.

    Unknown values fall back to ``cdn`` -- the safest default that always
    works. Validation of the *setting* happens in
    :mod:`ntasker.settings`; this function is forgiving on read so a
    typo'd DB row never breaks rendering.
    """
    value = (setting_value or DEFAULT_MODE).strip().lower()
    if value == "local":
        return "local"
    if value == "cdn":
        return "cdn"
    # ``auto`` and anything else: pick local iff the cache is complete.
    return "local" if local_assets_complete() else "cdn"


def get_spec(name: str) -> AssetSpec:
    """Return the manifest entry for ``name`` or raise ``KeyError``."""
    try:
        return _MANIFEST_BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"Unknown asset name: {name!r}") from exc


def get_asset_url(name: str, mode: ResolvedMode, version: str | None = None) -> str:
    """Return the URL to load ``name`` from in the given resolved mode.

    For ``mode=local`` we append ``?v=<version>`` as a cache-buster --
    the file lives next to a long-lived browser cache, so a version bump
    must invalidate it. CDN URLs are already version-pinned in their
    path and need no buster.
    """
    spec = get_spec(name)
    if mode == "cdn":
        return spec.cdn_url
    url = f"{LOCAL_URL_PREFIX}/{spec.local_path}"
    if version:
        url += f"?v={version}"
    return url


def get_sri(name: str, mode: ResolvedMode) -> str:
    """Return the SRI attribute *value* for a ``<link>``/``<script>`` tag.

    SRI is a cheap belt-and-braces check even for local files (catches
    on-disk corruption / tampering) -- so we always emit it, regardless
    of ``mode``. Templates spell out the full ``integrity="..."`` and
    ``crossorigin="anonymous"`` themselves; this function just returns
    the bare hash.
    """
    return get_spec(name).sri


# ---------------------------------------------------------------------------
# Validator wired into ntasker.settings.VALIDATORS
# ---------------------------------------------------------------------------


def validate_assets_mode(value: str) -> str:
    """Settings-validator for the ``assets_mode`` key.

    Accepts ``cdn``, ``local``, ``auto`` (case-insensitive), normalises
    to lowercase. Anything else raises ``ValueError`` -- the FastAPI
    layer maps that to HTTP 400.
    """
    if not value:
        raise ValueError("assets_mode darf nicht leer sein.")
    normalized = value.strip().lower()
    if normalized not in VALID_MODES:
        raise ValueError(
            f"assets_mode muss einer von {sorted(VALID_MODES)} sein, war: {value!r}"
        )
    return normalized
