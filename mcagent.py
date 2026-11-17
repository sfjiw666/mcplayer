# AstrBot Minecraft 玩家插件 - AI 游玩智能体
#
# 两个能力：
#   1. JSON 规划循环（自动游玩 / 任务模式）：
#      观察(状态+周围环境+截屏) -> LLM 输出 JSON 计划 -> 执行动作 -> 记录 -> 下一轮
#   2. LLM 工具（@机器人 自然语言控制游戏），按开发指南的 FunctionTool dataclass 方式定义，
#      通过 context.add_llm_tools() 注册。

from __future__ import annotations

import asyncio
import json
import re
from collections import deque

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

# 运行时引用（由 main.py 注入），避免在 dataclass 里加额外字段
_RT: dict = {}

# 规划器允许的动作 -> (桥接命令, 超时秒)
ACTION_TIMEOUTS = {
    "goto": 120,
    "move": 20,
    "look_at": 15,
    "dig": 40,
    "dig_down": 90,
    "place": 30,
    "collect": 200,
    "craft": 90,
    "equip": 15,
    "eat": 30,
    "attack": 40,
    "jump": 15,
    "chat": 15,
    "toss": 15,
    "respawn": 15,
    "map": 30,
}


class AgentError(Exception):
    pass


# ---------------- 观察与提示词 ----------------

async def build_observation(bridge, extra_note: str | None = None) -> str:
    """把游戏状态汇总成给 LLM 看的文本。"""
    status = await bridge.request("status", timeout=20)
    observe = await bridge.request("observe", timeout=30)
    inv = await bridge.request("inventory", timeout=15)

    def fmt_pos(p):
        return f"({p['x']}, {p['y']}, {p['z']})" if p else "未知"

    tod = status.get("time_of_day")
    # Minecraft 一天 24000 tick，0 是清晨，13000 左右入夜
    if isinstance(tod, int):
        phase = "白天" if 0 <= tod % 24000 < 12500 else "夜晚"

    lines = [
        f"- 位置: {fmt_pos(status.get('position'))}，朝向: {status.get('facing')}",
        f"- 生命: {status.get('health')}/20，饥饿: {status.get('food')}/20",
        f"- 维度: {status.get('dimension')}，生物群系: {status.get('biome') or '未知'}，游戏模式: {status.get('gamemode')}",
    ]
    if isinstance(tod, int):
        lines.append(f"- 时间: 第{status.get('day')}天 {phase} (tick={tod})，{'正在下雨' if status.get('is_raining') else '未下雨'}")
    lines.append(f"- 手持: {status.get('held_item') or '空手'}")
    lines.append(f"- 正前方(2格): {observe.get('front_eye_level') or '空'} / 脚前: {observe.get('front_feet_level') or '空'} / 脚下: {observe.get('under_feet')}")
    lines.append(f"- 周围方块: {', '.join(observe.get('blocks_around') or [])}")
    ents = observe.get("entities_nearby") or []
    if ents:
        lines.append("- 附近实体: " + ", ".join(f"{e['name']}({e['kind']},{e['distance']}格)" for e in ents[:6]))
    else:
        lines.append("- 附近实体: 无")
    items = inv.get("items") or []
    if items:
        lines.append("- 背包: " + ", ".join(f"{i['name']}x{i['count']}" for i in items[:10]))
    else:
        lines.append("- 背包: 空")
    if status.get("busy"):
        lines.append(f"- 注意: 机器人当前正在执行: {status.get('busy')}")
    if extra_note:
        lines.append(f"- 补充: {extra_note}")
    return "\n".join(lines)


