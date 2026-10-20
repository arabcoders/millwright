import configparser
import json
from pathlib import Path

import httpx
import pytest
from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from server import cli
from server.core.db import MIGRATIONS
from tests.helpers import database_engine, database_url_context


def run_cli(monkeypatch, argv, handler):
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setenv("MW_API_URL", "http://example")
    return cli.main(argv, client_factory=factory)


@pytest.mark.parametrize("action", ["healthcheck", "transcode"])
@pytest.mark.parametrize("failed", [False, True])
def test_files_bulk(monkeypatch, capsys, action, failed):
    def handler(request):
        assert request.url.path == "/api/files/bulk"
        assert json.loads(request.content) == {"action": action, "file_ids": [9, 7, 9]}
        return httpx.Response(
            202,
            json={
                "accepted": [] if failed else [{"file_id": 9, "job_id": 12, "kind": action, "status": "pending"}],
                "failed": [{"file_id": 9, "status": 409, "detail": "active job"}] if failed else [],
            },
        )

    assert run_cli(monkeypatch, ["files", "bulk", action, "9", "7", "9"], handler) == int(failed)
    output = capsys.readouterr().out
    assert output
    if failed:
        assert "active job" in output


def test_system_route(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/system"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"version": "1", "python": "3"})

    assert run_cli(monkeypatch, ["--token", "secret", "system"], handler) == 0


@pytest.mark.parametrize("sync_to_disk", [True, False])
def test_trash_restore_flags(monkeypatch, sync_to_disk):
    def handler(request):
        assert request.url.path == "/api/trash/abc/restore"
        assert json.loads(request.content) == {
            "replace_existing": True,
            "move_to_trash": True,
            "sync_to_disk": sync_to_disk,
        }
        return httpx.Response(200, json={"id": "abc", "restored_path": "/library/movie.mkv"})

    args = ["trash", "restore", "abc", "--replace-existing", "--move-to-trash"]
    if not sync_to_disk:
        args.append("--no-sync-to-disk")
    assert run_cli(monkeypatch, args, handler) == 0


def test_trash_restore_requires_replace(monkeypatch):
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(500)

    assert run_cli(monkeypatch, ["trash", "restore", "abc", "--move-to-trash"], handler) == 1
    assert not called


def test_local_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MW_API_TOKEN", raising=False)
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    (tmp_path / "config.toml").write_text('api_key = "file:api_key"\n', encoding="utf-8")
    (tmp_path / "api_key").write_text("local-key\n", encoding="utf-8")

    def handler(request):
        assert request.headers["authorization"] == "Bearer local-key"
        return httpx.Response(200, json={"version": "1", "python": "3"})

    assert run_cli(monkeypatch, ["--url", "http://127.0.0.1:8000", "system"], handler) == 0


@pytest.mark.parametrize(
    ("passwords", "status", "expected"),
    [
        ((" new password ", " new password "), 204, 0),
        (("new password", "different password"), 204, 1),
        (("", ""), 204, 1),
        (("new password", "new password"), 403, 1),
    ],
)
def test_password_reset_prompts(monkeypatch, tmp_path, capsys, passwords, status, expected):
    monkeypatch.delenv("MW_API_TOKEN", raising=False)
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    (tmp_path / "config.toml").write_text('api_key = "file:api_key"\n', encoding="utf-8")
    (tmp_path / "api_key").write_text("local-key\n", encoding="utf-8")
    entered = iter(passwords)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(entered))
    requests = []

    def handler(request):
        requests.append(request)
        assert (request.method, request.url.path) == ("POST", "/api/auth/reset-password")
        assert request.headers["authorization"] == "Bearer local-key"
        assert json.loads(request.content) == {"username": "operator", "password": passwords[0]}
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(status, json={"detail": "Private API key required"})

    assert (
        run_cli(
            monkeypatch,
            ["--url", "http://127.0.0.1:8000", "auth", "reset-password", "--username", "operator"],
            handler,
        )
        == expected
    )
    output = capsys.readouterr()
    confirmed = bool(passwords[0]) and passwords[0] == passwords[1]
    assert len(requests) == int(confirmed)
    assert ("Password reset for operator." in output.out) == (expected == 0)
    assert not any(password and password in output.out + output.err for password in passwords)
    if not passwords[0]:
        assert output.err.strip() == "Password cannot be empty."
    elif passwords[0] != passwords[1]:
        assert output.err.strip() == "Passwords do not match."
    elif status == 403:
        assert "Private API key required" in output.err


