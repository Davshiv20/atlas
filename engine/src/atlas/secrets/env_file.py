"""Credentials in a plaintext file.

⚠ Plaintext on disk, `0600`, gitignored. Appropriate for an engine on your own
machine and NOT sufficient for a deployment: anyone who can reach the API can
write a connection string here, and anyone who can read the disk can read it
back. Put authentication in front of the API and a real secret manager behind
this port before it runs anywhere shared.

Still the default because it is the only store that works everywhere — a
headless container has no keychain to unlock and no session to unlock it in.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from atlas.secrets.base import SecretStore
from atlas.settings import get_settings

logger = logging.getLogger(__name__)

HEADER = "# Written by Atlas. Plaintext credentials — never commit this file.\n"


class EnvFileSecretStore(SecretStore):
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def __repr__(self) -> str:
        return f"EnvFileSecretStore({str(self.path)!r})"

    @property
    def path(self) -> Path:
        return self._path or get_settings().atlas_secrets_file

    def load_into_environment(self) -> int:
        loaded = 0
        for name, value in self._read().items():
            if not os.environ.get(name):
                os.environ[name] = value
                loaded += 1
        return loaded

    def set(self, name: str, value: str) -> None:
        values = self._read()
        values[name] = value
        self._write(values)
        os.environ[name] = value
        logger.info("stored credential for %s", name)

    def clear(self, name: str) -> None:
        values = self._read()
        if values.pop(name, None) is None:
            return
        self._write(values)
        os.environ.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._read()

    def names(self) -> list[str]:
        return sorted(self._read())

    # ---- the file ---------------------------------------------------------

    def _read(self) -> dict[str, str]:
        target = self.path
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

    def _write(self, values: dict[str, str]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(HEADER + "".join(f"{k}={v}\n" for k, v in sorted(values.items())))
        target.chmod(0o600)
