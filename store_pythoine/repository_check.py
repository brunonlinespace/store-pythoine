from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlparse
import urllib.error
import urllib.request

from . import APP_VERSION, PUBLISHER
from .config import data_dir

TRUSTED_SNAPSHOT_SCHEMA = 1
MAX_TRUSTED_COMPONENTS = 128
MAX_COMPONENT_MANIFEST_BYTES = 256 * 1024
MAX_WORKERS = 8
REQUEST_TIMEOUT = 5.0
COMPONENT_MANIFEST = "suite-pythoine-component.json"


class RepositoryCheckError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedComponent:
    component_id: str
    name: str
    publisher_id: str
    repository: str | None


@dataclass(frozen=True, slots=True)
class RepositoryResult:
    component_id: str
    status: str
    latest_version: str | None = None
    release_url: str | None = None
    error: str | None = None


def trusted_snapshot_path() -> Path:
    return data_dir() / "suite-trusted-components.json"


def _github_repository(value: object) -> tuple[str, str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text:
        parts = [part for part in text.strip("/").split("/") if part]
        if len(parts) != 2:
            return None
        owner, repo = parts
        return owner, repo.removesuffix(".git"), f"https://github.com/{owner}/{repo.removesuffix('.git')}"
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    repo = repo.removesuffix(".git")
    return owner, repo, f"https://github.com/{owner}/{repo}"


def _load_snapshot(path: Path | None = None) -> tuple[TrustedComponent, ...]:
    source = (path or trusted_snapshot_path()).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RepositoryCheckError(
            "Suite Pythoine has not published a valid trusted repository snapshot. "
            "Open Store Pythoine from Suite and refresh again."
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != TRUSTED_SNAPSHOT_SCHEMA:
        raise RepositoryCheckError("Unsupported Suite trusted repository snapshot schema.")
    if payload.get("publisher_id") != PUBLISHER:
        raise RepositoryCheckError("Suite trusted repository snapshot publisher is not trusted.")
    rows = payload.get("components")
    if not isinstance(rows, list) or len(rows) > MAX_TRUSTED_COMPONENTS:
        raise RepositoryCheckError("Suite trusted repository snapshot is malformed or too large.")
    result: list[TrustedComponent] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise RepositoryCheckError("Suite trusted repository snapshot contains an invalid component entry.")
        component_id = str(raw.get("id") or "").strip().casefold()
        name = str(raw.get("name") or "").strip()
        publisher_id = str(raw.get("publisher_id") or "").strip()
        repository = str(raw.get("repository") or "").strip() or None
        if not component_id or component_id in seen or publisher_id != PUBLISHER:
            raise RepositoryCheckError("Suite trusted repository snapshot contains an invalid or untrusted identity.")
        seen.add(component_id)
        result.append(TrustedComponent(component_id, name or component_id, publisher_id, repository))
    return tuple(result)


def _read_remote_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": f"Store-Pythoine/{APP_VERSION}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            if not str(response.geturl()).startswith("https://"):
                raise RepositoryCheckError("Repository metadata redirected to a non-HTTPS URL.")
            body = response.read(MAX_COMPONENT_MANIFEST_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RepositoryCheckError(str(exc)) from exc
    if len(body) > MAX_COMPONENT_MANIFEST_BYTES:
        raise RepositoryCheckError("Repository component manifest exceeded the safety limit.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepositoryCheckError("Repository component manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise RepositoryCheckError("Repository component manifest is not a JSON object.")
    return payload


def _check_component(component: TrustedComponent) -> RepositoryResult:
    repo = _github_repository(component.repository)
    if repo is None:
        return RepositoryResult(component.component_id, "unavailable", error="No supported authorised GitHub repository is declared.")
    owner, repository, canonical_url = repo
    last_error = "Component manifest was not found in the authorised repository."
    payload: dict | None = None
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{owner}/{repository}/{branch}/{COMPONENT_MANIFEST}"
        try:
            payload = _read_remote_json(url)
            break
        except RepositoryCheckError as exc:
            last_error = str(exc)
    if payload is None:
        return RepositoryResult(component.component_id, "error", release_url=canonical_url + "/releases", error=last_error[:500])

    declared_id = str(payload.get("id") or "").strip().casefold()
    if declared_id != component.component_id:
        return RepositoryResult(component.component_id, "error", release_url=canonical_url + "/releases", error="Repository component ID does not match Suite's trusted identity.")
    declared_publisher = str(payload.get("publisher_id") or payload.get("publisher") or "").strip()
    if declared_publisher and declared_publisher != component.publisher_id:
        return RepositoryResult(component.component_id, "error", release_url=canonical_url + "/releases", error="Repository component publisher does not match Suite's trusted identity.")
    declared_repository = payload.get("repository")
    if declared_repository:
        normalized = _github_repository(declared_repository)
        if normalized is None or normalized[2].casefold() != canonical_url.casefold():
            return RepositoryResult(component.component_id, "error", release_url=canonical_url + "/releases", error="Repository component manifest points at a different repository.")
    version = str(payload.get("version") or "").strip()
    if not version or len(version) > 128 or any(ch.isspace() for ch in version):
        return RepositoryResult(component.component_id, "error", release_url=canonical_url + "/releases", error="Repository component manifest does not declare a usable version.")
    return RepositoryResult(component.component_id, "ok", latest_version=version, release_url=canonical_url + "/releases")


def check_trusted_repositories(path: Path | None = None) -> tuple[RepositoryResult, ...]:
    components = _load_snapshot(path)
    if not components:
        return ()
    results: dict[str, RepositoryResult] = {}
    workers = min(MAX_WORKERS, max(1, len(components)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="store-repo") as executor:
        futures = {executor.submit(_check_component, component): component.component_id for component in components}
        for future in as_completed(futures):
            component_id = futures[future]
            try:
                results[component_id] = future.result()
            except Exception as exc:
                results[component_id] = RepositoryResult(component_id, "error", error=str(exc)[:500])
    return tuple(results[component.component_id] for component in components)
