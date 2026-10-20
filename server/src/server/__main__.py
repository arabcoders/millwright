from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import logging
import os
import platform
import signal
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

import uvicorn
from common.secrets import resolve_secret
from uvicorn.lifespan.on import LifespanOn

from server.api.app import create_app
from server.core.config import ensure_config, load_config
from server.core.db import (
    check_db_revision,
    create_engine_and_session,
    prepare_db,
)
from server.core.logging import configure_access_logging, setup_logging
from server.core.paths import config_file

if TYPE_CHECKING:
    from collections.abc import Sequence
    from multiprocessing.process import BaseProcess
    from socket import socket
    from types import FrameType

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

LOG = logging.getLogger("server")

REPO_ROOT = Path(__file__).resolve().parents[3]


async def _serve(app: FastAPI, engine: AsyncEngine, *, host: str, port: int) -> None:
    app.state.shutdown_requested = asyncio.Event()

    class DrainedLifespan(LifespanOn):
        driver_started = False
        task: asyncio.Task[None] | None = None

        async def startup(self) -> None:
            self.driver_started = True
            await super().startup()

        async def main(self) -> None:
            self.task = asyncio.current_task()
            await super().main()

    class DrainedServer(uvicorn.Server):
        startup_task: asyncio.Task[BaseException | None] | None = None
        shutdown_task: asyncio.Task[None] | None = None

        async def startup(self, sockets: list[socket] | None = None) -> None:
            async def start() -> BaseException | None:
                try:
                    await super(DrainedServer, self).startup(sockets=sockets)
                except BaseException as exc:
                    # Uvicorn's startup SystemExit must reach the serve caller, not terminate the task's loop.
                    return exc
                return None

            # Wait for listener creation to finish even if serve is cancelled.
            self.startup_task = asyncio.create_task(start())
            error = await asyncio.shield(self.startup_task)
            if error is not None:
                raise error

        async def shutdown(self, sockets: list[socket] | None = None) -> None:
            # Request streams must finish before Uvicorn waits for task draining.
            app.state.shutdown_requested.set()
            if self.shutdown_task is None:
                self.shutdown_task = asyncio.create_task(super().shutdown(sockets=sockets))
            await asyncio.shield(self.shutdown_task)

    server: DrainedServer | None = None
    error: BaseException | None = None
    try:
        # Uvicorn's SansIO backend misreports completed HTTP denials as incomplete handshakes.
        config = uvicorn.Config(
            app, host=host, port=port, ws="wsproto", log_level="info", log_config=None, access_log=False
        )
        config.load()
        config.lifespan_class = DrainedLifespan
        server = DrainedServer(config)
        # Startup can fail after acquiring listeners but before setting started.
        server.servers = []
        await server.serve()
    except BaseException as exc:
        error = exc
    finally:

        async def cleanup() -> None:
            app.state.shutdown_requested.set()
            shutdown_error: BaseException | None = None
            if server is not None:
                server.should_exit = True
                if server.startup_task is not None:
                    shutdown_error = await server.startup_task
            lifespan = getattr(server, "lifespan", None)
            if server is not None and isinstance(lifespan, DrainedLifespan) and lifespan.driver_started:
                await lifespan.startup_event.wait()
                try:
                    if server.shutdown_task is not None:
                        await server.shutdown_task
                    elif not lifespan.shutdown_event.is_set():
                        await server.shutdown()
                except BaseException as exc:
                    if shutdown_error is None:
                        shutdown_error = exc
                finally:
                    try:
                        if shutdown_error is not None or server.force_exit:
                            for listener in server.servers:
                                listener.close()
                            for connection in tuple(server.server_state.connections):
                                try:
                                    connection.shutdown()
                                except BaseException as exc:
                                    if shutdown_error is None:
                                        shutdown_error = exc
                        tasks = tuple(server.server_state.tasks)
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                        for listener in server.servers:
                            await listener.wait_closed()
                    finally:
                        # A failed/forced network shutdown must not abandon application cleanup.
                        try:
                            if not lifespan.shutdown_event.is_set():
                                await lifespan.shutdown()
                        except BaseException as exc:
                            if shutdown_error is None:
                                shutdown_error = exc
                        finally:
                            if lifespan.task is not None:
                                await lifespan.task
            async with asyncio.timeout(5):
                await engine.dispose()
            if shutdown_error is not None:
                raise shutdown_error

        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            cleanup_task.result()
        except BaseException as exc:
            if error is None:
                raise
            LOG.error("server cleanup failed", exc_info=exc)
        if error is not None:
            raise error
        if cancelled:
            raise asyncio.CancelledError


def _run_dev_server(
    config_path: Path | None,
    database_url: str | None,
    host: str,
    port: int,
    no_auto_migration: bool,
) -> None:
    if os.name == "posix":
        # Watchfiles forwards SIGINT. Receiving the terminal signal too would force Uvicorn to skip lifespan cleanup.
        os.setsid()
    start(
        config_path=config_path,
        database_url=database_url,
        host=host,
        port=port,
        no_auto_migration=no_auto_migration,
    )