# 注意：本模板不用 str.format()（JSON 示例含大量花括号），用 __GOAL__/__EXTRA_RULE__ 标记替换
SYSTEM_PROMPT = """你是 Minecraft 生存游戏里的玩家智能体"小星"，正在替群友玩 Minecraft。
你每轮会收到：游戏状态观察(可能附带一张当前视角截图)、上几轮的执行结果。
你的目标是：__GOAL__

请只输出一个 JSON 对象（不要 markdown 代码块、不要多余文字），格式：
{"thought": "简短中文思考(50字内)", "say": "发给群友的中文播报(80字内，像在直播；没有可省略)", "actions": [{"cmd": "动作名", "args": {...}}], "task_done": true或false}

可用动作（每轮最多 3 个，按顺序执行）：
- goto {"x":int,"y":int,"z":int,"range":int}        寻路走到坐标附近（大范围移动首选）
- move {"direction":"forward|back|left|right","duration_ms":int,"jump":bool}  朝当前视角方向移动
- look_at {"x":int,"y":int,"z":int}                 转头看向坐标（move 之前用它可以改变方向）
- dig {"x":int,"y":int,"z":int}                     挖掉某坐标的方块(须在5格内，配合 goto)
- dig_down {"count":int}                            向脚下挖(慎用，最多5格)
- place {"x":int,"y":int,"z":int,"block":"名"}      放置背包里的方块
- collect {"block":"方块名","count":int,"radius":int} 采集附近方块并拾取(如 oak_log, cobblestone)
- craft {"item":"物品名","count":int}                合成(如 crafting_table, wooden_pickaxe, bread)
- equip {"item":"物品名"}                            把物品拿到手上
- eat {}                                            吃东西
- attack {"target":"nearest_hostile|实体名","times":int} 攻击附近实体
- jump {"times":int} / chat {"message":"游戏内发言"} / toss {"item":"名","count":int}
- respawn {}                                        死亡后重生

行动准则：
- 先看血量/饥饿：血量低(<=8)或饥饿低时先 eat 或撤退；天黑、附近有敌对生物时优先安全。
- 采集资源用 collect；去远处用 goto(可以给自己规划途经点)；挖精确方块用 dig。
- 动作失败会写在"上轮结果"里，不要原样重复失败动作。
- say 是给群友看的直播解说，要生动简短；多数轮都应该说点什么。
__EXTRA_RULE__"""

TASK_EXTRA = "\n- task_done 只有当任务目标已经完成(或确认无法完成)时才为 true，完成前必须继续行动。"


def _extract_json(text: str) -> dict:
    """从 LLM 输出中尽力提取 JSON 对象。"""
    text = text.strip()
    # 去掉 markdown 代码围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 找第一个 { 到最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise AgentError("模型没有返回可解析的 JSON")


# ---------------- 规划循环 ----------------

