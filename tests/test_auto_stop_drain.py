"""Offline stop/drain behavior with fake storage and event-controlled workers.

The controller is loaded in an isolated package with fake db/registrar modules;
collecting or running this file never imports or opens the operator's webui.db.
"""
from __future__ import annotations

import ast
import importlib.util
import logging
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def isolated_loop(monkeypatch):
    package_name = "_offline_auto_stop_tests"
    package = ModuleType(package_name)
    package.__path__ = []
    db = ModuleType(package_name + ".db")
    db.stats = Mock(return_value={"available": 1})
    db.get_setting = Mock(side_effect=lambda _key, default="": default)
    db.claim_next = Mock(return_value=None)
    db.release_unused = Mock()
    db._conn = Mock(side_effect=AssertionError("Test must provide fake storage"))
    registrar = ModuleType(package_name + ".registrar")
    registrar.start_registration = Mock(side_effect=AssertionError("Live registration forbidden"))
    package.db, package.registrar = db, registrar
    for name, module in ((package_name, package), (db.__name__, db), (registrar.__name__, registrar)):
        monkeypatch.setitem(sys.modules, name, module)
    # Catch any accidental storage import added to the controller in the future.
    monkeypatch.setattr(sqlite3, "connect", Mock(side_effect=AssertionError("Real SQLite forbidden")))
    spec = importlib.util.spec_from_file_location(
        package_name + ".auto_loop", Path(__file__).resolve().parents[1] / "webui" / "auto_loop.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_provider_class", lambda _source: SimpleNamespace(pooled=True))
    # Replace this module's time binding, not process-wide time/threading behavior.
    module.time = SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda _seconds: None)
    return SimpleNamespace(module=module, db=db, registrar=registrar, controller=module.AutoLoopController())


class Connection:
    def __init__(self, row, *, read_error=False, close_error=False, closed=None):
        self.row = row
        self.read_error = read_error
        self.close_error = close_error
        self.closed = False
        self.on_close = closed

    def execute(self, statement, parameters):
        assert statement.startswith("SELECT status, error_category FROM runs")
        assert parameters == ("offline-run",)
        if self.read_error:
            raise RuntimeError("private-read-detail")
        return self

    def fetchone(self):
        return self.row() if callable(self.row) else self.row

    def close(self):
        self.closed = True
        if self.on_close:
            self.on_close()
        if self.close_error:
            raise RuntimeError("private-close-detail")


def row(status="running", category=""):
    return {"status": status, "error_category": category}


@pytest.mark.parametrize("terminal,category,expected", [
    ("done", "", (True, "")),
    ("failed", "network", (False, "network")),
])
def test_stop_waits_for_terminal_result_and_closes_each_connection(isolated_loop, terminal, category, expected):
    ctx = isolated_loop
    connections = [Connection(row()), Connection(row(terminal, category))]
    ctx.db._conn = Mock(side_effect=connections)
    ctx.controller._stop_event.set()
    assert ctx.controller._wait_run_finish("offline-run") == expected
    assert ctx.db._conn.call_count == 2
    assert all(connection.closed for connection in connections)


def test_timeout_keeps_waiting_instead_of_finishing_a_live_run(isolated_loop, caplog):
    ctx = isolated_loop
    connections = [Connection(row()), Connection(None), Connection(row()), Connection(row("done"))]
    ctx.db._conn = Mock(side_effect=connections)
    ctx.module.time.monotonic = lambda: 10.0
    assert ctx.controller._wait_run_finish("offline-run", timeout=0) == (True, "")
    assert ctx.db._conn.call_count == 4
    assert all(connection.closed for connection in connections)
    assert caplog.text.count("等待已超过") == 1
    assert "继续等待实际结果" in caplog.text


@pytest.mark.parametrize("failure", ["read", "close"])
def test_storage_error_does_not_detach_unfinished_run_or_leak_details(isolated_loop, failure, caplog):
    ctx = isolated_loop
    first = Connection(row(), read_error=failure == "read", close_error=failure == "close")
    final = Connection(row("done"))
    ctx.db._conn = Mock(side_effect=[first, final])
    assert ctx.controller._wait_run_finish("offline-run") == (True, "")
    assert first.closed and final.closed
    assert "private-read-detail" not in caplog.text
    assert "private-close-detail" not in caplog.text


