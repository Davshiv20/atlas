"""The source-declaration port.

Atlas's fourth store, and the one holding the least: which databases have been
declared, and the *name* of the environment variable each one's URL is in.
Never a URL. That is the whole design of `Source`, and moving the declarations
into a database must not quietly change it — a row here is still safe to read
by anyone who can reach the store, because there is nothing in it to steal.

Small enough to have been a file, and it was one. It is a port now because it
was the last thing forcing a local file on an install that had moved everything
else, and because two engine processes sharing a directory could interleave a
`read`, an `add`, and a `write` and lose a declaration.

What is deliberately not here
-----------------------------

**Credentials.** `atlas.secrets` holds those, separately and on purpose.

**Whether a source works.** `configured` reads this process's environment and
`resolve_url` fails loudly when nothing is there; connection health is checked,
kept in memory, and never stored, because a result is only meaningful for the
process that produced it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager

from atlas.sources.models import Source


class SourceRepository(ABC):
    """Where the declared sources are kept."""

    @abstractmethod
    def list(self) -> list[Source]:
        """Every declared source, in a stable order."""

    @abstractmethod
    def get(self, source_id: str) -> Source:
        """Raises `SourceNotFound`. Never returns a placeholder: a source that
        reads as present but empty is one a workspace would happily bind to."""

    @abstractmethod
    def add(self, source: Source) -> Source:
        """Declare a source. Raises `DuplicateSource` on a name already taken —
        overwriting one would repoint every workspace bound to it."""

    @abstractmethod
    def remove(self, source_id: str) -> None:
        """Undeclare a source. Raises `SourceNotFound` if it was not there.

        The caller checks first that no workspace still references it; that is
        a question about workspaces, which this store knows nothing about.
        """

    @abstractmethod
    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive access to the whole registry.

        Coarse on purpose, and held across operations that span two stores:
        declaring a workspace against a source, and deleting a source that no
        workspace may still reference, are the same decision seen from two
        sides. Without one lock over both checks, a source can be removed
        between the check that nothing references it and the write.
        """
        raise NotImplementedError
