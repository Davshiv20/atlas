"""Per-workspace file storage.

Deliberately behind an interface. The files are fine for a single reviewer and
they make the intermediate state inspectable while the pipeline is still
changing shape, but concurrent reviewers need row-level writes rather than
whole-file rewrites. When that day comes, this class is what gets replaced.
"""

from __future__ import annotations

import re
from pathlib import Path

from atlas.evidence import ClaimEvidence, EvidenceStore
from atlas.facts import Fact, FactStore
from atlas.output import SchemaOutput
from atlas.questions import Question, QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Snapshot

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class InvalidWorkspace(ValueError):
    pass


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
    def snapshot_path(self) -> Path:
        return self.root / "snapshot.yaml"

    @property
    def facts_path(self) -> Path:
        return self.root / "facts.yaml"

    @property
    def questions_path(self) -> Path:
        return self.root / "questions.yaml"

    @property
    def output_path(self) -> Path:
        return self.root / "output.yaml"

    @property
    def evidence_path(self) -> Path:
        return self.root / "evidence.yaml"

    def exists(self) -> bool:
        return self.snapshot_path.exists()

    def read_snapshot(self) -> Snapshot:
        return Snapshot.read(self.snapshot_path)

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
            d.name for d in base.iterdir() if d.is_dir() and (d / "snapshot.yaml").exists()
        )


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