def test_stop_blocks_restart_until_old_run_finishes_without_false_failure(isolated_loop):
    ctx = isolated_loop
    controller = ctx.controller
    status = {"terminal": False}
    polls = queue.Queue()
    continue_poll = queue.Queue()
    connections = []
    events = []
    completion = {}
    original_broadcast = controller._broadcast

    def broadcast(kind, data):
        events.append((kind, dict(data)))
        original_broadcast(kind, data)

    def connect():
        current = row("done" if status["terminal"] else "running")
        connection = Connection(current, closed=lambda: polls.put(current["status"]))
        connections.append(connection)
        return connection

    def wait_for_test(_seconds):
        continue_poll.get(timeout=3)

    ctx.db._conn = Mock(side_effect=connect)
    ctx.db.claim_next = Mock(return_value={"email": "offline@example.invalid"})
    def start_registration(_account, _options, *, completed_event=None):
        completion["event"] = completed_event
        return "offline-run"

    ctx.registrar.start_registration = Mock(side_effect=start_registration)
    ctx.module.time.sleep = wait_for_test
    controller._broadcast = broadcast
    try:
        assert controller.start({"concurrency": 1, "proxy": "http://127.0.0.1:7890"})["ok"] is True
        assert polls.get(timeout=3) == "running"
        assert controller.stop()["ok"] is True
        # Permit another poll after stop. The old implementation exited here.
        continue_poll.put(None)
        assert polls.get(timeout=3) == "running"
        assert controller.status()["state"] != "stopped"
        assert controller.status()["stop_requested"] is True
        assert controller.start({"concurrency": 1})["ok"] is False
        assert controller.pause()["ok"] is False
        assert controller.resume()["ok"] is False
        assert not any(kind == "run_finished" for kind, _data in events)
        status["terminal"] = True
        completion["event"].set()
        continue_poll.put(None)
        controller._manage_thread.join(timeout=3)
        assert not controller._manage_thread.is_alive()
        finished = [data for kind, data in events if kind == "run_finished"]
        assert len(finished) == 1 and finished[0]["ok"] is True
        snapshot = controller.status()
        assert snapshot["state"] == "stopped"
        assert snapshot["stop_requested"] is False
        assert snapshot["registered_ok"] == 1
        assert snapshot["registered_fail"] == 0
        ctx.registrar.start_registration.assert_called_once()
        ctx.db.claim_next.assert_called_once()
        assert all(connection.closed for connection in connections)
    finally:
        controller._stop_event.set()
        status["terminal"] = True
        if completion.get("event") is not None:
            completion["event"].set()
        for _ in range(4):
            continue_poll.put(None)
        if controller._manage_thread is not None:
            controller._manage_thread.join(timeout=3)


@pytest.mark.parametrize("action", ["stop", "pause"])
def test_stop_or_pause_during_claim_releases_mailbox_without_dispatch(isolated_loop, action):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING

    def claim(**_kwargs):
        assert getattr(controller, action)()["ok"] is True
        return {"email": "offline@example.invalid"}

    ctx.db.claim_next = Mock(side_effect=claim)
    if action == "pause":
        # End the controlled paused worker after observing the release.
        def stop_paused_worker(_seconds):
            ctx.db.release_unused.assert_called_once_with("offline@example.invalid")
            controller.stop()
        ctx.module.time.sleep = stop_paused_worker
    controller._worker_loop(0)
    ctx.db.release_unused.assert_called_once_with("offline@example.invalid")
    ctx.registrar.start_registration.assert_not_called()
    ctx.db._conn.assert_not_called()
    assert controller.status()["registered_fail"] == 0


def test_stop_after_run_completed_records_success_without_claiming_again(isolated_loop):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING
    ctx.db.claim_next = Mock(return_value={"email": "offline@example.invalid"})
    connection = Connection(row("done"))
    ctx.db._conn = Mock(return_value=connection)

    def finished_registration(_account, _options, *, completed_event=None):
        completed_event.set()
        controller.stop()
        return "offline-run"

    ctx.registrar.start_registration = Mock(side_effect=finished_registration)
    controller._worker_loop(0)
    assert controller.status()["registered_ok"] == 1
    assert controller.status()["registered_fail"] == 0
    ctx.db.claim_next.assert_called_once()
    ctx.registrar.start_registration.assert_called_once()
    assert connection.closed


