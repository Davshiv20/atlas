from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from atlas import api
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


def test_snowflake_fields_are_encoded_into_a_connection_url(
    client, isolated, monkeypatch
) -> None:
    path = isolated / ".secrets.env"
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(path))
    get_settings.cache_clear()
    monkeypatch.setattr(
        api,
        "_probe",
        lambda source: api.ConnectionHealth(state="connected", detail="connected"),
    )
    source = {
        "id": "trellis",
        "adapter": "snowflake",
        "url_env": "TRELLIS_DATABASE_URL",
        "namespace": "POC_DB.TRELLIS_SOURCE",
    }
    assert client.post("/sources", json=source).status_code == 201

    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "SHIVAM",
            "password": "contains@special:/?#%",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    )

    assert response.status_code == 200
    parsed = make_url(os.environ["TRELLIS_DATABASE_URL"])
    assert parsed.username == "SHIVAM"
    assert parsed.password == "contains@special:/?#%"
    assert parsed.host == "myorg-myaccount"
    assert parsed.database == "POC_DB/TRELLIS_SOURCE"
    assert dict(parsed.query) == {"role": "ATLAS_READER", "warehouse": "POC_WH"}
    assert "contains@special" not in str(client.get("/sources").json())


def test_snowflake_external_browser_auth_builds_a_passwordless_url(
    client, isolated, monkeypatch
) -> None:
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    monkeypatch.setattr(
        api,
        "_probe",
        lambda source: api.ConnectionHealth(state="connected", detail="connected"),
    )
    source = {
        "id": "trellis",
        "adapter": "snowflake",
        "url_env": "TRELLIS_DATABASE_URL",
        "namespace": "POC_DB.TRELLIS_SOURCE",
    }
    assert client.post("/sources", json=source).status_code == 201

    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "SHIVAM",
            "auth_method": "external_browser",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    )

    assert response.status_code == 200
    parsed = make_url(os.environ["TRELLIS_DATABASE_URL"])
    assert parsed.username == "SHIVAM"
    assert parsed.password is None
    assert dict(parsed.query) == {
        "authenticator": "externalbrowser",
        "role": "ATLAS_READER",
        "warehouse": "POC_WH",
    }


def test_snowflake_mfa_push_auth_builds_the_connector_authenticator(
    client, isolated, monkeypatch
) -> None:
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    monkeypatch.setattr(
        api,
        "_probe",
        lambda source: api.ConnectionHealth(state="connected", detail="connected"),
    )
    source = {
        "id": "trellis",
        "adapter": "snowflake",
        "url_env": "TRELLIS_DATABASE_URL",
        "namespace": "POC_DB.TRELLIS_SOURCE",
    }
    assert client.post("/sources", json=source).status_code == 201

    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "SHIVAM",
            "auth_method": "mfa_push",
            "password": "secret",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    )

    assert response.status_code == 200
    parsed = make_url(os.environ["TRELLIS_DATABASE_URL"])
    assert parsed.password == "secret"
    assert dict(parsed.query) == {
        "authenticator": "username_password_mfa",
        "client_request_mfa_token": "true",
        "role": "ATLAS_READER",
        "warehouse": "POC_WH",
    }


def test_snowflake_totp_is_used_once_and_not_persisted(client, isolated, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    probed_urls: list[str | None] = []

    def record_probe(source, connection_url=None):
        probed_urls.append(connection_url)
        return api.ConnectionHealth(state="connected", detail="connected")

    monkeypatch.setattr(api, "_probe", record_probe)
    source = {
        "id": "trellis",
        "adapter": "snowflake",
        "url_env": "TRELLIS_DATABASE_URL",
        "namespace": "POC_DB.TRELLIS_SOURCE",
    }
    assert client.post("/sources", json=source).status_code == 201

    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "SHIVAM",
            "auth_method": "mfa_totp",
            "password": "secret",
            "passcode": "123456",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    )

    assert response.status_code == 200
    stored = make_url(os.environ["TRELLIS_DATABASE_URL"])
    probed = make_url(probed_urls[-1] or "")
    assert "passcode" not in stored.query
    assert probed.query["passcode"] == "123456"
    assert probed.query["client_request_mfa_token"] == "true"
    assert "123456" not in (isolated / ".secrets.env").read_text()


def test_snowflake_totp_requires_a_six_digit_code(client) -> None:
    client.post(
        "/sources",
        json={
            "id": "trellis",
            "adapter": "snowflake",
            "url_env": "TRELLIS_DATABASE_URL",
            "namespace": "POC_DB.TRELLIS_SOURCE",
        },
    )
    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "org-account",
            "username": "user",
            "auth_method": "mfa_totp",
            "password": "secret",
            "passcode": "12345",
            "warehouse": "warehouse",
            "role": "role",
        },
    )
    assert response.status_code == 422


