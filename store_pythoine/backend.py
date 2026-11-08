from __future__ import annotations

from pathlib import Path

from .catalogue import StoreEntry, download_verified as _download_verified, load_best_local


def download_component(component_id: str) -> tuple[StoreEntry, Path]:
    """Download one published Store package and verify its SHA-256.

    This backend only acquires verified bytes. Suite Pythoine remains the
    authority for trusted component identity and managed installation.
    """
    wanted = str(component_id or "").strip().casefold()
    if not wanted:
        raise RuntimeError("A Store component ID is required.")
    manifest, _source = load_best_local()
    entry = manifest.by_id().get(wanted)
    if entry is None:
        raise RuntimeError("The requested application has no Store content entry.")
    if not entry.installable:
        raise RuntimeError("The requested application has no verified Store package metadata.")
    suffix = ".AppImage" if entry.package_type == "appimage" or str(entry.download_url or "").split("?", 1)[0].casefold().endswith(".appimage") else ".zip"
    return entry, _download_verified(entry, fallback_name=f"{entry.component_id}{suffix}")
