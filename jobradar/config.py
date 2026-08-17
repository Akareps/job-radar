"""Config loading. Paths in settings.yaml resolve relative to the project root."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"
SECRETS = ROOT / "config" / "secrets.yaml"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else ROOT / path


def load_settings(path: str | Path | None = None) -> dict:
    settings = _read(Path(path) if path else DEFAULT_SETTINGS)
    settings.setdefault("database", "data/jobs.db")
    settings.setdefault("profile", "config/profile.yaml")
    settings.setdefault("companies", "config/companies.yaml")
    return settings


def load_secrets() -> dict:
    """API keys. Absent file is normal: keyed sources just stay dormant."""
    return _read(SECRETS)


def load_companies(path: str | Path) -> list[dict]:
    data = _read(resolve(str(path)))
    return data.get("companies", []) if isinstance(data, dict) else (data or [])


def save_companies(path: str | Path, companies: list[dict], header: str = "") -> None:
    target = resolve(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        if header:
            fh.write(header.rstrip() + "\n\n")
        yaml.safe_dump(
            {"companies": companies}, fh,
            sort_keys=False, allow_unicode=True, default_flow_style=False, width=100,
        )
