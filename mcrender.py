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
import os
import shutil
import tempfile
import time

from astrbot.api import logger


class RenderError(Exception):
    pass


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
            logger.warning(f"[mcrender] Playwright 截屏不可用，尝试系统浏览器兜底: {e}")
        return await self._screenshot_cli(viewer_url, out_path)

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
                if os.path.getsize(out_path) < 1024:
                    raise RenderError("截屏文件异常地小，渲染可能失败")
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

    async def _screenshot_cli(self, viewer_url: str, out_path: str) -> str:
        """用系统浏览器的 headless CLI 截图，不依赖 playwright。"""
        exe = self._find_browser_exe()
        if not exe:
            raise RenderError(
                "未找到 playwright 库，也没有找到系统 Chrome/Edge 浏览器可用于截屏。"
                "解决: 在 AstrBot 环境执行 pip install playwright && playwright install chromium，"
                "或安装 Chrome/Edge 浏览器。"
            )
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        user_data_dir = tempfile.mkdtemp(prefix="mc_shot_")
        budget = max(3000, self.render_wait_ms + 2000)
        args = [
            exe,
            "--headless=new",
            f"--screenshot={out_path}",
            f"--window-size={self.width},{self.height}",
            f"--virtual-time-budget={budget}",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
            f"--user-data-dir={user_data_dir}",
            viewer_url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=budget / 1000 + 30)
            except asyncio.TimeoutError:
                proc.kill()
                raise RenderError("系统浏览器截图超时")
            if not os.path.isfile(out_path) or os.path.getsize(out_path) < 1024:
                tail = (err or b"")[-300:].decode("utf-8", errors="replace")
                raise RenderError(f"系统浏览器未产出截图(tail: {tail})")
            return out_path
        finally:
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
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
