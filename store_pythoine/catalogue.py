from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.error
import urllib.request

from . import APP_VERSION, PUBLISHER
from .config import cache_dir, data_dir

STORE_SCHEMA = 3
SUPPORTED_STORE_SCHEMAS = frozenset({1, 2, 3})
MAX_SCREENSHOTS = 8
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_BYTES = 768 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoreScreenshot:
    relative_path: str
    sha256: str
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class StoreEntry:
    component_id: str
    latest_version: str | None = None
    release_date: str | None = None
    release_notes: str | None = None
    extended_description: str | None = None
    highlights: tuple[str, ...] = ()
    screenshots: tuple[StoreScreenshot, ...] = ()
    download_url: str | None = None
    sha256: str | None = None
    package_size: int | None = None
    package_type: str | None = None
    icon_url: str | None = None
    release_url: str | None = None
    check_status: str | None = None
    check_error: str | None = None

    @property
    def installable(self) -> bool:
        return bool(self.download_url and self.sha256)


@dataclass(frozen=True, slots=True)
class StoreManifest:
    revision: int
    entries: tuple[StoreEntry, ...]

    def by_id(self) -> dict[str, StoreEntry]:
        return {entry.component_id: entry for entry in self.entries}


class CatalogueError(RuntimeError):
    pass


def bundled_catalogue_path() -> Path:
    return Path(__file__).resolve().with_name("store-content.json")


def cached_catalogue_path() -> Path:
    return cache_dir() / "store-content.json"


def published_catalogue_path() -> Path:
    return data_dir() / "store-content.json"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _media_relative_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    rel = PurePosixPath(text)
    if (not text or rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts)
            or not rel.parts or rel.parts[0] != "media"):
        raise CatalogueError("Store screenshot path must be a safe relative media/ path.")
    if rel.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise CatalogueError("Store screenshots must be PNG, JPEG or WebP files.")
    return rel.as_posix()