def test_remote_key_not_sent(monkeypatch, tmp_path):
    monkeypatch.delenv("MW_API_TOKEN", raising=False)
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    (tmp_path / "config.toml").write_text('api_key = "file:api_key"\n', encoding="utf-8")
    (tmp_path / "api_key").write_text("local-key\n", encoding="utf-8")

    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"version": "1", "python": "3"})

    assert run_cli(monkeypatch, ["system"], handler) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "jobs", "show", "2"],
        ["jobs", "--json", "show", "2"],
        ["jobs", "show", "2", "--json"],
        ["flows", "show", "name", "--json"],
        ["nodes", "list", "--json"],
        ["api", "GET", "/api/system", "--json"],
    ],
)
@pytest.mark.parametrize("flag", ["--json", "-j"])
def test_json_flag_positions(monkeypatch, capsys, argv, flag):
    def handler(request):
        return httpx.Response(200, json=[{"id": 2}] if argv[0] == "nodes" else {"id": 2, "status": "done"})

    assert run_cli(monkeypatch, [flag if arg == "--json" else arg for arg in argv], handler) == 0
    assert json.loads(capsys.readouterr().out)


def test_json_flag_rejects():
    with pytest.raises(SystemExit) as rejected:
        cli.main(["jobs", "list", "--json=true"])
    assert rejected.value.code == 2


def test_jobs_watch_changes(monkeypatch, capsys):
    values = iter(
        [
            {"id": 3, "status": "running", "progress": {"percent": 1, "fps": 2}},
            {"id": 3, "status": "running", "progress": {"percent": 1, "fps": 2}},
            {"id": 3, "status": "succeeded", "progress": {"percent": 100}},
        ]
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    def handler(request):
        assert request.url.path == "/api/jobs/3/state"
        return httpx.Response(200, json=next(values))

    assert run_cli(monkeypatch, ["jobs", "watch", "3", "--json"], handler) == 0
    assert [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()] == ["running", "succeeded"]


def test_jobs_watch_timeout(monkeypatch):
    times = iter([0.0, 2.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))

    def handler(request):
        assert request.url.path == "/api/jobs/4/state"
        return httpx.Response(200, json={"id": 4, "status": "running", "progress": None})

    assert run_cli(monkeypatch, ["jobs", "watch", "4", "--timeout", "1"], handler) == 1


def test_stats_directories(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url)
        if request.url.path == "/api/stats":
            return httpx.Response(200, json={"jobs": 2})
        return httpx.Response(200, json={"path": "/tmp", "items": [{"name": "x", "path": "/tmp/x"}]})

    assert run_cli(monkeypatch, ["--json", "stats"], handler) == 0
    assert run_cli(monkeypatch, ["directories", "/tmp"], handler) == 0
    assert seen[1].params["path"] == "/tmp"


def test_api_rejects_path(monkeypatch):
    assert run_cli(monkeypatch, ["api", "GET", "/test"], lambda request: None) == 1


def test_api_no_content(monkeypatch, capsys):
    assert run_cli(monkeypatch, ["api", "DELETE", "/api/test"], lambda request: httpx.Response(204)) == 0
    assert capsys.readouterr().out == ""


def test_plugins_list_show(monkeypatch):
    plugin = {
        "manifest": {"id": "demo/plugin"},
    }

    def handler(request):
        assert request.url.raw_path.decode() in ("/api/plugins", "/api/plugins/demo%2Fplugin")
        return httpx.Response(200, json=[plugin] if request.url.raw_path == b"/api/plugins" else plugin)

    assert run_cli(monkeypatch, ["plugins", "list", "--json"], handler) == 0
    assert run_cli(monkeypatch, ["plugins", "show", "demo/plugin", "--json"], handler) == 0


@pytest.mark.parametrize(
    ("action", "body"),
    [("enable", {"enabled": True}), ("disable", {"enabled": False})],
)
def test_plugin_toggle(monkeypatch, action, body):
    def handler(request):
        assert (request.method, request.url.raw_path.decode()) == ("PATCH", "/api/plugins/demo%2Fplugin")
        assert json.loads(request.content) == body
        return httpx.Response(200, json={"manifest": {"id": "demo/plugin"}, "state": action})

    assert run_cli(monkeypatch, ["plugins", action, "demo/plugin"], handler) == 0


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_plugin_action(monkeypatch, action):
    def handler(request):
        assert (request.method, request.url.raw_path.decode()) == ("POST", f"/api/plugins/demo%2Fplugin/{action}")
        return httpx.Response(200, json={})

    assert run_cli(monkeypatch, ["plugins", action, "demo/plugin"], handler) == 0


def test_plugin_install(monkeypatch, tmp_path):
    archive = tmp_path / "demo.zip"
    archive.write_bytes(b"archive-bytes")

    def handler(request):
        assert request.url.path == "/api/plugins"
        fields = request.content
        assert b'filename="demo.zip"' in fields
        assert b'name="file"' in fields
        assert b"archive-bytes" in fields
        return httpx.Response(200, json={"manifest": {"id": "demo"}, "state": "enabled"})

    assert run_cli(monkeypatch, ["plugins", "install", str(archive)], handler) == 0


def test_missing_archive(monkeypatch, tmp_path):
    assert run_cli(monkeypatch, ["plugins", "install", str(tmp_path / "missing.zip")], lambda request: None) == 1


def test_db_local(monkeypatch, capsys, query_db):
    assert run_cli(monkeypatch, ["db", "query", "SELECT id, name FROM demo WHERE id = 1"], lambda request: None) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": 1, "name": "one"}]
    assert run_cli(monkeypatch, ["db", "query", "DROP TABLE demo"], lambda request: None) == 1
    assert run_cli(monkeypatch, ["db", "query", "DROP TABLE demo", "--write"], lambda request: None) == 0
    assert json.loads(capsys.readouterr().out) == {"rows_affected": -1}


@pytest.mark.parametrize("override", [False, True])
def test_db_revision(monkeypatch, override, database_url, tmp_path):
    url = database_url
    configured = url.set(database="unused") if override else url
    monkeypatch.setenv("MW_DATABASE_URL", configured.render_as_string(hide_password=False))
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path / "unconfigured"))
    called = []

    def revision(config, *, message, autogenerate):
        assert config.attributes["database_url"] == url
        assert config.get_main_option("script_location") == str(MIGRATIONS)
        assert config.get_section_option("post_write_hooks", "ruff.executable") == "uv"
        assert message == "add a field"
        assert autogenerate is True
        called.append(config)

    monkeypatch.setattr(cli.command, "revision", revision)
    args = ["db", "revision", "--message" if override else "-m", "add a field"]
    if override:
        args[1:1] = ["--database-url", url.render_as_string(hide_password=False)]
    assert run_cli(monkeypatch, args, lambda request: None) == 0
    assert len(called) == 1


