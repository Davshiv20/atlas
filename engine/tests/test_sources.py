from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from atlas.api import app
from atlas.settings import get_settings
from atlas.sources import Source, SourceRegistry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for leaked in ("ATLAS_DATABASE_URL", "OPENROUTER_API_KEY", "ELARA_DATABASE_URL"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ATLAS_SOURCES_FILE", str(tmp_path / "sources.yaml"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


VALID = {
    "id": "elara",
    "adapter": "postgresql",
    "url_env": "ELARA_DATABASE_URL",
    "namespace": "public",
}


# --- the registry holds no secrets -----------------------------------------


def test_a_source_records_the_variable_name_not_its_value(client, isolated) -> None:
    """The whole reason for the indirection: this file is safe to commit and
    safe to serve over an unauthenticated API."""
    client.post("/sources", json=VALID)
    written = (isolated / "sources.yaml").read_text()
    assert "ELARA_DATABASE_URL" in written
    assert "postgresql://" not in written


def test_listing_sources_never_returns_a_url(client, monkeypatch) -> None:
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql+psycopg://u:secret@host/db")
    client.post("/sources", json=VALID)
    body = client.get("/sources").json()
    assert "secret" not in str(body)
    assert body["sources"][0]["configured"] is True


def test_configured_is_false_when_the_variable_is_unset(client) -> None:
    client.post("/sources", json=VALID)
    assert client.get("/sources").json()["sources"][0]["configured"] is False


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["Has Caps", "../etc", "", "a" * 64])
def test_source_ids_are_validated(bad_id) -> None:
    with pytest.raises(ValidationError):
        Source(**{**VALID, "id": bad_id})


@pytest.mark.parametrize("bad_env", ["lowercase", "has-dash", "1LEADING"])
def test_url_env_must_look_like_an_env_var(bad_env) -> None:
    with pytest.raises(ValidationError):
        Source(**{**VALID, "url_env": bad_env})


def test_unknown_adapters_are_refused() -> None:
    with pytest.raises(ValidationError):
        Source(**{**VALID, "adapter": "mysql"})


def test_duplicate_ids_conflict(client) -> None:
    client.post("/sources", json=VALID)
    assert client.post("/sources", json=VALID).status_code == 409


# --- testing a connection --------------------------------------------------


def test_testing_an_unconfigured_source_names_the_missing_variable(client) -> None:
    client.post("/sources", json=VALID)
    body = client.post("/sources/elara/test").json()
    assert body["state"] == "failed"
    assert "No connection string for 'elara'" in body["detail"]


def test_testing_reports_a_connection_failure_without_leaking_the_password(
    client, monkeypatch
) -> None:
    monkeypatch.setenv(
        "ELARA_DATABASE_URL", "postgresql+psycopg://u:hunter2@127.0.0.1:1/nope"
    )
    client.post("/sources", json=VALID)
    body = client.post("/sources/elara/test").json()
    assert body["state"] == "failed"
    assert "hunter2" not in body["detail"]


def test_testing_an_unknown_source_is_404(client) -> None:
    assert client.post("/sources/nope/test").status_code == 404


# --- lifecycle -------------------------------------------------------------


def test_delete_removes_it(client) -> None:
    client.post("/sources", json=VALID)
    assert client.delete("/sources/elara").status_code == 204
    assert client.get("/sources").json()["sources"] == []


def test_deleting_an_unknown_source_is_404(client) -> None:
    assert client.delete("/sources/nope").status_code == 404


def test_registry_round_trips(isolated) -> None:
    registry = SourceRegistry()
    registry.add(Source(**VALID))
    registry.write()
    assert SourceRegistry.read().get("elara").url_env == "ELARA_DATABASE_URL"


# --- connected is checked, not assumed -------------------------------------


def test_a_new_source_starts_unknown_not_connected(client) -> None:
    """`configured` only means the variable holds something. Claiming
    "connected" before anyone connected is the thing this replaces."""
    client.post("/sources", json=VALID)
    source = client.get("/sources").json()["sources"][0]
    assert source["configured"] is False
    assert source["health"]["state"] == "failed"  # probed on create, no variable set


def test_a_failed_probe_is_remembered_on_the_listing(client, monkeypatch) -> None:
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/nope")
    client.post("/sources", json=VALID)
    source = client.get("/sources").json()["sources"][0]
    assert source["configured"] is True  # the variable is set…
    assert source["health"]["state"] == "failed"  # …and it still does not connect
    assert source["health"]["checked_at"] is not None


def test_health_never_carries_the_password(client, monkeypatch) -> None:
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql+psycopg://u:hunter2@127.0.0.1:1/x")
    client.post("/sources", json=VALID)
    assert "hunter2" not in str(client.get("/sources").json())


# --- credentials set from the UI -------------------------------------------


def test_saving_a_credential_makes_it_live_without_a_restart(client, isolated, monkeypatch) -> None:
    """The restart was the friction: the engine reads URLs from its
    environment, so writing the secret also sets it in this process."""
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    client.post("/sources", json=VALID)

    client.put(
        "/sources/elara/credentials",
        json={"url": "postgresql+psycopg://u:p@127.0.0.1:1/nope"},
    )
    source = client.get("/sources").json()["sources"][0]
    assert source["configured"] is True
    assert source["managed"] is True


def test_a_stored_credential_is_written_0600(client, isolated, monkeypatch) -> None:
    path = isolated / ".secrets.env"
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(path))
    get_settings.cache_clear()
    client.post("/sources", json=VALID)
    client.put("/sources/elara/credentials", json={"url": "postgresql://u:p@h/db"})

    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_the_secret_file_is_not_the_source_registry(client, isolated, monkeypatch) -> None:
    """Registry stays safe to commit; only the secrets file holds a URL."""
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    client.post("/sources", json=VALID)
    client.put("/sources/elara/credentials", json={"url": "postgresql://u:hunter2@h/db"})

    assert "hunter2" not in (isolated / "sources.yaml").read_text()
    assert "hunter2" not in str(client.get("/sources").json())


def test_forgetting_a_credential_clears_it(client, isolated, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    client.post("/sources", json=VALID)
    client.put("/sources/elara/credentials", json={"url": "postgresql://u:p@h/db"})

    assert client.delete("/sources/elara/credentials").status_code == 204
    source = client.get("/sources").json()["sources"][0]
    assert source["managed"] is False
    assert source["configured"] is False


def test_an_exported_variable_wins_over_the_stored_one(isolated, monkeypatch) -> None:
    """Someone who exported a variable at the shell meant it; silently
    overriding it would make the engine disagree with its operator."""
    from atlas.secrets import load_into_environment, set_secret

    path = isolated / ".secrets.env"
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(path))
    get_settings.cache_clear()

    set_secret("SOME_DB_URL", "postgresql://stored", path)
    monkeypatch.setenv("SOME_DB_URL", "postgresql://exported")
    load_into_environment(path)

    import os

    assert os.environ["SOME_DB_URL"] == "postgresql://exported"


def test_connection_errors_are_one_readable_line(client, isolated, monkeypatch) -> None:
    """psycopg reports every address it tried; the first FATAL is the message."""
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    client.post("/sources", json=VALID)
    health = client.put(
        "/sources/elara/credentials",
        json={"url": "postgresql+psycopg://nobody:wrong@127.0.0.1:1/nope"},
    ).json()

    assert health["state"] == "failed"
    assert "\n" not in health["detail"]
    assert len(health["detail"]) < 200
