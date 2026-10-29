"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from . import db, registrar
from mail_providers import MailProviderError, get_provider_class

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        self._stop_reason = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        # 目标成功数：0 = 不限量（保持旧行为）；>0 时累计成功达标即自动停止
        self._target_count: int = 0

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._last_message = "auto-loop 启动"
            self._stop_reason = ""
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            # 目标成功数（0=不限量）
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._publish_state()
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            if self._stop_event.is_set():
                return {"ok": False, "error": "正在停止，请等待当前注册任务结束"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._publish_state()
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            if self._stop_event.is_set():
                return {"ok": False, "error": "正在停止，请等待当前注册任务结束"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._publish_state()
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "正在停止，等待当前注册任务完成；显示已停止前请勿切换代理或关闭程序"
        self._publish_state()
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            # Queue the initial snapshot before publishing this subscriber.
            # Otherwise a concurrent stop can arrive before an older snapshot.
            q.put_nowait({"kind": "state", "data": self._snapshot()})
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "stop_requested": self._state != AutoLoopState.STOPPED and self._stop_event.is_set(),
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._publish_state()

    def _publish_state(self):
        # Keep snapshot creation and delivery ordered across workers. A UI
        # statistics/read failure must never detach a live registration.
        with self._lock:
            try:
                self._broadcast("state", self._snapshot())
            except Exception:
                logger.warning("推送调度状态失败；后台仍会等待当前任务结束")

    def _proxy_for_worker(self, worker_id: int) -> str:
        """按 worker_id 从代理池里挑一个代理。空池时回退到 options.proxy。"""
        if self._proxy_pool:
            return self._proxy_pool[worker_id % len(self._proxy_pool)]
        return self._options.get("proxy", "") or ""

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if category == "local_state":
                self._stop_reason = "本地任务状态异常，已停止继续派发；请核对运行记录和注册结果后再开始"
                self._stop_event.set()
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
                and not self._stop_event.is_set()
            )
            if self._stop_event.is_set():
                self._last_message = (
                    f"正在停止，等待其余注册任务完成（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
                if self._stop_reason:
                    self._last_message = self._stop_reason + "；正在等待其余任务结束"

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            logger.info(f"已达目标 {self._target_count} 个成功，触发自动停止")
            self._publish_state()
            return

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"连续 {self._consecutive_network_fails} 次网络/环境错误，"
                    f"自动暂停（号已自动 release，请检查代理后点恢复）"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        workers = []
        try:
            for wid in range(self._concurrency):
                if self._stop_event.is_set():
                    break
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                self._stop_event.wait(1.0)
            self._workers = workers
        except Exception as e:
            self._stop_event.set()
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            # A failed worker launch must not detach workers already running.
            for t in workers:
                t.join()
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = self._stop_reason or (
                    f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            self._publish_state()

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        proxy = self._proxy_for_worker(worker_id)
        logger.info(f"[worker-{worker_id}] 启动 (proxy={proxy or '直连'})")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 目标数量闸门：已成功 + 在跑的（复用 _worker_status 当在跑数）≥ 目标 → 本 worker 退出
            # 不新增易泄漏的计数器；_worker_status 已在锁内正常维护，最大限度压低超额
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] 目标 {self._target_count} 已锁定，退出"
                    )
                    return

            # claim 下一个号。要不要走号池由 provider 的 pooled 决定，
            # 非池化的（CF 这类自己造地址的）用虚拟占位。
            mail_source = db.get_setting("mail_source", "outlook")
            try:
                pooled = get_provider_class(mail_source).pooled
            except MailProviderError as e:
                logger.error(f"[worker-{worker_id}] {e}，停止")
                self._set_message(str(e))
                return
            if pooled:
                account = db.claim_next(kind=mail_source)
            else:
                account = {
                    "email": f"{mail_source}_placeholder_"
                             f"{int(time.time())}_{worker_id}@placeholder.local",
                    "password": "", "client_id": "", "refresh_token": "",
                    "relay_url": "", "kind": mail_source,
                }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # 给这个 run 注入 worker 自己的代理
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy

            # 启一个 run
            completed_event = threading.Event()
            try:
                # Serialize dispatch with stop/pause. If the request arrived
                # during claim_next(), release the unused mailbox instead of
                # starting another registration after the UI acknowledged it.
                with self._lock:
                    stop_requested = self._stop_event.is_set()
                    pause_requested = self._pause_event.is_set()
                    if stop_requested or pause_requested:
                        if pooled:
                            db.release_unused(account["email"])
                        if stop_requested:
                            return
                        continue
                    run_id = registrar.start_registration(
                        account, run_options, completed_event=completed_event,
                    )
                    self._worker_status[worker_id] = {
                        "email": account["email"],
                        "run_id": run_id,
                        "proxy": proxy,
                        "started_at": time.time(),
                    }
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if pooled:
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            try:
                self._publish_state()
                self._broadcast("run_started", {
                    "worker_id": worker_id,
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                })
            finally:
                # Even a notification failure cannot abandon a dispatched run.
                ok, category = self._wait_run_finish(
                    run_id, completed_event=completed_event,
                )

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._record_finish(ok, category)
            self._publish_state()
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds", 3))
            if cool_down > 0:
                deadline = time.monotonic() + cool_down
                while True:
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.1, remaining))

    def _wait_run_finish(
        self, run_id: str, timeout: int = 1800, *,
        completed_event: Optional[threading.Event] = None,
    ) -> tuple[bool, str]:
        """Drain the dispatched run; a stop request only prevents new work.

        The registrar thread is independent of this worker. Neither stop nor a
        timeout cancels it. The completion event covers its whole lifetime,
        including setup and cleanup; the database supplies the outcome. An
        exited thread without a trustworthy outcome stops the batch for review.
        """
        deadline = time.monotonic() + timeout
        warned_timeout = False
        warned_read = False
        failed_final_reads = 0
        while True:
            # Sample completion before reading: a running row read just before
            # the registrar commits 'done' must not be treated as orphaned.
            finished = completed_event is None or completed_event.is_set()
            con = None
            row = None
            read_ok = False
            try:
                con = db._conn()
                cur = con.execute(
                    "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
                )
                row = cur.fetchone()
                read_ok = True
            except Exception:
                if not warned_read:
                    logger.warning("读取注册任务状态失败，继续等待；尚未确认当前任务结束")
                    warned_read = True
            finally:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        if not warned_read:
                            logger.warning("关闭任务状态连接失败，继续按实际任务状态等待")
                            warned_read = True
            if finished and row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")
            if completed_event is not None and finished:
                # A completed thread can no longer repair a missing/running
                # row. Retry transient reads briefly, but do not wait forever
                # or release this mailbox for an automatic duplicate attempt.
                failed_final_reads += not read_ok
                if read_ok or failed_final_reads >= 3:
                    logger.error("注册线程已结束，但无法确认最终结果；停止本批任务，请检查本地数据库和注册结果")
                    try:
                        db.finish_run(
                            run_id, "failed", "本地任务状态异常，注册结果需人工核对",
                            category="local_state",
                        )
                    except Exception:
                        logger.warning("无法保存任务异常状态，请检查本地数据库")
                    return False, "local_state"
            if not warned_timeout and time.monotonic() >= deadline:
                logger.warning("注册任务等待已超过 %ss，继续等待实际结果；请勿切换代理或重复启动", timeout)
                warned_timeout = True
            time.sleep(1)


# 全局单例
CONTROLLER = AutoLoopController()
