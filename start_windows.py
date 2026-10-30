"""Windows launcher: prepare an isolated environment, then serve the built UI.

Only the initial setup (or a changed dependency file) uses pip/the network.
This module deliberately does not import the application while checking setup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 13)
DEPENDENCY_CHECK = (
    "import curl_cffi.requests, requests, socks, fastapi, uvicorn, pydantic; "
    "assert int(pydantic.__version__.split('.')[0]) >= 2"
)


class SetupError(RuntimeError):
    """An actionable setup failure, without a traceback or credentials."""


def check_python() -> None:
    if sys.platform != "win32":
        raise SetupError("此启动器用于 Windows 10/11。其他系统请使用 start_webui.py。")
    if not MIN_PYTHON <= sys.version_info[:2] <= MAX_PYTHON or struct.calcsize("P") != 8:
        raise SetupError(
            "需要 64 位 Python 3.11、3.12 或 3.13。请从 "
            "https://www.python.org/downloads/windows/ 安装，勾选 Add Python to PATH。"
        )


def check_node() -> str:
    configured = os.environ.get("OPENAI_SENTINEL_NODE_PATH", "").strip()
    node = configured or shutil.which("node")
    if not node:
        raise SetupError(
            "未找到 Node.js。请从 https://nodejs.org/ 安装 Node.js LTS（推荐 22/24 x64），"
            "安装后关闭此窗口，再双击 start_windows.bat。注册流程运行时需要 Node.js。"
        )
    try:
        result = subprocess.run(
            [node, "-p", "process.versions.node"],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True,
        )
        version = result.stdout.strip()
        if int(version.split(".", 1)[0]) < 18:
            raise ValueError("old Node.js")
    except (OSError, subprocess.SubprocessError, ValueError):
        raise SetupError(
            "Node.js 无法运行或版本低于 18。请安装 https://nodejs.org/ 的 22/24 LTS x64，"
            "并检查 OPENAI_SENTINEL_NODE_PATH（如果设置过）。"
        ) from None
    os.environ["OPENAI_SENTINEL_NODE_PATH"] = str(node)
    return version


def check_files(root: Path = ROOT) -> Path:
    if not (root / "webui" / "static" / "index.html").is_file():
        raise SetupError("缺少已构建的前端文件。请完整解压 ZIP 后启动，不要直接在压缩包内运行。")
    if not (root / "openai_sentinel_quickjs.js").is_file():
        raise SetupError("缺少 openai_sentinel_quickjs.js，请重新完整解压安装包。")
    for name in ("requirements-windows.txt", "requirements.txt"):
        requirements = root / name
        if requirements.is_file():
            return requirements
    raise SetupError("缺少依赖清单 requirements.txt，请重新完整解压安装包。")


def dependency_fingerprint(requirements: Path) -> str:
    # Include both files if the Windows list references the shared list.
    digest = hashlib.sha256()
    for path in dict.fromkeys((requirements, requirements.parent / "requirements.txt")):
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    digest.update(f"{sys.version_info[:3]}:{sys.implementation.name}:64".encode("ascii"))
    return digest.hexdigest()


def dependencies_available() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-c", DEPENDENCY_CHECK],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_dependencies(requirements: Path, venv_dir: Path) -> None:
    marker = venv_dir / ".windows-dependencies.sha256"
    fingerprint = dependency_fingerprint(requirements)
    try:
        saved = marker.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        saved = ""
    if saved == fingerprint and dependencies_available():
        print("[2/3] Python 依赖已就绪（本次无需下载）。", flush=True)
        return

    print("[2/3] 首次安装或更新 Python 依赖，请保持联网并等待完成……", flush=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--prefer-binary", "-r", str(requirements)],
            cwd=ROOT, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise SetupError(
            "依赖安装失败。请检查网络和上方 pip 错误后重新启动；不会删除已有账号数据。"
        ) from None
    if not dependencies_available():
        raise SetupError("依赖安装后仍无法导入。请检查上方错误，或关闭程序后删除 .venv 再启动。")
    marker.write_text(fingerprint + "\n", encoding="ascii")


def health_ready(url: str) -> bool:
    # Localhost must never go through HTTP_PROXY/HTTPS_PROXY.
    try:
        with build_opener(ProxyHandler({})).open(url + "api/health", timeout=1) as response:
            body = json.loads(response.read(65536))
        return isinstance(body, dict) and body.get("ok") is True and isinstance(body.get("stats"), dict)
    except (OSError, URLError, ValueError):
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def open_browser(url: str) -> None:
    try:
        if not webbrowser.open(url):
            print(f"浏览器未自动打开，请手动访问：{url}", flush=True)
    except Exception:
        print(f"浏览器未自动打开，请手动访问：{url}", flush=True)


def serve(port: int, no_browser: bool) -> int:
    url = f"http://127.0.0.1:{port}/"
    if port_in_use(port):
        if health_ready(url):
            print(f"程序已在运行：{url}（本次不重复启动）", flush=True)
            if not no_browser:
                open_browser(url)
            return 0
        raise SetupError(
            f"端口 {port} 已被其他程序占用。请关闭占用程序，或使用 "
            f"start_windows.bat --port {port + 1 if port < 65535 else 8766}。"
        )

    environment = os.environ.copy()
    environment.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
        "AUTH_HTTP_TRACE": "0", "AUTH_TRACE_DUMP": "0",
    })
    command = [sys.executable, "-m", "uvicorn", "webui.app:app", "--host", "127.0.0.1",
               "--port", str(port), "--no-access-log"]
    print(f"[3/3] 正在启动：{url}\n停止程序请在此窗口按 Ctrl+C。", flush=True)
    process = subprocess.Popen(command, cwd=ROOT, env=environment)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                raise SetupError(f"服务启动失败（退出码 {code}），请查看上方错误。")
            if health_ready(url):
                print(f"服务已就绪：{url}", flush=True)
                if not no_browser:
                    open_browser(url)
                return process.wait()
            time.sleep(0.25)
        raise SetupError("服务在 45 秒内未就绪，请查看上方错误。")
    except KeyboardInterrupt:
        print("\n正在停止服务……", flush=True)
        # Ctrl+C is delivered to both processes in the same Windows console.
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pass
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 本地一键启动（首次联网安装 Python 依赖）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="检查/安装依赖后退出，不启动服务")
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 范围内")
    try:
        check_python()
        requirements = check_files()
        node_version = check_node()
        print(f"[1/3] Python {sys.version.split()[0]} x64 / Node.js {node_version}", flush=True)
        venv_dir = ROOT / ".venv"
        venv_python = venv_dir / "Scripts" / "python.exe"
        if Path(sys.prefix).resolve() != venv_dir.resolve():
            if not venv_python.is_file():
                print("正在创建独立 Python 环境 .venv……", flush=True)
                try:
                    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
                except (OSError, subprocess.SubprocessError):
                    raise SetupError("无法创建 .venv。请确认安装了完整 Python，并将 ZIP 解压到可写目录。") from None
            try:
                probe = subprocess.run(
                    [str(venv_python), "-c", "import struct,sys; sys.exit(0 if "
                     "(3,11) <= sys.version_info[:2] <= (3,13) and struct.calcsize('P') == 8 else 1)"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                )
                if probe.returncode:
                    raise OSError("virtual environment is unusable")
            except (OSError, subprocess.SubprocessError):
                raise SetupError(
                    "现有 .venv 环境不可用。关闭程序后只删除项目中的 .venv 文件夹，再重新启动；"
                    "账号数据保存在 webui/webui.db，不要删除该数据库。"
                ) from None
            return subprocess.call([str(venv_python), str(Path(__file__).resolve()), *arguments])
        ensure_dependencies(requirements, venv_dir)
        if args.check_only:
            print("运行环境检查完成。", flush=True)
            return 0
        return serve(args.port, args.no_browser)
    except SetupError as error:
        print(f"\n[错误] {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
        return 0
    except OSError:
        print("\n[错误] 无法读写或启动程序。请完整解压到可写目录，并检查安装环境。", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
