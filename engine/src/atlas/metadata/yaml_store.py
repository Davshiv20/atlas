"""The file-backed metadata store.

One directory per workspace, one immutable directory per snapshot generation,
and a manifest at the root that points at whichever generation is active. Good
enough for a single reviewer and inspectable while the pipeline is still
changing shape, which is why it is still the default.

Its limits are the reason the port above it exists:

- `transaction` is an advisory `flock`, not a transaction. It serializes
  writers, so two processes cannot interleave; it cannot roll back. A composite
  operation that raises halfway leaves the earlier writes in place.
- Every write rewrites a whole file. Two reviewers approving different claims
  is a last-write-wins race that no lock granularity here can fix.

Everything about the layout — directory names, file names, the YAML itself,
and the migrations that keep older files loadable — is private to this module.
No caller outside it may name a path.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from atlas.evidence import EvidenceStore
from atlas.facts import PLURAL_ASPECTS, FactStore
from atlas.manifest import WorkspaceManifest, require_valid_name
from atlas.metadata.base import (
    MetadataRepository,
    NoSnapshot,
    UnknownWorkspace,
    WorkspaceBusy,
    WorkspaceExists,
)
from atlas.output import SchemaOutput
from atlas.questions import QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Snapshot

logger = logging.getLogger(__name__)

MANIFEST = "workspace.yaml"
SNAPSHOT = "snapshot.yaml"
FACTS = "facts.yaml"
QUESTIONS = "questions.yaml"
EVIDENCE = "evidence.yaml"
OUTPUT = "output.yaml"
LOCK = ".mutation.lock"

#: Everything a generation directory holds, in the order a person reading the
#: directory would expect to find it.
GENERATION_FILES = (SNAPSHOT, FACTS, EVIDENCE, QUESTIONS, OUTPUT)


class YamlMetadataRepository(MetadataRepository):
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or get_settings().atlas_output_dir

    def __repr__(self) -> str:
        return f"YamlMetadataRepository({str(self._root)!r})"

    # ---- layout ----------------------------------------------------------
    #
    # The only methods that know what a path looks like.

    def _workspace_root(self, workspace: str) -> Path:
        return self._root / require_valid_name(workspace)

    def _manifest_path(self, workspace: str) -> Path:
        return self._workspace_root(workspace) / MANIFEST

    def _generation_root(self, workspace: str, generation: int) -> Path:
        return self._workspace_root(workspace) / "generations" / str(generation)

    def _active(self, workspace: str, filename: str) -> Path:
        """Where the active generation keeps `filename`.

        A workspace that has never published — legacy data, or one created but
        not yet extracted — keeps its files at the root. The manifest pointer
        is what moves every accessor into a generation directory, and it is
        written last precisely so that move is atomic.
        """
        manifest_path = self._manifest_path(workspace)
        if manifest_path.exists():
            generation = _read_manifest(manifest_path).snapshot_generation
            if generation > 0:
                return self._generation_root(workspace, generation) / filename
        return self._workspace_root(workspace) / filename

    # ---- lifecycle -------------------------------------------------------

    def exists(self, workspace: str) -> bool:
        return self._manifest_path(workspace).exists()

    def has_snapshot(self, workspace: str) -> bool:
        return self._active(workspace, SNAPSHOT).exists()

    def has_semantics(self, workspace: str) -> bool:
        # File presence, not content: an empty facts.yaml is a file some run
        # wrote, and treating it as "nothing here" is how a refresh gets to
        # skip the confirmation that protects review work.
        return any(
            self._active(workspace, name).exists()
            for name in (FACTS, EVIDENCE, QUESTIONS, OUTPUT)
        )

    def list_workspaces(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(
            directory.name
            for directory in self._root.iterdir()
            if directory.is_dir()
            and ((directory / MANIFEST).exists() or (directory / SNAPSHOT).exists())
        )

    def create(self, workspace: str, manifest: WorkspaceManifest) -> None:
        path = self._manifest_path(workspace)
        if path.exists():
            raise WorkspaceExists(f"workspace {workspace!r} already exists")
        root = self._workspace_root(workspace)
        if root.exists() and any(entry.name != LOCK for entry in root.iterdir()):
            raise WorkspaceExists(
                f"workspace {workspace!r} already contains files; refusing to bind it implicitly"
            )
        _write_manifest(path, manifest)

    def adopt(self, workspace: str, source_id: str) -> WorkspaceManifest:
        """Copy root-level pre-manifest state into generation 1, then commit
        its pointer.

        The copy happens into a staging directory and the manifest is written
        last, so an interruption leaves the original files where every older
        accessor still expects them.
        """
        if self.exists(workspace):
            return self.read_manifest(workspace)
        root = self._workspace_root(workspace)
        legacy_snapshot = root / SNAPSHOT
        if not legacy_snapshot.exists():
            raise UnknownWorkspace(f"workspace {workspace!r} holds no unregistered data")

        with self.transaction(workspace):
            snapshot = _read_snapshot(legacy_snapshot)
            staging = root / "generations" / ".1.tmp"
            target = self._generation_root(workspace, 1)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            _write_snapshot(
                staging / SNAPSHOT,
                snapshot.model_copy(update={"source_id": source_id, "generation": 1}),
            )
            for filename in (FACTS, EVIDENCE, QUESTIONS, OUTPUT):
                legacy = root / filename
                if legacy.exists():
                    shutil.copy2(legacy, staging / filename)
            _read_snapshot(staging / SNAPSHOT)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)
            manifest = WorkspaceManifest(
                id=workspace, source_id=source_id, snapshot_generation=1
            )
            # The manifest is the single active-generation pointer, and it
            # commits last.
            _write_manifest(self._manifest_path(workspace), manifest)
            for filename in GENERATION_FILES:
                legacy = root / filename
                if legacy.exists():
                    legacy.unlink()
            return manifest

    def delete(self, workspace: str) -> None:
        root = self._workspace_root(workspace)
        if root.exists():
            shutil.rmtree(root)

    # ---- manifest --------------------------------------------------------

    def read_manifest(self, workspace: str) -> WorkspaceManifest:
        path = self._manifest_path(workspace)
        if not path.exists():
            raise UnknownWorkspace(f"workspace {workspace!r} is not registered")
        return _read_manifest(path)

    def write_manifest(self, workspace: str, manifest: WorkspaceManifest) -> None:
        _write_manifest(self._manifest_path(workspace), manifest)

    # ---- snapshots -------------------------------------------------------

    def publish_snapshot(self, workspace: str, snapshot: Snapshot) -> WorkspaceManifest:
        manifest = self.read_manifest(workspace)
        generation = manifest.snapshot_generation + 1
        root = self._workspace_root(workspace)
        staging = root / "generations" / f".{generation}.tmp"
        target = self._generation_root(workspace, generation)

        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        _write_snapshot(
            staging / SNAPSHOT,
            snapshot.model_copy(
                update={"source_id": manifest.source_id, "generation": generation}
            ),
        )
        # Read it back before anything points at it: a snapshot that does not
        # parse is not a generation, and finding that out now costs a staging
        # directory rather than the workspace.
        _read_snapshot(staging / SNAPSHOT)
        # A prior crash may have completed this inactive generation but failed
        # before advancing the pointer. Rebuilding is safe because the manifest
        # still names the previous one.
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
        # The previous generation stays complete and readable until this one
        # atomic replace advances the single active pointer.
        _write_manifest(
            self._manifest_path(workspace),
            manifest.model_copy(update={"snapshot_generation": generation}),
        )
        return self.read_manifest(workspace)

    def read_snapshot(self, workspace: str) -> Snapshot:
        path = self._active(workspace, SNAPSHOT)
        if not path.exists():
            raise NoSnapshot(f"workspace {workspace!r} has no snapshot")
        return _read_snapshot(path)

    # ---- semantic state --------------------------------------------------

    def read_facts(self, workspace: str) -> FactStore:
        return _read_facts(self._active(workspace, FACTS))

    def write_facts(self, workspace: str, facts: FactStore) -> None:
        _write_facts(self._active(workspace, FACTS), facts)

    def read_questions(self, workspace: str) -> QuestionLog:
        return _read_questions(self._active(workspace, QUESTIONS))

    def write_questions(self, workspace: str, questions: QuestionLog) -> None:
        _write_questions(self._active(workspace, QUESTIONS), questions)

    def read_evidence(self, workspace: str) -> EvidenceStore:
        return _read_evidence(self._active(workspace, EVIDENCE))

    def write_evidence(self, workspace: str, evidence: EvidenceStore) -> None:
        _write_evidence(self._active(workspace, EVIDENCE), evidence)

    def clear_semantics(self, workspace: str) -> dict[str, int]:
        removed: dict[str, int] = {}
        for label, filename in (
            ("facts", FACTS),
            ("evidence", EVIDENCE),
            ("questions", QUESTIONS),
            ("output", OUTPUT),
        ):
            path = self._active(workspace, filename)
            if path.exists():
                path.unlink()
                removed[label] = 1
        return removed

    # ---- projection ------------------------------------------------------

    def write_output(self, workspace: str, output: SchemaOutput) -> None:
        _write_output(self._active(workspace, OUTPUT), output)

    # ---- concurrency -----------------------------------------------------

    @contextmanager
    def transaction(self, workspace: str, *, blocking: bool = True) -> Iterator[None]:
        """An advisory interprocess lock, which is not a transaction.

        It is what serializes the API and the CLI writing the same workspace
        from different processes. It cannot roll back: a composite operation
        that raises partway leaves the writes it already made. Fixing that is a
        reason to implement this port against a database, not a reason to
        pretend here.
        """
        root = self._workspace_root(workspace)
        root.mkdir(parents=True, exist_ok=True)
        with (root / LOCK).open("a+") as handle:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as exc:
                raise WorkspaceBusy(
                    f"workspace {workspace!r} already has an active mutation"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- serialization ---------------------------------------------------------
#
# Free functions rather than methods on the models. A model that knows how to
# write itself to a path has picked a store, and the point of the port is that
# the store is a choice.


def _dump(path: Path, payload: dict[str, Any], *, width: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=width or 80)
    path.write_text(text)


def _dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    """For the manifest only. It is the pointer every read follows, so a
    half-written one is a workspace nobody can open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def _read_manifest(path: Path) -> WorkspaceManifest:
    return WorkspaceManifest.model_validate(_load(path))


