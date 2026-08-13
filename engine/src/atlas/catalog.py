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

    # ---- scoped writes ---------------------------------------------------
    #
    # What review uses. Each names the records it is changing, so two people
    # settling different claims a second apart both keep their decision — the
    # failure that read-all/write-all produces and never reports.

    def record_review(self, fact: Fact, evidence: EvidenceStore) -> None:
        """Persist one reviewed claim and the evidence behind the decision.

        The evidence store passed may be the whole one the caller was working
        from: appending is idempotent per record and per link, so the caller is
        not made to compute a delta it would get wrong under concurrency.
        """
        self.repository.upsert_facts(self.name, [fact])
        self.repository.append_evidence(self.name, evidence)

    def record_answered_question(
        self, question: Question, facts: FactStore, evidence: EvidenceStore
    ) -> None:
        """Persist a settled question, the claim it established, and its
        evidence.

        Takes the full fact store because answering re-scores one claim and the
        caller does not know which id until the answer is folded in; only the
        claims that actually changed are written.
        """
        self.repository.upsert_questions(self.name, [question])
        self.repository.upsert_facts(self.name, _changed(self.facts(), facts))
        self.repository.append_evidence(self.name, evidence)

    def settle_question(self, question: Question) -> None:
        """Record a question as answered or dismissed, touching nothing else."""
        self.repository.upsert_questions(self.name, [question])

    # ---- wholesale writes ------------------------------------------------
    #
    # For a job that holds the workspace exclusively and is rebuilding a
    # collection rather than editing records in it.

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
        with self.mutation():
            self.snapshot()
            # Merged against everything stored, so a human verdict on an
            # unchanged claim survives, but only the claims this table produced
            # are written back. A concurrent reviewer approving something in
            # another table is not in this set and cannot be overwritten by it.
            incoming = {fact.id for fact in facts}
            merged = self.facts().merge(facts)
            self.repository.upsert_facts(
                self.name, [f for f in merged.facts if f.id in incoming]
            )
            self.repository.append_evidence(self.name, evidence)

            # This table's questions are replaced, not appended: re-analysing
            # it asks its questions again, and two copies of one question is
            # two review decisions for one uncertainty.
            #
            # An answered question is kept whether or not this run asked it
            # again. The id hashes the question text, so re-analysis rarely
            # reproduces one byte for byte, and keying only off the fresh batch
            # dropped every answer for the table on each re-run.
            #
            # Scoped to this table. The wholesale version merged across the
            # whole log and wrote the result back beside the untouched
            # remainder, which left every *other* table's answered questions
            # stored twice.
            mine = [q for q in self.questions().questions if q.table == table]
            answered = {q.id for q in mine if q.settled}
            fresh = [q for q in questions if q.id not in answered]
            self.repository.remove_questions(
                self.name,
                {q.id for q in mine if q.id not in answered}
                - {q.id for q in fresh},
            )
            self.repository.upsert_questions(self.name, fresh)

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
        with self.mutation():
            self.snapshot()
            stored = self.facts()
            replacement = {fact.id for fact in facts}
            self.repository.remove_facts(
                self.name,
                {f.id for f in stored.facts if f.aspect == "join" and f.id not in replacement},
            )
            self.repository.upsert_facts(self.name, facts)

            # Links for a re-derived join are replaced rather than added to:
            # the previous run's rationale describes a map that no longer
            # exists. Everything else keeps its evidence untouched.
            kept = self.evidence()
            for record in evidence.records:
                kept.add(record)
            superseded = {link.claim_id for link in links}
            kept.links = [
                link for link in kept.links if link.claim_id not in superseded
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
        with self.mutation():
            self.snapshot()
            doomed_facts = {
                f.id for f in self.facts().facts if f.subject.split(".")[0] in tables
            }
            doomed_questions = {
                q.id for q in self.questions().questions if q.table in tables
            }

            evidence = self.evidence()
            kept_records = [r for r in evidence.records if not _touches(r.subjects, tables)]
            kept_ids = {r.id for r in kept_records}
            kept_links = [
                link
                for link in evidence.links
                if link.evidence_id in kept_ids
                and link.claim_id.split("#")[0].split(".")[0] not in tables
            ]

            removed = {
                "claims": self.repository.remove_facts(self.name, doomed_facts),
                "evidence": len(evidence.records) - len(kept_records),
                "questions": self.repository.remove_questions(self.name, doomed_questions),
            }
            # Evidence is rebuilt whole rather than deleted by id: a record is
            # dropped for touching one of these tables, and a link is dropped
            # for pointing at a record that went with it, so the surviving set
            # is what the filter produced rather than a list of ids.
            self.write_evidence(EvidenceStore(records=kept_records, links=kept_links))
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


def _changed(before: FactStore, after: FactStore) -> list[Fact]:
    """Which claims `after` states differently from `before`.

    Answering a question re-scores one claim, or writes a new one, out of a
    store that may hold thousands. Writing back only what moved is what keeps
    an unrelated claim someone approved a moment ago from being reverted to the
    copy this request happened to read.
    """
    stored = {fact.id: fact for fact in before.facts}
    return [fact for fact in after.facts if stored.get(fact.id) != fact]


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
