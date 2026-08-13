"""Declared sources in a YAML file.

Safe to commit and safe to serve: it holds environment-variable *names*, never
their values. That is why this one file survived every review of what Atlas
writes to disk.

Its limit is the same as the other file stores'. `lock` is an advisory `flock`,
so two processes on one machine are serialized and two machines sharing a
directory are not.
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from atlas.settings import get_settings
from atlas.sources.base import SourceRepository
from atlas.sources.models import DuplicateSource, Source, SourceNotFound

#: Which registry files this thread already holds, and how deep. `flock` is
#: owned per open file description, so a thread that holds the lock and opens
#: the file again waits for itself — and the API takes the lock around a block
#: that also calls through to `add`.
_LOCKS = threading.local()


class YamlSourceRepository(SourceRepository):
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def __repr__(self) -> str:
        return f"YamlSourceRepository({str(self.path)!r})"

    @property
    def path(self) -> Path:
        # Resolved per call rather than at construction: the setting is read
        # from the working directory, and a test that repoints it must not
        # need to know this object exists.
        return self._path or get_settings().atlas_sources_file

    def list(self) -> list[Source]:
        return self._read()

    def get(self, source_id: str) -> Source:
        found = next((s for s in self._read() if s.id == source_id), None)
        if found is None:
            raise SourceNotFound(source_id)
        return found

    def add(self, source: Source) -> Source:
        with self.lock():
            declared = self._read()
            if any(s.id == source.id for s in declared):
                raise DuplicateSource(f"a source named {source.id!r} already exists")
            self._write([*declared, source])
        return source

    def remove(self, source_id: str) -> None:
        with self.lock():
            declared = self._read()
            if not any(s.id == source_id for s in declared):
                raise SourceNotFound(source_id)
            self._write([s for s in declared if s.id != source_id])

    @contextmanager
    def lock(self) -> Iterator[None]:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        key = str(target.resolve() if target.exists() else target)
        held = _held_locks()
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return

        with target.with_name(f"{target.name}.lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ---- the file ---------------------------------------------------------

    def _read(self) -> list[Source]:
        target = self.path
        if not target.exists():
            return []
        payload = yaml.safe_load(target.read_text()) or {}
        return [Source.model_validate(entry) for entry in payload.get("sources", [])]

    def _write(self, sources: list[Source]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(
                {"sources": [s.model_dump(mode="json") for s in sources]},
                sort_keys=False,
                allow_unicode=True,
            )
        )


def _held_locks() -> dict[str, int]:
    depths = getattr(_LOCKS, "depths", None)
    if depths is None:
        depths = {}
        _LOCKS.depths = depths
    return depths