def _string_list(value: object, *, limit: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limit:
        raise CatalogueError("Store rich-text lists are malformed or exceed their safety limit.")
    result: list[str] = []
    for raw in value:
        text = str(raw or "").strip()
        if not text or len(text) > 500:
            raise CatalogueError("Store rich-text list entries must be non-empty and bounded.")
        result.append(text)
    return tuple(result)


def _screenshots(value: object) -> tuple[StoreScreenshot, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_SCREENSHOTS:
        raise CatalogueError("Store screenshot list is malformed or exceeds the safety limit.")
    result: list[StoreScreenshot] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise CatalogueError("Invalid Store screenshot entry.")
        relative = _media_relative_path(raw.get("path"))
        if relative in seen:
            raise CatalogueError("Store screenshot paths must be unique within an application.")
        seen.add(relative)
        digest = str(raw.get("sha256") or "").strip().casefold()
        if not _valid_sha256(digest):
            raise CatalogueError("Store screenshot SHA-256 is invalid.")
        caption = _optional_text(raw.get("caption"))
        if caption is not None and len(caption) > 500:
            raise CatalogueError("Store screenshot caption exceeds the safety limit.")
        result.append(StoreScreenshot(relative, digest, caption))
    return tuple(result)


def _parse(payload: object) -> StoreManifest:
    if not isinstance(payload, dict) or payload.get("schema") not in SUPPORTED_STORE_SCHEMAS:
        raise CatalogueError("Unsupported Store content schema.")
    if payload.get("publisher_id") != PUBLISHER:
        raise CatalogueError("Store content publisher is not trusted.")
    revision = payload.get("store_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise CatalogueError("Store content revision must be a non-negative integer.")
    raw = payload.get("components", [])
    if not isinstance(raw, list):
        raise CatalogueError("Store content components must be a list.")
    result: list[StoreEntry] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CatalogueError("Invalid Store content entry.")
        component_id = str(item.get("id") or "").strip().casefold()
        if not component_id or component_id in seen:
            raise CatalogueError("Store content entries require unique component IDs.")
        seen.add(component_id)
        download_url = _optional_text(item.get("download_url"))
        icon_url = _optional_text(item.get("icon_url"))
        release_url = _optional_text(item.get("release_url"))
        for label, url in (("package", download_url), ("icon", icon_url), ("release", release_url)):
            if url is not None and not url.startswith("https://"):
                raise CatalogueError(f"Only HTTPS Store {label} URLs are accepted.")
        sha256 = _optional_text(item.get("sha256"))
        if sha256 is not None:
            sha256 = sha256.casefold()
            if not _valid_sha256(sha256):
                raise CatalogueError(f"Invalid SHA-256 for Store entry {component_id}.")
        package_size = item.get("package_size")
        if package_size is not None:
            if isinstance(package_size, bool) or not isinstance(package_size, int) or package_size <= 0 or package_size > MAX_PACKAGE_BYTES:
                raise CatalogueError(f"Invalid package size for Store entry {component_id}.")
        package_type = _optional_text(item.get("package_type"))
        if package_type is not None:
            package_type = package_type.casefold()
            if package_type not in {"source-zip", "appimage"}:
                raise CatalogueError(f"Invalid package type for Store entry {component_id}.")
        check_status = _optional_text(item.get("check_status"))
        if check_status is not None and check_status not in {"ok", "error", "unavailable", "unchecked"}:
            raise CatalogueError(f"Invalid repository check status for Store entry {component_id}.")
        check_error = _optional_text(item.get("check_error"))
        if check_error is not None and len(check_error) > 500:
            raise CatalogueError(f"Repository check error is too long for Store entry {component_id}.")
        result.append(StoreEntry(
            component_id=component_id,
            latest_version=_optional_text(item.get("latest_version")),
            release_date=_optional_text(item.get("release_date")),
            release_notes=_optional_text(item.get("release_notes")),
            extended_description=_optional_text(item.get("extended_description")),
            highlights=_string_list(item.get("highlights"), limit=12),
            screenshots=_screenshots(item.get("screenshots")),
            download_url=download_url,
            sha256=sha256,
            package_size=package_size,
            package_type=package_type,
            icon_url=icon_url,
            release_url=release_url,
            check_status=check_status,
            check_error=check_error,
        ))
    return StoreManifest(revision=revision, entries=tuple(result))


def _payload(manifest: StoreManifest) -> dict:
    rows = []
    for entry in manifest.entries:
        row = {"id": entry.component_id}
        for key in ("latest_version", "release_date", "release_notes", "extended_description", "download_url", "sha256", "package_size", "package_type", "icon_url", "release_url", "check_status", "check_error"):
            value = getattr(entry, key)
            if value is not None:
                row[key] = value
        if entry.highlights:
            row["highlights"] = list(entry.highlights)
        if entry.screenshots:
            row["screenshots"] = [
                {
                    "path": shot.relative_path,
                    "sha256": shot.sha256,
                    **({"caption": shot.caption} if shot.caption else {}),
                }
                for shot in entry.screenshots
            ]
        rows.append(row)
    return {
        "schema": STORE_SCHEMA,
        "publisher_id": PUBLISHER,
        "store_revision": manifest.revision,
        "components": rows,
    }


def load_file(path: Path) -> StoreManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CatalogueError(str(exc)) from exc
    return _parse(payload)


def _copy_verified_media(manifest: StoreManifest, content_root: Path | None) -> None:
    if content_root is None:
        return
    root = content_root.resolve(strict=False)
    for entry in manifest.entries:
        for shot in entry.screenshots:
            source = (root / shot.relative_path).resolve(strict=False)
            try:
                source.relative_to(root)
            except ValueError:
                continue
            if not source.is_file() or source.is_symlink():
                continue
            try:
                if source.stat().st_size > MAX_MEDIA_BYTES or not _digest_ok(source, shot.sha256):
                    continue
            except OSError:
                continue
            target = data_dir() / shot.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)


def publish_manifest(manifest: StoreManifest, *, content_root: Path | None = None) -> Path:
    target = published_catalogue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_verified_media(manifest, content_root)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(_payload(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def load_best_local() -> tuple[StoreManifest, str]:
    """Load Store 0.0.4's local snapshot/enrichment contract.

    `store_revision` is now a local content-contract revision, not a global
    application-version authority. A cached repository snapshot may override
    the bundled seed only when it matches the installed Store's contract
    revision. Older/newer central-catalogue caches from previous experiments
    therefore cannot mask the installed Store release.
    """
    bundled = bundled_catalogue_path()
    try:
        bundled_manifest = load_file(bundled)
    except CatalogueError as exc:
        raise CatalogueError("The bundled Store content manifest is invalid.") from exc

    manifest = bundled_manifest
    source = "bundled"
    source_path = bundled
    cache = cached_catalogue_path()
    if cache.is_file():
        try:
            cached = load_file(cache)
        except CatalogueError:
            cached = None
        if cached is not None and cached.revision == bundled_manifest.revision:
            manifest = cached
            source = "cached"
            source_path = cache

    publish_manifest(manifest, content_root=source_path.parent / "content")
    return manifest, source


def refresh_repositories() -> tuple[StoreManifest, dict[str, int]]:
    """Refresh version discovery from Suite-authorised GitHub repositories.

    The remote repositories are authoritative for their own declared versions.
    Store's local manifest remains a normalized cache/enrichment snapshot only.
    """
    from .repository_check import check_trusted_repositories

    base, _source = load_best_local()
    rich = base.by_id()
    results = check_trusted_repositories()
    entries: list[StoreEntry] = []
    counts = {"checked": 0, "failed": 0, "unavailable": 0}
    for result in results:
        prior = rich.get(result.component_id)
        entries.append(StoreEntry(
            component_id=result.component_id,
            latest_version=(
                result.latest_version if result.status == "ok"
                else (prior.latest_version if prior and prior.check_status in {"ok", "error"} else None)
            ),
            release_date=None,
            release_notes=None,
            extended_description=prior.extended_description if prior else None,
            highlights=prior.highlights if prior else (),
            screenshots=prior.screenshots if prior else (),
            download_url=None,
            sha256=None,
            package_size=None,
            package_type=None,
            icon_url=prior.icon_url if prior else None,
            release_url=result.release_url or (prior.release_url if prior else None),
            check_status=result.status,
            check_error=result.error,
        ))
        if result.status == "ok":
            counts["checked"] += 1
        elif result.status == "unavailable":
            counts["unavailable"] += 1
        else:
            counts["failed"] += 1

    # Store revision is now a local snapshot/content-contract revision, not a
    # global version authority. Keep the installed Store's bundled revision so
    # cached dynamic results can be replaced safely by a later Store release.
    bundled_revision = load_file(bundled_catalogue_path()).revision
    manifest = StoreManifest(revision=bundled_revision, entries=tuple(entries))
    target = cached_catalogue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(_payload(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    publish_manifest(manifest)
    return manifest, counts


def _digest_ok(path: Path, expected: str) -> bool:
    expected = expected.strip().casefold()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def download_verified(entry: StoreEntry, *, fallback_name: str | None = None) -> Path:
    if not entry.installable:
        raise CatalogueError("This Store entry does not provide both a package URL and SHA-256.")
    assert entry.download_url is not None
    assert entry.sha256 is not None
    request = urllib.request.Request(entry.download_url, headers={"User-Agent": f"Store-Pythoine/{APP_VERSION}"}, method="GET")
    destination_dir = cache_dir() / "downloads"
    destination_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(entry.download_url.split("?", 1)[0]).name or fallback_name or f"{entry.component_id}.zip"
    target = destination_dir / filename
    temporary: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            if not str(response.geturl()).startswith("https://"):
                raise CatalogueError("Package redirected to a non-HTTPS URL.")
            with tempfile.NamedTemporaryFile("wb", dir=destination_dir, prefix=f".{filename}.", suffix=".part", delete=False) as handle:
                temporary = Path(handle.name)
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PACKAGE_BYTES:
                        raise CatalogueError("Package exceeded the download safety limit.")
                    handle.write(chunk)
        if temporary is None:
            raise CatalogueError("The Store package download did not produce a file.")
        if entry.package_size is not None and total != entry.package_size:
            raise CatalogueError("Downloaded package size does not match Store metadata.")
        if not _digest_ok(temporary, entry.sha256):
            raise CatalogueError("SHA-256 verification failed for the downloaded package.")
        temporary.replace(target)
        temporary = None
        return target
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        if isinstance(exc, CatalogueError):
            raise
        raise CatalogueError(str(exc)) from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
