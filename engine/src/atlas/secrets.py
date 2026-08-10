"""Connection strings, held outside the source registry.

`sources.yaml` records which environment variable holds a credential; this
holds the credential itself. Keeping them apart means the registry stays safe
to commit and safe to serve, and only this module ever touches a secret.

⚠ Plaintext on disk, `0600`, gitignored. That is appropriate for an engine on
your own machine and is NOT sufficient for a deployment: anyone who can reach
the API can write a connection string here, and anyone who can read the disk
can read it back. Replace with a real secret store — and put authentication in
front of the API — before this runs anywhere shared.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from atlas.settings import get_settings

logger = logging.getLogger(__name__)


def secrets_path() -> Path:
    return get_settings().atlas_secrets_file


def _read_all(path: Path | None = None) -> dict[str, str]:
    target = path or secrets_path()
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def load_into_environment(path: Path | None = None) -> int:
    """Populate `os.environ` from the store.

    A real environment variable always wins: someone who exported one at the
    shell means it, and silently overriding it would make the engine disagree
    with its own operator.
    """
    loaded = 0
    for name, value in _read_all(path).items():
        if not os.environ.get(name):
            os.environ[name] = value
            loaded += 1
    return loaded


def set_secret(name: str, value: str, path: Path | None = None) -> None:
    """Write a credential and make it live immediately.

    Setting `os.environ` here is what removes the restart: the engine reads
    connection URLs from the environment, so updating the process's own copy
    means the next probe uses the new value.
    """
    target = path or secrets_path()
    values = _read_all(target)
    values[name] = value

    target.parent.mkdir(parents=True, exist_ok=True)
    body = "# Written by Atlas. Plaintext credentials — never commit this file.\n" + "".join(
        f"{k}={v}\n" for k, v in sorted(values.items())
    )
    target.write_text(body)
    target.chmod(0o600)

    os.environ[name] = value
    logger.info("stored credential for %s", name)


def clear_secret(name: str, path: Path | None = None) -> None:
    target = path or secrets_path()
    values = _read_all(target)
    if values.pop(name, None) is None:
        return
    target.write_text(
        "# Written by Atlas. Plaintext credentials — never commit this file.\n"
        + "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    )
    target.chmod(0o600)
    os.environ.pop(name, None)


def has_secret(name: str, path: Path | None = None) -> bool:
    return name in _read_all(path)