def test_snowflake_password_auth_requires_a_password(client) -> None:
    client.post(
        "/sources",
        json={
            "id": "trellis",
            "adapter": "snowflake",
            "url_env": "TRELLIS_DATABASE_URL",
            "namespace": "POC_DB.TRELLIS_SOURCE",
        },
    )
    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "org-account",
            "username": "user",
            "warehouse": "warehouse",
            "role": "role",
        },
    )
    assert response.status_code == 422


def test_snowflake_fields_are_refused_for_a_postgres_source(client) -> None:
    client.post("/sources", json=VALID)
    response = client.put(
        "/sources/elara/credentials/snowflake",
        json={
            "account_identifier": "org-account",
            "username": "user",
            "password": "password",
            "warehouse": "warehouse",
            "role": "role",
        },
    )
    assert response.status_code == 409


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


def test_snowflake_key_pair_auth_needs_no_person_and_carries_no_password(
    client, isolated, monkeypatch
) -> None:
    """The only Snowflake method that survives a background job.

    Extraction and analysis run for minutes with nobody present. An interactive
    login authenticates once and leaves the stored credential unable to connect
    on its own — which is how a green connection test turned into `MFA with TOTP
    is required` several minutes into a run.
    """
    path = isolated / ".secrets.env"
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(path))
    get_settings.cache_clear()
    monkeypatch.setattr(
        api,
        "_probe",
        lambda source, connection_url=None: api.ConnectionHealth(
            state="connected", detail="connected"
        ),
    )
    assert (
        client.post(
            "/sources",
            json={
                "id": "trellis",
                "adapter": "snowflake",
                "url_env": "TRELLIS_DATABASE_URL",
                "namespace": "POC_DB.TRELLIS_SOURCE",
            },
        ).status_code
        == 201
    )

    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "ATLAS_SVC",
            "auth_method": "key_pair",
            "private_key_file": "/etc/atlas/snowflake_key.p8",
            "private_key_file_pwd": "keeps-the-key-shut",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    )
    assert response.status_code == 200

    parsed = make_url(os.environ["TRELLIS_DATABASE_URL"])
    # Exactly the parameter names the Python connector documents.
    assert parsed.query["authenticator"] == "SNOWFLAKE_JWT"
    assert parsed.query["private_key_file"] == "/etc/atlas/snowflake_key.p8"
    assert parsed.query["private_key_file_pwd"] == "keeps-the-key-shut"
    assert parsed.password is None, "key-pair auth must not carry a password"
    assert "client_request_mfa_token" not in parsed.query


def test_snowflake_key_pair_requires_a_key_file(client, isolated, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    assert (
        client.post(
            "/sources",
            json={
                "id": "trellis",
                "adapter": "snowflake",
                "url_env": "TRELLIS_DATABASE_URL",
                "namespace": "POC_DB.TRELLIS_SOURCE",
            },
        ).status_code
        == 201
    )
    response = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "ATLAS_SVC",
            "auth_method": "key_pair",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    )
    assert response.status_code == 422


def test_an_interactive_login_says_it_will_not_survive_a_job(
    client, isolated, monkeypatch
) -> None:
    """A green test on MFA proves a person was here, not that a job can run."""
    monkeypatch.setenv("ATLAS_SECRETS_FILE", str(isolated / ".secrets.env"))
    get_settings.cache_clear()
    monkeypatch.setattr(
        api,
        "_probe",
        lambda source, connection_url=None: api.ConnectionHealth(
            state="connected", detail="Connected — Snowflake 9.x"
        ),
    )
    assert (
        client.post(
            "/sources",
            json={
                "id": "trellis",
                "adapter": "snowflake",
                "url_env": "TRELLIS_DATABASE_URL",
                "namespace": "POC_DB.TRELLIS_SOURCE",
            },
        ).status_code
        == 201
    )
    body = client.put(
        "/sources/trellis/credentials/snowflake",
        json={
            "account_identifier": "myorg-myaccount",
            "username": "SHIVAM",
            "auth_method": "mfa_totp",
            "password": "secret",
            "passcode": "123456",
            "warehouse": "POC_WH",
            "role": "ATLAS_READER",
        },
    ).json()
    assert body["state"] == "connected"
    assert "unattended" in body["detail"]
    assert "ALLOW_CLIENT_MFA_CACHING" in body["detail"]
