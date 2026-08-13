"""The domain service over Atlas's own record.

Everything here is policy about a workspace: what may be published into it,
what a re-analysed table replaces rather than appends, what a regeneration is
allowed to discard. None of it knows where the record is kept — a
`MetadataRepository` does that, and this module composes its methods inside one
`transaction` per operation.

The split matters because the alternative was tried. When policy lived on the
storage object, every caller reached for a path when it wanted data, and a
second store could not be written without reimplementing the merge rules that
decide whether a reviewer's verdict survives.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from atlas.evidence import ClaimEvidence, EvidenceStore
from atlas.facts import Fact, FactStore
from atlas.manifest import InvalidWorkspace, WorkspaceManifest, require_valid_name
from atlas.metadata import get_repository
from atlas.metadata.base import (
    MetadataRepository,
    NoSnapshot,
    UnknownWorkspace,
    WorkspaceExists,
)
from atlas.questions import Question, QuestionLog
from atlas.snapshot import Snapshot

__all__ = [
    "Catalog",
    "InvalidWorkspace",
    "WorkspaceConflict",
    "list_workspaces",
    "referencing_source",
]


class WorkspaceConflict(ValueError):
    """The request is coherent but the workspace's state forbids it.

    Never a storage failure. Every one of these is a rule Atlas is enforcing —
    an immutable source binding, a stale generation, semantic state that a
    refresh would strand — and each maps to a 409.
    """


class Catalog:
    """One workspace's semantic record, and the rules for changing it."""

    def __init__(self, name: str, repository: MetadataRepository | None = None) -> None:
        self.name = require_valid_name(name)
        self.repository = repository or get_repository()

    def __repr__(self) -> str:
        return f"Catalog({self.name!r})"

    # ---- registration ----------------------------------------------------

    def is_registered(self) -> bool:
        return self.repository.exists(self.name)

    def has_snapshot(self) -> bool:
        return self.repository.has_snapshot(self.name)

    def has_semantics(self) -> bool:
        return self.repository.has_semantics(self.name)

    def manifest(self) -> WorkspaceManifest:
        try:
            return self.repository.read_manifest(self.name)
        except UnknownWorkspace as exc:
            raise WorkspaceConflict(str(exc)) from exc

    def register(self, source_id: str) -> WorkspaceManifest:
        """Bind this workspace to a source, once and for all.

        Binding again to the same source is the idempotent case and returns the
        existing manifest. Binding to a different one is refused: a workspace's
        claims are about one database, and rebinding would leave every stored
        claim describing something else.
        """
        if self.is_registered():
            manifest = self.manifest()
            if manifest.source_id != source_id:
                raise WorkspaceConflict(
                    f"workspace {self.name!r} is already bound to source {manifest.source_id!r}"
                )
            return manifest
        manifest = WorkspaceManifest(id=self.name, source_id=source_id)
        try:
            self.repository.create(self.name, manifest)
        except WorkspaceExists as exc:
            raise WorkspaceConflict(str(exc)) from exc
        return manifest

    def adopt(self, source_id: str) -> WorkspaceManifest:
        """Register data that predates manifests, binding it to `source_id`.

        The caller has already confirmed the source is declared. Refusing here
        instead would leave the workspace visible but unopenable.
        """
        try:
            return self.repository.adopt(self.name, source_id)
        except UnknownWorkspace as exc:
            raise WorkspaceConflict(str(exc)) from exc

    def write_manifest(self, manifest: WorkspaceManifest) -> None:
        if manifest.id != self.name:
            raise WorkspaceConflict("manifest id does not match the workspace it names")
        if self.is_registered() and self.manifest().source_id != manifest.source_id:
            raise WorkspaceConflict("workspace source_id cannot change")
        self.repository.write_manifest(self.name, manifest)

    def delete(self) -> None:
        self.repository.delete(self.name)

    # ---- identity --------------------------------------------------------

    def assert_identity(
        self, *, source_id: str, incarnation_id: str, generation: int
    ) -> None:
        """Refuse a write planned against a workspace that has since moved.

        Extraction and analysis take minutes. In that time the workspace can be
        deleted and recreated against another source, or refreshed to a new
        generation — and the run still holding the old identity would otherwise
        write its results over the new one.
        """
        manifest = self.manifest()
        if manifest.source_id != source_id or manifest.incarnation_id != incarnation_id:
            raise WorkspaceConflict(
                "workspace identity or source binding changed; refusing stale write"
            )
        if manifest.snapshot_generation != generation:
            raise WorkspaceConflict(
                f"workspace generation changed from {generation} to "
                f"{manifest.snapshot_generation}; refusing stale write"
            )

    def validate_snapshot(
        self, manifest: WorkspaceManifest | None = None, snapshot: Snapshot | None = None
    ) -> None:
        manifest = manifest or self.manifest()
        snapshot = snapshot or self._snapshot()
        if snapshot.source_id != manifest.source_id:
            raise WorkspaceConflict(
                f"snapshot source {snapshot.source_id!r} does not match workspace "
                f"source {manifest.source_id!r}"
            )
        if snapshot.generation != manifest.snapshot_generation:
            raise WorkspaceConflict(
                f"snapshot generation {snapshot.generation!r} does not match workspace "
                f"generation {manifest.snapshot_generation}"
            )

    # ---- reading ---------------------------------------------------------

    def _snapshot(self) -> Snapshot:
        try:
            return self.repository.read_snapshot(self.name)
        except NoSnapshot as exc:
            raise WorkspaceConflict(str(exc)) from exc

    def snapshot(self) -> Snapshot:
        """The active snapshot, checked against the manifest that points at it.

        Always the checked read. An unchecked one is available to the two
        callers that need it before a manifest exists — listing and adoption —
        and to nothing else, because reading a snapshot from a generation the
        workspace has moved past is how claims get attached to a schema that is
        no longer there.
        """
        snapshot = self._snapshot()
        self.validate_snapshot(self.manifest(), snapshot)
        return snapshot

    def unchecked_snapshot(self) -> Snapshot:
        """For the pre-registration path only: what source does this data claim
        to have come from?"""
        return self._snapshot()

    def facts(self) -> FactStore:
        return self.repository.read_facts(self.name)

    def questions(self) -> QuestionLog:
        return self.repository.read_questions(self.name)

    def evidence(self) -> EvidenceStore:
        return self.repository.read_evidence(self.name)

    def write_facts(self, facts: FactStore) -> None:
        self.repository.write_facts(self.name, facts)

    def write_questions(self, questions: QuestionLog) -> None:
        self.repository.write_questions(self.name, questions)

    def write_evidence(self, evidence: EvidenceStore) -> None:
        self.repository.write_evidence(self.name, evidence)

    # ---- writing ---------------------------------------------------------

    @contextmanager
    def mutation(self, *, blocking: bool = True) -> Iterator[None]:
        with self.repository.transaction(self.name, blocking=blocking):
            yield

    def publish(
        self,
        snapshot: Snapshot,
        *,
        reset_semantics: bool = False,
        expected_source_id: str | None = None,
        expected_incarnation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> WorkspaceManifest:
        """Make a freshly extracted snapshot the active generation.

        Semantic state does not cross a generation boundary — claims are about
        columns that may no longer exist — so a workspace holding any is
        refused unless the caller says explicitly that discarding it is
        intended.

        Deliberately not conditioned on the current generation. Publishing out
        of generation zero is the case where semantic state is not carried but
        *abandoned*: the record stays where it was written while every accessor
        moves to the new generation, so a reviewer's work disappears from the
        product while still occupying storage. Skipping the guard there
        protected the smaller loss and waved through the total one.
        """
        manifest = self.manifest()
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
                f"cannot publish source {snapshot.source_id!r} into workspace bound "
                f"to {manifest.source_id!r}"
            )
        if self.has_semantics() and not reset_semantics:
            raise WorkspaceConflict(
                "refresh would discard semantic state that a new snapshot "
                "generation cannot carry; reset_semantics must be explicit"
            )
        return self.repository.publish_snapshot(self.name, snapshot)

    def reset_semantics(self) -> dict[str, int]:
        return self.repository.clear_semantics(self.name)

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
        an engine restart discarded twenty minutes of model spend. Both were
        the same missing write.
        """
        self.snapshot()
        self.write_facts(self.facts().merge(facts))

        kept = self.evidence()
        for record in evidence.records:
            kept.add(record)
        kept.links.extend(evidence.links)
        self.write_evidence(kept)

        # This table's questions are replaced, not appended: re-analysing it
        # asks its questions again, and two copies of one question is two
        # review decisions for one uncertainty. `merge` carries any answer
        # across, so re-running never asks a reviewer something they settled.
        stored = self.questions()
        others = [q for q in stored.questions if q.table != table]
        self.write_questions(
            QuestionLog(questions=others + stored.merge(list(questions)).questions)
        )

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
        self.snapshot()
        stored = self.facts()
        kept_facts = [f for f in stored.facts if f.aspect != "join"]
        self.write_facts(FactStore(facts=kept_facts).merge(facts))

        kept = self.evidence()
        for record in evidence.records:
            kept.add(record)
        known = {link.claim_id for link in links}
        kept.links = [
            link for link in kept.links if link.claim_id not in known
        ] + list(links)
        self.write_evidence(kept)

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
        self.snapshot()
        facts = self.facts()
        kept_facts = [f for f in facts.facts if f.subject.split(".")[0] not in tables]

        evidence = self.evidence()
        kept_records = [r for r in evidence.records if not _touches(r.subjects, tables)]
        kept_ids = {r.id for r in kept_records}
        kept_links = [
            link
            for link in evidence.links
            if link.evidence_id in kept_ids
            and link.claim_id.split("#")[0].split(".")[0] not in tables
        ]

        questions = self.questions()
        kept_questions = [q for q in questions.questions if q.table not in tables]

        removed = {
            "claims": len(facts.facts) - len(kept_facts),
            "evidence": len(evidence.records) - len(kept_records),
            "questions": len(questions.questions) - len(kept_questions),
        }
        self.write_facts(FactStore(facts=kept_facts))
        self.write_evidence(EvidenceStore(records=kept_records, links=kept_links))
        self.write_questions(QuestionLog(questions=kept_questions))
        return removed


def list_workspaces(repository: MetadataRepository | None = None) -> list[str]:
    return (repository or get_repository()).list_workspaces()


def referencing_source(
    source_id: str, repository: MetadataRepository | None = None
) -> list[str]:
    """Which workspaces are bound to this source.

    Unregistered ones count. Their snapshot names the source they came from,
    and deleting it out from under them would leave data nothing can adopt.
    """
    store = repository or get_repository()
    found: list[str] = []
    for name in store.list_workspaces():
        catalog = Catalog(name, store)
        if catalog.is_registered():
            referenced = catalog.manifest().source_id
        elif catalog.has_snapshot():
            referenced = catalog.unchecked_snapshot().source_id
        else:
            referenced = None
        if referenced == source_id:
            found.append(name)
    return sorted(found)


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
