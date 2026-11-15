"""Secret values, looked up by name (spec: "Secrets").

On a laptop or in CI: environment variable DATAOPS_<SECRET>_<KEY> (upper
case, anything else as `_`), else a git-ignored secrets.yaml next to the
project:

    os-api:
      apiKey: ...

In Kubernetes, each Secret is mounted as a folder of files
(docs/kubernetes.md): $DATAOPS_SECRETS_DIR/<secret>/<key>. The environment still wins,
so a value can be overridden.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .loader import SECRET_FILES


def env_name(secret: str, key: str) -> str:
    return "DATAOPS_" + re.sub(r"[^A-Z0-9]+", "_", f"{secret}_{key}".upper()).strip("_")


SECRETS_DIR = "DATAOPS_SECRETS_DIR"


# A query parameter that carries a credential: key, api_key, access_token, sig, X-Amz-Signature, X-Amz-Credential...
SENSITIVE_PARAM = re.compile(r"([?&][\w.-]*(?:key|token|sig|signature|secret|password|credential)[\w.-]*=)[^&\s\"')]+", re.IGNORECASE)


def redact(text: str) -> str:
    """`text` with the credentials in any address it quotes replaced by ***. Errors from the HTTP
    library quote the full address, API key and all, and they go to the job's log and the ingest log."""
    return SENSITIVE_PARAM.sub(r"\1***", text)


def lookup(secret: str, key: str, root: Path | None = None) -> str | None:
    value = os.environ.get(env_name(secret, key))
    if value:
        return value
    mounted = os.environ.get(SECRETS_DIR)
    if mounted:
        path = Path(mounted) / secret / key
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    if root is None:
        return None
    for name in sorted(SECRET_FILES):
        path = Path(root) / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        entry = data.get(secret) if isinstance(data, dict) else None
        if isinstance(entry, dict) and entry.get(key) is not None:
            return str(entry[key])
    return None
