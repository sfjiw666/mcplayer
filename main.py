# AstrBot Minecraft 玩家插件
#
# 让机器人通过 mineflayer 真正进入 Minecraft 服务器游玩：
#   - 指令控制：走路/寻路/挖方块/放方块/采集/合成/攻击/吃东西...
#   - AI 游玩：自动模式与任务模式，LLM 观察环境(可带截屏视觉)决策下一步动作并解说
#   - 游戏截屏：prismarine-viewer 网页渲染 + Playwright 截图（失败时退回俯视地图），
#     可发到群里或私信（任意 AstrBot 支持的平台，主动消息能力因平台而异）
#
# 平台适配说明见 README.md。

from __future__ import annotations

import asyncio
import json
import base64
import os
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request, stream_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 让本目录下的辅助模块可以被导入（兼容 AstrBot 的多种插件加载方式）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import mcagent  # noqa: E402
from mcbridge import BridgeError, McBridge  # noqa: E402
from mcrender import (  # noqa: E402
    GameScreenCaptor,
    RenderError,
    cleanup_screenshots,
    make_screenshot_path,
)

PLUGIN_NAME = "astrbot_plugin_mcplayer"

HELP_TEXT = """🎮 Minecraft 玩家插件
—— 连接与状态 ——
/mc join [地址] [端口] [ID]   进入服务器(管理员)
/mc leave                     离开服务器(管理员)
/mc status                    查看机器人状态
/mc players                   查看在线玩家
/mc inventory                 查看背包
—— 观赏 ——
/mc shot                      游戏画面截屏
/mc map                       周围地形俯视图
/mc versions                  查看支持的游戏版本
/mc auto on|off               自动游玩开关(管理员)
/mc task <目标>               给 AI 布置一个任务
/mc bind | /mc unbind         订阅/退订本会话的截屏与播报
—— 操控 ——
/mc say <话>                  游戏内发言
/mc goto x y z                寻路走到坐标
/mc move 前后左右 [毫秒]       朝向移动
/mc dig x y z                 挖方块
/mc place x y z [方块]         放方块
/mc collect 方块名 [数量]      采集资源
/mc craft 物品名 [数量]        合成物品
/mc attack [目标] [次数]       攻击附近生物
/mc eat                       吃东西
/mc look x y z                转头看向坐标
提示: 也可以直接 @机器人 用自然语言指挥(需要已进服)。"""


