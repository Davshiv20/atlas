"""Which metadata store this process uses.

The mirror of `atlas.adapters.registry`, and deliberately the only place that
names an implementation. Everything else asks for a `MetadataRepository` and is
handed one, which is what makes swapping YAML for Atlas-owned PostgreSQL a
change to this file rather than to every caller.

Not cached. `ATLAS_OUTPUT_DIR` is read per call so a test that repoints it does
not have to know about an invalidation step — and constructing the YAML store
is a `Path` join, not a connection.
"""

from __future__ import annotations

from atlas.metadata.base import MetadataRepository
from atlas.metadata.yaml_store import YamlMetadataRepository


def get_repository() -> MetadataRepository:
    return YamlMetadataRepository()
