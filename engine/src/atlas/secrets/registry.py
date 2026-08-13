"""Which credential store this process uses.

`ATLAS_SECRET_STORE`, not `ATLAS_DATABASE_URL`. The other three stores follow
the record into PostgreSQL; this one deliberately does not, because a credential
in Atlas's own database is a credential in every backup and replica of it, and
what it unlocks is a customer's warehouse.

Defaults to the file store, which is the only one that works on a headless
host. Choosing the keychain is a statement about the machine, so it is made
deliberately rather than guessed from what happens to be installed.
"""

from __future__ import annotations

from atlas.secrets.base import SecretStore
from atlas.secrets.env_file import EnvFileSecretStore
from atlas.secrets.keyring_store import KeyringSecretStore
from atlas.settings import get_settings


def get_secret_store() -> SecretStore:
    if get_settings().atlas_secret_store == "keyring":
        return KeyringSecretStore()
    return EnvFileSecretStore()
