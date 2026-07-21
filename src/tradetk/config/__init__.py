"""Configuration: YAML + pydantic schema, validated on load."""

from tradetk.config.loader import load_config
from tradetk.config.schema import Config

__all__ = ["Config", "load_config"]
