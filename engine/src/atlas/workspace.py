"""Per-workspace file storage.

Deliberately behind an interface. The files are fine for a single reviewer and
they make the intermediate state inspectable while the pipeline is still
changing shape, but concurrent reviewers need row-level writes rather than
whole-file rewrites. When that day comes, this class is what gets replaced.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from atlas.evidence import ClaimEvidence, EvidenceStore
from atlas.facts import Fact, FactStore
from atlas.output import SchemaOutput
from atlas.questions import Question, QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Snapshot

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class InvalidWorkspace(ValueError):
    pass


class WorkspaceConflict(ValueError):
    pass


class WorkspaceBusy(WorkspaceConflict):
    pass


class WorkspaceManifest(BaseModel):
    schema_version: int = 1
    id: str
    source_id: str
    incarnation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    snapshot_generation: int = Field(default=0, ge=0)

    @field_validator("id", "source_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not SAFE_NAME.match(value):
            raise ValueError("must be lowercase alphanumeric with - or _, max 63 chars")
        return value

    @field_validator("incarnation_id")
    @classmethod
    def valid_incarnation(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("incarnation_id must be a 32-character lowercase hex UUID")
        return value

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported workspace manifest schema_version")
        return value

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_yaml(path, self.model_dump(mode="json"))

    @classmethod
    def read(cls, path: Path) -> WorkspaceManifest:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Workspace:
    def __init__(self, name: str, root: Path | None = None) -> None:
        # A workspace name becomes a directory name, so it is validated rather
        # than sanitized — silently rewriting "../etc" to something safe hides
        # what was actually attempted.
        if not SAFE_NAME.match(name):
            raise InvalidWorkspace(
                "workspace names must be lowercase alphanumeric with - or _, max 63 chars"
            )
        self.name = name
        self.root = (root or get_settings().atlas_output_dir) / name

    def __repr__(self) -> str:
        return f"Workspace({self.name!r})"

    @property
    def manifest_path(self) -> Path:
        return self.root / "workspace.yaml"

    def generation_root(self, generation: int) -> Path:
        return self.root / "generations" / str(generation)

    def _active_path(self, filename: str) -> Path:
        if self.has_manifest():
            generation = self.read_manifest().snapshot_generation
            if generation > 0:
                return self.generation_root(generation) / filename
        # Legacy workspaces and newly-created generation-zero workspaces use
        # root paths only until migration/first publication commits a manifest
        # pointer to an immutable generation directory.
        return self.root / filename

    @property
    def snapshot_path(self) -> Path:
        return self._active_path("snapshot.yaml")

    @property
    def facts_path(self) -> Path:
        return self._active_path("facts.yaml")

    @property
    def questions_path(self) -> Path:
        return self._active_path("questions.yaml")

    @property
    def output_path(self) -> Path:
        return self._active_path("output.yaml")

    @property
    def evidence_path(self) -> Path:
        return self._active_path("evidence.yaml")

    def exists(self) -> bool:
        return self.snapshot_path.exists()

    def has_manifest(self) -> bool:
        return self.manifest_path.exists()

    def read_manifest(self) -> WorkspaceManifest:
        return WorkspaceManifest.read(self.manifest_path)

    def create_manifest(self, source_id: str) -> WorkspaceManifest:
        if self.manifest_path.exists():
            manifest = self.read_manifest()
            if manifest.source_id != source_id:
                raise WorkspaceConflict(
                    f"workspace {self.name!r} is already bound to source {manifest.source_id!r}"
                )
            return manifest
        if self.root.exists() and any(
            path.name != ".mutation.lock" for path in self.root.iterdir()
        ):
            raise WorkspaceConflict(
                f"workspace {self.name!r} already contains files; refusing to bind it implicitly"
            )
        manifest = WorkspaceManifest(id=self.name, source_id=source_id)
        manifest.write(self.manifest_path)
        return manifest

    def write_manifest(self, manifest: WorkspaceManifest) -> None:
        if manifest.id != self.name:
            raise WorkspaceConflict("manifest id does not match workspace directory")
        if self.manifest_path.exists():
            existing = self.read_manifest()
            if existing.source_id != manifest.source_id:
                raise WorkspaceConflict("workspace source_id cannot change")
        manifest.write(self.manifest_path)

    @contextmanager
    def mutation_lock(self, *, blocking: bool = True) -> Iterator[None]:
        """Coordinate API and CLI writers through one interprocess file lock."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".mutation.lock").open("a+") as handle:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as exc:
                raise WorkspaceBusy(
                    f"workspace {self.name!r} already has an active mutation"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_snapshot(self) -> Snapshot:
        return Snapshot.read(self.snapshot_path)

    def read_current_snapshot(self) -> Snapshot:
        manifest = self.read_manifest()
        snapshot = self.read_snapshot()
        self.validate_snapshot(manifest, snapshot)
        return snapshot

    def validate_snapshot(
        self, manifest: WorkspaceManifest | None = None, snapshot: Snapshot | None = None
    ) -> None:
        manifest = manifest or self.read_manifest()
        snapshot = snapshot or self.read_snapshot()
        if snapshot.source_id != manifest.source_id:
            raise WorkspaceConflict(
                f"snapshot source {snapshot.source_id!r} does not match workspace source {manifest.source_id!r}"
            )
        if snapshot.generation != manifest.snapshot_generation:
            raise WorkspaceConflict(
                f"snapshot generation {snapshot.generation!r} does not match workspace generation "
                f"{manifest.snapshot_generation}"
            )

    def assert_identity(
        self, *, source_id: str, incarnation_id: str, generation: int
    ) -> None:
        manifest = self.read_manifest()
        if manifest.source_id != source_id or manifest.incarnation_id != incarnation_id:
            raise WorkspaceConflict(
                "workspace identity or source binding changed; refusing stale write"
            )
        if manifest.snapshot_generation != generation:
            raise WorkspaceConflict(
                f"workspace generation changed from {generation} to "
                f"{manifest.snapshot_generation}; refusing stale write"
            )

    def assert_generation(self, generation: int) -> None:
        """Compatibility helper for callers that already hold the current manifest."""
        manifest = self.read_manifest()
        self.assert_identity(
            source_id=manifest.source_id,
            incarnation_id=manifest.incarnation_id,
            generation=generation,
        )

    def has_semantic_state(self) -> bool:
        return any(
            path.exists()
            for path in (self.facts_path, self.evidence_path, self.questions_path, self.output_path)
        )

    def reset_semantics(self) -> dict[str, int]:
        removed: dict[str, int] = {}
        for label, path in (
            ("facts", self.facts_path),
            ("evidence", self.evidence_path),
            ("questions", self.questions_path),
            ("output", self.output_path),
        ):
            if path.exists():
                path.unlink()
                removed[label] = 1
        return removed

    def migrate_legacy(self, source_id: str) -> WorkspaceManifest:
        """Copy root-level legacy state into generation 1, then commit its pointer."""
        if self.has_manifest():
            return self.read_manifest()
        legacy_snapshot = self.root / "snapshot.yaml"
        if not legacy_snapshot.exists():
            raise WorkspaceConflict(f"workspace {self.name!r} has no legacy snapshot")
        with self.mutation_lock():
            snapshot = Snapshot.read(legacy_snapshot)
            staging = self.root / "generations" / ".1.tmp"
            target = self.generation_root(1)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            snapshot.model_copy(update={"source_id": source_id, "generation": 1}).write(
                staging / "snapshot.yaml"
            )
            for filename in ("facts.yaml", "evidence.yaml", "questions.yaml", "output.yaml"):
                legacy = self.root / filename
                if legacy.exists():
                    shutil.copy2(legacy, staging / filename)
            Snapshot.read(staging / "snapshot.yaml")
            shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)
            manifest = WorkspaceManifest(id=self.name, source_id=source_id, snapshot_generation=1)
            # The manifest is the single active-generation pointer and commits last.
            manifest.write(self.manifest_path)
            for filename in ("snapshot.yaml", "facts.yaml", "evidence.yaml", "questions.yaml", "output.yaml"):
                legacy = self.root / filename
                if legacy.exists():
                    legacy.unlink()
            return manifest

    def publish_snapshot(
        self,
        snapshot: Snapshot,
        *,
        reset_semantics: bool = False,
        expected_source_id: str | None = None,
        expected_incarnation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> WorkspaceManifest:
        manifest = self.read_manifest()
        if expected_source_id is not None and expected_incarnation_id is not None:
            self.assert_identity(
                source_id=expected_source_id,
                incarnation_id=expected_incarnation_id,
                generation=(
                    manifest.snapshot_generation
                    if expected_generation is None
                    else expected_generation
                ),
            )
        if snapshot.source_id is not None and snapshot.source_id != manifest.source_id:
            raise WorkspaceConflict(
                f"cannot publish source {snapshot.source_id!r} into workspace bound to {manifest.source_id!r}"
            )
        if (
            manifest.snapshot_generation > 0
            and self.has_semantic_state()
            and not reset_semantics
        ):
            raise WorkspaceConflict(
                "refresh would carry semantic state into a new snapshot generation; "
                "reset_semantics must be explicit"
            )
        next_generation = manifest.snapshot_generation + 1
        staging = self.root / "generations" / f".{next_generation}.tmp"
        target = self.generation_root(next_generation)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        staged = snapshot.model_copy(
            update={"source_id": manifest.source_id, "generation": next_generation}
        )
        staged.write(staging / "snapshot.yaml")
        Snapshot.read(staging / "snapshot.yaml")
        # A prior crash may have completed this inactive generation but failed
        # before advancing the manifest pointer. It is safe to rebuild because
        # the current manifest still points at the previous generation.
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
        # The previous generation remains complete and readable until this one
        # atomic manifest replace advances the single active pointer.
        self.write_manifest(manifest.model_copy(update={"snapshot_generation": next_generation}))
        return self.read_manifest()

    def read_facts(self) -> FactStore:
        return FactStore.read(self.facts_path)

    def read_questions(self) -> list[Question]:
        """Validated, not raw dicts — the output builder needs objects, and an
        untyped list is how a dict reached it and crashed the compile."""
        return QuestionLog.read(self.questions_path).questions

    def read_output(self) -> SchemaOutput:
        return SchemaOutput.read(self.output_path)

    def read_evidence(self) -> EvidenceStore:
        return EvidenceStore.read(self.evidence_path)

    def absorb(
        self,
        table: str,
        facts: list[Fact],
        questions: list[Question],
        evidence: EvidenceStore,
    ) -> None:
        """Persist one table's analysis, merged into what is already stored.

        Called as each table finishes rather than once at the end of the run.
        A five-table run used to hold four tables' claims in one thread's
        memory until the fifth completed: the console could not show them, and
        an engine restart discarded twenty minutes of model spend. Both were the
        same missing write.
        """
        self.read_current_snapshot()
        self.read_facts().merge(facts).write(self.facts_path)

        kept = self.read_evidence()
        for record in evidence.records:
            kept.add(record)
        kept.links.extend(evidence.links)
        kept.write(self.evidence_path)

        # This table's questions are replaced, not appended: re-analysing it
        # asks its questions again, and two copies of one question is two
        # review decisions for one uncertainty. `merge` carries any answer
        # across, so re-running never asks a reviewer something they settled.
        stored = QuestionLog(questions=self.read_questions())
        others = [q for q in stored.questions if q.table != table]
        QuestionLog(
            questions=others + stored.merge(list(questions)).questions
        ).write(self.questions_path)

    def absorb_relationships(
        self,
        facts: list[Fact],
        links: list[ClaimEvidence],
        evidence: EvidenceStore,
    ) -> None:
        """Persist the mechanically-derived relationship map.

        Kept separate from `absorb` because it is not scoped to one table: a
        join belongs to two, and the map is rebuilt whole each run rather than
        merged table by table.

        Existing join claims are *replaced*, not merged. Relationships have a
        single owner now — derived mechanically from constraints and checks —
        and a model-authored claim about the same edge is a second answer to a
        settled question. Any human verdict on one is lost, which is the right
        trade: the replacement is enforced or verified, and carries more
        authority than the review it discards.
        """
        self.read_current_snapshot()
        stored = self.read_facts()
        kept = [f for f in stored.facts if f.aspect != "join"]
        FactStore(facts=kept).merge(facts).write(self.facts_path)

        kept = self.read_evidence()
        for record in evidence.records:
            kept.add(record)
        known = {link.claim_id for link in links}
        kept.links = [
            link for link in kept.links if link.claim_id not in known
        ] + list(links)
        kept.write(self.evidence_path)

    def drop(self, tables: set[str]) -> dict[str, int]:
        """Remove claims, questions, and evidence for these tables only.

        Scoped rather than wholesale so regenerating one table cannot discard
        review already done on another.

        Evidence is replaced rather than accumulated because records carry
        `valid_as_of`: keeping observations from several runs leaves nothing
        marking which is current, and a claim could end up citing an orphan
        count measured against a snapshot that no longer exists. The cost is
        that content-addressed history is lost for these tables — acceptable
        while regeneration is a testing affordance, not a production path.
        """
        self.read_current_snapshot()
        facts = FactStore.read(self.facts_path)
        kept_facts = [f for f in facts.facts if f.subject.split(".")[0] not in tables]

        evidence = self.read_evidence()
        kept_records = [
            r for r in evidence.records if not _touches(r.subjects, tables)
        ]
        kept_ids = {r.id for r in kept_records}
        kept_links = [
            link
            for link in evidence.links
            if link.evidence_id in kept_ids
            and link.claim_id.split("#")[0].split(".")[0] not in tables
        ]

        questions = QuestionLog.read(self.questions_path)
        kept_questions = [q for q in questions.questions if q.table not in tables]

        removed = {
            "claims": len(facts.facts) - len(kept_facts),
            "evidence": len(evidence.records) - len(kept_records),
            "questions": len(questions.questions) - len(kept_questions),
        }
        FactStore(facts=kept_facts).write(self.facts_path)
        EvidenceStore(records=kept_records, links=kept_links).write(self.evidence_path)
        QuestionLog(questions=kept_questions).write(self.questions_path)
        return removed

    @classmethod
    def list_all(cls, root: Path | None = None) -> list[str]:
        base = root or get_settings().atlas_output_dir
        if not base.exists():
            return []
        return sorted(
            d.name
            for d in base.iterdir()
            if d.is_dir() and ((d / "workspace.yaml").exists() or (d / "snapshot.yaml").exists())
        )

    @classmethod
    def referencing_source(cls, source_id: str, root: Path | None = None) -> list[str]:
        found: list[str] = []
        for name in cls.list_all(root):
            workspace = cls(name, root)
            if workspace.has_manifest():
                referenced = workspace.read_manifest().source_id
            elif workspace.snapshot_path.exists():
                referenced = workspace.read_snapshot().source_id
            else:
                referenced = None
            if referenced == source_id:
                found.append(name)
        return sorted(found)

    def delete(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)


def _atomic_write_yaml(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)


def _touches(subjects: list[str], tables: set[str]) -> bool:
    """Whether an evidence record concerns any of these tables.

    Subjects are `relation:schema.table` or `field:table.column`, so the table
    name is the last dotted segment's owner either way.
    """
    for subject in subjects:
        _, _, target = subject.partition(":")
        parts = [p for p in target.split(".") if p]
        if any(part in tables for part in parts):
            return True
    return False