def test_manager_failure_still_waits_for_already_started_worker(isolated_loop):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING
    controller._concurrency = 2
    worker_started = threading.Event()
    release_worker = threading.Event()
    joined_worker = threading.Event()
    creation_attempts = []
    original_thread = threading.Thread

    def worker(_worker_id):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        joined_worker.set()

    def make_thread(**kwargs):
        creation_attempts.append(kwargs)
        if len(creation_attempts) == 2:
            raise RuntimeError("injected worker creation failure")
        return original_thread(**kwargs)

    # Avoid the normal one-second worker startup interval while preserving stop.
    class StopEvent:
        def __init__(self):
            self.inner = threading.Event()
        def is_set(self):
            return self.inner.is_set()
        def set(self):
            self.inner.set()
        def wait(self, _timeout):
            return self.inner.is_set()

    controller._stop_event = StopEvent()
    controller._worker_loop = worker
    ctx.module.threading = SimpleNamespace(Thread=make_thread)
    manager = original_thread(target=controller._manage_loop, daemon=True)
    try:
        manager.start()
        assert worker_started.wait(timeout=3)
        # The second worker launch failure requests stop, but the first still runs.
        assert controller._stop_event.inner.wait(timeout=3)
        assert controller.status()["state"] != "stopped"
        assert not joined_worker.is_set()
        release_worker.set()
        manager.join(timeout=3)
        assert not manager.is_alive()
        assert joined_worker.is_set()
        assert controller.status()["state"] == "stopped"
    finally:
        release_worker.set()
        manager.join(timeout=3)


@pytest.mark.parametrize("terminal,expected", [
    ("done", (True, "")),
    ("failed", (False, "network")),
])
def test_terminal_database_state_still_waits_for_thread_completion(isolated_loop, terminal, expected):
    ctx = isolated_loop
    completed = threading.Event()
    connections = []

    def connect():
        connection = Connection(row(terminal, "network"))
        connections.append(connection)
        return connection

    def finish_thread(_seconds):
        assert not completed.is_set()
        completed.set()

    ctx.db._conn = Mock(side_effect=connect)
    ctx.module.time.sleep = finish_thread
    assert ctx.controller._wait_run_finish("offline-run", completed_event=completed) == expected
    assert completed.is_set()
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("stored", [row(), None])
def test_thread_exit_without_terminal_database_state_is_local_failure(isolated_loop, stored):
    ctx = isolated_loop
    completed = threading.Event()
    completed.set()
    connection = Connection(stored)
    ctx.db._conn = Mock(return_value=connection)
    assert ctx.controller._wait_run_finish("offline-run", completed_event=completed) == (False, "local_state")
    assert connection.closed


def test_completed_thread_with_persistent_read_errors_does_not_wait_forever(isolated_loop, caplog):
    ctx = isolated_loop
    completed = threading.Event()
    completed.set()
    connections = [Connection(None, read_error=True) for _ in range(3)]
    ctx.db._conn = Mock(side_effect=connections)
    assert ctx.controller._wait_run_finish("offline-run", completed_event=completed) == (False, "local_state")
    assert ctx.db._conn.call_count == 3
    assert all(connection.closed for connection in connections)
    assert "private-read-detail" not in caplog.text


def test_completed_thread_tolerates_two_transient_read_errors(isolated_loop):
    ctx = isolated_loop
    completed = threading.Event()
    completed.set()
    connections = [Connection(None, read_error=True), Connection(None, read_error=True), Connection(row("done"))]
    ctx.db._conn = Mock(side_effect=connections)
    assert ctx.controller._wait_run_finish("offline-run", completed_event=completed) == (True, "")
    assert all(connection.closed for connection in connections)


def test_completion_during_database_read_does_not_misclassify_stale_running_row(isolated_loop):
    ctx = isolated_loop
    completed = threading.Event()

    def finish_after_read():
        completed.set()
        return row()

    connections = [Connection(finish_after_read), Connection(row("done"))]
    ctx.db._conn = Mock(side_effect=connections)
    assert ctx.controller._wait_run_finish("offline-run", completed_event=completed) == (True, "")
    assert all(connection.closed for connection in connections)