def start(
    config_path: Path | None = None,
    database_url: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    no_auto_migration: bool = False,
) -> None:
    faulthandler.enable(all_threads=True)
    path = config_path if config_path is not None else config_file()
    ensure_config(path)
    config = load_config(path)
    setup_logging(timezone=config.tz)
    LOG.info(
        "Server process %s starting with Python %s and PostgreSQL.",
        os.getpid(),
        platform.python_version(),
    )
    configure_access_logging(enabled=config.access_log)
    LOG.info("config %s", path)

    database_url = resolve_secret(database_url) if database_url is not None else config.database_url
    if no_auto_migration:
        check_db_revision(database_url)
    engine, sessionmaker = create_engine_and_session(database_url)
    try:
        if not no_auto_migration:
            prepare_db(engine)
        app = create_app(sessionmaker, config=config, config_path=path)
    except BaseException:

        async def dispose() -> None:
            async with asyncio.timeout(5):
                await engine.dispose()

        try:
            asyncio.run(dispose())
        except BaseException as exc:
            LOG.error("engine cleanup failed before serving", exc_info=exc)
        raise
    # Dispose the engine in Uvicorn's event loop after the app lifespan.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(app, engine, host=host, port=port))


def _stop_dev_process(process: BaseProcess) -> None:
    pid = process.pid
    if pid is None:
        return
    if process.is_alive():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
    # Do not force-kill a child while its filesystem workers or recovery tasks are running.
    process.join()
    process.close()


def run_dev(
    *, config_path: Path | None, database_url: str | None, host: str, port: int, no_auto_migration: bool = False
) -> None:
    from multiprocessing import get_context

    from watchfiles import PythonFilter, watch

    faulthandler.enable(all_threads=True)
    path = config_path if config_path is not None else config_file()
    ensure_config(path)
    setup_logging(timezone=load_config(path).tz)
    stopping = Event()

    def stop_requested(_signum: int, _frame: FrameType | None) -> None:
        stopping.set()

    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    context = get_context("spawn")
    args = (config_path, database_url, host, port, no_auto_migration)
    process: BaseProcess | None = context.Process(target=_run_dev_server, args=args)
    try:
        for sig in handlers:
            signal.signal(sig, stop_requested)
        process.start()
        for changes in watch(
            REPO_ROOT,
            stop_event=stopping,
            raise_interrupt=False,
            yield_on_timeout=True,
            rust_timeout=1000,
            watch_filter=PythonFilter(
                ignore_paths=(
                    REPO_ROOT / "tests",
                    REPO_ROOT / "ui",
                    REPO_ROOT / "docs",
                    REPO_ROOT / "plugins",
                    REPO_ROOT / "server" / "src" / "server" / "migrations",
                )
            ),
        ):
            if stopping.is_set():
                break
            assert process is not None
            exitcode = process.exitcode
            if exitcode is not None:
                if exitcode < 0:
                    LOG.error(
                        "Server child %s exited unexpectedly with signal %s (%s).",
                        process.pid,
                        signal.Signals(-exitcode).name,
                        -exitcode,
                    )
                else:
                    LOG.error("Server child %s exited unexpectedly with code %s.", process.pid, exitcode)
                raise SystemExit(128 - exitcode if exitcode < 0 else exitcode or 1)
            if not changes:
                continue
            LOG.info("Reloading server after Python file changes.")
            _stop_dev_process(process)
            process = None
            if stopping.is_set():
                break
            process = context.Process(target=_run_dev_server, args=args)
            process.start()
    finally:
        try:
            if process is not None:
                _stop_dev_process(process)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start the transcode server", allow_abbrev=False)
    parser.add_argument("--dev", action="store_true", help="Reload the server when Python files change")
    parser.add_argument(
        "--no-auto-migration", action="store_true", help="Refuse startup unless the database migration is current"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Config file or directory (default: $MW_CONFIG_PATH/config.toml or ~/.config/millwright/config.toml)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL or file:path (default: MW_DATABASE_URL or database_url in server config)",
    )
    parser.add_argument("--host", default=os.environ.get("MW_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("MW_PORT", "8000"))
    args, _ = parser.parse_known_args(argv)

    config_path = config_file(args.config)
    ensure_config(config_path)

    if args.dev:
        run_dev(
            config_path=config_path,
            database_url=args.database_url,
            host=args.host,
            port=args.port,
            no_auto_migration=args.no_auto_migration,
        )
    else:
        start(
            config_path=config_path,
            database_url=args.database_url,
            host=args.host,
            port=args.port,
            no_auto_migration=args.no_auto_migration,
        )


if __name__ == "__main__":
    main()