def _write_manifest(path: Path, manifest: WorkspaceManifest) -> None:
    _dump_atomic(path, manifest.model_dump(mode="json"))


def _read_snapshot(path: Path) -> Snapshot:
    return Snapshot.model_validate(_load(path))


def _write_snapshot(path: Path, snapshot: Snapshot) -> None:
    _dump(path, snapshot.model_dump(mode="json", exclude_none=True))


def _read_facts(path: Path) -> FactStore:
    if not path.exists():
        return FactStore()
    raw = _load(path)
    return FactStore.model_validate(
        {**raw, "facts": [_migrate_fact(f) for f in raw.get("facts", [])]}
    )


def _write_facts(path: Path, facts: FactStore) -> None:
    # `endorsement` is derived on every read and is deliberately not written.
    # A derived value on disk outlives the code that produced it: renaming one
    # of its states once left every stored workspace unreadable.
    _dump(
        path,
        facts.model_dump(
            mode="json", exclude_none=True, exclude={"facts": {"__all__": {"endorsement"}}}
        ),
    )


def _migrate_fact(raw: dict) -> dict:
    """Give a pre-discriminator claim one, so old catalogues still load.

    A stored-format concern, which is why it lives with the store rather than
    with the model: a repository that keeps claims as rows will have entirely
    different history to reconcile, or none.

    The discriminator is derived from the claim text, which makes it stable
    across reads — the same file parses to the same ids every time. It is
    deliberately ugly: a `legacy-` prefix in an id is a visible marker that
    this claim predates the plural-aspect rule and may be several findings
    concatenated into one.
    """
    if raw.get("aspect") not in PLURAL_ASPECTS or raw.get("discriminator") is not None:
        return raw
    digest = hashlib.sha256(str(raw.get("claim", "")).encode()).hexdigest()[:8]
    logger.info(
        "migrating %s#%s: assigning discriminator legacy-%s",
        raw.get("subject"),
        raw.get("aspect"),
        digest,
    )
    return {**raw, "discriminator": f"legacy-{digest}"}


def _read_questions(path: Path) -> QuestionLog:
    if not path.exists():
        return QuestionLog()
    return QuestionLog.model_validate(_load(path))


def _write_questions(path: Path, questions: QuestionLog) -> None:
    _dump(path, questions.model_dump(mode="json"))


def _read_evidence(path: Path) -> EvidenceStore:
    if not path.exists():
        return EvidenceStore()
    return EvidenceStore.model_validate(_load(path))


def _write_evidence(path: Path, evidence: EvidenceStore) -> None:
    _dump(path, evidence.model_dump(mode="json", exclude_none=True), width=100)


def _write_output(path: Path, output: SchemaOutput) -> None:
    _dump(path, output.model_dump(mode="json", exclude_none=True), width=100)