def test_db_revision_message():
    with pytest.raises(SystemExit) as rejected:
        cli.main(["db", "revision"])
    assert rejected.value.code == 2


@pytest.mark.parametrize("source", ["env", "argument", "file"])
def test_db_upgrade(monkeypatch, source, database_url, tmp_path):
    url = database_url
    configured = url if source == "env" else url.set(database="unused")
    monkeypatch.setenv("MW_DATABASE_URL", configured.render_as_string(hide_password=False))
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path / "unconfigured"))
    called = []

    def prepare(engine):
        assert engine.url == url
        called.append(engine)

    monkeypatch.setattr(cli, "prepare_db", prepare)
    args = ["db", "upgrade"]
    if source == "argument":
        args[1:1] = ["--database-url", url.render_as_string(hide_password=False)]
    elif source == "file":
        secret = tmp_path / "db_connection"
        secret.write_text(url.render_as_string(hide_password=False) + "\n")
        monkeypatch.chdir(tmp_path)
        args[1:1] = ["--database-url", f"file:{secret.name}"]
    assert run_cli(monkeypatch, args, lambda request: None) == 0
    assert len(called) == 1


@pytest.mark.parametrize(
    ("action", "error"),
    [
        ("revision", CommandError("database is not current")),
        ("revision", configparser.Error("invalid migration config")),
        ("revision", ValueError("invalid migration option")),
        ("upgrade", OSError("cannot open database")),
        ("upgrade", SQLAlchemyError("database is locked")),
    ],
)
def test_db_migration_errors(monkeypatch, action, error, database_url):
    monkeypatch.setenv("MW_DATABASE_URL", database_url.render_as_string(hide_password=False))

    def fail(*args, **kwargs):
        raise error

    if action == "revision":
        monkeypatch.setattr(cli.command, "revision", fail)
    else:
        monkeypatch.setattr(cli, "prepare_db", fail)
    args = ["db", action] + (["-m", "add a field"] if action == "revision" else [])
    assert run_cli(monkeypatch, args, lambda request: None) == 1


@pytest.fixture
def query_db(monkeypatch, tmp_path):
    with database_engine("cli-query") as engine:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT UNIQUE)"))
            connection.execute(text("INSERT INTO demo VALUES (1, 'one'), (2, 'two')"))
        monkeypatch.setenv("MW_DATABASE_URL", engine.url.render_as_string(hide_password=False))
        monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path / "unconfigured"))
        yield engine