def test_read_errors_cannot_end_an_active_registration(isolated_loop):
    ctx = isolated_loop
    completed = threading.Event()
    connections = [Connection(None, read_error=True) for _ in range(4)] + [Connection(row("done"))]
    sleeps = []

    def finish_later(_seconds):
        sleeps.append(True)
        if len(sleeps) == 4:
            completed.set()

    ctx.module.time.sleep = finish_later
    ctx.db._conn = Mock(side_effect=connections)
    assert ctx.controller._wait_run_finish("offline-run", completed_event=completed) == (True, "")
    assert len(sleeps) == 4
    assert all(connection.closed for connection in connections)


def test_local_state_failure_stops_batch_without_releasing_or_dispatching_again(isolated_loop):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING
    ctx.db.claim_next = Mock(return_value={"email": "offline@example.invalid"})
    ctx.db._conn = Mock(return_value=Connection(row()))

    def abandoned_registration(_account, _options, *, completed_event=None):
        completed_event.set()
        return "offline-run"

    ctx.registrar.start_registration = Mock(side_effect=abandoned_registration)
    controller._worker_loop(0)
    assert controller._stop_event.is_set()
    assert controller.status()["registered_fail"] == 1
    ctx.registrar.start_registration.assert_called_once()
    ctx.db.claim_next.assert_called_once()
    ctx.db.release_unused.assert_not_called()


def test_post_dispatch_snapshot_error_cannot_detach_live_registration(isolated_loop):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING
    polls = queue.Queue()
    continue_poll = queue.Queue()
    completion = {}
    worker_errors = []
    ctx.db.claim_next = Mock(return_value={"email": "offline@example.invalid"})
    # Fail only the first snapshot after dispatch; future status is available.
    ctx.db.stats = Mock(side_effect=[RuntimeError("simulated stats failure")] + [{}] * 30)

    def start_registration(_account, _options, *, completed_event=None):
        completion["event"] = completed_event
        return "offline-run"

    def connect():
        return Connection(row("done"), closed=lambda: polls.put(True))

    def wait_for_test(_seconds):
        continue_poll.get(timeout=3)

    def run_worker():
        try:
            controller._worker_loop(0)
        except Exception as error:
            worker_errors.append(error)

    ctx.registrar.start_registration = Mock(side_effect=start_registration)
    ctx.db._conn = Mock(side_effect=connect)
    ctx.module.time.sleep = wait_for_test
    worker = threading.Thread(target=run_worker, daemon=True)
    try:
        worker.start()
        assert polls.get(timeout=3)
        assert worker.is_alive()
        assert not completion["event"].is_set()
        assert controller.stop()["ok"] is True
        assert controller.start({"concurrency": 1})["ok"] is False
        completion["event"].set()
        continue_poll.put(None)
        worker.join(timeout=3)
        assert not worker.is_alive()
        ctx.registrar.start_registration.assert_called_once()
        ctx.db.release_unused.assert_not_called()
    finally:
        controller._stop_event.set()
        if completion.get("event") is not None:
            completion["event"].set()
        for _ in range(4):
            continue_poll.put(None)
        worker.join(timeout=3)


@pytest.mark.parametrize("cool_down", [0, 0.05, 120, 300])
def test_success_keeps_requested_cooldown_before_next_claim(isolated_loop, cool_down):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING
    controller._options = {"cool_down_seconds": cool_down}
    slept = []
    ctx.db._conn = Mock(return_value=Connection(row("done")))

    def claim(**_kwargs):
        if ctx.db.claim_next.call_count == 1:
            return {"email": "offline@example.invalid"}
        assert sum(slept) == pytest.approx(cool_down)
        controller.stop()
        return None

    def finished_registration(_account, _options, *, completed_event=None):
        completed_event.set()
        return "offline-run"

    def virtual_sleep(seconds):
        assert seconds > 0
        ctx.registrar.start_registration.assert_called_once()
        ctx.db.claim_next.assert_called_once()
        slept.append(seconds)

    ctx.db.claim_next = Mock(side_effect=claim)
    ctx.registrar.start_registration = Mock(side_effect=finished_registration)
    ctx.module.time.sleep = virtual_sleep
    ctx.module.time.monotonic = lambda: sum(slept)
    controller._worker_loop(0)
    assert sum(slept) == pytest.approx(cool_down)
    assert ctx.db.claim_next.call_count == 2
    assert controller.status()["registered_ok"] == 1
    ctx.db.release_unused.assert_not_called()


