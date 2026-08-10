from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atlas.api import mount_console

SHELL = "<!doctype html><title>atlas</title>"


@pytest.fixture
def built(tmp_path):
    (tmp_path / "index.html").write_text(SHELL)
    (tmp_path / "app.js").write_text("console.log(1)")
    return tmp_path


def app_with(directory) -> FastAPI:
    application = FastAPI()

    @application.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @application.get("/workspaces/{name}/output")
    def output(name: str) -> dict:
        return {"workspace": name}

    mount_console(application, directory)
    return application


def test_the_api_still_wins_over_the_catch_all(built) -> None:
    """A mount at "/" matches everything. Registered before the routers it
    would swallow the entire API."""
    client = TestClient(app_with(built))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/workspaces/elara/output").json() == {"workspace": "elara"}


def test_a_browser_navigation_gets_the_app_shell(built) -> None:
    """`/workspaces/elara` exists only in the browser's router, so the server
    has to answer it with index.html."""
    client = TestClient(app_with(built))
    response = client.get(
        "/map", headers={"accept": "text/html", "sec-fetch-dest": "document"}
    )
    assert response.status_code == 200
    assert response.text == SHELL


def test_a_missing_asset_is_not_answered_with_html(built) -> None:
    """The fallback is for navigations only. Handing a page of HTML to a fetch
    that expected JSON turns a 404 into a parse error somewhere else."""
    client = TestClient(app_with(built))
    response = client.get("/missing.js", headers={"accept": "*/*"})
    assert response.status_code == 404
    assert "<!doctype html>" not in response.text.lower()


def test_a_real_asset_is_served_as_itself(built) -> None:
    client = TestClient(app_with(built))
    assert client.get("/app.js").text == "console.log(1)"


def test_no_build_is_not_an_error(tmp_path) -> None:
    """In development the console runs on its own port; there is nothing here
    to serve and the engine must still start."""
    client = TestClient(app_with(tmp_path / "never-built"))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/map").status_code == 404


def test_the_bare_root_serves_the_app(built) -> None:
    """It only worked for browsers: anything not asking for HTML got a 404 at
    "/", because the fallback keys off the Accept header."""
    client = TestClient(app_with(built))
    response = client.get("/", headers={"accept": "*/*"})
    assert response.status_code == 200
    assert response.text == SHELL