class GameAgent:
    def __init__(self, plugin):
        self.plugin = plugin
        self.history: deque = deque(maxlen=6)  # 最近几轮的"动作 -> 结果"
        self.running_mode: str | None = None  # None / 'auto' / 'task'
        self._auto_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ---- 基础 ----

    async def _provider_id(self) -> str:
        ctx = self.plugin.context
        pid = (self.plugin.config.get("ai") or {}).get("provider_id") or ""
        if pid:
            return pid
        umo = self.plugin.last_session_umo
        if not umo:
            raise AgentError("没有可用的模型：请在插件配置里选择模型，或先从聊天会话里启动机器人。")
        return await ctx.get_current_chat_provider_id(umo)

    async def _llm(self, prompt: str, image_uri: str | None = None) -> str:
        provider_id = await self._provider_id()
        ai_cfg = self.plugin.config.get("ai") or {}
        extra_rule = TASK_EXTRA if self.running_mode == "task" else ""
        system_prompt = (
            SYSTEM_PROMPT.replace("__GOAL__", self.plugin.current_goal())
            .replace("__EXTRA_RULE__", extra_rule)
        )
        kwargs = {}
        if image_uri and ai_cfg.get("use_vision", True):
            kwargs["image_urls"] = [image_uri]
        try:
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=prompt,
                **kwargs,
            )
        except Exception as e:
            # 视觉失败时退回纯文本
            if "image_urls" in kwargs:
                logger.warning(f"[mcagent] 视觉请求失败，退回纯文本: {e}")
                resp = await self.plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
            else:
                raise
        return resp.completion_text or ""

    async def _decide(self, observation: str, image_uri: str | None) -> dict:
        hist_text = "\n".join(self.history) if self.history else "(刚开始，还没有历史)"
        prompt = f"【上几轮执行结果】\n{hist_text}\n\n【当前观察】\n{observation}\n\n请输出 JSON 计划。"
        text = await self._llm(prompt, image_uri)
        try:
            return _extract_json(text)
        except AgentError:
            # 重试一次
            text2 = await self._llm(prompt + "\n\n注意：只输出一个合法的 JSON 对象，不要任何其他文字。", None)
            return _extract_json(text2)

    async def execute_plan(self, plan: dict) -> list[str]:
        """执行计划中的动作，返回结果摘要列表。"""
        results: list[str] = []
        actions = plan.get("actions") or []
        if not isinstance(actions, list):
            actions = []
        for act in actions[:3]:
            if not isinstance(act, dict):
                continue
            cmd = str(act.get("cmd") or "").strip()
            args = act.get("args") or {}
            if cmd not in ACTION_TIMEOUTS:
                results.append(f"{cmd}: 未知动作，已跳过")
                continue
            if cmd == "look_at":
                cmd_name, args = "look", {"x": args.get("x"), "y": args.get("y"), "z": args.get("z")}
            else:
                cmd_name = cmd
            try:
                data = await self.plugin.bridge.request(cmd_name, args, timeout=ACTION_TIMEOUTS[cmd])
                results.append(f"{cmd}({json.dumps(args, ensure_ascii=False)}) -> 成功 {self._brief(cmd, data)}")
            except Exception as e:
                results.append(f"{cmd}({json.dumps(args, ensure_ascii=False)}) -> 失败: {e}")
        return results

    @staticmethod
    def _brief(cmd: str, data) -> str:
        if not isinstance(data, dict):
            return ""
        if cmd == "collect":
            return f"采集了 {data.get('collected')} 个"
        if cmd == "craft":
            return f"合成了 {data.get('crafted')}x{data.get('count')}"
        if cmd == "eat":
            return f"吃了 {data.get('ate')}，饥饿 {data.get('food')}/20"
        if cmd == "goto":
            p = data.get("position") or {}
            return f"到达 ({p.get('x')},{p.get('y')},{p.get('z')})"
        if cmd == "dig":
            return f"挖掉了 {data.get('dug')}"
        if cmd == "attack":
            return f"攻击了 {data.get('attacked')} 次: {data.get('targets')}"
        if cmd == "respawn":
            return "已重生"
        return ""

    # ---- 轮次 ----

    async def run_round(self) -> dict:
        """执行一轮：截屏 -> 观察 -> 决策 -> 行动。返回本轮信息。"""
        bridge = self.plugin.bridge
        say = ""
        thought = ""

        # 截屏（给视觉模型 + 播报）
        image_uri = None
        shot_path, _shot_err = await self.plugin.take_game_screenshot(push=False)
        if shot_path:
            from mcrender import image_to_data_uri

            try:
                image_uri = image_to_data_uri(shot_path)
            except Exception as e:
                logger.warning(f"[mcagent] 截屏转码失败: {e}")

        observation = await build_observation(bridge)
        try:
            plan = await self._decide(observation, image_uri)
        except AgentError as e:
            self.history.append("决策失败: " + str(e))
            return {"say": "", "thought": "", "error": str(e), "shot_path": shot_path}

        thought = str(plan.get("thought") or "").strip()
        say = str(plan.get("say") or "").strip()
        results = await self.execute_plan(plan)

        if results:
            self.history.append("；".join(results))
        round_text = f"💭 {thought}" if thought else ""
        return {
            "say": say,
            "thought": round_text,
            "results": results,
            "task_done": bool(plan.get("task_done")),
            "shot_path": shot_path,
        }

    # ---- 自动游玩 ----

    async def auto_loop(self):
        plugin = self.plugin
        interval = (plugin.config.get("ai") or {}).get("auto_interval_seconds") or 25
        logger.info("[mcagent] 自动游玩已开启")
        await plugin.push_text("🎮 自动游玩已开启！我会边玩边播报，快看截图~")
        while not self._stop_event.is_set():
            try:
                if not plugin.bridge_online():
                    await plugin.push_text("⚠️ 与服务器断开了，自动游玩暂停。")
                    break
                rnd = await self.run_round()
                if rnd.get("error"):
                    await plugin.push_text(f"🤔 这轮思考出了点问题: {rnd['error']}")
                else:
                    await plugin.push_round(rnd)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[mcagent] 自动游玩轮次异常: {e}")
                await plugin.push_text(f"⚠️ 自动游玩出了点小状况: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        logger.info("[mcagent] 自动游玩已结束")

    def start_auto(self):
        if self.running_mode:
            raise AgentError(f"当前已在运行: {self.running_mode}")
        self._stop_event = asyncio.Event()
        self.running_mode = "auto"
        self._auto_task = asyncio.create_task(self.auto_loop())

    async def stop_auto(self):
        self._stop_event.set()
        if self._auto_task:
            try:
                await asyncio.wait_for(self._auto_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._auto_task.cancel()
            self._auto_task = None
        self.running_mode = None

    # ---- 任务模式 ----

    async def run_task(self, goal: str):
        """带明确目标的任务循环，返回总结文本。"""
        if self.running_mode:
            raise AgentError(f"当前已在运行: {self.running_mode}，请先停止。")
        self.running_mode = "task"
        self.plugin.set_task_goal(goal)
        max_rounds = int((self.plugin.config.get("ai") or {}).get("task_max_rounds") or 15)
        try:
            await self.plugin.push_text(f"🎯 收到任务: {goal}\n开始执行，我会随时播报进度~")
            final_summary = ""
            for i in range(max_rounds):
                if not self.plugin.bridge_online():
                    final_summary = "连接中断，任务中止。"
                    break
                rnd = await self.run_round()
                await self.plugin.push_round(rnd)
                if rnd.get("task_done"):
                    final_summary = "✅ 任务完成！"
                    break
                await asyncio.sleep(2)
            else:
                final_summary = f"⏰ 已达到最大轮数({max_rounds})，任务暂停。可以继续用 /mc task 追加指令。"
            if not final_summary:
                final_summary = "任务结束。"
            await self.plugin.push_text(final_summary)
            return final_summary
        finally:
            self.running_mode = None
            self.plugin.set_task_goal(None)


# ---------------- LLM 工具（聊天中直接用自然语言控制机器人） ----------------

# pydantic dataclass 按开发指南的模式定义
from pydantic import Field  # noqa: E402
from pydantic.dataclasses import dataclass  # noqa: E402


@dataclass
class McStatusTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_status"
    description: str = "查询机器人在 Minecraft 里的实时状态：位置、生命值、周围环境、背包等。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        bridge = _RT.get("bridge")
        if not bridge:
            return "机器人未启动 Minecraft 模块"
        try:
            return await build_observation(bridge)
        except Exception as e:
            return f"查询失败: {e}"


@dataclass
class McChatTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_chat"
    description: str = "让机器人在 Minecraft 游戏内公屏说一句话。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "要在游戏内发送的话"},
            },
            "required": ["message"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        bridge = _RT.get("bridge")
        if not bridge:
            return "机器人未启动 Minecraft 模块"
        try:
            await bridge.request("chat", {"message": str(kwargs.get("message", ""))[:200]}, timeout=20)
            return f"已在游戏内发言: {kwargs.get('message')}"
        except Exception as e:
            return f"发言失败: {e}"


@dataclass
class McGotoTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_goto"
    description: str = "让机器人寻路走到 Minecraft 世界中的某个坐标附近（大范围移动用这个）。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "目标 X 坐标"},
                "y": {"type": "number", "description": "目标 Y 坐标"},
                "z": {"type": "number", "description": "目标 Z 坐标"},
            },
            "required": ["x", "y", "z"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        bridge = _RT.get("bridge")
        if not bridge:
            return "机器人未启动 Minecraft 模块"
        try:
            data = await bridge.request(
                "goto",
                {"x": kwargs.get("x"), "y": kwargs.get("y"), "z": kwargs.get("z"), "range": 2},
                timeout=130,
            )
            p = (data or {}).get("position") or {}
            return f"已走到 ({p.get('x')},{p.get('y')},{p.get('z')})"
        except Exception as e:
            return f"移动失败: {e}"


@dataclass
class McCollectTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_collect"
    description: str = "让机器人在附近采集若干个某种方块（自动寻路、挖掘并捡起掉落物），例如 oak_log、cobblestone、sand。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "方块英文名，如 oak_log"},
                "count": {"type": "number", "description": "数量，默认 1，最大 32"},
            },
            "required": ["block"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        bridge = _RT.get("bridge")
        if not bridge:
            return "机器人未启动 Minecraft 模块"
        try:
            data = await bridge.request(
                "collect",
                {"block": kwargs.get("block"), "count": int(kwargs.get("count") or 1)},
                timeout=210,
            )
            return f"采集完成: {(data or {}).get('collected')} 个 {kwargs.get('block')}"
        except Exception as e:
            return f"采集失败: {e}"


@dataclass
class McCraftTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_craft"
    description: str = "让机器人合成物品，例如 crafting_table、wooden_pickaxe、bread。材料不足或没有配方会失败。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "物品英文名"},
                "count": {"type": "number", "description": "数量，默认 1"},
            },
            "required": ["item"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        bridge = _RT.get("bridge")
        if not bridge:
            return "机器人未启动 Minecraft 模块"
        try:
            data = await bridge.request(
                "craft",
                {"item": kwargs.get("item"), "count": int(kwargs.get("count") or 1)},
                timeout=100,
            )
            return f"合成成功: {data.get('crafted')} x{data.get('count')}"
        except Exception as e:
            return f"合成失败: {e}"


@dataclass
class McInventoryTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_inventory"
    description: str = "查看机器人在 Minecraft 里的背包物品列表。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        bridge = _RT.get("bridge")
        if not bridge:
            return "机器人未启动 Minecraft 模块"
        try:
            data = await bridge.request("inventory", timeout=15)
            items = (data or {}).get("items") or []
            if not items:
                return "背包是空的"
            return "背包: " + ", ".join(f"{i['name']}x{i['count']}" for i in items[:15])
        except Exception as e:
            return f"查询失败: {e}"


@dataclass
class McScreenshotTool(FunctionTool[AstrAgentContext]):
    name: str = "mc_game_screenshot"
    description: str = "对 Minecraft 游戏画面截屏，并把截图发送到当前聊天会话。仅在机器人已进入服务器时可用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        plugin = _RT.get("plugin")
        if not plugin:
            return "机器人未启动 Minecraft 模块"
        try:
            path, err = await plugin.take_game_screenshot(push=False)
            if not path:
                return f"截屏失败: {err}"
            # 尝试发送到触发本工具的会话
            event = getattr(getattr(context, "context", None), "event", None)
            if event is not None:
                import astrbot.api.message_components as Comp
                from astrbot.api.event import MessageChain

                await event.send(
                    MessageChain().message("📸 游戏截图").file_image(path)
                )
                return "截图已发送"
            await plugin.push_image(path, "📸 游戏截图")
            return "截图已推送到订阅会话"
        except Exception as e:
            return f"截屏失败: {e}"


def get_all_tools() -> list:
    """返回插件注册的全部 LLM 工具实例。"""
    return [
        McStatusTool(),
        McChatTool(),
        McGotoTool(),
        McCollectTool(),
        McCraftTool(),
        McInventoryTool(),
        McScreenshotTool(),
    ]
