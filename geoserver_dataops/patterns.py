"""Products named by a pattern (deliveries/roads_*.zip): the newest matching file is loaded."""

from __future__ import annotations

import re


def is_pattern(product) -> bool:
    return isinstance(product, str) and any(c in product for c in "*?[")


def pattern_stem(pattern: str) -> str:
    """A pattern's name without its wildcards and extension: roads_*.zip -> roads."""
    name = re.sub(r"\.[A-Za-z0-9]+$", "", pattern.rsplit("/", 1)[-1])  # the file name, no extension
    name = re.sub(r"\[[^\]]*\]|[*?]", "", name)  # no wildcards
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "data"
