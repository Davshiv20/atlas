"""The Atlas-owned PostgreSQL metadata store.

The same port as the YAML store, and the reason the port exists. Two things it
can do that a directory cannot:

**Roll back.** `transaction` is a real transaction. A composite operation in
`atlas.catalog` that raises halfway leaves nothing behind, where the file store
leaves whatever it had already written.

**Hold more than one writer honestly.** Claims, questions, and evidence are
rows, so a scoped write is an `INSERT ... ON CONFLICT DO UPDATE` touching the
records it names. Two reviewers settling different claims write different rows
and neither reads the other's.

Not to be confused with a *source* adapter. Those read someone else's database
and know its dialect; this one writes Atlas's own record and is the only place
in the engine that knows Atlas has a schema at all.

The schema lives in `migrations/`, not here. A store that creates its own
tables on connect has no way to change them later.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from atlas.evidence import ClaimEvidence, EvidenceRecord, EvidenceStore
from atlas.facts import Fact, FactStore
from atlas.manifest import WorkspaceManifest, require_valid_name
from atlas.metadata.base import (
    MetadataRepository,
    NoSnapshot,
    UnknownWorkspace,
    WorkspaceBusy,
    WorkspaceExists,
)
from atlas.questions import Question, QuestionLog
from atlas.snapshot import Snapshot


class PostgresMetadataRepository(MetadataRepository):
    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        self._url = url
        # `pool_pre_ping` because an engine sitting idle between analyses long
        # outlives a connection the database or a proxy decided to close, and
        # the failure surfaces as a dead connection mid-write rather than at
        # checkout.
        self._engine = engine or create_engine(url, pool_pre_ping=True, future=True)
        self._state = threading.local()

    def __repr__(self) -> str:
        return f"PostgresMetadataRepository({self._engine.url.render_as_string()!r})"

    def dispose(self) -> None:
        self._engine.dispose()

    # ---- connections -----------------------------------------------------

    @contextmanager
    def _borrow(self, workspace: str | None, *, write: bool) -> Iterator[Connection]:
        """The connection this thread is already using, or a fresh one.

        Joining an open connection is what makes every method here composable:
        a method called from inside `transaction` — or from inside another
        method — reads its own uncommitted writes and commits once with
        everything else, rather than opening a second transaction that
        deadlocks against the first on rows it is holding.
        """
        if workspace is not None:
            require_valid_name(workspace)
        active = getattr(self._state, "connection", None)
        if active is not None:
            yield active
            return
        opener = self._engine.begin if write else self._engine.connect
        with opener() as connection:
            self._state.connection = connection
            try:
                yield connection
            finally:
                self._state.connection = None

    def _read(self, workspace: str | None = None) -> Any:
        return self._borrow(workspace, write=False)

    def _write(self, workspace: str | None = None) -> Any:
        return self._borrow(workspace, write=True)

    # ---- lifecycle -------------------------------------------------------

    def exists(self, workspace: str) -> bool:
        with self._read(workspace) as connection:
            return _scalar(connection, _EXISTS, {"w": workspace}) is not None

    def has_snapshot(self, workspace: str) -> bool:
        with self._read(workspace) as connection:
            generation = _scalar(connection, _GENERATION, {"w": workspace})
            if not generation:
                return False
            return (
                _scalar(
                    connection,
                    "SELECT 1 FROM snapshots WHERE workspace = :w AND generation = :g",
                    {"w": workspace, "g": generation},
                )
                is not None
            )

    def has_semantics(self, workspace: str) -> bool:
        with self._read(workspace) as connection:
            generation = _scalar(connection, _GENERATION, {"w": workspace})
            if generation is None:
                return False
            params = {"w": workspace, "g": generation}
            return any(
                _scalar(connection, f"SELECT 1 FROM {table} WHERE workspace = :w "
                        "AND generation = :g LIMIT 1", params) is not None
                for table in ("claims", "questions", "evidence_records")
            )

    def list_workspaces(self) -> list[str]:
        with self._read() as connection:
            return [
                row[0]
                for row in connection.execute(text("SELECT name FROM workspaces ORDER BY name"))
            ]

    def create(self, workspace: str, manifest: WorkspaceManifest) -> None:
        with self._write(workspace) as connection:
            if _scalar(connection, _EXISTS, {"w": workspace}) is not None:
                raise WorkspaceExists(f"workspace {workspace!r} already exists")
            connection.execute(
                text(
                    "INSERT INTO workspaces "
                    "(name, schema_version, source_id, incarnation_id, created_at, "
                    " snapshot_generation) "
                    "VALUES (:name, :schema_version, :source_id, :incarnation_id, "
                    "        :created_at, :snapshot_generation)"
                ),
                {
                    "name": workspace,
                    "schema_version": manifest.schema_version,
                    "source_id": manifest.source_id,
                    "incarnation_id": manifest.incarnation_id,
                    "created_at": manifest.created_at,
                    "snapshot_generation": manifest.snapshot_generation,
                },
            )

    def adopt(self, workspace: str, source_id: str) -> WorkspaceManifest:
        """There is no unregistered data here, so there is nothing to adopt.

        A row exists because something inserted it, and inserting one requires
        a manifest. Answering `UnknownWorkspace` is the honest reply; creating
        an empty workspace so the caller has something to open would invent a
        binding nobody made.
        """
        if self.exists(workspace):
            return self.read_manifest(workspace)
        raise UnknownWorkspace(
            f"workspace {workspace!r} does not exist in this store; "
            "a database store holds no pre-manifest data to adopt"
        )

    def delete(self, workspace: str) -> None:
        with self._write(workspace) as connection:
            connection.execute(
                text("DELETE FROM workspaces WHERE name = :w"), {"w": workspace}
            )

    # ---- manifest --------------------------------------------------------

    def read_manifest(self, workspace: str) -> WorkspaceManifest:
        with self._read(workspace) as connection:
            row = connection.execute(
                text(
                    "SELECT name, schema_version, source_id, incarnation_id, created_at, "
                    "       snapshot_generation "
                    "FROM workspaces WHERE name = :w"
                ),
                {"w": workspace},
            ).mappings().first()
        if row is None:
            raise UnknownWorkspace(f"workspace {workspace!r} is not registered")
        return WorkspaceManifest(
            id=row["name"],
            schema_version=row["schema_version"],
            source_id=row["source_id"],
            incarnation_id=row["incarnation_id"],
            created_at=row["created_at"],
            snapshot_generation=row["snapshot_generation"],
        )

    def write_manifest(self, workspace: str, manifest: WorkspaceManifest) -> None:
        """Insert or replace. Unlike `create` this does not refuse an existing
        workspace: the caller has already decided the manifest it is holding is
        the one that should stand, and `atlas.catalog` is where the rules about
        that live."""
        with self._write(workspace) as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (name, schema_version, source_id, "
                    "  incarnation_id, created_at, snapshot_generation) "
                    "VALUES (:name, :schema_version, :source_id, :incarnation_id, "
                    "        :created_at, :snapshot_generation) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "  schema_version = EXCLUDED.schema_version, "
                    "  source_id = EXCLUDED.source_id, "
                    "  incarnation_id = EXCLUDED.incarnation_id, "
                    "  created_at = EXCLUDED.created_at, "
                    "  snapshot_generation = EXCLUDED.snapshot_generation"
                ),
                {
                    "name": workspace,
                    "schema_version": manifest.schema_version,
                    "source_id": manifest.source_id,
                    "incarnation_id": manifest.incarnation_id,
                    "created_at": manifest.created_at,
                    "snapshot_generation": manifest.snapshot_generation,
                },
            )

    # ---- snapshots -------------------------------------------------------

    def publish_snapshot(self, workspace: str, snapshot: Snapshot) -> WorkspaceManifest:
        """One transaction: the generation and the pointer to it land together.

        The file store had to write the generation, then the pointer, and
        reason about a crash between them. Here there is no between.
        """
        with self.transaction(workspace):
            manifest = self.read_manifest(workspace)
            generation = manifest.snapshot_generation + 1
            staged = snapshot.model_copy(
                update={"source_id": manifest.source_id, "generation": generation}
            )
            with self._write(workspace) as connection:
                connection.execute(
                    text(
                        "INSERT INTO snapshots (workspace, generation, document) "
                        "VALUES (:w, :g, CAST(:document AS jsonb))"
                    ),
                    {"w": workspace, "g": generation, "document": _json(staged)},
                )
                connection.execute(
                    text(
                        "UPDATE workspaces SET snapshot_generation = :g WHERE name = :w"
                    ),
                    {"w": workspace, "g": generation},
                )
            return self.read_manifest(workspace)

    def read_snapshot(self, workspace: str) -> Snapshot:
        with self._read(workspace) as connection:
            document = _scalar(
                connection,
                "SELECT s.document FROM snapshots s JOIN workspaces w "
                "  ON w.name = s.workspace AND w.snapshot_generation = s.generation "
                "WHERE s.workspace = :w",
                {"w": workspace},
            )
        if document is None:
            raise NoSnapshot(f"workspace {workspace!r} has no snapshot")
        return Snapshot.model_validate(document)

    # ---- reading ---------------------------------------------------------

    def read_facts(self, workspace: str) -> FactStore:
        rows = self._documents(workspace, "claims", "ORDER BY claim_id")
        return FactStore(facts=[Fact.model_validate(row) for row in rows])

    def read_questions(self, workspace: str) -> QuestionLog:
        rows = self._documents(workspace, "questions", "ORDER BY question_id")
        return QuestionLog(questions=[Question.model_validate(row) for row in rows])

    def read_evidence(self, workspace: str) -> EvidenceStore:
        records = self._documents(workspace, "evidence_records", "ORDER BY record_id")
        links = self._documents(
            workspace, "evidence_links", "ORDER BY claim_id, record_id, relationship"
        )
        return EvidenceStore(
            records=[EvidenceRecord.model_validate(row) for row in records],
            links=[ClaimEvidence.model_validate(row) for row in links],
        )

    def _documents(self, workspace: str, table: str, order: str) -> list[dict[str, Any]]:
        with self._read(workspace) as connection:
            generation = _scalar(connection, _GENERATION, {"w": workspace})
            if generation is None:
                return []
            return [
                row[0]
                for row in connection.execute(
                    text(
                        f"SELECT document FROM {table} "
                        f"WHERE workspace = :w AND generation = :g {order}"
                    ),
                    {"w": workspace, "g": generation},
                )
            ]

    # ---- scoped writes ---------------------------------------------------

    def upsert_facts(self, workspace: str, facts: list[Fact]) -> None:
        if not facts:
            return
        with self._write(workspace) as connection:
            generation = self._require_generation(connection, workspace)
            connection.execute(
                text(
                    "INSERT INTO claims (workspace, generation, claim_id, subject, aspect, "
                    "                    status, consequence, confidence, document) "
                    "VALUES (:w, :g, :claim_id, :subject, :aspect, :status, :consequence, "
                    "        :confidence, CAST(:document AS jsonb)) "
                    "ON CONFLICT (workspace, generation, claim_id) DO UPDATE SET "
                    "  subject = EXCLUDED.subject, aspect = EXCLUDED.aspect, "
                    "  status = EXCLUDED.status, consequence = EXCLUDED.consequence, "
                    "  confidence = EXCLUDED.confidence, document = EXCLUDED.document"
                ),
                [
                    {
                        "w": workspace,
                        "g": generation,
                        "claim_id": fact.id,
                        "subject": fact.subject,
                        "aspect": fact.aspect,
                        "status": fact.status.value,
                        "consequence": fact.consequence.value,
                        "confidence": fact.confidence,
                        "document": _json_fact(fact),
                    }
                    for fact in facts
                ],
            )

    def remove_facts(self, workspace: str, ids: Collection[str]) -> int:
        return self._remove(workspace, "claims", "claim_id", ids)

    def upsert_questions(self, workspace: str, questions: list[Question]) -> None:
        if not questions:
            return
        with self._write(workspace) as connection:
            generation = self._require_generation(connection, workspace)
            connection.execute(
                text(
                    "INSERT INTO questions (workspace, generation, question_id, subject, "
                    "                       relation, status, document) "
                    "VALUES (:w, :g, :question_id, :subject, :relation, :status, "
                    "        CAST(:document AS jsonb)) "
                    "ON CONFLICT (workspace, generation, question_id) DO UPDATE SET "
                    "  subject = EXCLUDED.subject, relation = EXCLUDED.relation, "
                    "  status = EXCLUDED.status, document = EXCLUDED.document"
                ),
                [
                    {
                        "w": workspace,
                        "g": generation,
                        "question_id": question.id,
                        "subject": question.subject,
                        "relation": question.table,
                        "status": question.status.value,
                        "document": _json(question),
                    }
                    for question in questions
                ],
            )

    def remove_questions(self, workspace: str, ids: Collection[str]) -> int:
        return self._remove(workspace, "questions", "question_id", ids)

    def append_evidence(self, workspace: str, evidence: EvidenceStore) -> None:
        if not evidence.records and not evidence.links:
            return
        with self._write(workspace) as connection:
            generation = self._require_generation(connection, workspace)
            if evidence.records:
                # `DO NOTHING`, because a record is content-addressed: the same
                # id is the identical observation and there is nothing to
                # update it to.
                connection.execute(
                    text(
                        "INSERT INTO evidence_records "
                        "  (workspace, generation, record_id, subjects, document) "
                        "VALUES (:w, :g, :record_id, :subjects, CAST(:document AS jsonb)) "
                        "ON CONFLICT (workspace, generation, record_id) DO NOTHING"
                    ),
                    [
                        {
                            "w": workspace,
                            "g": generation,
                            "record_id": record.id,
                            "subjects": list(record.subjects),
                            "document": _json(record),
                        }
                        for record in evidence.records
                    ],
                )
            if evidence.links:
                connection.execute(
                    text(
                        "INSERT INTO evidence_links "
                        "  (workspace, generation, claim_id, record_id, relationship, document) "
                        "VALUES (:w, :g, :claim_id, :record_id, :relationship, "
                        "        CAST(:document AS jsonb)) "
                        "ON CONFLICT (workspace, generation, claim_id, record_id, relationship) "
                        "DO NOTHING"
                    ),
                    [
                        {
                            "w": workspace,
                            "g": generation,
                            "claim_id": link.claim_id,
                            "record_id": link.evidence_id,
                            "relationship": link.relationship.value,
                            "document": _json(link),
                        }
                        for link in evidence.links
                        # A link to a record neither stored nor arriving would
                        # violate the foreign key. Dropping it is right: it
                        # cites an observation that does not exist.
                        if self._record_present(connection, workspace, generation, link)
                        or any(record.id == link.evidence_id for record in evidence.records)
                    ],
                )

    # ---- wholesale writes ------------------------------------------------

    # Each clears and refills inside one `_write`, so the replacement is a
    # single transaction. Committing the delete and then failing the insert
    # would leave the collection empty, which is the one outcome no caller
    # asked for.

    def write_facts(self, workspace: str, facts: FactStore) -> None:
        with self._write(workspace) as connection:
            self._clear(connection, workspace, "claims")
            self.upsert_facts(workspace, facts.facts)

    def write_questions(self, workspace: str, questions: QuestionLog) -> None:
        with self._write(workspace) as connection:
            self._clear(connection, workspace, "questions")
            self.upsert_questions(workspace, questions.questions)

    def write_evidence(self, workspace: str, evidence: EvidenceStore) -> None:
        with self._write(workspace) as connection:
            # Links go with their records through the cascade, but a link whose
            # record survives elsewhere would not, so both are cleared.
            self._clear(connection, workspace, "evidence_links")
            self._clear(connection, workspace, "evidence_records")
            self.append_evidence(workspace, evidence)

    def clear_semantics(self, workspace: str) -> dict[str, int]:
        removed: dict[str, int] = {}
        with self._write(workspace) as connection:
            generation = _scalar(connection, _GENERATION, {"w": workspace})
            if generation is None:
                return removed
            for label, table in (
                ("facts", "claims"),
                ("evidence", "evidence_records"),
                ("questions", "questions"),
            ):
                count = connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace = :w AND generation = :g"),
                    {"w": workspace, "g": generation},
                ).rowcount
                if count:
                    removed[label] = count
        return removed

    # ---- helpers ---------------------------------------------------------

    def _clear(self, connection: Connection, workspace: str, table: str) -> int:
        generation = self._require_generation(connection, workspace)
        return connection.execute(
            text(f"DELETE FROM {table} WHERE workspace = :w AND generation = :g"),
            {"w": workspace, "g": generation},
        ).rowcount

    def _require_generation(self, connection: Connection, workspace: str) -> int:
        generation = _scalar(connection, _GENERATION, {"w": workspace})
        if generation is None:
            raise UnknownWorkspace(f"workspace {workspace!r} is not registered")
        return generation

    def _remove(
        self, workspace: str, table: str, column: str, ids: Collection[str]
    ) -> int:
        if not ids:
            return 0
        with self._write(workspace) as connection:
            generation = _scalar(connection, _GENERATION, {"w": workspace})
            if generation is None:
                return 0
            return connection.execute(
                text(
                    f"DELETE FROM {table} WHERE workspace = :w AND generation = :g "
                    f"AND {column} = ANY(:ids)"
                ),
                {"w": workspace, "g": generation, "ids": list(ids)},
            ).rowcount

    def _record_present(
        self, connection: Connection, workspace: str, generation: int, link: ClaimEvidence
    ) -> bool:
        return (
            _scalar(
                connection,
                "SELECT 1 FROM evidence_records WHERE workspace = :w AND generation = :g "
                "AND record_id = :r",
                {"w": workspace, "g": generation, "r": link.evidence_id},
            )
            is not None
        )

    # ---- concurrency -----------------------------------------------------

    @contextmanager
    def transaction(self, workspace: str, *, blocking: bool = True) -> Iterator[None]:
        """A real transaction, held open for the whole composite operation.

        A session-scoped advisory lock serializes writers across processes, and
        the surrounding transaction is what makes a failure leave nothing
        behind — the thing the file store cannot offer and the reason to be
        here at all.

        Re-entrant within a thread, like the file store's: the request path
        takes the lock in the write guard and then again in the scoped write
        underneath. Nested entry joins the open transaction rather than opening
        a second one, so the whole request still commits or rolls back once.
        """
        require_valid_name(workspace)
        depth = getattr(self._state, "depth", 0)
        if depth:
            self._state.depth = depth + 1
            try:
                yield
            finally:
                self._state.depth -= 1
            return

        key = _advisory_key(workspace)
        with self._engine.begin() as connection:
            if blocking:
                connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            else:
                acquired = _scalar(
                    connection, "SELECT pg_try_advisory_xact_lock(:k)", {"k": key}
                )
                if not acquired:
                    raise WorkspaceBusy(
                        f"workspace {workspace!r} already has an active mutation"
                    )
            self._state.connection = connection
            self._state.depth = 1
            try:
                yield
            finally:
                self._state.connection = None
                self._state.depth = 0


_EXISTS = "SELECT 1 FROM workspaces WHERE name = :w"
_GENERATION = "SELECT snapshot_generation FROM workspaces WHERE name = :w"


def _scalar(connection: Connection, sql: str, params: dict[str, Any]) -> Any:
    return connection.execute(text(sql), params).scalar()


def _json(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json", exclude_none=True))


def _json_fact(fact: Fact) -> str:
    """As `_json`, minus the derived endorsement.

    Derived state has no business in the durable record: a value on disk
    outlives the code that produced it, and renaming one of its states once
    left every stored workspace unreadable.
    """
    return json.dumps(
        fact.model_dump(mode="json", exclude_none=True, exclude={"endorsement"})
    )


def _advisory_key(workspace: str) -> int:
    """A stable signed 64-bit lock key for a workspace name.

    Derived in Python rather than with `hashtext`, whose result is not
    guaranteed stable across PostgreSQL versions — an upgrade that changed it
    would silently stop two processes locking each other out.
    """
    digest = hashlib.sha256(workspace.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)