@register(PLUGIN_NAME, "梦创", "让机器人玩 Minecraft 并截屏播报", "v1.2.1")
class McPlayerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.last_session_umo: str | None = None

        # 数据目录（持久化数据放 data 目录，避免插件更新被覆盖）
        self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
        os.makedirs(self.data_dir, exist_ok=True)

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        node_dir = os.path.join(plugin_dir, "node_bot")

        node_cfg = self.config.get("node") or {}
        self.bridge = McBridge(
            node_bot_dir=node_dir,
            node_executable=node_cfg.get("executable") or "node",
            auto_install=bool(node_cfg.get("auto_install_deps", True)),
            log_file=os.path.join(self.data_dir, "node_bridge.log"),
        )
        self.bridge.on_event(self._on_bridge_event)

        cam = self.config.get("camera") or {}
        self.captor = GameScreenCaptor(
            width=int(cam.get("width") or 1280),
            height=int(cam.get("height") or 720),
            render_wait_ms=int(cam.get("render_wait_ms") or 8000),
        )

        self.agent = mcagent.GameAgent(self)

        self._mc_online = False
        self._connect_args: dict | None = None
        self._reconnects = 0
        self._reconnecting = False
        self._runtime_push: set[str] = set()
        self._task_goal: str | None = None
        self._chat_buffer: list[str] = []
        self._chat_flush_lock = asyncio.Lock()
        self._tools = []

        # WebUI 可视化视图状态
        import time as _time_mod

        self._time_mod = _time_mod
        self._web_events: list[dict] = []  # 游戏事件环形缓冲(给视图页)
        self._event_seq = 0  # 事件序号，SSE 据此通知前端拉取
        self._shot_lock = asyncio.Lock()  # 截屏并发保护(视图页与聊天指令互斥)
        self._last_shot_source: str | None = None  # 最近一次截屏来源: viewer|topdown
        self._terminated = False

    # ================= 生命周期 =================

    async def initialize(self):
        mcagent._RT["plugin"] = self
        mcagent._RT["bridge"] = self.bridge
        # 注册 LLM 工具：让用户在普通聊天里也能用自然语言指挥机器人
        try:
            self._tools = mcagent.get_all_tools()
            self.context.add_llm_tools(*self._tools)
            logger.info(f"[{PLUGIN_NAME}] 已注册 {len(self._tools)} 个 LLM 工具")
        except Exception as e:
            self._tools = []
            logger.warning(f"[{PLUGIN_NAME}] LLM 工具注册失败(指令不受影响): {e}")
        self._register_web_apis()

    def _register_web_apis(self):
        """注册 WebUI 可视化视图的后端 API(路由须以插件名为前缀)。"""
        try:
            routes = [
                ("/status", self.web_status, ["GET"], "Bot status for dashboard view"),
                ("/inventory", self.web_inventory, ["GET"], "Bot inventory slots"),
                ("/screenshot", self.web_screenshot, ["GET"], "Take a game screenshot"),
                ("/events", self.web_events, ["GET"], "Recent game events"),
                ("/control", self.web_control, ["POST"], "Dashboard control actions"),
                ("/stream", self.web_stream, ["GET"], "SSE status stream"),
            ]
            for route, handler, methods, desc in routes:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{route}", handler, methods, desc
                )
            logger.info(f"[{PLUGIN_NAME}] 已注册 {len(routes)} 个可视化视图 API")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 可视化视图 API 注册失败: {e}")

    async def terminate(self):
        self._terminated = True
        try:
            if self.agent.running_mode:
                await self.agent.stop_auto()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 停止自动游玩失败: {e}")
        try:
            await self.bridge.stop()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 停止桥接端失败: {e}")
        try:
            await self.captor.close()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 关闭截屏器失败: {e}")
        try:
            for tool in self._tools:
                self.context.provider_manager.llm_tools.remove_func(tool.name)
        except Exception:
            pass
        logger.info(f"[{PLUGIN_NAME}] 插件已卸载")

    # ================= 小工具 =================

    def _cfg(self, section: str) -> dict:
        v = self.config.get(section)
        return v if isinstance(v, dict) else {}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        extra = set(str(i) for i in (self._cfg("safety").get("admin_ids") or []))
        return str(event.get_sender_id()) in extra

    def _tokens(self, event: AstrMessageEvent, sub: str) -> list[str]:
        """取出子指令后面的参数 token。"""
        parts = (event.message_str or "").split()
        for i, p in enumerate(parts):
            if p == sub:
                return parts[i + 1 :]
        return parts[2:] if len(parts) > 2 else []

    def bridge_online(self) -> bool:
        return self.bridge.is_running() and self._mc_online

    def current_goal(self) -> str:
        if self._task_goal:
            return self._task_goal
        return (
            self._cfg("ai").get("auto_play_goal")
            or "在保证安全的前提下自由探索世界，收集基础资源，建造一个小庇护所。"
        )

    def set_task_goal(self, goal: str | None):
        self._task_goal = goal

    def push_targets(self) -> list[str]:
        cfg_targets = list(self._cfg("notify").get("push_sessions") or [])
        return list(dict.fromkeys(cfg_targets + sorted(self._runtime_push)))

    # ================= 推送（多平台适配核心路径） =================

    async def push_text(self, text: str):
        """向所有订阅会话(群/私聊)推送文本。"""
        text = self._clip(text)
        for umo in self.push_targets():
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as e:
                # qq_official 等平台不支持主动消息，失败不影响其他平台
                logger.warning(f"[{PLUGIN_NAME}] 推送到 {umo} 失败: {e}")

    async def push_image(self, image_path: str, caption: str = ""):
        """向所有订阅会话推送一张图（可带说明文字）。"""
        caption = self._clip(caption)

        async def build_chain() -> MessageChain:
            chain = MessageChain()
            if caption:
                chain = chain.message(caption)
            return chain.file_image(image_path)

        for umo in self.push_targets():
            try:
                await self.context.send_message(umo, await build_chain())
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 推送图片到 {umo} 失败: {e}")

    async def push_round(self, rnd: dict):
        """推送 AI 游玩一轮的结果：解说 + 截屏。"""
        texts = []
        if rnd.get("say"):
            texts.append(rnd["say"])
        if rnd.get("thought") and rnd["thought"] != rnd.get("say"):
            texts.append(rnd["thought"])
        if rnd.get("results"):
            texts.append("⚙️ " + "；".join(rnd["results"])[:300])
        shot = rnd.get("shot_path")
        want_image = bool(self._cfg("notify").get("screenshot_every_round", True)) and shot
        text = "\n".join(t for t in texts if t)
        if want_image:
            await self.push_image(shot, text or "📸")
        elif text:
            await self.push_text(text)

    @staticmethod
    def _clip(text: str, limit: int = 1600) -> str:
        """不同平台消息长度限制不同，超长时截断。"""
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    # ================= 游戏截屏 =================

    async def take_game_screenshot(
        self, push: bool = False, caption: str = ""
    ) -> tuple[str | None, str | None]:
        """截取游戏画面。优先网页渲染，失败退回俯视地图。

        返回 (文件路径或 None, 失败原因或 None)。
        """
        if not self.bridge_online():
            return None, "机器人未连接服务器"
        path = make_screenshot_path(self.data_dir)
        node_cfg = self._cfg("node")
        cam_cfg = self._cfg("camera")
        done = False
        last_err: str | None = None

        if node_cfg.get("viewer_enabled", True):
            port = node_cfg.get("viewer_port") or 3000
            url = f"http://127.0.0.1:{int(port)}/"
            try:
                await self.captor.screenshot_viewer(url, path)
                done = True
                self._last_shot_source = "viewer"
            except RenderError as e:
                last_err = f"网页渲染失败: {e}"
                logger.warning(f"[{PLUGIN_NAME}] 网页渲染截屏失败: {e}")
            except Exception as e:
                last_err = f"网页渲染异常: {e}"
                logger.warning(f"[{PLUGIN_NAME}] 截屏异常: {e}")

        if not done and cam_cfg.get("fallback_topdown", True):
            try:
                data = await self.bridge.request(
                    "map",
                    {"radius": 16, "scale": 7, "time_budget_ms": 20000},
                    timeout=60,
                )
                if float(data.get("loaded_ratio") or 0) < 0.03:
                    last_err = last_err or "周围区块尚未加载完成，稍等几秒再试"
                else:
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(data["png_base64"]))
                    done = True
                    last_err = None
                    self._last_shot_source = "topdown"
            except Exception as e:
                last_err = last_err or f"俯视地图生成失败: {e}"
                logger.warning(f"[{PLUGIN_NAME}] 俯视地图生成失败: {e}")

        if not done:
            return None, last_err or "截屏失败(未知原因)"

        try:
            cleanup_screenshots(self.data_dir, keep=int(self._cfg("notify").get("max_screenshots_kept") or 30))
        except Exception:
            pass

        if push:
            await self.push_image(path, caption or "📸 游戏截图")
        return path, None

    # ================= WebUI 可视化视图 API =================

    def _record_event(self, etype: str, text: str):
        """游戏事件进入环形缓冲，供视图页展示与 SSE 通知。"""
        self._web_events.append(
            {"t": int(self._time_mod.time()), "type": etype, "text": str(text)[:200]}
        )
        if len(self._web_events) > 60:
            del self._web_events[: len(self._web_events) - 60]
        self._event_seq += 1

    async def web_status(self):
        """GET /status — 机器人实时状态(视图页与 SSE 共用)。"""
        data = {
            "online": self.bridge_online(),
            "agent_mode": self.agent.running_mode,
            "goal": self._task_goal or (self._cfg("ai").get("auto_play_goal") or ""),
            "busy": None,
            "username": None,
            "position": None,
            "health": None,
            "food": None,
            "dimension": None,
            "biome": None,
            "facing": None,
            "yaw": None,
            "held_item": None,
            "day": None,
            "time_phase": "",
            "is_raining": False,
            "players": [],
            "viewer_port": self._cfg("node").get("viewer_port") or 3000,
        }
        if not data["online"]:
            return json_response(data)
        try:
            st = await self.bridge.request("status", timeout=20)
        except Exception as e:
            return json_response({**data, "error": str(e)})
        tod = st.get("time_of_day")
        phase = ""
        if isinstance(tod, int):
            phase = "白天" if 0 <= tod % 24000 < 12500 else "夜晚"
        data.update(
            {
                "username": st.get("username"),
                "position": st.get("position"),
                "health": st.get("health"),
                "food": st.get("food"),
                "dimension": st.get("dimension"),
                "biome": st.get("biome"),
                "facing": st.get("facing"),
                "yaw": st.get("yaw"),
                "held_item": st.get("held_item"),
                "day": st.get("day"),
                "time_phase": phase,
                "is_raining": bool(st.get("is_raining")),
                "players": st.get("players_online") or [],
                "busy": st.get("busy"),
            }
        )
        return json_response(data)

    async def web_screenshot(self):
        """GET /screenshot?mode=view|map — 截一张图返回 dataURI。"""
        mode = (request.query.get("mode") or "view").lower()
        if not self.bridge_online():
            return error_response("机器人未连接服务器", status_code=409)
        if self._shot_lock.locked():
            return error_response("正在截屏中，请稍候", status_code=409)
        async with self._shot_lock:
            path = None
            if mode == "map":
                try:
                    data = await self.bridge.request(
                        "map", {"radius": 16, "scale": 7, "time_budget_ms": 20000}, timeout=60
                    )
                    if float(data.get("loaded_ratio") or 0) < 0.03:
                        return error_response("周围区块尚未加载完成，稍等几秒再试", status_code=409)
                    path = make_screenshot_path(self.data_dir)
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(data["png_base64"]))
                    self._last_shot_source = "topdown"
                except Exception as e:
                    return error_response(f"俯视地图生成失败: {e}", status_code=500)
            else:
                path, err = await self.take_game_screenshot()
                if not path:
                    return error_response(err or "截屏失败", status_code=500)
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
            except OSError as e:
                return error_response(f"读取截屏文件失败: {e}", status_code=500)
            return json_response(
                {
                    "image": f"data:image/png;base64,{b64}",
                    "mode": mode,
                    "source": self._last_shot_source or ("topdown" if mode == "map" else "viewer"),
                    "took_at": int(self._time_mod.time()),
                }
            )

    async def web_inventory(self):
        """GET /inventory — 背包槽位数据(视图页背包卡片)。"""
        empty = {"online": False, "slots": [], "items": [], "held": None, "total": 0}
        if not self.bridge_online():
            return json_response(empty)
        try:
            data = await self.bridge.request("inventory", timeout=15)
        except Exception as e:
            return json_response({**empty, "online": True, "error": str(e)})
        return json_response({
            "online": True,
            "slots": data.get("slots") or [],
            "items": data.get("items") or [],
            "held": data.get("held"),
            "total": data.get("total") or 0,
        })

    async def web_events(self):
        """GET /events — 最近的游戏事件。"""
        return json_response({"events": self._web_events, "seq": self._event_seq})

    async def web_control(self):
        """POST /control {action} — 视图页控制台。"""
        payload = await request.json(default={})
        action = str(payload.get("action") or "")
        try:
            if action == "join":
                if self.bridge_online():
                    return json_response({"message": "机器人已在线"})
                args = self._build_connect_args()
                await self.bridge.start()
                data = await self.bridge.request("connect", args, timeout=90)
                self._connect_args = args
                self._reconnects = 0
                self._record_event("info", f"进入服务器 {args['host']}:{args['port']} (网页)")
                return json_response({"message": f"已进入服务器! ID: {data.get('username')}"})
            if action == "leave":
                if self.agent.running_mode:
                    await self.agent.stop_auto()
                await self.bridge.request("disconnect", timeout=30)
                self._mc_online = False
                self._record_event("info", "离开服务器 (网页)")
                return json_response({"message": "已离开服务器"})
            if action == "auto_on":
                if not self.bridge_online():
                    return error_response("机器人未在线", status_code=409)
                if self.agent.running_mode:
                    return json_response({"message": f"已在运行: {self.agent.running_mode}"})
                self.agent.start_auto()
                return json_response({"message": "自动游玩已开启"})
            if action == "auto_off":
                if self.agent.running_mode != "auto":
                    return json_response({"message": "自动游玩未开启"})
                await self.agent.stop_auto()
                return json_response({"message": "自动游玩已关闭"})
            if action == "stop":
                await self.bridge.request("stop", timeout=15)
                return json_response({"message": "已停止当前动作"})
            return error_response(f"未知操作: {action}", status_code=400)
        except BridgeError as e:
            return error_response(str(e), status_code=500)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] web control {action} 失败: {e}")
            return error_response(str(e), status_code=500)

    async def web_stream(self):
        """GET /stream — SSE：每 3s 推送状态；有新事件时追加通知。"""

        async def gen():
            last_seq = self._event_seq
            while not self._terminated:
                try:
                    payload = {"type": "status", "data": await self._snapshot_status()}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if self._event_seq != last_seq:
                        last_seq = self._event_seq
                        yield f"data: {json.dumps({'type': 'event'}, ensure_ascii=False)}\n\n"
                except Exception as e:
                    logger.error(f"[{PLUGIN_NAME}] SSE 状态推送失败: {e}")
                await asyncio.sleep(3)

        return stream_response(gen())

    async def _snapshot_status(self) -> dict:
        """web_status 的数据部分(SSE 复用)，不构造 HTTP 响应。"""
        data = {
            "online": self.bridge_online(),
            "agent_mode": self.agent.running_mode,
            "busy": None,
        }
        if not data["online"]:
            return data
        try:
            st = await self.bridge.request("status", timeout=10)
            data.update(
                {
                    "username": st.get("username"),
                    "position": st.get("position"),
                    "health": st.get("health"),
                    "food": st.get("food"),
                    "dimension": st.get("dimension"),
                    "biome": st.get("biome"),
                    "facing": st.get("facing"),
                    "yaw": st.get("yaw"),
                    "held_item": st.get("held_item"),
                    "day": st.get("day"),
                    "time_phase": "",
                    "is_raining": bool(st.get("is_raining")),
                    "players": st.get("players_online") or [],
                    "busy": st.get("busy"),
                }
            )
            tod = st.get("time_of_day")
            if isinstance(tod, int):
                data["time_phase"] = "白天" if 0 <= tod % 24000 < 12500 else "夜晚"
        except Exception:
            pass
        return data

    # ================= 桥接事件处理 =================

    async def _on_bridge_event(self, name: str, data: dict):
        # 注意：这里在桥接读循环里被调用，慢操作交给后台任务
        try:
            if name == "login":
                self._mc_online = True
                self._reconnects = 0
                self._record_event("info", f"已登录为 {data.get('username')}")
            elif name == "spawn":
                self._mc_online = True
                pos = data.get("position") or {}
                self._record_event("info", f"进入世界 {pos.get('x')},{pos.get('y')},{pos.get('z')}")
                if self._cfg("notify").get("push_on_join", True):
                    await self.push_text(f"🎮 已进入服务器！位置 {pos.get('x')},{pos.get('y')},{pos.get('z')}")
                if self._reconnecting:
                    self._reconnecting = False
                    await self.push_text(f"✅ 已重新进入服务器！{data.get('position') or ''}")
            elif name == "chat":
                self._record_event("chat", f"{data.get('username')}: {data.get('message')}")
                if self._cfg("notify").get("relay_player_chat"):
                    self._queue_chat_relay(f"[MC] {data.get('username')}: {data.get('message')}")
            elif name == "death":
                self._record_event("death", "机器人死亡")
                if self._cfg("notify").get("push_on_death", True):
                    asyncio.create_task(self._handle_death())
            elif name == "kicked":
                self._record_event("kicked", f"被踢出: {data.get('reason')}")
                self._mc_online = False
                if self._cfg("notify").get("push_on_kick", True):
                    await self.push_text(f"😢 被踢出服务器: {data.get('reason')}")
            elif name == "end":
                self._record_event("info", f"连接结束: {data.get('reason') or '未知'}")
                self._mc_online = False
                asyncio.create_task(self._handle_disconnect(str(data.get("reason") or "")))
            elif name == "login_url":
                self._record_event("info", "等待微软正版授权...")
                await self.push_text(f"🔐 微软正版授权：请在浏览器打开以下链接完成登录(10分钟内有效):\n{data.get('url')}")
            elif name == "viewer":
                if not data.get("enabled"):
                    self._record_event("info", "网页渲染器未启用(截屏将用俯视地图)")
                    logger.warning(f"[{PLUGIN_NAME}] 网页渲染器未启用: {data.get('error')}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 处理桥接事件 {name} 失败: {e}")

    def _queue_chat_relay(self, line: str):
        self._chat_buffer.append(line)

        async def flush():
            await asyncio.sleep(8)
            async with self._chat_flush_lock:
                lines, self._chat_buffer = self._chat_buffer[:12], []
            if lines:
                await self.push_text("\n".join(lines))

        if not getattr(self, "_flush_scheduled", False):
            self._flush_scheduled = True

            async def wrapper():
                await flush()
                self._flush_scheduled = False

            asyncio.create_task(wrapper())

    async def _handle_death(self):
        await self.push_text("💀 我死了... (位置: " + str((await self._safe_status_pos())) + ")")
        await asyncio.sleep(1.5)
        await self.take_game_screenshot(push=True, caption="💀 死亡现场...")
        # 忽略返回值：死亡截屏失败不影响后续重生流程
        # 自动重生
        await asyncio.sleep(2)
        try:
            await self.bridge.request("respawn", timeout=15)
            await self.push_text("🔄 已自动重生，继续玩！")
        except Exception as e:
            await self.push_text(f"⚠️ 自动重生失败: {e}，试试 /mc respawn")

    async def _safe_status_pos(self) -> str | None:
        try:
            st = await self.bridge.request("status", timeout=10)
            return st.get("position")
        except Exception:
            return None

    async def _handle_disconnect(self, reason: str):
        safety = self._cfg("safety")
        if not safety.get("auto_reconnect", True) or not self._connect_args:
            if reason:
                await self.push_text(f"🔌 与服务器断开: {reason or '未知原因'}")
            return
        max_n = int(safety.get("max_reconnects") or 5)
        if self._reconnects >= max_n:
            await self.push_text(f"🔌 断线且重连次数用尽({max_n})，停止重连。可手动 /mc join")
            return
        self._reconnects += 1
        self._reconnecting = True
        await self.push_text(
            f"🔌 断线({reason or '未知'})，{10} 秒后自动重连 (第 {self._reconnects}/{max_n} 次)"
        )
        await asyncio.sleep(10)
        if self._mc_online or not self._connect_args:
            self._reconnecting = False
            return
        try:
            await self.bridge.request("connect", self._connect_args, timeout=75)
        except Exception as e:
            self._reconnecting = False
            logger.warning(f"[{PLUGIN_NAME}] 自动重连失败: {e}")
            if self._reconnects < max_n:
                await self._handle_disconnect("重连失败")

    # ================= LLM 工具的运行时引用 =================

    def _sync_runtime(self):
        mcagent._RT["bridge"] = self.bridge

    # ================= 指令 =================

    @filter.command_group("mc")
    def mc():
        """Minecraft 玩家插件指令组"""
        pass

    # ---- 连接管理 ----

    def _build_connect_args(self, host: str | None = None, port: int | None = None,
                            username: str | None = None) -> dict:
        """按配置(可带临时覆盖)构造 Node 端连接参数。"""
        srv = self._cfg("server")
        return {
            "host": host or (srv.get("host") or "127.0.0.1"),
            "port": int(port or int(srv.get("port") or 25565)),
            "username": username or (srv.get("username") or "AstrBot"),
            "auth": srv.get("auth") or "offline",
            "password": srv.get("password") or "",
            "version": srv.get("version") or "",
            "viewer": bool(self._cfg("node").get("viewer_enabled", True)),
            "viewerOptions": {
                "port": int(self._cfg("node").get("viewer_port") or 3000),
                "viewDistance": int(self._cfg("node").get("viewer_view_distance") or 6),
                "firstPerson": (self._cfg("camera").get("mode") == "first_person"),
            },
        }

    @mc.command("join")
    async def mc_join(self, event: AstrMessageEvent):
        """进入 Minecraft 服务器(管理员)。用法: /mc join [地址] [端口] [游戏ID]"""
        self.last_session_umo = event.unified_msg_origin
        self._sync_runtime()
        if not self._is_admin(event):
            yield event.plain_result("⛔ 该指令仅管理员可用")
            return
        tokens = self._tokens(event, "join")
        host = tokens[0] if len(tokens) >= 1 else None
        try:
            port = int(tokens[1]) if len(tokens) >= 2 else None
        except ValueError:
            yield event.plain_result("端口必须是数字")
            return
        username = tokens[2] if len(tokens) >= 3 else None
        connect_args = self._build_connect_args(host, port, username)

        if self.bridge_online():
            yield event.plain_result("机器人已在线，请先 /mc leave")
            return

        self._runtime_push.add(event.unified_msg_origin)
        yield event.plain_result(
            f"🚧 正在进入 {connect_args['host']}:{connect_args['port']} (ID: {connect_args['username']})..."
            " 首次运行需要安装依赖，请耐心等待"
        )
        try:
            await self.bridge.start()
            data = await self.bridge.request("connect", connect_args, timeout=75)
            self._connect_args = connect_args
            self._reconnects = 0
            yield event.plain_result(
                f"✅ 已进入服务器! ID: {data.get('username')} | 版本: {data.get('version')}\n"
                f"位置: {data.get('position')}\n试试 /mc shot 截个屏，或 /mc auto on 让我自己玩~"
            )
        except BridgeError as e:
            msg = str(e)
            if "protocol" in msg.lower() or "unsupported" in msg.lower():
                # 版本兼容问题：附带当前支持的版本范围与升级指引
                try:
                    vers = await self.bridge.request("versions", timeout=15)
                    latest = ", ".join((vers.get("latest_supported") or [])[:6])
                    msg += (
                        f"\n💡 当前依赖支持的最新版本: {latest}"
                        "\n解决: ① 在插件 node_bot 目录执行 npm update 升级依赖后重试"
                        "(上游 mineflayer 支持该版本后即可进服)"
                        " ② 或暂时改用受支持版本的服务器"
                    )
                except Exception:
                    pass
            yield event.plain_result(f"❌ 进入失败: {msg}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] join 异常: {e}")
            yield event.plain_result(f"❌ 出错了: {e}")

    @mc.command("leave")
    async def mc_leave(self, event: AstrMessageEvent):
        """离开服务器(管理员)"""
        if not self._is_admin(event):
            yield event.plain_result("⛔ 该指令仅管理员可用")
            return
        try:
            if self.agent.running_mode:
                await self.agent.stop_auto()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 停止自动游玩失败: {e}")
        try:
            await self.bridge.request("disconnect", timeout=30)
            yield event.plain_result("👋 已离开服务器")
        except Exception as e:
            yield event.plain_result(f"离开时出错: {e}")
        self._mc_online = False

    @mc.command("cmd")
    async def mc_cmd(self, event: AstrMessageEvent):
        """以机器人身份执行服务器命令(管理员)。用法: /mc cmd /gamemode creative"""
        if not self._is_admin(event):
            yield event.plain_result("⛔ 该指令仅管理员可用")
            return
        tokens = self._tokens(event, "cmd")
        if not tokens:
            yield event.plain_result("用法: /mc cmd /<服务器命令>")
            return
        command = " ".join(tokens)
        try:
            await self.bridge.request("command", {"command": command}, timeout=20)
            yield event.plain_result(f"已执行: {command}")
        except Exception as e:
            yield event.plain_result(f"执行失败: {e}")

    # ---- 状态/观赏 ----

    @mc.command("status")
    async def mc_status(self, event: AstrMessageEvent):
        """查看机器人游戏状态"""
        self.last_session_umo = event.unified_msg_origin
        if not self.bridge_online():
            yield event.plain_result("当前未连接服务器。管理员可用 /mc join 进入。")
            return
        try:
            st = await self.bridge.request("status", timeout=20)
            players = st.get("players_online") or []
            names = ", ".join(p["username"] for p in players[:10]) or "无"
            text = (
                f"🎮 {st.get('username')} @ {st.get('dimension')}/{st.get('biome') or '?'}\n"
                f"❤️ {st.get('health')}/20  🍗 {st.get('food')}/20  模式: {st.get('gamemode')}\n"
                f"📍 {st.get('position')}  朝向: {st.get('facing')}\n"
                f"🧑‍🤝‍🧑 在线({len(players)}): {names}"
            )
            yield event.plain_result(text)
            yield self._status_buttons(event)
        except Exception as e:
            yield event.plain_result(f"获取状态失败: {e}")

    def _status_buttons(self, event: AstrMessageEvent):
        """状态指令附带的快捷按钮；不支持按钮的平台会显示 fallback 文字。"""
        try:
            import astrbot.api.message_components as Comp

            row = Comp.ActionRow(
                buttons=[
                    Comp.Button(
                        id="mcplayer:shot",
                        label="📸 截个屏",
                        action=Comp.Button.CallbackAction(data={"act": "shot"}),
                    ),
                    Comp.Button(
                        id="mcplayer:auto",
                        label="🎮 自动游玩",
                        action=Comp.Button.CallbackAction(data={"act": "auto"}),
                    ),
                    Comp.Button(
                        id="mcplayer:status",
                        label="🔄 刷新",
                        action=Comp.Button.CallbackAction(data={"act": "status"}),
                    ),
                ],
                action_timeout=600,
                fallback_text="提示: /mc shot 截屏 | /mc auto on 自动游玩 | /mc status 刷新",
            )
            return event.chain_result([row])
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 按钮构建失败，退化为纯文本: {e}")
            return event.plain_result("提示: /mc shot 截屏 | /mc auto on 自动游玩")

    @mc.command("shot")
    async def mc_shot(self, event: AstrMessageEvent):
        """对游戏画面截屏并发送"""
        self.last_session_umo = event.unified_msg_origin
        yield event.plain_result("📸 正在截屏...")
        path, err = await self.take_game_screenshot()
        if not path:
            yield event.plain_result(f"❌ 截屏失败: {err}\n💡 可尝试 /mc map 俯视图，或查看插件日志排查渲染器问题")
            return
        import astrbot.api.message_components as Comp

        yield event.chain_result(
            [
                Comp.Plain("📸 当前游戏画面"),
                Comp.Image.fromFileSystem(path),
            ]
        )

    @mc.command("map")
    async def mc_map(self, event: AstrMessageEvent):
        """生成周围地形的俯视图"""
        if not self.bridge_online():
            yield event.plain_result("未连接服务器")
            return
        yield event.plain_result("🗺️ 正在绘制俯视图...")
        try:
            data = await self.bridge.request(
                "map", {"radius": 16, "scale": 7, "time_budget_ms": 20000}, timeout=60
            )
            if float(data.get("loaded_ratio") or 0) < 0.03:
                yield event.plain_result("⏳ 周围区块尚未加载完成，稍等几秒再试（刚进服时需要几秒加载地形）")
                return
            path = make_screenshot_path(self.data_dir)
            with open(path, "wb") as f:
                f.write(base64.b64decode(data["png_base64"]))
            import astrbot.api.message_components as Comp

            c = data.get("center") or {}
            yield event.chain_result(
                [
                    Comp.Plain(f"🗺️ 以 ({c.get('x')},{c.get('y')},{c.get('z')}) 为中心 ±{data.get('radius')} 格"),
                    Comp.Image.fromFileSystem(path),
                ]
            )
        except Exception as e:
            yield event.plain_result(f"绘制失败: {e}")

    @mc.command("bind")
    async def mc_bind(self, event: AstrMessageEvent):
        """把当前会话(群聊或私聊)订阅为截屏与播报接收地"""
        umo = event.unified_msg_origin
        targets = self._cfg("notify").get("push_sessions") or []
        if umo in targets:
            yield event.plain_result("本会话已经订阅过啦")
            return
        targets.append(umo)
        self.config["notify"]["push_sessions"] = targets
        self.config.save_config()
        yield event.plain_result("✅ 已订阅！机器人游玩时的截屏和解说会发到这里(群里或私信都行)。退订用 /mc unbind")

    @mc.command("unbind")
    async def mc_unbind(self, event: AstrMessageEvent):
        """退订当前会话"""
        targets = self._cfg("notify").get("push_sessions") or []
        umo = event.unified_msg_origin
        if umo not in targets:
            self._runtime_push.discard(umo)
            yield event.plain_result("本会话未订阅")
            return
        targets.remove(umo)
        self.config["notify"]["push_sessions"] = targets
        self.config.save_config()
        yield event.plain_result("已退订")

    # ---- AI 游玩 ----

    @mc.command("auto")
    async def mc_auto(self, event: AstrMessageEvent):
        """自动游玩开关(管理员)。用法: /mc auto on|off"""
        self.last_session_umo = event.unified_msg_origin
        if not self._is_admin(event):
            yield event.plain_result("⛔ 该指令仅管理员可用")
            return
        tokens = self._tokens(event, "auto")
        action = tokens[0].lower() if tokens else ""
        if action == "on":
            if not self.bridge_online():
                yield event.plain_result("请先让机器人进入服务器 (/mc join)")
                return
            if self.agent.running_mode:
                yield event.plain_result(f"已在运行: {self.agent.running_mode}")
                return
            self._runtime_push.add(event.unified_msg_origin)
            self.agent.start_auto()
            yield event.plain_result("🎮 自动游玩已开启！我会自己探索、播报、发截图。关闭: /mc auto off")
        elif action == "off":
            if self.agent.running_mode != "auto":
                yield event.plain_result("自动游玩未开启")
                return
            await self.agent.stop_auto()
            yield event.plain_result("🛑 自动游玩已关闭")
        else:
            state = self.agent.running_mode or "未开启"
            yield event.plain_result(f"当前状态: {state}\n用法: /mc auto on | /mc auto off")

    @mc.command("task")
    async def mc_task(self, event: AstrMessageEvent):
        """给 AI 布置一个游玩任务。用法: /mc task 去造一个工作台并合成一把木镐"""
        self.last_session_umo = event.unified_msg_origin
        goal = " ".join(self._tokens(event, "task")).strip()
        if not goal:
            yield event.plain_result("请描述任务，例如: /mc task 去砍20个木头然后造一把木镐")
            return
        if not self.bridge_online():
            yield event.plain_result("请先让机器人进入服务器 (/mc join)")
            return
        if self.agent.running_mode:
            yield event.plain_result(f"当前正在执行: {self.agent.running_mode}，请等它完成或先停止")
            return
        self._runtime_push.add(event.unified_msg_origin)

        async def runner():
            try:
                await self.agent.run_task(goal)
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 任务异常: {e}")
                await self.push_text(f"任务执行出错: {e}")

        asyncio.create_task(runner())
        yield event.plain_result(f"🎯 任务已接受: {goal}\n进度会实时播报到这里(最多 {self._cfg('ai').get('task_max_rounds') or 15} 轮)")

    # ---- 操控指令 ----

    @mc.command("say")
    async def mc_say(self, event: AstrMessageEvent):
        """让机器人在游戏内发言。用法: /mc say 大家好！"""
        text = " ".join(self._tokens(event, "say")).strip()
        if not text:
            yield event.plain_result("想让我说什么呀？用法: /mc say 大家好")
            return
        try:
            await self.bridge.request("chat", {"message": text[:200]}, timeout=20)
            yield event.plain_result(f"已发言: {text[:200]}")
        except Exception as e:
            yield event.plain_result(f"发言失败: {e}")

    @mc.command("goto")
    async def mc_goto(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """寻路走到坐标附近。用法: /mc goto 100 64 -200"""
        try:
            yield event.plain_result(f"🚶 正在前往 ({x}, {y}, {z})...")
            data = await self.bridge.request("goto", {"x": x, "y": y, "z": z, "range": 2}, timeout=130)
            p = (data or {}).get("position") or {}
            yield event.plain_result(f"✅ 已到达 ({p.get('x')}, {p.get('y')}, {p.get('z')})")
        except Exception as e:
            yield event.plain_result(f"移动失败: {e}")

    @mc.command("move")
    async def mc_move(self, event: AstrMessageEvent):
        """朝当前面向移动一段。用法: /mc move 前 2000 (前/后/左/右, 默认1000ms)"""
        tokens = self._tokens(event, "move")
        dmap = {"前": "forward", "后": "back", "左": "left", "右": "right",
                "forward": "forward", "back": "back", "left": "left", "right": "right"}
        if not tokens or tokens[0] not in dmap:
            yield event.plain_result("用法: /mc move 前|后|左|右 [毫秒]")
            return
        direction = dmap[tokens[0]]
        try:
            duration = int(tokens[1]) if len(tokens) > 1 else 1000
        except ValueError:
            duration = 1000
        try:
            data = await self.bridge.request(
                "move", {"direction": direction, "duration_ms": duration}, timeout=20
            )
            yield event.plain_result(f"🚶 完成，现在在 {data.get('position')}")
        except Exception as e:
            yield event.plain_result(f"移动失败: {e}")

    @mc.command("look")
    async def mc_look(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """转头看向某坐标"""
        try:
            await self.bridge.request("look", {"x": x, "y": y, "z": z}, timeout=15)
            yield event.plain_result("👀 好的，已看过去")
        except Exception as e:
            yield event.plain_result(f"失败: {e}")

    @mc.command("dig")
    async def mc_dig(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """挖掉指定坐标的方块(需在5格内)"""
        try:
            yield event.plain_result("⛏️ 正在挖...")
            data = await self.bridge.request("dig", {"x": x, "y": y, "z": z}, timeout=45)
            yield event.plain_result(f"✅ 挖掉了 {data.get('dug')}")
        except Exception as e:
            yield event.plain_result(f"挖掘失败: {e}")

    @mc.command("place")
    async def mc_place(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """在坐标处放置方块(默认用背包第一格)。用法: /mc place 10 64 3 cobblestone"""
        tokens = self._tokens(event, "place")
        block = tokens[3] if len(tokens) > 3 else None
        try:
            data = await self.bridge.request(
                "place", {"x": x, "y": y, "z": z, "block": block}, timeout=30
            )
            yield event.plain_result(f"✅ 放置了 {data.get('placed')}")
        except Exception as e:
            yield event.plain_result(f"放置失败: {e}")

    @mc.command("collect")
    async def mc_collect(self, event: AstrMessageEvent):
        """采集资源。用法: /mc collect oak_log 10"""
        tokens = self._tokens(event, "collect")
        if not tokens:
            yield event.plain_result("用法: /mc collect 方块名 [数量]，例如 /mc collect oak_log 10")
            return
        block = tokens[0]
        try:
            count = max(1, min(int(tokens[1]) if len(tokens) > 1 else 1, 32))
        except ValueError:
            count = 1
        yield event.plain_result(f"⛏️ 开始采集 {count} 个 {block}，可能需要一点时间...")
        try:
            data = await self.bridge.request("collect", {"block": block, "count": count}, timeout=210)
            yield event.plain_result(f"✅ 采集完成: {data.get('collected')}/{count} 个 {block}")
        except Exception as e:
            yield event.plain_result(f"采集失败: {e}")

    @mc.command("craft")
    async def mc_craft(self, event: AstrMessageEvent):
        """合成物品。用法: /mc craft crafting_table 1"""
        tokens = self._tokens(event, "craft")
        if not tokens:
            yield event.plain_result("用法: /mc craft 物品名 [数量]，例如 /mc craft crafting_table")
            return
        item = tokens[0]
        try:
            count = max(1, min(int(tokens[1]) if len(tokens) > 1 else 1, 64))
        except ValueError:
            count = 1
        try:
            data = await self.bridge.request("craft", {"item": item, "count": count}, timeout=100)
            yield event.plain_result(f"✅ 合成了 {data.get('crafted')} x{data.get('count')}")
        except Exception as e:
            yield event.plain_result(f"合成失败: {e}")

    @mc.command("attack")
    async def mc_attack(self, event: AstrMessageEvent):
        """攻击附近生物。用法: /mc attack zombie 5 或 /mc attack (自动攻击最近敌对生物)"""
        tokens = self._tokens(event, "attack")
        target = tokens[0] if tokens else "nearest_hostile"
        try:
            times = max(1, min(int(tokens[1]) if len(tokens) > 1 else 5, 20))
        except ValueError:
            times = 5
        try:
            data = await self.bridge.request("attack", {"target": target, "times": times}, timeout=45)
            yield event.plain_result(f"⚔️ 攻击了 {data.get('attacked')} 次: {data.get('targets')}")
        except Exception as e:
            yield event.plain_result(f"攻击失败: {e}")

    @mc.command("eat")
    async def mc_eat(self, event: AstrMessageEvent):
        """让机器人吃东西"""
        try:
            data = await self.bridge.request("eat", timeout=30)
            yield event.plain_result(f"😋 吃了 {data.get('ate')}，饥饿 {data.get('food')}/20")
        except Exception as e:
            yield event.plain_result(f"进食失败: {e}")

    @mc.command("inventory")
    async def mc_inventory(self, event: AstrMessageEvent):
        """查看背包"""
        try:
            data = await self.bridge.request("inventory", timeout=15)
            items = data.get("items") or []
            if not items:
                yield event.plain_result("背包空空如也")
                return
            lines = [f"🎒 背包({data.get('total')}件):"]
            lines += [f"  {i['displayName']} x{i['count']}" for i in items[:15]]
            if len(items) > 15:
                lines.append(f"  ... 还有 {len(items) - 15} 种")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @mc.command("players")
    async def mc_players(self, event: AstrMessageEvent):
        """查看在线玩家"""
        try:
            data = await self.bridge.request("players", timeout=15)
            names = ", ".join(p["username"] for p in data.get("players") or []) or "无人"
            yield event.plain_result(f"🧑‍🤝‍🧑 在线: {names}")
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @mc.command("respawn")
    async def mc_respawn(self, event: AstrMessageEvent):
        """死亡后重生"""
        try:
            await self.bridge.request("respawn", timeout=15)
            yield event.plain_result("🔄 已重生")
        except Exception as e:
            yield event.plain_result(f"重生失败: {e}")

    @mc.command("versions")
    async def mc_versions(self, event: AstrMessageEvent):
        """查看当前支持的游戏版本"""
        try:
            vers = await self.bridge.request("versions", timeout=15)
            latest = vers.get("latest_supported") or []
            viewer = vers.get("viewer_supported") or []
            yield event.plain_result(
                f"📦 可进服的最新版本: {', '.join(latest) or '未知'}\n"
                f"🖼️ 网页截屏支持的版本: {', '.join(viewer) or '未知'}(其余版本自动用最近版本贴图或俯视地图)"
            )
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @mc.command("help")
    async def mc_help(self, event: AstrMessageEvent):
        """查看帮助"""
        yield event.plain_result(HELP_TEXT)

    # ---- 按钮回调 ----

    @filter.button_action("mcplayer:shot")
    async def btn_shot(self, event: AstrMessageEvent):
        if not self.bridge_online():
            yield event.plain_result("未连接服务器")
            return
        path, err = await self.take_game_screenshot()
        if not path:
            yield event.plain_result(f"截屏失败: {err}")
            return
        import astrbot.api.message_components as Comp

        yield event.chain_result([Comp.Plain("📸 游戏截图"), Comp.Image.fromFileSystem(path)])

    @filter.button_action("mcplayer:auto")
    async def btn_auto(self, event: AstrMessageEvent):
        # 切换开关
        if self.agent.running_mode == "auto":
            await self.agent.stop_auto()
            yield event.plain_result("🛑 自动游玩已关闭")
        else:
            if not self.bridge_online():
                yield event.plain_result("请先让机器人进入服务器 (/mc join)")
                return
            if self.agent.running_mode:
                yield event.plain_result(f"已在运行: {self.agent.running_mode}")
                return
            self.agent.start_auto()
            yield event.plain_result("🎮 自动游玩已开启！关闭: /mc auto off")

    @filter.button_action("mcplayer:status")
    async def btn_status(self, event: AstrMessageEvent):
        async for r in self.mc_status(event):
            yield r
