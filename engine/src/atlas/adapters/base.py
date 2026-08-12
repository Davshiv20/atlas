"""The port: what a database must provide to be catalogued.

Atlas core reasons about tables, columns, and observations. Adapters know one
dialect's syntax. Adding a warehouse means writing an adapter, not touching the
analysis.

Two boundaries matter here, and both are easy to blur:

**The adapter observes; core judges.** `execute_check` returns numbers and the
scope they were measured over — never a verdict. If each adapter decided
pass/fail, Postgres and Snowflake could reach different conclusions from
identical data and nothing would surface the disagreement.

**Constraints are declarations, not guarantees, until an engine says otherwise.**
Postgres refuses to violate a foreign key; Snowflake records one and never
checks it — except on hybrid tables, where it does. So enforcement is recorded
per constraint rather than assumed from the dialect.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self

from atlas.snapshot import Snapshot


@dataclass(frozen=True)
class DatabaseCapabilities:
    """What this engine guarantees, as distinct from what it accepts.

    Defaults describe a constraint-enforcing OLTP database. A warehouse adapter
    overrides them, and the overrides are the whole reason this exists — a port
    that exposed only syntax would let Snowflake's unenforced keys score as
    guarantees.
    """

    foreign_keys_enforced: bool = True
    supports_read_only_transaction: bool = True
    supports_statement_timeout: bool = True


# --- checks ----------------------------------------------------------------


@dataclass(frozen=True)
class GrainCheck:
    """Does one row correspond to one of the thing the key names?"""

    relation: str
    key_fields: list[str]
    type: str = "grain"


@dataclass(frozen=True)
class JoinCheck:
    """Do the source rows find a match, and how many?"""

    source_relation: str
    source_fields: list[str]
    target_relation: str
    target_fields: list[str]
    type: str = "join"


@dataclass(frozen=True)
class DistributionCheck:
    """What values occur, and how often."""

    relation: str
    field: str
    limit: int = 20
    type: str = "distribution"


@dataclass(frozen=True)
class NullabilityCheck:
    """How complete a column actually is, regardless of what NOT NULL says."""

    relation: str
    fields: list[str]
    type: str = "nullability"


@dataclass(frozen=True)
class OrderingCheck:
    """Does one timestamp always follow another?"""

    relation: str
    earlier_field: str
    later_field: str
    type: str = "ordering"


Check = GrainCheck | JoinCheck | DistributionCheck | NullabilityCheck | OrderingCheck


@dataclass
class CheckObservation:
    """What a check measured. Deliberately verdict-free.

    Core turns this into evidence and decides whether the assertion held. An
    adapter that returned `status` would be making a judgement its peers could
    make differently.
    """

    check_type: str
    observations: dict[str, Any] = field(default_factory=dict)
    complete_scan: bool = True
    rows_examined: int | None = None
    sampled: bool = False
    sample_fraction: float | None = None
    sql: str = ""
    limitations: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass
class ConnectionInfo:
    """What a successful connection can report about itself.

    A bare "ok" is weak confirmation: it proves a socket opened, not that the
    credentials reach the schema you named. The server banner and a table count
    over the target namespace are what let a reader tell "connected" from
    "connected to the wrong place".
    """

    server_version: str
    namespace: str
    table_count: int
    reachable: bool = True


class UnsupportedDatabase(RuntimeError):
    pass


class DatabaseAdapter(ABC):
    """One engine's way of answering the questions Atlas asks."""

    capabilities: DatabaseCapabilities = DatabaseCapabilities()

    #: Names the engine on every piece of evidence it produces. Declared on the
    #: port because `checks.py` has to record it and must not know that some
    #: adapters happen to be built on SQLAlchemy — it was reaching through to
    #: `adapter.engine.dialect` to get this.
    dialect: str = "unknown"

    @abstractmethod
    def test_connection(self) -> None:
        """Raise if the source is unreachable or the credentials are wrong.

        Called before any long job so a bad connection fails in the request
        that caused it, rather than minutes into a background run.
        """

    @abstractmethod
    def probe(self, namespace: str) -> ConnectionInfo:
        """Cheap confirmation: server version and how many tables are visible.

        Must stay to a couple of queries — this runs from a setup form, not a
        background job.
        """

    @abstractmethod
    def extract_structure(self, namespace: str) -> Snapshot:
        """Tables, columns, keys, indexes, comments.

        `namespace` is whatever addresses a schema on this engine: `public` on
        Postgres, `ANALYTICS.PUBLIC` on Snowflake. The adapter parses it.
        """

    @abstractmethod
    def profile(
        self, snapshot: Snapshot, on_table: Callable[[str], None] | None = None
    ) -> Snapshot:
        """Return a copy of the snapshot with column distributions filled in.

        `on_table` is called with each table name as it starts. Profiling is
        the slow half of an extract — a full aggregate sweep and a value scan
        per column — and without this the caller can only report that it
        began, which on a warehouse leaves minutes of silence."""

    @abstractmethod
    def execute_check(self, check: Check) -> CheckObservation:
        """Run one typed check and return what it measured."""

    @abstractmethod
    def close(self) -> None:
        """Release connections. Safe to call twice."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
