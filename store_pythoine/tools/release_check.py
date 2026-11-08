from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from store_pythoine import APP_VERSION
from store_pythoine import catalogue as catalogue_module
from store_pythoine import repository_check as repository_module
from store_pythoine.backend import download_component
from store_pythoine.catalogue import (
    CatalogueError,
    StoreEntry,
    StoreManifest,
    StoreScreenshot,
    download_verified,
    load_best_local,
    publish_manifest,
    refresh_repositories,
)
from store_pythoine.config import cache_dir, data_dir

VERSION = "0.0.4"
PACKAGE = Path(__file__).resolve().parent.parent
ROOT = PACKAGE.parent
EXCLUDED = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", ".venv", "venv"}


def audit(errors: list[str]) -> None:
    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    check(APP_VERSION == VERSION, "Store Python version mismatch")
    package_manifest = json.loads((ROOT / "store-pythoine.json").read_text(encoding="utf-8"))
    check(package_manifest.get("version") == VERSION, "Store package manifest version mismatch")
    component_manifest = json.loads((ROOT / "suite-pythoine-component.json").read_text(encoding="utf-8"))
    check(component_manifest.get("id") == "store-pythoine", "Store Suite component manifest identity mismatch")
    check(component_manifest.get("version") == VERSION, "Store Suite component manifest version mismatch")
    check(component_manifest.get("repository") == "https://github.com/brunonlinespace/store-pythoine",
          "Store Suite component manifest repository mismatch")

    check(not (PACKAGE / "app.py").exists(), "standalone Store Qt window remains packaged")
    check(not (PACKAGE / "suite_bridge.py").exists(), "standalone Store Suite-query bridge remains packaged")
    check(not (PACKAGE / "catalogue.json").exists(), "duplicate authoritative Store identity catalogue remains")
    check((PACKAGE / "repository_check.py").is_file(), "trusted repository checker missing")
    check((PACKAGE / "store-content.json").is_file(), "bundled Store content overlay missing")
    requirements = (PACKAGE / "requirements.txt").read_text(encoding="utf-8")
    check("PyQt" not in requirements and "PySide" not in requirements, "headless Store still depends on a Qt GUI runtime")

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    catalogue_source = (PACKAGE / "catalogue.py").read_text(encoding="utf-8")
    repository_source = (PACKAGE / "repository_check.py").read_text(encoding="utf-8")
    check("store_pythoine.app" not in main_source and "QApplication" not in main_source,
          "normal Store invocation still falls through to a standalone GUI")
    for token in ('"--publish-content"', '"--refresh-online"', '"--download-component"', '"--version"'):
        check(token in main_source, f"headless Store command missing: {token}")
    check("refresh_repositories" in main_source and "refresh_remote" not in main_source,
          "Store refresh CLI still uses the central catalogue authority")
    check("DEFAULT_CATALOGUE_URL" not in catalogue_source and "raw.githubusercontent.com/brunonlinespace/store-pythoine/main/store-content.json" not in catalogue_source,
          "central Store version catalogue URL remains authoritative")
    check("suite-trusted-components.json" in repository_source,
          "Store does not consume Suite's trusted repository projection")
    check("ThreadPoolExecutor" in repository_source and "MAX_WORKERS" in repository_source,
          "repository checks are not bounded/concurrent")
    check("suite-pythoine-component.json" in repository_source,
          "repository checker does not use the distributed Suite component declaration")

    bundled = json.loads((PACKAGE / "store-content.json").read_text(encoding="utf-8"))
    check(bundled.get("schema") == 3 and bundled.get("store_revision") == 6,
          "Store 0.0.4 content schema/revision mismatch")
    bundled_rows = {str(row.get("id")): row for row in bundled.get("components", []) if isinstance(row, dict)}
    own = bundled_rows.get("store-pythoine", {})
    check(own.get("latest_version") == VERSION, "Store self-enrichment version mismatch")
    check(own.get("check_status") == "unchecked", "bundled Store self state should be unchecked/offline")
    check("central" in str(own.get("release_notes", "")).casefold(),
          "Store release notes do not document removal of central version authority")

    old_data = os.environ.get("XDG_DATA_HOME")
    old_cache = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory(prefix="store-004-repository-") as folder:
        os.environ["XDG_DATA_HOME"] = str(Path(folder) / "data")
        os.environ["XDG_CACHE_HOME"] = str(Path(folder) / "cache")
        try:
            manifest, source = load_best_local()
            target = publish_manifest(manifest)
            check(source == "bundled", "fresh Store did not select bundled local content")
            check(manifest.revision == 6, "fresh Store did not select revision 6")
            check(target == data_dir() / "store-content.json" and target.is_file(),
                  "Store did not publish to stable XDG data location")

            stale_cache = cache_dir() / "store-content.json"
            stale_cache.parent.mkdir(parents=True, exist_ok=True)
            stale_cache.write_text(json.dumps({
                "schema": 2, "publisher_id": "brunonlinespace", "store_revision": 5,
                "components": [{"id": "nuxpad", "latest_version": "999.0"}],
            }), encoding="utf-8")
            selected, selected_source = load_best_local()
            check(selected.revision == 6 and selected_source == "bundled",
                  "old central cache masked the newer repository-authority Store release")

            media_root = Path(folder) / "authored-content"
            media_file = media_root / "media" / "rico-plus" / "window.png"
            media_file.parent.mkdir(parents=True, exist_ok=True)
            media_bytes = b"store-rich-content-media-fixture"
            media_file.write_bytes(media_bytes)
            digest = hashlib.sha256(media_bytes).hexdigest()
            rich = StoreManifest(99, (StoreEntry(
                "rico-plus", highlights=("Highlight",),
                screenshots=(StoreScreenshot("media/rico-plus/window.png", digest, "Main window"),),
            ),))
            rich_target = publish_manifest(rich, content_root=media_root)
            rich_payload = json.loads(rich_target.read_text(encoding="utf-8"))
            published_media = data_dir() / "media" / "rico-plus" / "window.png"
            check(rich_payload.get("schema") == 3 and rich_payload["components"][0].get("screenshots"),
                  "Store schema-3 screenshot metadata was not published")
            check(published_media.is_file() and published_media.read_bytes() == media_bytes,
                  "verified Store screenshot media was not copied into XDG data")

            # Reset local selection to the 0.0.4 bundled seed before repository refresh.
            if stale_cache.exists():
                stale_cache.unlink()
            publish_manifest(load_best_local()[0])

            trusted = {
                "schema": 1,
                "publisher_id": "brunonlinespace",
                "suite_version": "0.12.2",
                "components": [
                    {"id": "nuxpad", "name": "Nuxpad 2", "publisher_id": "brunonlinespace",
                     "repository": "https://github.com/brunonlinespace/nuxpad"},
                    {"id": "store-pythoine", "name": "Store Pythoine", "publisher_id": "brunonlinespace",
                     "repository": "brunonlinespace/store-pythoine"},
                    {"id": "no-repo", "name": "No Repo", "publisher_id": "brunonlinespace", "repository": None},
                ],
            }
            trusted_path = data_dir() / "suite-trusted-components.json"
            trusted_path.parent.mkdir(parents=True, exist_ok=True)
            trusted_path.write_text(json.dumps(trusted), encoding="utf-8")

            class FakeResponse:
                def __init__(self, body: bytes, url: str) -> None:
                    self.body = body
                    self.url = url
                    self.offset = 0
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
                def geturl(self):
                    return self.url
                def read(self, size: int = -1):
                    if size is None or size < 0:
                        size = len(self.body) - self.offset
                    if self.offset >= len(self.body):
                        return b""
                    chunk = self.body[self.offset:self.offset + size]
                    self.offset += len(chunk)
                    return chunk

            original_repo_urlopen = repository_module.urllib.request.urlopen
            calls: list[str] = []
            lock = threading.Lock()
            def fake_repository_urlopen(request, timeout=0):
                url = request.full_url
                with lock:
                    calls.append(url)
                if "/nuxpad/" in url:
                    payload = {
                        "id": "nuxpad", "version": "2.1.1", "publisher_id": "brunonlinespace",
                        "repository": "https://github.com/brunonlinespace/nuxpad",
                    }
                elif "/store-pythoine/" in url:
                    payload = {
                        "id": "store-pythoine", "version": "0.0.4", "publisher": "brunonlinespace",
                        "repository": "https://github.com/brunonlinespace/store-pythoine",
                    }
                else:
                    raise OSError("unexpected repository")
                return FakeResponse(json.dumps(payload).encode("utf-8"), url)

            try:
                repository_module.urllib.request.urlopen = fake_repository_urlopen
                refreshed, counts = refresh_repositories()
                by_id = refreshed.by_id()
                check(counts == {"checked": 2, "failed": 0, "unavailable": 1},
                      "repository refresh counts are incorrect")
                check(by_id["nuxpad"].latest_version == "2.1.1" and by_id["nuxpad"].check_status == "ok",
                      "Nuxpad repository did not become authoritative for its version")
                check(by_id["store-pythoine"].latest_version == "0.0.4" and by_id["store-pythoine"].check_status == "ok",
                      "Store did not discover its own version through its authorised repository")
                check(by_id["no-repo"].check_status == "unavailable" and by_id["no-repo"].latest_version is None,
                      "component without a supported repository was not isolated as unavailable")
                check(all("raw.githubusercontent.com" in url for url in calls),
                      "repository refresh contacted a non-raw GitHub metadata endpoint")
                check(all("store-content.json" not in url for url in calls),
                      "repository refresh still queried the central Store content file")
                check(all("suite-pythoine-component.json" in url for url in calls),
                      "repository refresh queried anything other than distributed component declarations")
                check((data_dir() / "store-content.json").is_file(),
                      "repository refresh did not publish the generated local snapshot")

                # A failed later refresh keeps last-known version only as cache evidence,
                # but marks the check failed so Suite cannot call it a current update result.
                def failing_urlopen(request, timeout=0):
                    raise OSError("offline fixture")
                repository_module.urllib.request.urlopen = failing_urlopen
                failed_refresh, failed_counts = refresh_repositories()
                failed_by_id = failed_refresh.by_id()
                check(failed_counts["failed"] == 2,
                      "per-repository failures were not isolated and counted")
                check(failed_by_id["nuxpad"].latest_version == "2.1.1" and failed_by_id["nuxpad"].check_status == "error",
                      "failed refresh did not preserve last-known version as explicitly stale cache evidence")
            finally:
                repository_module.urllib.request.urlopen = original_repo_urlopen

            # Acquisition remains isolated/compatible but is not required by
            # repository discovery. It is tested independently with synthetic metadata.
            package_bytes = b"verified-store-package-fixture"
            package_digest = hashlib.sha256(package_bytes).hexdigest()
            acquisition_manifest = StoreManifest(6, (StoreEntry(
                "rico-plus", latest_version="9.9.9",
                download_url="https://example.invalid/rico-plus.zip",
                sha256=package_digest, package_size=len(package_bytes), package_type="source-zip",
            ),))
            acquisition_cache = cache_dir() / "store-content.json"
            acquisition_cache.write_text(json.dumps(catalogue_module._payload(acquisition_manifest)), encoding="utf-8")
            original_download_urlopen = catalogue_module.urllib.request.urlopen
            catalogue_module.urllib.request.urlopen = lambda request, timeout=0: FakeResponse(
                package_bytes, request.full_url
            )
            try:
                acquired_entry, acquired_path = download_component("rico-plus")
                check(acquired_entry.component_id == "rico-plus" and acquired_path.read_bytes() == package_bytes,
                      "isolated verified acquisition compatibility regressed")
                bad = StoreEntry("rico-plus", latest_version="9.9.10",
                                 download_url="https://example.invalid/bad.zip", sha256="0" * 64)
                try:
                    download_verified(bad, fallback_name="bad.zip")
                except CatalogueError:
                    pass
                else:
                    errors.append("Store acquisition accepted a package with the wrong SHA-256")
            finally:
                catalogue_module.urllib.request.urlopen = original_download_urlopen
        finally:
            if old_data is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old_data
            if old_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_cache

    manifest_path = ROOT / "SOURCE_MANIFEST.sha256"
    listed: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="ascii").splitlines():
        digest, sep, relative = line.partition("  ")
        if not sep:
            errors.append("malformed SOURCE_MANIFEST entry")
            continue
        listed[relative] = digest
    actual: dict[str, Path] = {}
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in rel.parts):
            if path.is_file():
                errors.append(f"generated/excluded file packaged: {rel}")
            continue
        if path.is_symlink():
            errors.append(f"symlink packaged: {rel}")
            continue
        if path.is_file() and path != manifest_path:
            actual[rel.as_posix()] = path
    check(set(listed) == set(actual), "SOURCE_MANIFEST file set mismatch")
    for name, path in actual.items():
        if name in listed:
            check(hashlib.sha256(path.read_bytes()).hexdigest() == listed[name],
                  f"SOURCE_MANIFEST checksum mismatch: {name}")


def main() -> int:
    errors: list[str] = []
    try:
        audit(errors)
    except Exception as exc:
        errors.append(f"release gate exception: {type(exc).__name__}: {exc}")
    if errors:
        for error in errors:
            print("FAIL:", error)
        return 1
    print(f"PASS: Store Pythoine {VERSION} trusted-repository backend/source release gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
