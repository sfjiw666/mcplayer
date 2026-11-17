# AstrBot Minecraft 玩家插件 - 游戏画面截屏
#
# 方案(按优先级自动降级)：
#   1. Playwright(AstrBot 环境若带) 打开渲染页截图
#   2. 系统 Chrome/Edge 的 headless CLI 截图(不依赖任何 Python 库)
#   3. Node 端俯视地图(main.py 兜底调用)
# 说明：不使用 prismarine-viewer 的 headless 模式，因为其依赖 node-canvas-webgl
#       原生编译，在 Windows/常见服务器上极易失败；浏览器渲染成功率高得多。

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import struct
import tempfile
import time
import zlib

from astrbot.api import logger


class RenderError(Exception):
    pass


# ---------------- 截图有效性检测(纯色防御) ----------------

def _png_color_diversity(path: str, sample_rows: int = 14) -> int | None:
    """用纯标准库解码 PNG 并统计量化颜色种类。

    返回采样到的(量化到 32 级/通道的)颜色种数；解析不了返回 None。
    天空色清屏等"纯色块"截图颜色种数极少，真实地形则有几十上百种。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        width = height = 0
        bit_depth = color_type = 0
        idat = b""
        while pos + 8 <= len(data):
            (length,) = struct.unpack(">I", data[pos : pos + 4])
            ctype = data[pos + 4 : pos + 8].decode("ascii", "replace")
            chunk = data[pos + 8 : pos + 8 + length]
            pos += 12 + length
            if ctype == "IHDR":
                width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
            elif ctype == "IDAT":
                idat += chunk
            elif ctype == "IEND":
                break
        if bit_depth != 8 or color_type not in (2, 6) or width <= 0 or height <= 0:
            return None  # 非常见格式不做判断
        channels = 4 if color_type == 6 else 3
        stride = width * channels
        raw = zlib.decompress(idat)
        if len(raw) < (stride + 1) * height:
            return None
        prev = bytearray(stride)
        colors: set[tuple[int, int, int]] = set()
        step = max(1, height // sample_rows)
        xstep = max(1, width // 24)
        for y in range(height):
            base = y * (stride + 1)
            ftype = raw[base]
            line = bytearray(raw[base + 1 : base + 1 + stride])
            if ftype == 1:  # Sub
                for i in range(channels, stride):
                    line[i] = (line[i] + line[i - channels]) & 0xFF
            elif ftype == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ftype == 3:  # Average
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif ftype == 4:  # Paeth
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i - channels] if i >= channels else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            prev = line
            if y % step == 0:
                for x in range(0, width, xstep):
                    o = x * channels
                    colors.add((line[o] >> 3, line[o + 1] >> 3, line[o + 2] >> 3))
        return len(colors)
    except Exception:
        return None


def assert_screenshot_renders(path: str) -> None:
    """截图内容必须是"真的画面"而不是纯色块，否则视为失败走兜底。"""
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        raise RenderError("截屏文件异常地小，渲染可能失败")
    diversity = _png_color_diversity(path)
    if diversity is not None and diversity <= 3:
        raise RenderError("截屏内容为纯色块(渲染未生效)，已判为无效")


class GameScreenCaptor:
    """管理一个 Playwright Chromium 实例，对 prismarine-viewer 页面截屏。"""

    LAUNCH_ARGS = [
        "--enable-unsafe-swiftshader",  # 允许无 GPU 环境用 SwiftShader 跑 WebGL
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]

    def __init__(self, width: int = 1280, height: int = 720, render_wait_ms: int = 8000):
        self.width = width
        self.height = height
        self.render_wait_ms = render_wait_ms

        self._playwright = None
        self._browser = None
        self._launch_lock = asyncio.Lock()

    async def _ensure_browser(self):
        async with self._launch_lock:
            if self._browser and self._browser.is_connected():
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise RenderError(
                    "未找到 playwright 库（AstrBot 本体应自带）。请检查 AstrBot 安装是否完整。"
                ) from e
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True, args=self.LAUNCH_ARGS
                )
                logger.info("[mcrender] Chromium 已启动（用于游戏截屏）")
            except Exception as e:
                self._playwright = None
                self._browser = None
                msg = (
                    "启动 Chromium 失败。如果提示缺少浏览器，请在 AstrBot 环境中执行: "
                    f"playwright install chromium。原始错误: {e}"
                )
                raise RenderError(msg) from e

    async def close(self):
        for closer in (self._browser, self._playwright):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self._browser = None
        self._playwright = None

    async def screenshot_viewer(self, viewer_url: str, out_path: str) -> str:
        """打开渲染页面并截屏保存到 out_path，返回 out_path。

        优先 Playwright；库缺失或浏览器不可用时自动降级为系统浏览器 CLI 截图。
        """
        try:
            return await self._screenshot_playwright(viewer_url, out_path)
        except RenderError as e:
            logger.warning(f"[mcrender] Playwright 截屏不可用，改用系统浏览器 CDP 截图: {e}")
        return await self._screenshot_cdp(viewer_url, out_path)

    async def _screenshot_playwright(self, viewer_url: str, out_path: str) -> str:
        await self._ensure_browser()

        last_err: Exception | None = None
        for attempt in (1, 2):  # 浏览器偶发崩溃时重试一次（会重新拉起）
            page = None
            try:
                page = await self._browser.new_page(
                    viewport={"width": self.width, "height": self.height}
                )
                await page.goto(viewer_url, wait_until="domcontentloaded", timeout=30000)
                # 等待 canvas 出现（WebGL 画布）
                await page.wait_for_selector("canvas", timeout=20000)
                # 等待区块加载渲染
                await page.wait_for_timeout(self.render_wait_ms)
                # 尽力隐藏页面上的说明文字等 HUD 噪音
                await page.add_style_tag(
                    content="""
                    #info, #debug, .crosshair { display: none !important; }
                    body { margin: 0 !important; overflow: hidden !important; }
                    """
                )
                await page.wait_for_timeout(300)
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                await page.screenshot(path=out_path, full_page=False)
                assert_screenshot_renders(out_path)
                return out_path
            except RenderError:
                raise
            except Exception as e:
                last_err = e
                logger.warning(f"[mcrender] 截屏失败(第 {attempt} 次): {e}")
                # 浏览器可能已崩溃，强制重建
                await self.close()
                await self._ensure_browser()
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
        raise RenderError(f"截屏失败: {last_err}")

    # ---------- 系统浏览器 CLI 兜底 ----------

    @staticmethod
    def _find_browser_exe() -> str | None:
        """找系统里的 Chromium 系浏览器。"""
        # PATH 里找
        for name in ("chrome", "msedge", "chromium", "chromium-browser", "google-chrome"):
            p = shutil.which(name)
            if p:
                return p
        # Windows 常见安装路径（Server/桌面版 Edge 基本必装）
        win_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in win_paths:
            if p and os.path.isfile(p):
                return p
        return None

    async def _screenshot_cdp(self, viewer_url: str, out_path: str) -> str:
        """系统浏览器 headless + CDP 截图(真实时间等待，不依赖 playwright)。

        不用 --screenshot/--virtual-time-budget：虚拟时间与 socket.io 长连接
        不兼容会挂死。CDP 方式可以精确控制"等区块加载完再截图"。
        """
        exe = self._find_browser_exe()
        if not exe:
            raise RenderError(
                "未找到 playwright 库，也没有找到系统 Chrome/Edge 浏览器可用于截屏。"
                "解决: 在 AstrBot 环境执行 pip install playwright && playwright install chromium，"
                "或安装 Chrome/Edge 浏览器。"
            )
        try:
            import aiohttp
        except ImportError as e:
            raise RenderError("系统浏览器截图需要 aiohttp(AstrBot 应自带)") from e

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        # 随机空闲端口做调试端口
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dbg_port = s.getsockname()[1]
        user_data_dir = tempfile.mkdtemp(prefix="mc_shot_")
        args = [
            exe,
            "--headless=new",
            f"--remote-debugging-port={dbg_port}",
            f"--window-size={self.width},{self.height}",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # 等调试端口就绪并拿 page target
            ws_url = None
            deadline = time.time() + 20
            async with aiohttp.ClientSession() as session:
                while time.time() < deadline:
                    try:
                        async with session.get(
                            f"http://127.0.0.1:{dbg_port}/json", timeout=aiohttp.ClientTimeout(total=2)
                        ) as resp:
                            targets = await resp.json(content_type=None)
                        page = next((t for t in targets if t.get("type") == "page"), None)
                        if page and page.get("webSocketDebuggerUrl"):
                            ws_url = page["webSocketDebuggerUrl"]
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)
                if not ws_url:
                    raise RenderError("系统浏览器调试端口未就绪")

                async with session.ws_connect(
                    ws_url, max_msg_size=128 * 1024 * 1024
                ) as ws:
                    msg_id = 0
                    pending: dict[int, asyncio.Future] = {}

                    async def send(method: str, params: dict | None = None) -> dict:
                        nonlocal msg_id
                        msg_id += 1
                        fut: asyncio.Future = asyncio.get_running_loop().create_future()
                        pending[msg_id] = fut
                        await ws.send_json({"id": msg_id, "method": method, "params": params or {}})
                        return await asyncio.wait_for(fut, timeout=30)

                    recv_task = asyncio.create_task(self._cdp_recv_loop(ws, pending))

                    try:
                        await send("Page.enable")
                        await send(
                            "Emulation.setDeviceMetricsOverride",
                            {
                                "width": self.width,
                                "height": self.height,
                                "deviceScaleFactor": 1,
                                "mobile": False,
                            },
                        )
                        nav = await send("Page.navigate", {"url": viewer_url})
                        if nav.get("errorText"):
                            raise RenderError(f"页面导航失败: {nav['errorText']}")
                        # 真实时间等待区块加载与渲染
                        await asyncio.sleep(max(2.0, self.render_wait_ms / 1000))
                        shot = await send("Page.captureScreenshot", {"format": "png"})
                        data_b64 = (shot.get("result") or {}).get("data")
                        if not data_b64:
                            raise RenderError("浏览器未返回截图数据")
                        with open(out_path, "wb") as f:
                            f.write(base64.b64decode(data_b64))
                    finally:
                        recv_task.cancel()
            assert_screenshot_renders(out_path)
            return out_path
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            shutil.rmtree(user_data_dir, ignore_errors=True)

    @staticmethod
    async def _cdp_recv_loop(ws, pending: dict[int, asyncio.Future]):
        """分发 CDP 响应到对应 future。"""
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                mid = m.get("id")
                if mid and mid in pending and not pending[mid].done():
                    if "error" in m:
                        pending[mid].set_exception(
                            RenderError(str(m["error"].get("message", "cdp error")))
                        )
                    else:
                        pending[mid].set_result(m)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def image_to_data_uri(path: str) -> str:
    """把本地图片转成 data URI，用于把截屏喂给多模态模型。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


def make_screenshot_path(data_dir: str) -> str:
    return os.path.join(
        data_dir, "screenshots", f"mc_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000}.png"
    )


def cleanup_screenshots(data_dir: str, keep: int = 30):
    """只保留最近 keep 张截屏。"""
    shot_dir = os.path.join(data_dir, "screenshots")
    try:
        files = [
            os.path.join(shot_dir, f)
            for f in os.listdir(shot_dir)
            if f.startswith("mc_") and f.endswith(".png")
        ]
        files.sort()
        for path in files[:-keep] if keep > 0 else []:
            try:
                os.remove(path)
            except OSError:
                pass
    except FileNotFoundError:
        pass
