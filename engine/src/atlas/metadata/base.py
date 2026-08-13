"""The metadata persistence port.

Atlas owns two kinds of storage and they are not the same shape. A *source*
adapter reads someone else's database and is allowed to know everything about
that database's dialect. A *metadata* adapter stores Atlas's own record — the
snapshots it captured, the claims it inferred, the evidence behind them, and
the decisions a human made — and is allowed to know nothing about the domain.

This module is the second port. It exists so the durable record can move from
YAML files to Atlas-owned PostgreSQL without any caller learning that it moved.

What is deliberately *not* here
-------------------------------

**Composite operations.** Absorbing a table's analysis, dropping a table's
semantics, or rebuilding the relationship map are read-modify-write sequences
carrying product policy: a re-analysed table's questions are replaced rather
than appended, evidence is superseded rather than accumulated because records
carry `valid_as_of`. Policy belongs to `atlas.catalog`, which composes the
methods below inside one `transaction`. Putting it here would give every future
adapter the same policy to reimplement, and the second implementation would get
it subtly wrong.

**Storage mechanics.** `publish_snapshot` states the intent — this snapshot
becomes the active generation, atomically, or not at all. How that is achieved
is the adapter's business: a staging directory and `os.replace` for files, a
transaction for a database. A port that named the staging directory would not
be implementable by anything but a filesystem.

**Validation.** Whether a snapshot may be published into a workspace is a
question about identity and generation, not about storage. `atlas.catalog`
asks it, using `read_manifest`.

**Serialization.** No method here takes or returns a path, a document, a row,
or a connection. Those are one implementation's vocabulary, and a port that
speaks them has already chosen that implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager

from atlas.evidence import EvidenceStore
from atlas.facts import FactStore
from atlas.manifest import WorkspaceManifest
from atlas.output import SchemaOutput
from atlas.questions import QuestionLog
from atlas.snapshot import Snapshot


class MetadataRepositoryError(RuntimeError):
    """Storage could not satisfy the request. Never raised for a domain refusal."""


class UnknownWorkspace(MetadataRepositoryError):
    """Named workspace does not exist in this store."""


class WorkspaceExists(MetadataRepositoryError):
    """`create` was asked to register a name the store already holds."""


class NoSnapshot(MetadataRepositoryError):
    """The workspace holds no snapshot. Distinct from `UnknownWorkspace`: one
    is answered by extracting, the other by creating."""


class WorkspaceBusy(MetadataRepositoryError):
    """Another writer holds this workspace. Raised only by a non-blocking
    `transaction`, so a caller can answer 409 instead of waiting."""


class MetadataRepository(ABC):
    """Where Atlas keeps its own record.

    Every method is scoped to one workspace name rather than binding a
    repository to a workspace. A workspace is data in this store, not a handle
    to a directory, and the API serves many of them per process.

    Reads return domain models. Writes take them.
    """

    # ---- lifecycle -------------------------------------------------------

    @abstractmethod
    def exists(self, workspace: str) -> bool:
        """Whether this store has a manifest for the workspace — that is,
        whether the workspace is registered, not whether it holds data."""

    @abstractmethod
    def has_snapshot(self, workspace: str) -> bool:
        """Whether the active generation has a readable snapshot. False for a
        workspace that has been created but never extracted."""

    @abstractmethod
    def has_semantics(self, workspace: str) -> bool:
        """Whether any claim, question, or evidence record is stored for the
        active generation. Asked before anything that would strand them."""

    @abstractmethod
    def list_workspaces(self) -> list[str]:
        """Every workspace this store holds, registered or not, in a stable
        order. Unregistered ones exist because earlier versions of Atlas wrote
        data before manifests existed; `adopt` is how they are brought in."""

    @abstractmethod
    def create(self, workspace: str, manifest: WorkspaceManifest) -> None:
        """Register a new workspace. Raises if it already exists — creating
        over a live workspace is how review work disappears."""

    @abstractmethod
    def adopt(self, workspace: str, source_id: str) -> WorkspaceManifest:
        """Bring unregistered data under management, bound to `source_id`.

        A store that cannot hold unregistered data raises `UnknownWorkspace`,
        which is the honest answer rather than a silently created empty
        workspace. The caller has already established that `source_id` is
        declared; this does not re-ask.
        """

    @abstractmethod
    def delete(self, workspace: str) -> None:
        """Remove the workspace and everything in it. Silent if absent."""

    # ---- manifest --------------------------------------------------------

    @abstractmethod
    def read_manifest(self, workspace: str) -> WorkspaceManifest:
        """Raises `UnknownWorkspace` rather than returning a default. A
        manifest that reads as empty is indistinguishable from one that was
        never written, and the difference decides whether data is discarded."""

    @abstractmethod
    def write_manifest(self, workspace: str, manifest: WorkspaceManifest) -> None: ...

    # ---- snapshots -------------------------------------------------------

    @abstractmethod
    def publish_snapshot(self, workspace: str, snapshot: Snapshot) -> WorkspaceManifest:
        """Make `snapshot` the active generation and return the advanced
        manifest.

        Atomic: on failure the previous generation stays active and readable.
        The store stamps the snapshot with the workspace's source and the
        generation it is being published as — a generation number means
        nothing outside the store that assigns it.

        The caller has already decided that publishing is allowed, including
        what happens to semantic state the new generation will not carry. This
        does not re-ask.
        """

    @abstractmethod
    def read_snapshot(self, workspace: str) -> Snapshot:
        """The active generation's snapshot. Raises `NoSnapshot` when there is
        none — an empty snapshot and an unextracted workspace are different
        things and only one of them is worth analysing.

        Readable for an unregistered workspace, which is how a caller learns
        what source to `adopt` it into.
        """

    # ---- semantic state --------------------------------------------------
    #
    # Each returns an empty store rather than raising when nothing has been
    # written. A workspace that has been extracted but never analysed is an
    # ordinary state, not an error, and it is most of a workspace's life.

    @abstractmethod
    def read_facts(self, workspace: str) -> FactStore: ...

    @abstractmethod
    def write_facts(self, workspace: str, facts: FactStore) -> None: ...

    @abstractmethod
    def read_questions(self, workspace: str) -> QuestionLog: ...

    @abstractmethod
    def write_questions(self, workspace: str, questions: QuestionLog) -> None: ...

    @abstractmethod
    def read_evidence(self, workspace: str) -> EvidenceStore: ...

    @abstractmethod
    def write_evidence(self, workspace: str, evidence: EvidenceStore) -> None: ...

    @abstractmethod
    def clear_semantics(self, workspace: str) -> dict[str, int]:
        """Discard claims, questions, evidence, and the projection. Returns
        what was removed, per kind, so a caller can report it."""

    # ---- projection ------------------------------------------------------
    #
    # `SchemaOutput` is derived from snapshot plus facts plus evidence, and the
    # API rebuilds it per request rather than serving what is stored. It is
    # written as an export — something a person or another tool reads out of
    # band — which is why there is no `read_output` here: nothing in Atlas may
    # treat it as a source. Invariant 12.

    @abstractmethod
    def write_output(self, workspace: str, output: SchemaOutput) -> None: ...

    # ---- concurrency -----------------------------------------------------

    @abstractmethod
    @contextmanager
    def transaction(self, workspace: str, *, blocking: bool = True) -> Iterator[None]:
        """Exclusive write access to one workspace.

        Every composite operation in `atlas.catalog` runs inside one of these,
        so an adapter that can offer real atomicity should: writes made in a
        transaction that raises must not be visible afterwards. The YAML store
        cannot promise that and says so in its own docstring — which is the
        honest reason to want this port implemented against a database.

        `blocking=False` raises `WorkspaceBusy` instead of waiting.
        """
        raise NotImplementedError
