"""The credential port.

`atlas.sources` records *which* environment variable holds a connection string.
This holds the string. Keeping them apart is what lets the source registry stay
safe to serve over an unauthenticated API, and it is the reason only this
package ever touches a secret.

Why there is no database implementation
---------------------------------------

Every other Atlas store gained one and this one deliberately does not. A
credential in Atlas's own PostgreSQL is a credential in every backup, every
replica, and every dump anyone takes to debug something — and the thing it
unlocks is a customer's warehouse. A file with `0600` on it is a worse secret
store in the abstract and a better one in practice, because its blast radius
stops at the machine.

What belongs here instead is an adapter for a real secret manager, and the port
exists so that is a new file rather than a change to every caller.

The contract
------------

Secrets are addressed by environment-variable name, because that is what a
`Source` records and what the connectors read. `load_into_environment` is the
only bulk operation and it never overwrites a variable that is already set: an
operator who exported one at the shell meant it, and an engine that silently
disagrees with its own operator is unarguable with.

No method returns a secret. A store can be asked whether it holds one, and can
be asked to make it live in this process, and that is all — a `get` would
invite the value into a log line, a traceback, or an API response.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SecretStore(ABC):
    @abstractmethod
    def load_into_environment(self) -> int:
        """Populate `os.environ` from the store, returning how many were set.

        A real environment variable always wins.
        """

    @abstractmethod
    def set(self, name: str, value: str) -> None:
        """Store a credential and make it live in this process.

        Setting the environment here is what removes the restart: connectors
        read URLs from it, so the next connection attempt uses the new value.
        """

    @abstractmethod
    def clear(self, name: str) -> None:
        """Forget a credential, and unset it here. Silent if absent."""

    @abstractmethod
    def has(self, name: str) -> bool:
        """Whether Atlas is holding this one.

        Distinct from whether the variable is set: an operator's exported value
        is not Atlas's to edit or delete, and the console needs to know which
        it is looking at before offering a change.
        """

    @abstractmethod
    def names(self) -> list[str]:
        """Which variables this store holds. Names only, never values — used to
        move credentials between stores without either one seeing the other's."""