def test_stop_while_idle_finishes_without_starting_a_registration(isolated_loop):
    ctx = isolated_loop
    controller = ctx.controller
    controller._state = ctx.module.AutoLoopState.RUNNING
    controller._concurrency = 1
    idle = threading.Event()
    continue_idle = threading.Event()
    ctx.db.claim_next = Mock(return_value=None)

    def wait_while_idle(_seconds):
        idle.set()
        assert continue_idle.wait(timeout=3)

    ctx.module.time.sleep = wait_while_idle
    manager = threading.Thread(target=controller._manage_loop, daemon=True)
    try:
        manager.start()
        assert idle.wait(timeout=3)
        assert controller.stop()["ok"] is True
        continue_idle.set()
        manager.join(timeout=3)
        assert not manager.is_alive()
        assert controller.status()["state"] == "stopped"
        ctx.registrar.start_registration.assert_not_called()
        ctx.db.release_unused.assert_not_called()
    finally:
        controller._stop_event.set()
        continue_idle.set()
        manager.join(timeout=3)


def load_registration_starter(db, do_register, monkeypatch, tmp_path):
    """Load the real launcher alone, without importing DB or auth dependencies."""
    source = Path(__file__).resolve().parents[1] / "webui" / "registrar.py"
    parsed = ast.parse(source.read_text(encoding="utf-8"))
    functions = [node for node in parsed.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"start_registration", "_run_registration_task"}]
    threads = []

    def make_thread(**kwargs):
        thread = threading.Thread(**kwargs)
        threads.append(thread)
        return thread

    namespace = {
        "db": db, "_do_register": do_register, "LOG_DIR": tmp_path,
        "uuid": uuid, "queue": queue, "_lock": threading.Lock(), "_run_queues": {},
        "threading": SimpleNamespace(Thread=make_thread, Event=threading.Event),
        "logging": logging, "Path": Path, "QueueLogHandler": type("OfflineHandler", (logging.Handler,), {}),
        "_current_run": threading.local(), "_emit_status": Mock(),
    }
    # Deferred annotations mirror registrar.py while avoiding dependency imports.
    isolated_ast = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    ast.fix_missing_locations(isolated_ast)
    exec(compile(isolated_ast, str(source), "exec"), namespace)
    # Expected thread failures are inspected below, not emitted as pytest warnings.
    failures = []
    monkeypatch.setattr(threading, "excepthook", failures.append)
    return SimpleNamespace(start=namespace["start_registration"], threads=threads, failures=failures)


@pytest.mark.parametrize("raises,persist_fails", [(False, False), (True, False), (True, True)])
def test_registrar_launcher_signals_completion_only_after_target_exits(isolated_loop, monkeypatch, tmp_path, raises, persist_fails):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    ctx = isolated_loop
    ctx.db.create_run = Mock()
    ctx.db.finish_run = Mock(side_effect=OSError("simulated database write failure") if persist_fails else None)

    def register(*_args):
        started.set()
        assert release.wait(timeout=3)
        if raises:
            raise OSError("simulated setup failure")

    launcher = load_registration_starter(ctx.db, register, monkeypatch, tmp_path)
    try:
        run_id = launcher.start({"email": "offline@example.invalid"}, {}, completed_event=completed)
        assert isinstance(run_id, str) and run_id
        assert started.wait(timeout=3)
        assert not completed.is_set()
        release.set()
        assert completed.wait(timeout=3)
        for thread in launcher.threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        ctx.db.create_run.assert_called_once()
        if raises:
            assert ctx.db.finish_run.call_args.kwargs["category"] == "local_state"
        else:
            ctx.db.finish_run.assert_not_called()
        assert not launcher.failures
    finally:
        release.set()
        for thread in launcher.threads:
            thread.join(timeout=3)


def test_single_registration_caller_can_still_omit_completion_event(isolated_loop, monkeypatch, tmp_path):
    ctx = isolated_loop
    ctx.db.create_run = Mock()
    ctx.db.finish_run = Mock()
    registered = threading.Event()
    launcher = load_registration_starter(ctx.db, lambda *_args: registered.set(), monkeypatch, tmp_path)
    try:
        run_id = launcher.start({"email": "offline@example.invalid"}, {})
        assert isinstance(run_id, str) and run_id
        assert registered.wait(timeout=3)
    finally:
        for thread in launcher.threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
    assert not launcher.failures
