"""Full-result persistence and exports, using only disposable SQLite databases."""
from __future__ import annotations

import base64
import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def ui(tmp_path, monkeypatch):
    database = tmp_path / "registration-json-test.db"
    connect = sqlite3.connect
    # First import initializes db.py; redirect even that call away from real accounts.
    with monkeypatch.context() as first_import:
        first_import.setattr(
            sqlite3, "connect",
            lambda _path, *args, **kwargs: connect(str(database), *args, **kwargs),
        )
        db = importlib.import_module("webui.db")
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()
    app = importlib.import_module("webui.app")
    with TestClient(app.app) as client:
        yield SimpleNamespace(db=db, client=client, database=database)


def captured_result(email="snapshot@example.invalid", token="offline-at"):
    return {
        "schema_version": 1,
        "program": "gpt-auto-register",
        "mode": "protocol",
        "capture_complete": True,
        "snapshot_scope": "registration_time",
        "run_id": "offline-run-1",
        "captured_at": "2026-10-23T00:00:00Z",
        "flags": {"refresh_token_requested": False, "two_factor_requested": True},
        "credentials": {
            "email": email, "password": "offline-password", "access_token": token,
            "session_token": "offline-st", "refresh_token": "", "id_token": "",
            "device_id": "offline-device", "csrf_token": "offline-csrf",
            "cookie_header": "session=offline-st; device=offline-device",
            "totp_secret": "JBSWY3DPEHPK3PXP", "totp_factor_id": "offline-factor",
        },
        "cookies": {"session": "offline-st"},
        "events": [{"step": "account", "status": 200, "note": "离线测试事件"}],
        "totp_result": {"enabled": True, "factor_id": "offline-factor"},
        "custom_future_field": {"preserve": [1, None, False]},
    }


def save_snapshot(ui, result=None):
    result = result if result is not None else captured_result()
    ui.db.save_registered({
        **result["credentials"], "registration_result": result,
        "custom_metadata": {"label": "current metadata"},
    })
    return result


def export(ui, **selection):
    response = ui.client.post("/api/registered/export", json={
        "format": "registration_json", **selection,
    })
    assert response.status_code == 200
    body = response.json()
    raw = base64.b64decode(body["b64"])
    assert body["mime"] == "application/json"
    assert body["mode"] == "download"
    assert body["size"] == len(raw)
    return body, json.loads(raw)


def test_complete_result_roundtrip_is_independent_of_metadata(ui):
    expected = save_snapshot(ui)
    email = expected["credentials"]["email"]
    result = ui.db.get_registration_result(f" {email.upper()} ")
    assert result == expected
    result["events"].clear()
    assert ui.db.get_registration_result(email) == expected
    with sqlite3.connect(ui.database) as con:
        raw, extra = con.execute(
            "SELECT registration_json, extra_json FROM registered WHERE email=?", (email,),
        ).fetchone()
    assert json.loads(raw) == expected
    assert "registration_result" not in json.loads(extra)
    assert "registration_json" not in json.loads(extra)
    assert json.loads(extra)["custom_metadata"]["label"] == "current metadata"


def test_large_snapshot_stays_out_of_ordinary_interfaces(ui):
    result = captured_result()
    result["events"].append({"private_marker": "snapshot-only-marker"})
    save_snapshot(ui, result)
    email = result["credentials"]["email"]
    for value in (
        ui.db.get_registered(email), ui.db.list_registered_full(),
        ui.db.list_registered_by_emails([email]),
        ui.client.get(f"/api/registered/{email}").json(),
    ):
        encoded = json.dumps(value)
        assert "registration_json" not in encoded
        assert "registration_result" not in encoded
        assert "snapshot-only-marker" not in encoded
    listed = ui.client.get("/api/registered").json()["items"][0]
    assert listed["has_registration_json"] is True
    assert "access_token" not in listed
    assert "snapshot-only-marker" not in json.dumps(listed)


@pytest.mark.parametrize("old_totp_columns", [False, True])
def test_legacy_schema_migrates_idempotently_without_changing_credentials(ui, old_totp_columns):
    totp_columns = ", totp_secret TEXT, totp_factor_id TEXT" if old_totp_columns else ""
    with sqlite3.connect(ui.database) as con:
        con.execute("DROP TABLE registered")
        con.execute(
            "CREATE TABLE registered (email TEXT PRIMARY KEY, password TEXT, access_token TEXT, "
            "session_token TEXT, refresh_token TEXT, id_token TEXT, device_id TEXT, "
            "csrf_token TEXT, cookie_header TEXT, extra_json TEXT, created_at REAL"
            + totp_columns + ")"
        )
        con.execute("INSERT INTO registered(email,password,access_token,created_at) VALUES(?,?,?,?)",
                    ("legacy@example.invalid", "legacy-password", "legacy-at", 123.0))
    ui.db.init_db()
    ui.db.init_db()
    with sqlite3.connect(ui.database) as con:
        columns = [row[1] for row in con.execute("PRAGMA table_info(registered)")]
    assert columns.count("registration_json") == 1
    result = ui.db.get_registration_result("legacy@example.invalid")
    assert result["source"] == "legacy_database"
    assert result["capture_complete"] is False
    assert "无法还原" in result["capture_note"]
    assert result["credentials"]["access_token"] == "legacy-at"
    assert result["credentials"]["password"] == "legacy-password"
    assert result["events"] == []
    assert result["cookies"] == {}
    assert "totp_result" not in result
    assert "run_id" not in result
    assert ui.db.list_registered()[0]["has_registration_json"] is False


