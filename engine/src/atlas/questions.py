"""Questions raised during analysis — what no query can settle.

A separate module from `agent` so that anything reading a workspace can parse
them without importing the LLM stack, and so the type is available to the
output builder rather than passing an untyped list around.

Questions are the only route past `ClaimPolicy.ceiling`. Business meaning caps
at OBSERVED however much data is scanned, and the policy lifts it only for a
human decision or an authoritative artifact. Until answers existed that branch
was unreachable: every semantics claim in a run sat at 0.65 with no mechanism
in the product capable of moving it. An answered question is that mechanism.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, computed_field


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    # Asked, and not worth answering — the column is dead, the distinction does
    # not matter for this workflow, nobody knows. Distinct from OPEN so a
    # reviewer is not shown it again on every pass.
    DISMISSED = "dismissed"


class Question(BaseModel):
    subject: str
    question: str
    evidence: str
    table: str = ""
    # What the answer would establish. The agent knows this when it asks, and
    # without it an answer has no claim to attach to.
    aspect: str = "semantics"

    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None

    @computed_field
    @property
    def id(self) -> str:
        """Stable across runs, so an answer survives re-analysis.

        Keyed on what is being asked rather than on when: re-analysing a table
        asks the same question again, and a reviewer should not be made to
        answer it twice because the wording of the evidence changed.
        """
        material = f"{self.subject}|{self.question.strip().lower()}"
        return "question:" + hashlib.sha256(material.encode()).hexdigest()[:12]

    @property
    def settled(self) -> bool:
        return self.status is not QuestionStatus.OPEN

    def answered(self, answer: str, reviewer: str) -> Question:
        return self.model_copy(
            update={
                "status": QuestionStatus.ANSWERED,
                "answer": answer,
                "answered_by": reviewer,
                "answered_at": datetime.now(UTC),
            }
        )

    def dismissed(self, reason: str, reviewer: str) -> Question:
        return self.model_copy(
            update={
                "status": QuestionStatus.DISMISSED,
                "answer": reason,
                "answered_by": reviewer,
                "answered_at": datetime.now(UTC),
            }
        )


class QuestionLog(BaseModel):
    questions: list[Question] = Field(default_factory=list)

    def get(self, question_id: str) -> Question | None:
        return next((q for q in self.questions if q.id == question_id), None)

    def replace(self, question: Question) -> QuestionLog:
        return QuestionLog(
            questions=[question if q.id == question.id else q for q in self.questions]
        )

    def merge(self, incoming: list[Question]) -> QuestionLog:
        """Fold a fresh round of questions in, keeping every answer.

        A settled question is kept whether or not the new run asked it again.
        Keying only off `incoming` looked equivalent and was not: the id hashes
        the question text, so re-analysis rarely reproduces one byte for byte,
        and every answer for that table was silently dropped on each re-run —
        the exact failure this method exists to prevent.
        """
        settled = {q.id: q for q in self.questions if q.settled}
        fresh = [q for q in incoming if q.id not in settled]
        return QuestionLog(questions=[*settled.values(), *fresh])

    @property
    def open(self) -> list[Question]:
        return [q for q in self.questions if not q.settled]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        )

    @classmethod
    def read(cls, path: Path) -> QuestionLog:
        if not path.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
