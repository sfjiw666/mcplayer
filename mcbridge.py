# AstrBot Minecraft 玩家插件 - Node 桥接端管理器
#
# 职责：
#   1. 以子进程方式启动/停止 node_bot/bot.js
#   2. 首次运行时自动 npm install
#   3. 通过 stdin/stdout 的 JSON 行协议与 Node 端通信
#   4. 把 Node 推送的事件（chat/death/kicked/...）分发给插件

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from typing import Any, Awaitable, Callable

from astrbot.api import logger


class BridgeError(Exception):
    """桥接端调用失败。"""


class BridgeGone(BridgeError):
    """Node 子进程已退出。"""


class McBridge:
    """管理 Node.js 桥接子进程并提供 RPC。"""

    def __init__(
        self,
        node_bot_dir: str,
        node_executable: str = "node",
        auto_install: bool = True,
        log_file: str | None = None,
    ):
        self.node_bot_dir = node_bot_dir
        self.node_executable = node_executable
        self.auto_install = auto_install
        self.log_file = log_file

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()  # 串行化 spawn / request 启动路径
        self._event_handlers: list[Callable[[str, dict], Awaitable[None]]] = []
        self._log_fh = None

    # ---------- 事件订阅 ----------

    def on_event(self, handler: Callable[[str, dict], Awaitable[None]]):
        self._event_handlers.append(handler)

    async def _dispatch_event(self, name: str, data: dict):
        # 在后台任务中执行处理器，避免慢处理阻塞桥接读循环
        for handler in self._event_handlers:
            task = asyncio.create_task(self._safe_call(handler, name, data or {}))
            # 持引用防止任务被 GC
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    _background_tasks: set = set()

    async def _safe_call(self, handler, name: str, data: dict):
        try:
            await handler(name, data)
        except Exception as e:
            logger.error(f"[mcbridge] 事件处理器异常 ({name}): {e}")

    # ---------- 进程管理 ----------

    def _find_node(self) -> str:
        node = self.node_executable or "node"
        path = shutil.which(node)
        if not path and os.path.isfile(node):
            path = node
        if not path:
            raise BridgeGone(
                "找不到 node 可执行文件。请先安装 Node.js (>=16) 并确保在 PATH 中，"
                "或在插件配置中指定完整路径。"
            )
        return path

    def _deps_installed(self) -> bool:
        return os.path.isdir(os.path.join(self.node_bot_dir, "node_modules", "mineflayer"))

    async def install_deps(self):
        """在 node_bot 目录执行 npm install。"""
        npm = shutil.which("npm")
        if not npm:
            raise BridgeGone(
                "找不到 npm。请先安装 Node.js/npm，或手动在插件目录 node_bot 下执行 npm install。"
            )
        cmd = [npm, "install", "--no-audit", "--no-fund"]
        if sys.platform == "win32" and npm.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c", *cmd]

        logger.info("[mcbridge] 正在安装 Node 依赖（首次运行需要几分钟）...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.node_bot_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            raise BridgeError("npm install 超时（15 分钟），请检查网络后重试。")
        text = out.decode("utf-8", errors="replace")[-2000:]
        if proc.returncode != 0:
            raise BridgeError(f"npm install 失败:\n{text}")
        logger.info("[mcbridge] Node 依赖安装完成")

    async def start(self):
        """启动桥接进程（如果已在运行则跳过）。"""
        async with self._lock:
            if self._proc and self._proc.returncode is None:
                return

            node_path = self._find_node()
            if not self._deps_installed():
                if not self.auto_install:
                    raise BridgeError(
                        "Node 依赖未安装且已关闭自动安装。请手动在插件 node_bot 目录执行 npm install。"
                    )
                await self.install_deps()

            script = os.path.join(self.node_bot_dir, "bot.js")
            if not os.path.isfile(script):
                raise BridgeError(f"找不到桥接脚本: {script}")

            if self.log_file:
                os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
                self._log_fh = open(self.log_file, "a", encoding="utf-8")

            self._proc = await asyncio.create_subprocess_exec(
                node_path,
                script,
                cwd=self.node_bot_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._log_fh if self._log_fh else asyncio.subprocess.STDOUT,
                limit=64 * 1024 * 1024,  # 俯视地图 png 走 base64，放宽管道行长度
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            logger.info(f"[mcbridge] 桥接进程已启动 (pid={self._proc.pid})")

        # 注意：就绪检查必须在锁外做（request_raw 内部会请求 _lock），且用
        # request_raw 而非 request（后者带失败重试逻辑，避免 start -> ping 失败 -> start 递归）
        try:
            await self.request_raw("ping", timeout=15)
        except Exception as e:
            await self.stop()
            raise BridgeError(f"桥接进程启动失败: {e}")

    async def stop(self):
        proc = self._proc
        self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        self._fail_pending(BridgeGone("桥接端已停止"))
        if proc and proc.returncode is None:
            # 直接写 stdin 请求优雅退出（不能用 request_raw：此时 _proc 已置空会被跳过）
            try:
                proc.stdin.write(b'{"cmd": "shutdown"}\n')
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        logger.info("[mcbridge] 桥接进程已停止")

    def is_running(self) -> bool:
        return bool(self._proc and self._proc.returncode is None)

    def _fail_pending(self, exc: Exception):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ---------- 读写循环 ----------

    async def _reader_loop(self):
        proc = self._proc
        assert proc and proc.stdout
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"[mcbridge] 收到非 JSON 行: {text[:200]}")
                    continue

                if "id" in msg and ("ok" in msg):
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if msg.get("ok"):
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(BridgeError(msg.get("error") or "未知错误"))
                elif "event" in msg:
                    await self._dispatch_event(msg["event"], msg.get("data") or {})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[mcbridge] 读取循环异常: {e}")
        finally:
            self._fail_pending(BridgeGone("桥接进程已退出"))
            if self._proc is not None:
                # 只有非主动 stop 的退出才值得告警
                logger.warning("[mcbridge] 桥接进程 stdout 已关闭（进程可能退出）")

    # ---------- RPC ----------

    async def request_raw(self, cmd: str, args: dict | None = None, timeout: float = 90) -> Any:
        """发送一条命令；若进程未启动会自动拉起（shutdown 除外）。返回 data 部分。"""
        if not self.is_running():
            if cmd == "shutdown":
                return None
            await self.start()

        async with self._lock:
            if not self.is_running():
                raise BridgeGone("桥接进程未运行")
            self._next_id += 1
            rid = self._next_id
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            payload = json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n"
            try:
                self._proc.stdin.write(payload.encode("utf-8"))
                await self._proc.stdin.drain()
            except Exception as e:
                self._pending.pop(rid, None)
                raise BridgeGone(f"发送失败（桥接进程可能已退出）: {e}")

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise BridgeError(f"命令 {cmd} 超时（{timeout:.0f}s）")

    async def request(self, cmd: str, args: dict | None = None, timeout: float = 90) -> Any:
        try:
            return await self.request_raw(cmd, args, timeout)
        except BridgeGone:
            # 进程意外退出，尝试重启一次
            logger.warning("[mcbridge] 桥接进程不可用，尝试重新拉起...")
            await self.stop()
            return await self.request_raw(cmd, args, timeout)