def test_legacy_reconstruction_keeps_all_saved_credential_fields(ui):
    expected = captured_result()["credentials"]
    ui.db.save_registered(expected)
    result = ui.db.get_registration_result(expected["email"])
    assert result["credentials"] == expected
    assert result["capture_complete"] is False


@pytest.mark.parametrize("corrupt", ["broken-json", "[]", "null"])
def test_invalid_stored_snapshot_falls_back_without_fabricating_success(ui, corrupt):
    result = save_snapshot(ui)
    email = result["credentials"]["email"]
    with sqlite3.connect(ui.database) as con:
        con.execute("UPDATE registered SET registration_json=? WHERE email=?", (corrupt, email))
    fallback = ui.db.get_registration_result(email)
    assert fallback["source"] == "legacy_database"
    assert fallback["capture_complete"] is False
    assert fallback["credentials"] == result["credentials"]
    assert "totp_result" not in fallback


def test_snapshot_api_returns_exact_object_or_404(ui):
    result = save_snapshot(ui)
    email = result["credentials"]["email"]
    response = ui.client.get(f"/api/registered/{email}/registration-json")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": result}
    assert ui.client.get("/api/registered/unknown@example.invalid/registration-json").status_code == 404
    assert ui.db.get_registration_result("unknown@example.invalid") is None


def test_single_snapshot_download_is_object_with_original_credentials(ui):
    result = save_snapshot(ui)
    email = result["credentials"]["email"]
    body, output = export(ui, emails=[email])
    assert body["count"] == 1
    assert body["emails"] == [email]
    assert body["legacy_count"] == 0
    assert output == result


def test_selected_and_all_exports_match_exact_accounts_and_report_legacy(ui):
    first = save_snapshot(ui)
    second = save_snapshot(ui, captured_result("two@example.invalid", "second-at"))
    legacy = captured_result("legacy@example.invalid", "legacy-at")["credentials"]
    ui.db.save_registered(legacy)
    emails = [first["credentials"]["email"], second["credentials"]["email"]]
    body, selected = export(ui, emails=[*emails, emails[0].upper()])
    assert body["count"] == 2
    assert body["legacy_count"] == 0
    assert {r["credentials"]["email"] for r in selected} == set(emails)
    assert {r["credentials"]["access_token"] for r in selected} == {"offline-at", "second-at"}
    body, all_results = export(ui, all=True, emails=["ignored@example.invalid"])
    assert body["count"] == 3
    assert body["legacy_count"] == 1
    assert [r["credentials"]["email"] for r in all_results] == body["emails"]
    assert {r["credentials"]["email"] for r in all_results} == {*emails, legacy["email"]}


def test_capture_incomplete_new_result_is_not_counted_as_legacy(ui):
    result = captured_result()
    result["capture_complete"] = False
    result["capture_note"] = "事件记录已达到容量限制"
    save_snapshot(ui, result)
    body, output = export(ui, all=True)
    assert body["legacy_count"] == 0
    assert output == result


def test_no_accounts_export_is_empty_array(ui):
    body, output = export(ui, all=True)
    assert body["count"] == 0
    assert body["legacy_count"] == 0
    assert output == []


def test_manual_updates_and_checks_do_not_rewrite_registration_evidence(ui):
    result = save_snapshot(ui)
    email = result["credentials"]["email"]
    assert ui.db.update_registered_manual(email, password="updated-password", totp_secret="")
    ui.db.record_pool_push(email, {"ok": True, "status": "added", "message": "离线测试"})
    ui.db.update_plus_check(email, {"status": "free"})
    assert ui.db.get_registration_result(email) == result
    assert ui.db.get_registered(email)["password"] == "updated-password"
    old_export = ui.client.post("/api/registered/export", json={"format": "email_pw", "emails": [email]})
    assert old_export.json()["text"] == email + "----updated-password"


def test_resave_without_capture_keeps_old_snapshot_separate_from_current_tokens(ui):
    old = save_snapshot(ui)
    email = old["credentials"]["email"]
    ui.db.save_registered({**old["credentials"], "access_token": "new-current-at"})
    retained = ui.db.get_registration_result(email)
    assert retained["credentials"] == old["credentials"]
    assert retained["run_id"] == old["run_id"]
    assert retained["captured_at"] == old["captured_at"]
    assert retained["export_metadata"]["snapshot_retained_from_earlier_run"] is True
    assert ui.db.get_registered(email)["access_token"] == "new-current-at"
    replacement = captured_result(email, "new-captured-at")
    replacement["run_id"] = "offline-run-2"
    replacement["captured_at"] = "2026-10-24T00:00:00Z"
    save_snapshot(ui, replacement)
    assert ui.db.get_registration_result(email) == replacement
    assert "registration_snapshot_retained" not in ui.db.get_registered(email)["extra"]


@pytest.mark.parametrize("invalid", [[], "wrong type", 4])
def test_invalid_capture_cannot_erase_existing_record(ui, invalid):
    original = save_snapshot(ui)
    with pytest.raises(ValueError, match="JSON 对象"):
        ui.db.save_registered({**original["credentials"], "registration_result": invalid})
    assert ui.db.get_registration_result(original["credentials"]["email"]) == original


def test_format_catalog_and_existing_deletion_still_work(ui):
    formats = ui.client.get("/api/registered/export/formats").json()["formats"]
    assert {f["id"] for f in formats} >= {"registration_json", "at", "email_pw", "email_pw_2fa"}
    result = save_snapshot(ui)
    email = result["credentials"]["email"]
    response = ui.client.delete(f"/api/registered/{email}")
    assert response.status_code == 200
    assert ui.db.get_registration_result(email) is None
