"""Credentials in the operating system's keychain.

Better than the file store on a workstation, and only there. The value is
encrypted at rest by the OS and never sits readable on disk, so a stolen laptop
or a mis-shared directory does not hand over a customer's warehouse.

It is not the default and should not be. A keychain needs a session to unlock
it: a headless container has neither, and `keyring` degrades to a backend that
either fails or — worse, depending on what is installed — stores plaintext
somewhere else. Choosing this is a statement about the machine Atlas is running
on, which is why it is a setting rather than a guess.

`keyring` is already a dependency: the Snowflake connector's
`secure-local-storage` extra installs it to cache MFA tokens.
"""

from __future__ import annotations

import logging
import os

from atlas.secrets.base import SecretStore

logger = logging.getLogger(__name__)

#: The keychain entry Atlas groups its credentials under. Fixed rather than
#: derived from a path, so moving the working directory does not orphan them.
SERVICE = "atlas.engine"

#: Which variables Atlas has stored is itself kept in the keychain, under a
#: reserved name. A keychain can look up an entry but not enumerate a service's
#: entries portably, and without a list there is no way to load them at start-up
#: or to move them to another store.
INDEX = "__atlas_index__"


class KeyringSecretStore(SecretStore):
    def __init__(self, service: str = SERVICE) -> None:
        self._service = service

    def __repr__(self) -> str:
        return f"KeyringSecretStore({self._service!r})"

    def load_into_environment(self) -> int:
        loaded = 0
        for name in self.names():
            if os.environ.get(name):
                continue
            value = self._backend().get_password(self._service, name)
            if value is not None:
                os.environ[name] = value
                loaded += 1
        return loaded

    def set(self, name: str, value: str) -> None:
        if name == INDEX:
            raise ValueError(f"{INDEX!r} is reserved")
        backend = self._backend()
        backend.set_password(self._service, name, value)
        self._write_index(sorted({*self.names(), name}))
        os.environ[name] = value
        logger.info("stored credential for %s in the keychain", name)

    def clear(self, name: str) -> None:
        known = self.names()
        if name not in known:
            return
        backend = self._backend()
        try:
            backend.delete_password(self._service, name)
        except Exception:  # noqa: BLE001 - backends raise their own missing-entry errors
            # Already gone from the keychain. Dropping it from the index is
            # still right: leaving it there means every start-up tries to load
            # an entry that is not coming back.
            logger.debug("no keychain entry for %s to delete", name)
        self._write_index([n for n in known if n != name])
        os.environ.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self.names()

    def names(self) -> list[str]:
        raw = self._backend().get_password(self._service, INDEX)
        return [n for n in (raw or "").split(",") if n]

    def _write_index(self, names: list[str]) -> None:
        self._backend().set_password(self._service, INDEX, ",".join(names))

    @staticmethod
    def _backend():
        import keyring

        return keyring
