#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from store_pythoine import APP_NAME, APP_VERSION


def _value_after(flag: str) -> str | None:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return None
    return sys.argv[index + 1] if index + 1 < len(sys.argv) else ""


def _usage() -> str:
    return (
        f"{APP_NAME} {APP_VERSION} — headless Suite Pythoine backend\n"
        "\n"
        "Use Store Pythoine from inside Suite Pythoine. Backend commands:\n"
        "  --publish-content\n"
        "  --refresh-online\n"
        "  --download-component <component-id>\n"
        "  --version\n"
    )


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        raise SystemExit(0)
    if "--help" in sys.argv or "-h" in sys.argv:
        print(_usage(), end="")
        raise SystemExit(0)
    if "--publish-content" in sys.argv:
        from store_pythoine.catalogue import load_best_local, published_catalogue_path
        load_best_local()
        print(published_catalogue_path())
        raise SystemExit(0)
    if "--refresh-online" in sys.argv:
        try:
            from store_pythoine.catalogue import published_catalogue_path, refresh_repositories
            manifest, counts = refresh_repositories()
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({
            "status": "ok",
            "store_revision": manifest.revision,
            "checked": counts.get("checked", 0),
            "failed": counts.get("failed", 0),
            "unavailable": counts.get("unavailable", 0),
            "published": str(published_catalogue_path()),
        }, sort_keys=True))
        raise SystemExit(0)
    component_id = _value_after("--download-component")
    if component_id is not None:
        if not component_id:
            print("Usage: --download-component <component-id>", file=sys.stderr)
            raise SystemExit(2)
        try:
            from store_pythoine.backend import download_component
            component, path = download_component(component_id)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({
            "status": "verified",
            "component_id": component.component_id,
            "latest_version": component.latest_version,
            "path": str(path),
        }, sort_keys=True))
        raise SystemExit(0)
    print(_usage(), file=sys.stderr, end="")
    raise SystemExit(2)
