"""Offline launcher checks; these do not claim to execute Windows binaries."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

import pytest

import start_windows as launcher


def package_files(tmp_path: Path) -> Path:
    (tmp_path / "webui" / "static").mkdir(parents=True)
    (tmp_path / "webui" / "static" / "index.html").write_text("<html></html>")
    (tmp_path / "openai_sentinel_quickjs.js").write_text("// bundled runtime adapter")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.32.5\n")
    return requirements


@pytest.mark.parametrize("version", [(3, 11), (3, 12), (3, 13)])
def test_python_accepts_supported_x64(version):
    with patch.object(launcher.sys, "platform", "win32"), \
            patch.object(launcher.sys, "version_info", version), \
            patch.object(launcher.struct, "calcsize", return_value=8):
        launcher.check_python()


@pytest.mark.parametrize("platform,version,bits", [
    ("darwin", (3, 13), 8), ("win32", (3, 10), 8),
    ("win32", (3, 14), 8), ("win32", (3, 12), 4),
])
def test_python_rejects_unsupported_runtime(platform, version, bits):
    with patch.object(launcher.sys, "platform", platform), \
            patch.object(launcher.sys, "version_info", version), \
            patch.object(launcher.struct, "calcsize", return_value=bits), \
            pytest.raises(launcher.SetupError):
        launcher.check_python()


def test_node_is_required_without_running_installers():
    with patch.dict(launcher.os.environ, {}, clear=True), \
            patch.object(launcher.shutil, "which", return_value=None), \
            patch.object(launcher.subprocess, "run") as run, \
            pytest.raises(launcher.SetupError, match="Node.js"):
        launcher.check_node()
    run.assert_not_called()


def test_node_custom_path_with_spaces_is_one_argument():
    node_path = r"C:\Program Files\nodejs\node.exe"
    with patch.dict(launcher.os.environ, {"OPENAI_SENTINEL_NODE_PATH": node_path}, clear=True), \
            patch.object(launcher.subprocess, "run", return_value=Mock(stdout="24.0.0\n")) as run:
        assert launcher.check_node() == "24.0.0"
        assert run.call_args.args[0] == [node_path, "-p", "process.versions.node"]


@pytest.mark.parametrize("version", ["16.0.0", "not a version"])
def test_node_invalid_version_is_actionable(version):
    with patch.dict(launcher.os.environ, {"OPENAI_SENTINEL_NODE_PATH": "node"}, clear=True), \
            patch.object(launcher.subprocess, "run", return_value=Mock(stdout=version)), \
            pytest.raises(launcher.SetupError, match="Node.js"):
        launcher.check_node()


def test_package_requires_built_frontend_and_js(tmp_path):
    with pytest.raises(launcher.SetupError, match="前端"):
        launcher.check_files(tmp_path)
    requirements = package_files(tmp_path)
    assert launcher.check_files(tmp_path) == requirements
    windows = tmp_path / "requirements-windows.txt"
    windows.write_text("-r requirements.txt\n")
    assert launcher.check_files(tmp_path) == windows


def test_dependency_fingerprint_changes_for_shared_and_windows_lists(tmp_path):
    shared = package_files(tmp_path)
    windows = tmp_path / "requirements-windows.txt"
    windows.write_text("-r requirements.txt\n")
    first = launcher.dependency_fingerprint(windows)
    shared.write_text("requests==2.32.6\n")
    second = launcher.dependency_fingerprint(windows)
    windows.write_text("-r requirements.txt\n# second revision\n")
    assert len({first, second, launcher.dependency_fingerprint(windows)}) == 3


def test_unchanged_healthy_environment_does_not_run_pip(tmp_path):
    requirements = package_files(tmp_path)
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    (venv_dir / ".windows-dependencies.sha256").write_text(launcher.dependency_fingerprint(requirements))
    with patch.object(launcher, "dependencies_available", return_value=True), \
            patch.object(launcher.subprocess, "run") as run:
        launcher.ensure_dependencies(requirements, venv_dir)
    run.assert_not_called()


def test_failed_install_does_not_mark_environment_ready(tmp_path):
    requirements = package_files(tmp_path)
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    with patch.object(launcher.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pip")), \
            pytest.raises(launcher.SetupError, match="安装失败"):
        launcher.ensure_dependencies(requirements, venv_dir)
    assert not (venv_dir / ".windows-dependencies.sha256").exists()


def test_successful_install_stamps_only_after_import_check(tmp_path):
    requirements = package_files(tmp_path)
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    with patch.object(launcher.subprocess, "run") as run, \
            patch.object(launcher, "dependencies_available", return_value=True):
        launcher.ensure_dependencies(requirements, venv_dir)
    assert "-r" in run.call_args.args[0]
    assert str(requirements) in run.call_args.args[0]
    assert (venv_dir / ".windows-dependencies.sha256").read_text().strip() == launcher.dependency_fingerprint(requirements)


def test_invalid_successful_import_does_not_stamp(tmp_path):
    requirements = package_files(tmp_path)
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    with patch.object(launcher.subprocess, "run"), \
            patch.object(launcher, "dependencies_available", return_value=False), \
            pytest.raises(launcher.SetupError, match="无法导入"):
        launcher.ensure_dependencies(requirements, venv_dir)
    assert not (venv_dir / ".windows-dependencies.sha256").exists()


@pytest.mark.parametrize("body,ready", [
    ({"ok": True, "stats": {}}, True), ({"ok": True}, False),
    ({"ok": False, "stats": {}}, False), (["ok"], False),
])
def test_health_uses_bounded_read_and_ignores_proxy_environment(body, ready):
    response = Mock()
    response.read.return_value = json.dumps(body).encode()
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    with patch.object(launcher, "build_opener", return_value=opener), \
            patch.object(launcher, "ProxyHandler") as handler:
        assert launcher.health_ready("http://127.0.0.1:8765/") is ready
    handler.assert_called_once_with({})
    response.read.assert_called_once_with(65536)


def test_existing_instance_opens_without_starting_second_server():
    with patch.object(launcher, "port_in_use", return_value=True), \
            patch.object(launcher, "health_ready", return_value=True), \
            patch.object(launcher, "open_browser") as browser, \
            patch.object(launcher.subprocess, "Popen") as popen:
        assert launcher.serve(8765, False) == 0
    popen.assert_not_called()
    browser.assert_called_once_with("http://127.0.0.1:8765/")


def test_foreign_port_conflict_never_opens_browser():
    with patch.object(launcher, "port_in_use", return_value=True), \
            patch.object(launcher, "health_ready", return_value=False), \
            patch.object(launcher, "open_browser") as browser, \
            pytest.raises(launcher.SetupError, match="端口"):
        launcher.serve(8765, False)
    browser.assert_not_called()


def test_browser_opens_after_ready_and_service_is_local_only():
    process = Mock()
    process.poll.side_effect = [None, 0]
    process.wait.return_value = 0
    events = []
    with patch.object(launcher, "port_in_use", return_value=False), \
            patch.object(launcher, "health_ready", side_effect=lambda url: events.append("ready") or True), \
            patch.object(launcher, "open_browser", side_effect=lambda url: events.append("browser")), \
            patch.object(launcher.subprocess, "Popen", return_value=process) as popen:
        assert launcher.serve(8765, False) == 0
    assert events == ["ready", "browser"]
    command = popen.call_args.args[0]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert popen.call_args.kwargs["env"]["AUTH_TRACE_DUMP"] == "0"


def test_failed_start_does_not_open_browser():
    process = Mock()
    process.poll.return_value = 1
    with patch.object(launcher, "port_in_use", return_value=False), \
            patch.object(launcher, "open_browser") as browser, \
            patch.object(launcher.subprocess, "Popen", return_value=process), \
            pytest.raises(launcher.SetupError, match="退出码"):
        launcher.serve(8765, False)
    browser.assert_not_called()
    process.terminate.assert_not_called()


def test_broken_moved_venv_fails_with_recovery_instructions(tmp_path, capsys):
    package_files(tmp_path)
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"placeholder")
    with patch.object(launcher, "ROOT", tmp_path), \
            patch.object(launcher, "check_python"), \
            patch.object(launcher, "check_files", return_value=tmp_path / "requirements.txt"), \
            patch.object(launcher, "check_node", return_value="24.0.0"), \
            patch.object(launcher.subprocess, "run", return_value=Mock(returncode=1)), \
            patch.object(launcher.subprocess, "call") as call:
        assert launcher.main(["--check-only"]) == 1
    call.assert_not_called()
    assert "只删除项目中的 .venv" in capsys.readouterr().err


def test_venv_reentry_preserves_actual_arguments(tmp_path):
    package_files(tmp_path)
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"placeholder")
    with patch.object(launcher, "ROOT", tmp_path), \
            patch.object(launcher, "check_python"), \
            patch.object(launcher, "check_files", return_value=tmp_path / "requirements.txt"), \
            patch.object(launcher, "check_node", return_value="24.0.0"), \
            patch.object(launcher.subprocess, "run", return_value=Mock(returncode=0)), \
            patch.object(launcher.subprocess, "call", return_value=0) as call:
        assert launcher.main(["--port", "8766", "--no-browser"]) == 0
    assert call.call_args.args[0][-3:] == ["--port", "8766", "--no-browser"]
