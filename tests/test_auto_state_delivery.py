"""Offline SSE ordering checks; no real registrar, database, or network access."""
from __future__ import annotations

import threading

import pytest

from test_auto_stop_drain import isolated_loop


@pytest.mark.parametrize("publication", ["subscribe", "publish"])
def test_snapshot_delivery_stays_ordered_with_concurrent_stop(isolated_loop, publication):
    controller = isolated_loop.controller
    controller._state = "running"
    output = {}
    errors = []
    snapshot_ready = threading.Event()
    release_snapshot = threading.Event()
    stop_attempted = threading.Event()
    original_snapshot = controller._snapshot

    if publication == "publish":
        output["queue"] = controller.subscribe()
        output["queue"].get_nowait()  # Discard the pre-existing subscription snapshot.

    def delayed_snapshot():
        result = original_snapshot()
        if threading.current_thread().name == "offline-state-publisher":
            snapshot_ready.set()
            if not release_snapshot.wait(3):
                raise AssertionError("Test did not release the captured snapshot")
        return result

    controller._snapshot = delayed_snapshot

    def publish():
        try:
            if publication == "subscribe":
                output["queue"] = controller.subscribe()
            else:
                controller._publish_state()
        except BaseException as exc:
            errors.append(exc)

    def stop():
        try:
            stop_attempted.set()
            output["stop"] = controller.stop()
        except BaseException as exc:
            errors.append(exc)

    publisher = threading.Thread(target=publish, name="offline-state-publisher")
    stopper = threading.Thread(target=stop, name="offline-state-stopper")
    publisher.start()
    stopper_started = False
    try:
        assert snapshot_ready.wait(3)
        # The snapshot has been copied but not enqueued. Stop must not be able
        # to publish a newer state during this window and then be overwritten.
        acquired = controller._lock.acquire(blocking=False)
        if acquired:
            controller._lock.release()
        assert not acquired, "Snapshot delivery released the ordering lock too early"
        stopper.start()
        stopper_started = True
        assert stop_attempted.wait(3)
    finally:
        release_snapshot.set()
        publisher.join(3)
        if stopper_started:
            stopper.join(3)

    assert not publisher.is_alive()
    assert not stopper.is_alive()
    assert not errors
    assert output["stop"] == {"ok": True}
    q = output["queue"]
    states = []
    while not q.empty():
        event = q.get_nowait()
        assert event["kind"] == "state"
        states.append((event["data"]["state"], event["data"]["stop_requested"]))
    assert states == [("running", False), ("running", True)]


def test_failed_state_publication_preserves_stop_and_can_recover(isolated_loop, caplog):
    controller = isolated_loop.controller
    controller._state = "running"
    q = controller.subscribe()
    q.get_nowait()
    isolated_loop.db.stats.side_effect = RuntimeError("private-storage-detail")
    assert controller.stop() == {"ok": True}
    assert controller._stop_event.is_set()
    assert controller._state == "running"
    assert q.empty()
    assert "private-storage-detail" not in caplog.text
    assert "推送调度状态失败" in caplog.text

    isolated_loop.db.stats.side_effect = None
    controller._publish_state()
    recovered = q.get_nowait()["data"]
    assert recovered["state"] == "running"
    assert recovered["stop_requested"] is True
