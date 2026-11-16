# astrbot_plugin_mcplayer 🎮⛏️

让 AstrBot 机器人**真正进入 Minecraft 服务器游玩**：走路、挖矿、建造、打怪、合成、生存，
游玩过程自动**截屏发到群里或私信里**，像看直播一样围观机器人玩 Minecraft。

> 灵感与依赖：机器人客户端基于 [mineflayer](https://github.com/PrismarineJS/mineflayer)，
> 画面渲染基于 [prismarine-viewer](https://github.com/PrismarineJS/prismarine-viewer)，
> 感谢 PrismarineJS 社区的开源工作。

---

## 工作原理

```
QQ/微信/TG/Discord... ──► AstrBot 插件 (Python)
                            │  stdin/stdout JSON 行协议
                            ▼
                     Node.js 子进程 (mineflayer 真实客户端)
                            │  加入服务器，执行动作
                            ▼
                prismarine-viewer 网页渲染器(仅本机监听)
                            │
                            ▼
              Playwright(AstrBot 自带) 打开页面 → 截屏 → 发回群/私信
```

- 插件端负责：指令解析、AI 决策（LLM）、消息推送、多平台适配。
- Node 端负责：真实进入服务器、寻路/挖掘/放置/合成等游戏动作、世界数据读取。

## 环境要求

| 依赖 | 说明 |
| --- | --- |
| AstrBot | >= v4.5.7 |
| Node.js | **>= 16**，需 `node`/`npm` 在 PATH 中（截屏渲染不依赖任何原生模块） |
| Minecraft 服务器 | 任意可加入的服务器（离线模式或正版服务器） |
| LLM 提供商 | 使用 AI 游玩（auto/task）时需要，支持 function-calling 的模型；视觉决策需支持图片输入 |

Python 无需额外依赖：截屏用的 Playwright 是 AstrBot 本体自带的。

## 安装

1. 将本仓库放入 `AstrBot/data/plugins/`（或通过 WebUI 插件市场安装）。
2. 首次 `/mc join` 时会自动执行 `npm install` 安装 Node 依赖（约 1-3 分钟）。
3. 在 WebUI 的插件配置里填好服务器地址、端口、游戏 ID（默认 127.0.0.1:25565 / AstrBot）。

## 快速开始

```
/mc join                  # 机器人进入服务器(管理员)；也可 /mc join mc.example.com 25565 Steve
/mc status                # 查看状态
/mc shot                  # 截一张当前画面发到群里
/mc task 砍10个木头并合成一把木镐     # 给 AI 布置任务，它会边做边播报
/mc auto on               # 自由游玩模式：自己探索、解说、定时截屏(管理员)
/mc bind                  # 把当前群/私信订阅为截屏接收地(任何人可订阅自己的会话)
```

也可以在机器人进服后直接 **@机器人 用自然语言指挥**（LLM 工具）：
"@机器人 去挖10个圆石" "@机器人 在游戏里说大家好" "@机器人 截个屏看看现在在哪"。

## 指令一览

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/mc join [地址] [端口] [ID]` | 进入服务器 | 管理员 |
| `/mc leave` | 离开服务器 | 管理员 |
| `/mc auto on/off` | AI 自由游玩开关 | 管理员 |
| `/mc cmd /<命令>` | 以机器人身份执行服务器命令 | 管理员 |
| `/mc task <目标>` | AI 任务模式 | 所有人 |
| `/mc shot` | 游戏画面截屏 | 所有人 |
| `/mc map` | 周围地形俯视图(兜底方案，必成功) | 所有人 |
| `/mc bind` / `/mc unbind` | 订阅/退订本会话的播报与截屏 | 所有人 |
| `/mc status` / `players` / `inventory` | 状态/在线玩家/背包 | 所有人 |
| `/mc say <话>` | 游戏内发言 | 所有人 |
| `/mc goto x y z` | 寻路到坐标 | 所有人 |
| `/mc move 前/后/左/右 [毫秒]` | 朝向移动 | 所有人 |
| `/mc look x y z` | 转头看向坐标 | 所有人 |
| `/mc dig x y z` | 挖指定坐标方块 | 所有人 |
| `/mc place x y z [方块]` | 放置方块 | 所有人 |
| `/mc collect 方块名 [数量]` | 自动采集(寻路+挖掘+捡起) | 所有人 |
| `/mc craft 物品 [数量]` | 合成(自动找附近工作台) | 所有人 |
| `/mc attack [目标] [次数]` | 攻击附近生物 | 所有人 |
| `/mc eat` / `respawn` | 吃东西 / 重生 | 所有人 |

## 截屏发到群里或私信里

三种方式：

1. **订阅会话**：在任何群聊或私聊里发 `/mc bind`，机器人的游玩解说、事件(死亡/掉线)、
   AI 每轮截屏都会主动推送到那里。配置页里的"推送会话"可批量选择已有会话。
2. **随指令发送**：`/mc shot` 直接在当前会话返回截图。
3. **AI 游玩播报**：`/mc auto on` 后每轮行动附带截屏 + 中文解说。

截屏方案自动降级：网页渲染(真实游戏画面) → 俯视地图(纯数据绘制，任何环境都能出图)。

## AI 游玩模式说明

- **自动模式** `/mc auto on`：以配置里的"长期目标"自由游玩，每个间隔(默认25s)一轮：
  截屏 → 观察(位置/血量/背包/周围方块/实体) → LLM 决策 JSON 计划 → 执行 ≤3 个动作 → 解说+截屏。
- **任务模式** `/mc task <目标>`：同样循环，但以完成任务为终止条件(有最大轮数保护)。
- 若配置了**视觉模型**且开启"把截屏发给模型"，LLM 能真正"看见"游戏画面来做决策。
- 请注意 token 消耗：每轮包含一次 LLM 请求(视觉时含图片)。建议用便宜或本地模型。

## 多平台适配

插件在 metadata.yaml 声明支持全部 AstrBot 平台适配器。适配细节：

| 能力 | 说明 |
| --- | --- |
| 图片发送 | 使用 `Image.fromFileSystem`，AstrBot 自动按平台转 base64/URL |
| 主动推送 | `/mc bind` 的截屏推送走 `context.send_message`；**qq_official(QQ官方机器人) 不支持主动消息**，此类平台请用 `/mc shot` 被动获取 |
| 交互按钮 | 状态栏按钮在不支持回调按钮的平台(OneBot v11 等)自动显示文字回退提示 |
| 消息长度 | 所有推送统一截断(1600字)，避免超限 |
| 指令唤醒 | 需按各平台配置的唤醒前缀(默认 `/`) |

## 配置

所有配置都可在 WebUI 插件配置页修改，含：服务器连接(离线/微软正版)、渲染器、截屏尺寸与视角、
AI 模型选择(`select_provider`)、推送会话(`umo` 多选)、额外管理员(`sender_id`)、自动重连等。

## 常见问题

- **提示找不到 node / npm**：安装 Node.js LTS 版本，或配置"node 可执行文件路径"。
- **截屏全黑**：无 GPU 环境用 SwiftShader 软渲染，个别机器较慢，把配置里的"渲染等待时长"调大
  (如 15000ms)；或直接用 `/mc map` 俯视图。
- **提示缺少浏览器**：在 AstrBot 环境执行 `playwright install chromium`。
- **进服超时**：检查地址端口；正版服务器需把登录方式改为 microsoft 并能访问微软服务。
- **AI 动作失败**：多数是地形/距离/材料问题，LLM 会读到失败原因并在下一轮调整；也可手动
  `/mc goto` 靠近后再试。
- **npm install 失败**：检查网络/代理；也可手动在插件 `node_bot/` 目录执行 `npm install`。

## 安全提示

- `/mc cmd` 可以让机器人执行任意服务器命令，仅限管理员，请勿泄露给不受信任的成员。
- 机器人会占用服务器的一个玩家位；请在遵守目标服务器规则的前提下使用。
- 建议给 AI 配置消耗限额的模型，auto 模式会周期性调用 LLM。

## 目录结构

```
astrbot_plugin_mcplayer/
├── main.py            # 插件入口：指令/事件/推送/多平台适配
├── mcbridge.py        # Node 子进程管理 + JSON 行协议 RPC
├── mcagent.py         # AI 游玩智能体(观察-决策-行动循环) + LLM 工具
├── mcrender.py        # Playwright 游戏截屏
├── _conf_schema.json  # WebUI 配置
└── node_bot/          # Node 桥接端(mineflayer)
    ├── bot.js         # 游戏客户端与动作实现
    └── lib/           # 纯JS PNG编码器 + 俯视地图渲染
```

## License

MIT
