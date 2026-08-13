"""One set of expectations for credentials, run against every store.

The clause that matters most is the one about an exported variable winning.
Someone who set a variable at the shell meant it, and a store that silently
overrode it would make the engine disagree with its own operator about which
database it is connected to — a disagreement with no symptom until the wrong
data comes back.

The keychain store is exercised against a fake backend rather than the real
one. Touching a developer's actual keychain from a test suite is not something
to do quietly, and CI has no session to unlock. What is under test is this
adapter's bookkeeping — chiefly the index it keeps, because a keychain cannot
portably enumerate its own entries and a lost index means credentials that
exist and can never be loaded again.
"""

from __future__ import annotations

import os

import pytest

from atlas.secrets.env_file import EnvFileSecretStore
from atlas.secrets.keyring_store import INDEX, KeyringSecretStore


class FakeKeyring:
    """Enough of `keyring` to hold entries, and no enumeration — which is the
    limitation the real backends have and the index exists to work around."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.entries.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.entries[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        if (service, name) not in self.entries:
            raise KeyError(name)
        del self.entries[(service, name)]


def _file(tmp_path, _monkeypatch) -> EnvFileSecretStore:
    return EnvFileSecretStore(tmp_path / ".secrets.env")


def _keychain(_tmp_path, _monkeypatch) -> KeyringSecretStore:
    return _fake_keychain()


def _fake_keychain() -> KeyringSecretStore:
    """A store whose backend is one throwaway keychain, per call.

    The instance attribute shadows the class's `_backend`, so nothing here can
    reach the developer's real keychain even if the fake is wrong.
    """
    store = KeyringSecretStore("atlas.test")
    fake = FakeKeyring()
    store._backend = lambda: fake
    return store


@pytest.fixture(params=[_file, _keychain], ids=["file", "keyring"])
def store(request, tmp_path, monkeypatch):
    for leaked in ("ELARA_DATABASE_URL", "OTHER_URL"):
        monkeypatch.delenv(leaked, raising=False)
    built = request.param(tmp_path, monkeypatch)
    yield built
    for leaked in ("ELARA_DATABASE_URL", "OTHER_URL"):
        os.environ.pop(leaked, None)


URL = "postgresql://user:hunter2@db.internal/app"


def test_an_empty_store_holds_nothing(store) -> None:
    assert store.names() == []
    assert store.has("ELARA_DATABASE_URL") is False


def test_storing_a_credential_makes_it_live_without_a_restart(store) -> None:
    """Connectors read URLs from the environment, so a credential that needed a
    restart to take effect would mean saving one and immediately being told it
    does not work."""
    store.set("ELARA_DATABASE_URL", URL)

    assert os.environ["ELARA_DATABASE_URL"] == URL
    assert store.has("ELARA_DATABASE_URL") is True
    assert store.names() == ["ELARA_DATABASE_URL"]


def test_replacing_a_credential_replaces_it_everywhere(store) -> None:
    store.set("ELARA_DATABASE_URL", URL)
    store.set("ELARA_DATABASE_URL", "postgresql://user:newpass@db.internal/app")

    assert os.environ["ELARA_DATABASE_URL"].endswith("newpass@db.internal/app")
    assert store.names() == ["ELARA_DATABASE_URL"]


def test_clearing_forgets_it_and_unsets_it(store) -> None:
    store.set("ELARA_DATABASE_URL", URL)

    store.clear("ELARA_DATABASE_URL")

    assert store.has("ELARA_DATABASE_URL") is False
    assert "ELARA_DATABASE_URL" not in os.environ
    assert store.names() == []


def test_clearing_something_absent_is_silent(store) -> None:
    store.clear("NEVER_STORED")


def test_an_exported_variable_wins_over_a_stored_one(store, monkeypatch) -> None:
    """Someone who exported one at the shell meant it. Overriding it silently
    makes the engine disagree with its operator about which database it is
    talking to, and nothing reports the disagreement."""
    store.set("ELARA_DATABASE_URL", URL)
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql://exported")

    loaded = store.load_into_environment()

    assert os.environ["ELARA_DATABASE_URL"] == "postgresql://exported"
    assert loaded == 0


def test_loading_populates_what_the_environment_is_missing(store, monkeypatch) -> None:
    store.set("ELARA_DATABASE_URL", URL)
    store.set("OTHER_URL", "postgresql://other")
    monkeypatch.delenv("ELARA_DATABASE_URL")
    monkeypatch.delenv("OTHER_URL")

    assert store.load_into_environment() == 2
    assert os.environ["ELARA_DATABASE_URL"] == URL


def test_has_is_about_atlas_holding_it_not_the_variable_being_set(
    store, monkeypatch
) -> None:
    """An operator's exported value is not Atlas's to edit or delete. The
    console needs to know which it is looking at before offering to change it.
    """
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql://exported")

    assert store.has("ELARA_DATABASE_URL") is False


def test_names_never_carry_values(store) -> None:
    store.set("ELARA_DATABASE_URL", URL)

    assert "hunter2" not in str(store.names())


def test_the_keychain_index_is_not_addressable_as_a_credential() -> None:
    """It is bookkeeping, and a caller that overwrote it would leave every
    stored credential in the keychain and unreachable."""
    store = _fake_keychain()

    with pytest.raises(ValueError, match="reserved"):
        store.set(INDEX, "anything")
