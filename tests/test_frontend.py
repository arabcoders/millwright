from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from server.api import app as app_module
from server.api.app import create_app
from server.core.config import ServerConfig
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_frontend_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ui = tmp_path / "ui"
    assets = ui / "assets"
    assets.mkdir(parents=True)
    (ui / "index.html").write_text("<html>Millwright</html>")
    (ui / "404.html").write_text("<html>Not found</html>")
    (assets / "app.js").write_text("const app = 'Millwright';")
    (ui / "manifest.webmanifest").write_text('{"name":"Millwright"}')
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (ui / "secret.txt").symlink_to(secret)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").touch()
    (repo / "README.md").write_text("# Bundled guide\n")
    (repo / "config.toml").write_text("api_key = 'private'")
    monkeypatch.setattr(app_module, "_REPO_ROOT", repo)
    monkeypatch.setenv("MW_UI_PATH", str(ui))

    async with database_context("frontend", tables=()) as factory:
        app = create_app(factory, config=ServerConfig(api_key="test-key"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            for path in ("/", "/jobs/42", "/flows/example/edit", "/index.html", "/docs", "/docs/installation"):
                response = await client.get(path)
                assert response.status_code == 200
                assert response.text == "<html>Millwright</html>"
                assert response.headers["cache-control"] == "no-cache"
            head = await client.head("/jobs/42")
            assert head.status_code == 200
            assert head.content == b""
            script = await client.get("/assets/app.js")
            assert script.status_code == 200
            assert "javascript" in script.headers["content-type"]
            assert script.text == "const app = 'Millwright';"
            assert (await client.get("/manifest.webmanifest")).json() == {"name": "Millwright"}
            for path in (
                "/assets/missing.js",
                "/assets/missing",
                "/secret.txt",
                "/%2e%2e/secret.txt",
                "/api/missing",
                "/api",
                "/api/docs/config.toml",
                "/api/docs/../config.toml",
                "/api/docs/missing.md",
            ):
                response = await client.get(path)
                assert response.status_code == 404
                assert response.headers["content-type"] == "application/json"
            assert (await client.post("/jobs/42")).status_code == 405
            assert (await client.post("/api/missing")).status_code == 404
            assert (await client.get("/api/libraries")).status_code == 401
            assert (await client.post("/api/auth/logout")).status_code == 401
            assert (await client.get("/api/auth/oidc/login")).status_code == 404
            assert (await client.get("/health")).json()["status"] == "ok"
            guide = await client.get("/api/docs/README.md")
            assert guide.status_code == 200
            assert guide.text == "# Bundled guide\n"
            assert guide.headers["content-type"].startswith("text/markdown")
            for path in ("/api/docs", "/api/redoc"):
                rendered = await client.get(path)
                assert rendered.status_code == 404
            redirect = await client.get("/api/docs/oauth2-redirect")
            assert redirect.status_code == 404
            assert (await client.get("/api/openapi.json")).status_code == 404

        enabled = create_app(factory, config=ServerConfig(api_key="test-key", enable_docs=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=enabled), base_url="http://testserver"
        ) as client:
            guide = await client.get("/api/docs/README.md")
            assert guide.status_code == 200
            assert guide.text == "# Bundled guide\n"
            for path in ("/api/docs", "/api/redoc"):
                rendered = await client.get(path)
                assert rendered.status_code == 200
                assert "/api/openapi.json" in rendered.text
            redirect = await client.get("/api/docs/oauth2-redirect")
            assert redirect.status_code == 200
            assert redirect.headers["content-type"].startswith("text/html")
            assert (await client.get("/api/openapi.json")).json()["info"]["title"] == "Millwright"


@pytest.mark.asyncio
async def test_installed_frontend_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ui = tmp_path / "prefix/share/millwright/ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html>Installed UI</html>")
    docs = tmp_path / "prefix/share/millwright/documentation/docs"
    docs.mkdir(parents=True)
    (docs / "installation.md").write_text("# Installed guide\n")
    monkeypatch.delenv("MW_UI_PATH", raising=False)
    async with database_context("installed-frontend", tables=()) as factory:
        with monkeypatch.context() as context:
            context.setattr(app_module, "_REPO_ROOT", tmp_path / "installed")
            context.setattr(app_module.sys, "prefix", str(tmp_path / "prefix"))
            app = create_app(factory, config=ServerConfig())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.text == "<html>Installed UI</html>"
            guide = await client.get("/api/docs/docs/installation.md")
            assert guide.status_code == 200
            assert guide.text == "# Installed guide\n"