@pytest.mark.parametrize(
    ("sql", "returned", "stored"),
    [
        (
            "INSERT INTO demo VALUES (3, 'three'), (4, 'four') RETURNING id, name",
            [{"id": 3, "name": "three"}, {"id": 4, "name": "four"}],
            [(1, "one"), (2, "two"), (3, "three"), (4, "four")],
        ),
        (
            "UPDATE demo SET name = name || '-updated' RETURNING id, name",
            [{"id": 1, "name": "one-updated"}, {"id": 2, "name": "two-updated"}],
            [(1, "one-updated"), (2, "two-updated")],
        ),
        (
            "DELETE FROM demo RETURNING id, name",
            [{"id": 1, "name": "one"}, {"id": 2, "name": "two"}],
            [],
        ),
    ],
)
def test_returning_commits(monkeypatch, capsys, query_db, sql, returned, stored):
    assert run_cli(monkeypatch, ["db", "query", sql], lambda request: None) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert run_cli(monkeypatch, ["db", "query", sql, "--write"], lambda request: None) == 0
    assert json.loads(capsys.readouterr().out) == returned
    with query_db.connect() as connection:
        assert connection.execute(text("SELECT id, name FROM demo ORDER BY id")).all() == stored


def test_failed_write_rollback(monkeypatch, capsys, query_db):
    sql = "UPDATE demo SET name = 'changed' RETURNING id, name"
    assert run_cli(monkeypatch, ["db", "query", sql, "--write"], lambda request: None) == 1
    output = capsys.readouterr()
    assert output.out == ""
    with query_db.connect() as connection:
        assert connection.execute(text("SELECT id, name FROM demo ORDER BY id")).all() == [(1, "one"), (2, "two")]


def test_action_json(monkeypatch, capsys):
    payload = {"manifest": {"id": "demo"}, "state": "enabled", "extra": "full-payload"}

    def handler(request):
        return httpx.Response(200, json=payload)

    assert run_cli(monkeypatch, ["plugins", "restart", "demo", "--json"], handler) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("as_json", [False, True])
def test_node_remove(monkeypatch, capsys, as_json):
    def handler(request):
        assert (request.method, request.url.path) == ("DELETE", "/api/nodes/4")
        return httpx.Response(204)

    args = ["nodes", "remove", "4"] + (["--json"] if as_json else [])
    assert run_cli(monkeypatch, args, handler) == 0
    if as_json:
        assert capsys.readouterr().out == ""


def test_data_file(monkeypatch, tmp_path):
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"x": 1}))

    def handler(request):
        assert json.loads(request.content) == {"x": 1}
        return httpx.Response(200, json={})

    assert run_cli(monkeypatch, ["api", "POST", "/api/test", "--data-file", str(body)], handler) == 0


