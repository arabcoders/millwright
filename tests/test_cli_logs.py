import json

import httpx
import pytest

from server import cli
from tests.test_cli import run_cli


def log_page(ids, *, latest_id=None, truncated=False):
    return {
        "entries": [
            {
                "id": id_,
                "timestamp": "2026-10-08T20:07:45Z",
                "level": "error",
                "logger": "uvicorn.error",
                "message": f"Message {id_}",
                "fields": {"request_id": "request-1"},
            }
            for id_ in ids
        ],
        "latest_id": max(ids, default=0) if latest_id is None else latest_id,
        "oldest_id": min(ids, default=None),
        "next_before": None,
        "truncated": truncated,
    }


@pytest.mark.parametrize("cursor", ["before", "after"])
@pytest.mark.parametrize("short", [False, True])
def test_logs_filters(monkeypatch, cursor, short):
    payload = log_page([3])

    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/api/system":
            assert request.headers["authorization"] == "Bearer fixture-key"
            return httpx.Response(200, json={"timezone": "Asia/Kuwait"})
        assert request.url.path == "/api/logs"
        assert request.headers["authorization"] == "Bearer fixture-key"
        assert dict(request.url.params) == {
            "limit": "20",
            "levels": "warning,error",
            "logger": "uvicorn.error",
            "search": "request-1",
            cursor: "4",
        }
        return httpx.Response(200, json=payload)

    flags = {
        "token": "-k",
        "levels": "-l",
        "logger": "-g",
        "search": "-s",
        "limit": "-n",
        "before": "-b",
        "after": "-a",
    }

    def option(name):
        return flags[name] if short else f"--{name}"

    assert (
        run_cli(
            monkeypatch,
            [
                option("token"),
                "fixture-key",
                "logs",
                option("levels"),
                "Warning, ERROR",
                option("logger"),
                "uvicorn.error",
                option("search"),
                "request-1",
                option("limit"),
                "20",
                option(cursor),
                "4",
            ],
            handler,
        )
        == 0
    )


def test_logs_json_envelope(monkeypatch, capsys):
    payload = log_page([3])
    assert run_cli(monkeypatch, ["logs", "--json"], lambda request: httpx.Response(200, json=payload)) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("as_json", [False, True])
def test_logs_follow_cursor(monkeypatch, capsys, as_json):
    pages = iter(
        [
            log_page([2], latest_id=3),
            log_page([4, 5], latest_id=9),
            log_page([7, 8], latest_id=9),
            log_page([], latest_id=10),
            log_page([11], latest_id=12),
        ]
    )
    cursors = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    def handler(request):
        if request.url.path == "/api/system":
            return httpx.Response(200, json={"timezone": "Asia/Kuwait"})
        assert request.url.path == "/api/logs"
        assert request.url.params["levels"] == "error"
        assert request.url.params["logger"] == "uvicorn.error"
        assert request.url.params["search"] == "request-1"
        cursors.append(request.url.params.get("after"))
        return httpx.Response(200, json=next(pages))

    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert (
        run_cli(
            monkeypatch,
            [
                "logs",
                "--follow",
                "--limit",
                "2",
                "--levels",
                "error",
                "--logger",
                "uvicorn.error",
                "--search",
                "request-1",
                *(["--json"] if as_json else []),
            ],
            handler,
        )
        == 0
    )
    assert cursors == [None, "3", "5", "8", "10"]
    assert sleeps == [1, 1, 1]
    if as_json:
        assert [json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()] == [2, 4, 5, 7, 8, 11]


def test_logs_resume_pages(monkeypatch):
    pages = iter([log_page([1, 2], latest_id=5, truncated=True), log_page([3, 4], latest_id=5), log_page([5])])
    cursors = []

    def handler(request):
        if request.url.path == "/api/system":
            return httpx.Response(200, json={"timezone": "Asia/Kuwait"})
        cursors.append(request.url.params["after"])
        return httpx.Response(200, json=next(pages))

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    assert run_cli(monkeypatch, ["logs", "--follow", "--after", "0", "--limit", "2"], handler) == 0
    assert cursors == ["0", "2", "4"]


@pytest.mark.parametrize("short", [False, True])
def test_logs_follow_timeout(monkeypatch, short):
    now = [100.0]
    requested = []
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        now[0] += seconds

    def handler(request):
        if request.url.path == "/api/system":
            return httpx.Response(200, json={"timezone": "Asia/Kuwait"})
        requested.append(request)
        return httpx.Response(200, json=log_page([1]))

    monkeypatch.setattr(cli.time, "sleep", sleep)
    args = ["logs", "-f", "-t", "0.5"] if short else ["logs", "--follow", "--timeout", "0.5"]
    assert run_cli(monkeypatch, args, handler) == 0
    assert len(requested) == 1
    assert now[0] == 100.5


@pytest.mark.parametrize("follow", [False, True])
def test_logs_api_failure(monkeypatch, follow):
    assert (
        run_cli(
            monkeypatch,
            ["logs", *(["--follow"] if follow else [])],
            lambda request: httpx.Response(403, json={"detail": "Not authorized"}),
        )
        == 1
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--limit", "0"],
        ["--limit", "501"],
        ["--before", "0"],
        ["--after", "-1"],
        ["--follow", "--before", "3"],
        ["--timeout", "1"],
    ],
)
def test_logs_invalid_options(monkeypatch, args):
    def handler(request):
        pytest.fail("Invalid options must not send a request")

    assert run_cli(monkeypatch, ["logs", *args], handler) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--levels", "verbose"],
        ["--levels", "error,"],
        ["--before", "3", "--after", "1"],
        ["--timeout", "0"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
    ],
)
def test_logs_parse_errors(args):
    with pytest.raises(SystemExit) as rejected:
        cli.main(["logs", *args])
    assert rejected.value.code == 2
