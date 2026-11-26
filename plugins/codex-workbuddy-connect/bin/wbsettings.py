"""Tiny persistent settings store for this plugin.

The only setting today is ``hidden_regions``: regions that should stay out of
the Codex model picker.  Hiding is intentionally weaker than disabling:

* the region's credentials are still loaded, so ``/health`` and
  ``/wb/account-status`` can still report the account;
* ``/v1/models`` and the generated ``workbuddy-catalog.json`` omit it;
* requests that explicitly address a hidden region fail with a clear
  ``region_unavailable`` message instead of silently falling back.

Settings live next to ``config.toml``::

    %USERPROFILE%\\.codex\\workbuddy-settings.json
    {"hidden_regions": ["ai"]}

``WB_SETTINGS`` can point at another file (tests use this).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REGION_IDS = ("ai", "cn")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def settings_path() -> Path:
    explicit = os.environ.get("WB_SETTINGS")
    if explicit:
        return Path(explicit).expanduser()
    config = os.environ.get("WB_CODEX_CONFIG")
    if config:
        return Path(config).expanduser().parent / "workbuddy-settings.json"
    return codex_home() / "workbuddy-settings.json"


def _clean_regions(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip().lower()
        if text in REGION_IDS and text not in out:
            out.append(text)
    return out


def read_settings() -> dict:
    path = settings_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def hidden_regions() -> set[str]:
    """Regions hidden from the picker, from settings plus an env override."""
    hidden = set(_clean_regions(read_settings().get("hidden_regions")))
    override = os.environ.get("WORKBUDDY_HIDDEN_REGIONS")
    if override:
        hidden.update(_clean_regions(override))
    return hidden


def write_settings(doc: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def set_region_hidden(region: str, hidden: bool) -> Path:
    region = str(region).strip().lower()
    if region not in REGION_IDS:
        raise ValueError(f"区域只能是 {' / '.join(REGION_IDS)}")
    doc = read_settings()
    current = _clean_regions(doc.get("hidden_regions"))
    if hidden and region not in current:
        current.append(region)
    if not hidden and region in current:
        current.remove(region)
    doc["hidden_regions"] = [r for r in REGION_IDS if r in current]
    return write_settings(doc)