def test_jobs_list_contract(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/jobs"
        assert dict(request.url.params) == {
            "status": "queued",
            "limit": "3",
            "sort_by": "created_at",
            "sort_dir": "desc",
        }
        return httpx.Response(
            200,
            json={
                "items": [],
                "pagination": {"limit": 3, "offset": 0, "total": 0},
            },
        )

    assert run_cli(monkeypatch, ["jobs", "list", "--status", "queued", "--limit", "3", "--json"], handler) == 0


def test_jobs_show_contract(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/jobs/7"
        assert request.url.params["events"] == "1"
        return httpx.Response(200, json={"id": 7, "attempts": []})

    assert run_cli(monkeypatch, ["jobs", "show", "7", "--json"], handler) == 0


def test_stats_json_dates(monkeypatch, capsys):
    paths = []
    payload = {"backlog": {"remaining": 1, "estimated_finish_at": "2026-01-02T01:00:00Z"}}

    def handler(request):
        paths.append(request.url.path)
        assert request.url.path == "/api/stats"
        return httpx.Response(200, json=payload)

    assert run_cli(monkeypatch, ["stats", "--json"], handler) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert paths == ["/api/stats"]


@pytest.mark.parametrize("args", [["jobs", "show", "7"], ["nodes", "list"], ["libraries", "progress"]])
def test_json_dates_unchanged(monkeypatch, capsys, args):
    timestamp = "2026-01-01T23:30:00.123456Z"
    if args[0] == "jobs":
        payload = {"attempts": [{"claimed_at": timestamp}], "events": [{"ts": timestamp}]}
        path = "/api/jobs/7"
    elif args[0] == "nodes":
        payload = [{"last_seen_at": timestamp}]
        path = "/api/nodes"
    else:
        payload = {"libraries": [{"scanner": {"next_run_at": timestamp}, "discovery": {"finished_at": timestamp}}]}
        path = "/api/libraries/progress"

    def handler(request):
        assert request.url.path == path
        return httpx.Response(200, json=payload)

    assert run_cli(monkeypatch, [*args, "--json"], handler) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_jobs_log_contract(monkeypatch, capsys):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/7"):
            assert request.url.params["events"] == "0"
            return httpx.Response(
                200, json={"attempts": [{"id": 3, "attempt_number": 1}, {"id": 8, "attempt_number": 4}]}
            )
        assert request.url.path == "/api/jobs/7/attempts/8/log"
        assert request.headers["Range"] == "bytes=-262144"
        return httpx.Response(200, content=b"tail")

    assert run_cli(monkeypatch, ["jobs", "log", "7"], handler) == 0
    assert capsys.readouterr().out == "tail"
    assert len(seen) == 2


def test_complete_log(monkeypatch, capsys):
    def handler(request):
        assert request.url.path == "/api/jobs/7/attempts/3/log"
        assert "range" not in request.headers
        return httpx.Response(200, content=b"complete")

    assert run_cli(monkeypatch, ["jobs", "log", "7", "--attempt", "3", "--complete"], handler) == 0
    assert capsys.readouterr().out == "complete"


def test_missing_attempt(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"attempts": []})

    assert run_cli(monkeypatch, ["jobs", "log", "7"], handler) == 1


@pytest.mark.parametrize(
    ("action", "method", "path", "body"),
    [
        ("cancel", "POST", "/api/jobs/7/cancel", None),
        ("bump", "POST", "/api/jobs/7/bump", {}),
    ],
)
def test_job_action(monkeypatch, action, method, path, body):
    def handler(request):
        assert (request.method, request.url.path) == (method, path)
        assert json.loads(request.content) == body if body is not None else not request.content
        return httpx.Response(
            202 if action == "requeue" else 200,
            json={"id": 7, "status": "pending" if action == "requeue" else "staging"},
        )

    assert run_cli(monkeypatch, ["jobs", action, "7"], handler) == 0


def test_bulk_requeue(monkeypatch, capsys):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/jobs/bulk"
        assert json.loads(request.content) == {"action": "retry", "job_ids": [7, 8]}
        return httpx.Response(
            202,
            json={
                "accepted": [7],
                "failed": [{"id": 8, "detail": "active job"}],
            },
        )

    assert run_cli(monkeypatch, ["jobs", "bulk", "requeue", "7", "8"], handler) == 0
    assert "active job" in capsys.readouterr().out


@pytest.mark.parametrize("as_json", [False, True])
def test_requeue_accepted(monkeypatch, capsys, as_json):
    payload = {"id": 7, "status": "pending"}

    def handler(request):
        assert request.url.path == "/api/jobs/7/retry"
        assert not request.content
        return httpx.Response(202, json=payload)

    assert run_cli(monkeypatch, ["jobs", "requeue", "7"] + (["--json"] if as_json else []), handler) == 0
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == payload
    else:
        assert output


@pytest.mark.parametrize("as_json", [False, True])
def test_bulk_accepted(monkeypatch, capsys, as_json):
    payload = {"accepted": [7, 8], "failed": []}

    def handler(request):
        assert request.url.path == "/api/jobs/bulk"
        assert json.loads(request.content) == {"action": "retry", "job_ids": [7, 8]}
        return httpx.Response(202, json=payload)

    assert run_cli(monkeypatch, ["jobs", "bulk", "requeue", "7", "8"] + (["--json"] if as_json else []), handler) == 0
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == payload
    else:
        assert output


def test_requeue_conflict(monkeypatch, capsys):
    def handler(request):
        return httpx.Response(
            409,
            json={"detail": "File already has an active job."},
        )

    assert run_cli(monkeypatch, ["jobs", "requeue", "7"], handler) == 1
    assert "File already has an active job." in capsys.readouterr().err


def test_files_list_contract(monkeypatch):
    sort = "updated_at"

    def handler(request):
        assert dict(request.url.params) == {
            "query": "movie",
            "library_id": "2",
            "transcode_status": "pending",
            "health_status": "ok",
            "limit": "5",
            "offset": "1",
            "sort_by": sort,
            "sort_dir": "desc",
        }
        return httpx.Response(
            200,
            json={
                "items": [],
                "pagination": {"limit": 5, "offset": 1, "total": 0},
            },
        )

    args = [
        "files",
        "list",
        "--query",
        "movie",
        "--library-id",
        "2",
        "--transcode-status",
        "pending",
        "--health-status",
        "ok",
        "--limit",
        "5",
        "--offset",
        "1",
        "--sort-by",
        sort,
        "--json",
    ]
    assert run_cli(monkeypatch, args, handler) == 0


@pytest.mark.parametrize("command", [("jobs", "list"), ("files", "list")])
def test_list_json_envelope(monkeypatch, capsys, command):
    payload = {"items": [{"id": 4}], "pagination": {"limit": 50, "offset": 0, "total": 1}}

    def handler(request):
        return httpx.Response(200, json=payload)

    assert run_cli(monkeypatch, ["--json", *command], handler) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("action", ["healthcheck", "transcode"])
@pytest.mark.parametrize("as_json", [False, True])
def test_file_action(monkeypatch, capsys, action, as_json):
    payload = {"job_id": 9, "kind": action, "status": "queued" if action == "healthcheck" else "pending"}

    def handler(request):
        assert (request.method, request.url.path, request.content) == ("POST", f"/api/files/4/{action}", b"")
        return httpx.Response(202, json=payload)

    assert run_cli(monkeypatch, ["files", action, "4"] + (["--json"] if as_json else []), handler) == 0
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == payload
    else:
        assert output


@pytest.mark.parametrize(
    ("action", "method", "path"),
    [("list", "GET", "/api/libraries"), ("show", "GET", "/api/libraries/4"), ("scan", "POST", "/api/libraries/4/scan")],
)
def test_library_routes(monkeypatch, action, method, path):
    def handler(request):
        assert (request.method, request.url.path) == (method, path)
        return httpx.Response(
            200,
            json=[] if action == "list" else {"id": 4, "name": "Movies", "library_id": 4, "status": "queued"},
        )

    args = ["libraries", action] + ([] if action == "list" else ["4"])
    assert run_cli(monkeypatch, args, handler) == 0


def test_library_delete(monkeypatch):
    def handler(request):
        assert (request.method, request.url.path) == ("DELETE", "/api/libraries/4")
        return httpx.Response(202, json={"library_id": 4, "status": "deleting"})

    assert run_cli(monkeypatch, ["libraries", "delete", "4"], handler) == 0


def test_library_create(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/libraries"
        assert json.loads(request.content) == {
            "name": "Movies",
            "roots": ["/a", "/b"],
            "cooldown_s": 12,
            "schedule": None,
            "watch": True,
            "scan_on_start": True,
            "scan_interval_s": 3600,
            "mkv_stats": True,
            "healthcheck_enabled": False,
            "processing_cutoff": {},
            "trash_mode": "all",
            "trash_retention_days": 14,
        }
        return httpx.Response(201, json={"id": 4, "name": "Movies"})

    args = [
        "libraries",
        "create",
        "Movies",
        "/a",
        "/b",
        "--cooldown-s",
        "12",
        "--watch",
        "--scan-on-start",
        "--scan-interval-s",
        "3600",
        "--mkv-stats",
        "--trash-mode",
        "all",
        "--trash-retention-days",
        "14",
    ]
    assert run_cli(monkeypatch, args, handler) == 0


def test_library_cutoff_create(monkeypatch):
    def handler(request):
        if request.url.path == "/api/system":
            return httpx.Response(200, json={"timezone": "Asia/Kuwait"})
        body = json.loads(request.content)
        assert body["processing_cutoff"] == {
            "at": "2026-01-01T12:00:00Z",
            "fields": ["first_seen", "mtime"],
            "match": "any",
        }
        return httpx.Response(201, json={"id": 4, "name": "Movies"})

    assert (
        run_cli(
            monkeypatch,
            [
                "libraries",
                "create",
                "Movies",
                "/movies",
                "--processing-cutoff-at",
                "2026-01-01T12:00:00Z",
                "--processing-cutoff-fields",
                "first_seen",
                "mtime",
                "--processing-cutoff-match",
                "any",
            ],
            handler,
        )
        == 0
    )


@pytest.mark.parametrize(
    "timezone, value, expected",
    [
        ("Asia/Kuwait", "2026-01-01T12:00:00", "2026-01-01T09:00:00Z"),
        ("Asia/Kuwait", "2026-01-01T12:00:00+01:00", "2026-01-01T11:00:00Z"),
        ("Europe/Berlin", "2026-10-25T02:30:00", "2026-10-25T00:30:00Z"),
    ],
)
def test_cutoff_local_datetime(monkeypatch, timezone, value, expected):
    def handler(request):
        if request.url.path == "/api/system":
            return httpx.Response(200, json={"timezone": timezone})
        assert request.method == "POST" and request.url.path == "/api/libraries"
        assert json.loads(request.content)["processing_cutoff"] == {
            "at": expected,
            "fields": ["created_at"],
            "match": "all",
        }
        return httpx.Response(201, json={"id": 4, "name": "Movies"})

    assert (
        run_cli(monkeypatch, ["libraries", "create", "Movies", "/movies", "--processing-cutoff-at", value], handler)
        == 0
    )


def test_cutoff_skipped_datetime(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/system"
        return httpx.Response(200, json={"timezone": "Europe/Berlin"})

    assert (
        run_cli(
            monkeypatch,
            ["libraries", "create", "Movies", "/movies", "--processing-cutoff-at", "2026-03-29T02:30:00"],
            handler,
        )
        == 1
    )


@pytest.mark.parametrize("raw_api", [False, True])
def test_cutoff_raw_json(monkeypatch, raw_api):
    body = {"processing_cutoff": {"at": "2026-01-01T12:00:00+03:00"}}
    args = ["api", "POST", "/api/libraries"] if raw_api else ["libraries", "update", "4"]

    def handler(request):
        assert request.url.path == ("/api/libraries" if raw_api else "/api/libraries/4")
        assert json.loads(request.content) == body
        return httpx.Response(200, json={"id": 4, "name": "Movies", **body})

    assert run_cli(monkeypatch, [*args, "--data", json.dumps(body)], handler) == 0


def test_trash_retention_validation():
    with pytest.raises(SystemExit):
        cli.main(["libraries", "create", "Movies", "/movies", "--trash-retention-days", "0"])


@pytest.mark.parametrize("action", ["pause", "resume", "status"])
def test_cli_scanner_control(monkeypatch, action: str):
    def handler(request):
        method = "GET" if action == "status" else "POST"
        path = "/api/libraries/scanner" if action == "status" else f"/api/libraries/scanner/{action}"
        assert (request.method, request.url.path) == (method, path)
        return httpx.Response(200, json={"paused": action == "pause"})

    assert run_cli(monkeypatch, ["libraries", "scanner", action], handler) == 0


def test_cli_scan_cancel(monkeypatch):
    def handler(request):
        assert (request.method, request.url.path) == ("POST", "/api/libraries/4/scan/cancel")
        return httpx.Response(200, json={"library_id": 4, "status": "cancelling"})

    assert run_cli(monkeypatch, ["libraries", "cancel", "4"], handler) == 0


@pytest.mark.parametrize("source", ["data", "data-file"])
def test_library_update(monkeypatch, tmp_path, source):
    body = {"name": "New", "watch": False}
    body_file = tmp_path / "library.json"
    body_file.write_text(json.dumps(body))

    def handler(request):
        assert (request.method, request.url.path) == ("PATCH", "/api/libraries/4")
        assert json.loads(request.content) == body
        return httpx.Response(200, json={"id": 4, "name": "New"})

    args = ["libraries", "update", "4", f"--{source}", json.dumps(body) if source == "data" else str(body_file)]
    assert run_cli(monkeypatch, args, handler) == 0


@pytest.mark.parametrize("body", ["[]", "not-json"])
def test_library_bad_body(monkeypatch, body):
    assert run_cli(monkeypatch, ["libraries", "update", "4", "--data", body], lambda request: None) == 1


def test_library_scan_json(monkeypatch, capsys):
    payload = {"library_id": 4, "status": "already_queued"}

    def handler(request):
        return httpx.Response(200, json=payload)

    assert run_cli(monkeypatch, ["libraries", "scan", "4", "--json"], handler) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize(
    ("action", "path"),
    [("list", "/api/flows"), ("facts", "/api/flows/facts"), ("show", "/api/flows/a%2Fb%3Fc")],
)
def test_flow_routes(monkeypatch, action, path):
    def handler(request):
        assert (request.method, request.url.raw_path.decode()) == ("GET", path)
        return httpx.Response(200, json=[] if action != "show" else {"id": 1, "name": "a/b?c", "version": 2})

    args = ["flows", action] + ([] if action in {"list", "facts"} else ["a/b?c"])
    assert run_cli(monkeypatch, args, handler) == 0


def test_flow_save_rename(monkeypatch, tmp_path):
    definition = {"steps": [{"plugin": "x"}]}
    flow_file = tmp_path / "flow.json"
    flow_file.write_text(json.dumps(definition))
    seen = []

    def handler(request):
        seen.append((request.method, request.url.raw_path.decode(), json.loads(request.content)))
        return httpx.Response(200, json={"id": 1, "name": "new", "version": 1})

    assert run_cli(monkeypatch, ["flows", "save", "old/name", str(flow_file)], handler) == 0
    assert (
        run_cli(monkeypatch, ["flows", "rename", "old/name", "new", str(flow_file), "--expected-version", "1"], handler)
        == 0
    )
    assert seen == [
        ("PUT", "/api/flows/old%2Fname", {"definition": definition, "expected_version": None}),
        ("PATCH", "/api/flows/old%2Fname", {"name": "new", "definition": definition, "expected_version": 1}),
    ]


def test_flow_delete(monkeypatch):
    def handler(request):
        assert (request.method, request.url.raw_path.decode()) == ("DELETE", "/api/flows/a%2Fb?expected_version=1")
        return httpx.Response(204)

    assert run_cli(monkeypatch, ["flows", "delete", "a/b", "--expected-version", "1"], handler) == 0
    assert run_cli(monkeypatch, ["flows", "delete", "a/b", "--expected-version", "1", "--json"], handler) == 0


def test_library_resume(monkeypatch):
    def handler(request):
        assert request.method == "PATCH" and request.url.path == "/api/libraries/7"
        assert json.loads(request.content) == {"suspended": False}
        return httpx.Response(200, json={"id": 7, "name": "reviewed", "suspended": False})

    assert run_cli(monkeypatch, ["libraries", "resume", "7"], handler) == 0


def test_flow_dry_run(monkeypatch, tmp_path, capsys):
    flow_file = tmp_path / "flow.json"
    flow_file.write_text('{"name":"flow","argv":["sentinel"]}')

    payload = {"status": "ok", "reason": "ready", "definition": "sentinel", "argv": ["sentinel"]}

    def handler(request):
        assert (request.method, request.url.path) == ("POST", "/api/flows/dry-run")
        assert json.loads(request.content) == {"file_id": 7, "flow": {"name": "flow", "argv": ["sentinel"]}}
        return httpx.Response(200, json=payload)

    assert run_cli(monkeypatch, ["flows", "dry-run", "7", str(flow_file), "--json"], handler) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("as_json", [False, True])
def test_local_dry_run(monkeypatch, tmp_path, capsys, as_json):
    media = tmp_path / "unindexed.mkv"
    media.write_bytes(b"untouched")
    media.with_suffix(".info.json").write_text('{"title":"Local sidecar","formats":[{"url":"not-forwarded"}]}')
    definition = {
        "entry": "metadata",
        "nodes": [
            {"id": "metadata", "type": "plugin", "plugin": "action.embed_metadata"},
            {"id": "cleanup", "type": "plugin", "plugin": "transcode.remux"},
        ],
        "edges": [{"src": "metadata", "port": "next", "dst": "cleanup"}],
    }
    fake = str(Path(__file__).parent / "fakes" / "ffprobe.py")
    monkeypatch.setattr(cli.shutil, "which", lambda name: fake if name == "ffprobe" else None)
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == "/api/flows"
            return httpx.Response(
                200,
                json=[{"id": 42, "name": "preview", "version": 1, "definition": definition}],
            )
        assert request.url.path == "/api/flows/dry-run"
        body = json.loads(request.content)
        assert body["probe"]["file_path"] == str(media)
        assert body["probe"]["size_bytes"] == 9
        assert "file_id" not in body
        assert body["sidecar_subtitles"] == []
        assert body["flow"] == definition
        assert body["sidecar_metadata"]["sources"]["info_json"]["title"] == "Local sidecar"
        assert "not-forwarded" not in json.dumps(body)
        return httpx.Response(
            200,
            json={
                "status": "matched",
                "source": {"kind": "local", "path": str(media)},
                "steps": [{"node": "size", "outcome": "deferred"}],
            },
        )

    argv = ["flows", "dry-run", "--flow", "pReViEw", str(media)]
    if as_json:
        argv.append("--json")
    assert run_cli(monkeypatch, argv, handler) == 0
    if as_json:
        result = json.loads(capsys.readouterr().out)
        assert result["source"]["kind"] == "local"
        assert result["steps"][0]["outcome"] == "deferred"
    assert seen == [("GET", "/api/flows"), ("POST", "/api/flows/dry-run")]
    assert media.read_bytes() == b"untouched"


def test_local_missing_file(monkeypatch, tmp_path):
    def handler(request):
        pytest.fail("missing local file must not send a request")

    assert run_cli(monkeypatch, ["flows", "dry-run", "--flow", "preview", str(tmp_path / "missing.mkv")], handler) == 1


def test_local_missing_flow(monkeypatch, tmp_path):
    media = tmp_path / "unindexed.mkv"
    media.write_bytes(b"untouched")

    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/flows"
        return httpx.Response(200, json=[])

    assert run_cli(monkeypatch, ["flows", "dry-run", "--flow", "missing", str(media)], handler) == 1


@pytest.mark.parametrize(("selector", "expected"), [("Preview", 1), ("preview", 2), ("1", 1), ("3", 3), ("STRASSE", 4)])
def test_flow_selector_precedence(selector, expected):
    rows = [
        {"id": 1, "name": "Preview"},
        {"id": 2, "name": "preview"},
        {"id": 3, "name": "1"},
        {"id": 4, "name": "Straße"},
    ]
    assert cli._resolve_flow(rows, selector)["id"] == expected


def test_ambiguous_flow_name(monkeypatch, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"untouched")

    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/flows"
        return httpx.Response(200, json=[{"id": 1, "name": "Preview"}, {"id": 2, "name": "preview"}])

    assert run_cli(monkeypatch, ["flows", "dry-run", "--flow", "PREVIEW", str(media)], handler) == 1
