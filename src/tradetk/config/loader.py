"""Load and validate configuration from YAML.

Usage as a smoke test:
    uv run python -m tradetk.config.loader config/config.example.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from tradetk.config.schema import Config


def load_config(path: str | Path) -> Config:
    """Read a YAML file and return a validated :class:`Config`.

    Raises FileNotFoundError if the file is missing and pydantic ValidationError
    (with a readable message) if the contents violate the schema.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping, got {type(raw).__name__}")
    return Config.model_validate(raw)


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tradetk.config.loader <config.yaml>", file=sys.stderr)
        return 2
    try:
        cfg = load_config(argv[1])
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"config invalid:\n{exc}", file=sys.stderr)
        return 1
    print(f"OK: {argv[1]} is valid.")
    print(cfg.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
