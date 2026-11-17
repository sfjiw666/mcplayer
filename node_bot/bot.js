// AstrBot Minecraft 玩家插件 - Node 桥接端
//
// 通过 stdin/stdout 与 Python 插件端通信（每行一个 JSON）：
//   Python -> Node: {"id": 1, "cmd": "goto", "args": {...}}
//   Node   -> Python: {"id": 1, "ok": true, "data": {...}} 或错误 {"id": 1, "ok": false, "error": "..."}
//   事件推送: {"event": "chat", "data": {...}}
//
// 注意：stdout 只允许输出协议 JSON，所有日志一律走 stderr（console.error）。
'use strict'

const readline = require('readline')
const Vec3Mod = require('vec3')
const Vec3 = Vec3Mod.Vec3 || Vec3Mod

// ---- 协议输出（stdout 专用）----
const STDOUT = process.stdout
function protoSend(obj) {
  try {
    STDOUT.write(JSON.stringify(obj) + '\n')
  } catch (e) {
    /* 管道关闭等情况，静默 */
  }
}
function reply(id, ok, data, error) {
  protoSend({ id, ok, data: data ?? null, error: error ?? null })
}
function emit(event, data) {
  protoSend({ event, data: data ?? null })
}

// mineflayer 内部偶尔会 console.log（比如微软登录链接），重定向到 stderr，避免污染协议通道
console.log = (...a) => console.error('[bot]', ...a)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---- 全局状态 ----
/** @type {import('mineflayer').Bot | null} */
let bot = null
let connectArgs = null // 最近一次连接参数（用于自动重连）
let connecting = false // 连接互斥保护
let viewerStarted = false
let busyAction = null // 当前占用型动作名
let busyCancelRequested = false
let followTimer = null
let currentOperation = null // { cancel: Function } 可取消句柄

function isOnline() {
  return !!(bot && bot.entity)
}

// ================= 连接管理 =================

async function startViewer(opts = {}) {
  if (viewerStarted || !bot) return
  const port = opts.port ?? 3000
  const viewDistance = opts.viewDistance ?? 6
  const firstPerson = opts.firstPerson ?? false
  try {
    const prismarineViewer = require('prismarine-viewer')
    prismarineViewer.mineflayer(bot, { port, viewDistance, firstPerson })
    viewerStarted = true
    console.error(`[viewer] 网页渲染器已启动: http://127.0.0.1:${port} (firstPerson=${firstPerson})`)
    emit('viewer', { enabled: true, port })
  } catch (e) {
    console.error('[viewer] 启动失败(不影响游戏):', e.message)
    emit('viewer', { enabled: false, error: String(e.message || e) })
  }
}

async function doConnect(args) {
  if (isOnline()) throw new Error('机器人已在线，请先 /mc leave')
  if (connecting) throw new Error('正在连接中，请稍候')
  connecting = true

  const mineflayer = require('mineflayer')
  const { pathfinder } = require('mineflayer-pathfinder')

  const settings = {
    host: args.host || '127.0.0.1',
    port: args.port || 25565,
    username: args.username || 'AstrBot',
    auth: args.auth === 'microsoft' ? 'microsoft' : 'offline',
    version: args.version && String(args.version).trim() ? String(args.version).trim() : false,
    hideErrors: false,
    checkTimeoutInterval: 60 * 1000,
  }
  if (args.auth === 'microsoft' && args.password) settings.password = args.password

  connectArgs = { ...args }

  bot = mineflayer.createBot(settings)
  bot.loadPlugin(pathfinder)

  // ---- 事件转发 ----
  bot.once('login', () => {
    console.error('[mc] 已登录:', bot.username)
    emit('login', { username: bot.username, host: settings.host, port: settings.port, version: bot.version })
  })

  bot.once('spawn', async () => {
    console.error('[mc] 已进入世界')
    emit('spawn', { position: posData() })
    if (args.viewer !== false) {
      await startViewer(args.viewerOptions || {})
    }
  })

  bot.on('chat', (username, message) => {
    if (!username || username === bot.username) return // 过滤自己的发言，避免回环
    emit('chat', { username, message: String(message) })
  })

  bot.on('systemMessage', (message) => {
    const text = String(message)
    if (text && text.length < 400) emit('system', { message: text })
  })

  bot.on('death', () => {
    console.error('[mc] 机器人死亡')
    emit('death', { position: posData() })
  })

  bot.on('health', () => {
    emit('health', { health: bot.health, food: bot.food })
  })

  bot.on('kicked', (reason) => {
    let reasonText = String(reason)
    try {
      reasonText = JSON.parse(reason)?.text || reasonText
    } catch (e) { /* 保持原文 */ }
    console.error('[mc] 被踢出:', reasonText)
    emit('kicked', { reason: reasonText })
  })

  bot.on('error', (err) => {
    const msg = String(err && err.message ? err.message : err)
    console.error('[mc] 错误:', msg)
    // 微软设备码登录：mineflayer 会把授权链接放进错误信息里
    const urlMatch = msg.match(/https:\/\/(?:www\.)?microsoft\.com\/link\S*/i)
    if (urlMatch) emit('login_url', { url: urlMatch[0] })
    emit('error', { message: msg })
  })

  bot.on('end', (reason) => {
    console.error('[mc] 连接结束:', reason)
    const wasOnline = isOnline()
    stopViewerLocal()
    emit('end', { reason: String(reason || ''), was_online: wasOnline })
  })

  // 等待 spawn（进服成功）或首报错误
  return await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup()
      releaseLock()
      reject(new Error('连接超时（60 秒未进入世界），请检查地址/端口/版本'))
    }, 60000)

    function releaseLock() {
      connecting = false
    }

    const onSpawn = () => {
      cleanup()
      releaseLock()
      resolve({ ok: true, username: bot.username, version: bot.version, position: posData() })
    }
    const onError = (err) => {
      cleanup()
      releaseLock()
      let msg = String(err && err.message ? err.message : err)
      if (/unsupported protocol/i.test(msg)) {
        const latest = latestPlayableVersions(1)[0] || '未知'
        msg += `。服务器版本过新: 当前 Node 依赖最高支持 ${latest}。` +
          `可在插件 node_bot 目录执行 npm update 升级依赖后重试(上游支持后即可进服)，或改用受支持版本的服务器`
      }
      reject(new Error(msg))
    }
    const onEnd = () => {
      cleanup()
      releaseLock()
      reject(new Error('连接被服务器关闭'))
    }
    function cleanup() {
      clearTimeout(timeout)
      bot?.removeListener('spawn', onSpawn)
      bot?.removeListener('error', onError)
      bot?.removeListener('end', onEnd)
    }
    bot.once('spawn', onSpawn)
    bot.once('error', onError)
    bot.once('end', onEnd)
  })
}

function stopViewerLocal() {
  viewerStarted = false
}

async function doDisconnect() {
  cancelCurrent()
  if (followTimer) {
    clearInterval(followTimer)
    followTimer = null
  }
  if (bot) {
    try {
      if (bot.viewer && bot.viewer.close) bot.viewer.close()
    } catch (e) { /* ignore */ }
    try {
      bot.quit()
    } catch (e) { /* ignore */ }
    bot = null
  }
  viewerStarted = false
  return { ok: true }
}

// ================= 基础查询 =================

function posData() {
  if (!isOnline()) return null
  const p = bot.entity.position
  return { x: +p.x.toFixed(1), y: +p.y.toFixed(1), z: +p.z.toFixed(1) }
}

function fmtPos(v) {
  return v ? `(${v.x}, ${v.y}, ${v.z})` : '未知'
}

function doStatus() {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const p = posData()
  const held = bot.entity.heldItem ?? bot.heldItem
  const yaw = bot.entity.yaw
  // mineflayer 的 yaw: -PI..PI，0 表示朝 +Z(南)。换算成东南西北
  const deg = ((yaw * 180) / Math.PI + 360) % 360
  const dirNames = ['南', '西南', '西', '西北', '北', '东北', '东', '东南']
  const dir = dirNames[Math.round(deg / 45) % 8]
  const players = Object.values(bot.players)
    .filter((pl) => pl.entity || pl.username === bot.username)
    .map((pl) => ({ username: pl.username, ping: pl.ping ?? 0 }))

  let biome = null
  try {
    const under = bot.blockAt(bot.entity.position)
    biome = under?.biome?.name ?? null
  } catch (e) { /* ignore */ }

  return {
    username: bot.username,
    position: p,
    health: Math.round((bot.health ?? 0) * 10) / 10,
    food: Math.round((bot.food ?? 0) * 10) / 10,
    gamemode: bot.game?.gameMode ?? null,
    dimension: bot.game?.dimension ?? null,
    biome,
    yaw: Math.round(deg),
    facing: dir,
    held_item: held ? `${held.displayName ?? held.name} x${held.count ?? 1}` : null,
    time_of_day: bot.time?.timeOfDay ?? null,
    day: bot.time?.day ?? null,
    is_raining: !!bot.isRaining,
    players_online: players,
    busy: busyAction,
  }
}

function doObserve(args = {}) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const pos = bot.entity.position
  const base = new Vec3(Math.floor(pos.x), Math.floor(pos.y), Math.floor(pos.z))

  // 视线方向的水平单位向量（mineflayer yaw: 0 = +Z）
  const yaw = bot.entity.yaw
  const fx = -Math.sin(yaw)
  const fz = -Math.cos(yaw)

  const front = (dy, dist) => {
    const b = bot.blockAt(new Vec3(Math.floor(pos.x + fx * dist), Math.floor(pos.y) + dy, Math.floor(pos.z + fz * dist)))
    return b ? b.name : null
  }

  // 采样周围方块类型
  const counts = {}
  const radius = Math.min(args.radius ?? 4, 8)
  for (let dy = -1; dy <= 2; dy++) {
    for (let dx = -radius; dx <= radius; dx += 2) {
      for (let dz = -radius; dz <= radius; dz += 2) {
        let b = null
        try {
          b = bot.blockAt(base.offset(dx, dy, dz))
        } catch (e) { /* ignore */ }
        if (b && b.name && b.name !== 'air' && b.name !== 'cave_air') {
          counts[b.name] = (counts[b.name] || 0) + 1
        }
      }
    }
  }
  const around = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12)
    .map(([name, n]) => `${name}x${n}`)

  // 附近实体
  const entities = Object.values(bot.entities)
    .filter((e) => e !== bot.entity && e.position && e.position.distanceTo(pos) < 24)
    .map((e) => ({
      name: e.name || e.displayName || e.type,
      kind: e.kind || e.type,
      distance: +e.position.distanceTo(pos).toFixed(1),
    }))
    .sort((a, b) => a.distance - b.distance)
    .slice(0, 8)

  return {
    position: posData(),
    facing: doStatus().facing,
    health: Math.round((bot.health ?? 0) * 10) / 10,
    food: Math.round((bot.food ?? 0) * 10) / 10,
    front_eye_level: front(0, 2),
    front_feet_level: front(-1, 2),
    front_ground: front(-1, 1),
    under_feet: (bot.blockAt(base.offset(0, -1, 0)) || {}).name ?? null,
    blocks_around: around,
    entities_nearby: entities,
    time_of_day: bot.time?.timeOfDay ?? null,
    light_level: (() => {
      try {
        return bot.blockAt(pos)?.light ?? null
      } catch (e) {
        return null
      }
    })(),
  }
}

function doInventory() {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const items = {}
  for (const item of bot.inventory.items()) {
    const key = item.name
    if (!items[key]) items[key] = { name: item.name, displayName: item.displayName ?? item.name, count: 0 }
    items[key].count += item.count ?? 1
  }
  const list = Object.values(items).sort((a, b) => b.count - a.count)
  return { items: list, total: list.reduce((s, i) => s + i.count, 0) }
}

function doPlayers() {
  if (!bot) throw new Error('机器人未连接服务器')
  return {
    players: Object.values(bot.players).map((p) => ({ username: p.username, gamemode: p.gamemode ?? null })),
  }
}

// ================= 动作实现 =================

function withBusy(name, fn) {
  if (busyAction) throw new Error(`当前正忙: ${busyAction}，请稍候或先发 stop`)
  busyAction = name
  busyCancelRequested = false
  return Promise.resolve()
    .then(fn)
    .finally(() => {
      busyAction = null
      busyCancelRequested = false
      currentOperation = null
    })
}

function cancelCurrent() {
  if (!bot) return
  busyCancelRequested = true
  try {
    // 停止移动控制
    for (const c of ['forward', 'back', 'left', 'right', 'jump', 'sprint', 'sneak']) {
      bot.setControlState(c, false)
    }
    bot.stopDigging()
    if (bot.pathfinder) bot.pathfinder.setGoal(null)
    if (currentOperation && currentOperation.cancel) currentOperation.cancel()
  } catch (e) { /* ignore */ }
}

function bestToolFor(block) {
  if (!block) return null
  const name = block.name || ''
  let want = null
  if (/stone|ore|obsidian|deepslate|netherrack|end_stone|iron_block|gold_block|diamond_block|lapis|anvil|rail/.test(name)) want = 'pickaxe'
  else if (/log|wood|planks|fence|door|slab|stairs|crafting_table|barrel|bookshelf|chest/.test(name)) want = 'axe'
  else if (/dirt|grass|sand|gravel|snow|clay|mud|soul_soil|soul_sand|podzol|farmland/.test(name)) want = 'shovel'
  else if (/leaves|wool/.test(name)) want = 'shears'
  if (!want) return null
  return bot.inventory.items().find((i) => i.name.includes(want)) || null
}

async function equipBestTool(block) {
  const tool = bestToolFor(block)
  if (tool) {
    try {
      await bot.equip(tool, 'hand')
    } catch (e) { /* 装备失败就徒手挖 */ }
  }
}

async function doGoto(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const { Movements, goals } = require('mineflayer-pathfinder')

  let movements
  try {
    movements = new Movements(bot)
  } catch (e) {
    movements = new Movements(bot, bot.registry)
  }
  bot.pathfinder.setMovements(movements)

  const range = Math.max(0, Math.min(args.range ?? 1, 8))
  const goal = new goals.GoalNear(args.x, args.y, args.z, range)
  const timeoutSec = Math.min(args.timeout ?? 120, 300)

  return await new Promise((resolve, reject) => {
    let done = false
    const timeout = setTimeout(() => {
      finish(reject, new Error(`寻路超时(${timeoutSec}s)，当前 ${fmtPos(posData())}`))
    }, timeoutSec * 1000)

    currentOperation = {
      cancel: () => finish(reject, new Error('已取消')),
    }

    function finish(cb, arg) {
      if (done) return
      done = true
      clearTimeout(timeout)
      bot.removeListener('goal_reached', onReached)
      if (bot.pathfinder) bot.pathfinder.removeListener('path_update', onPath)
      cb(arg)
    }
    function onReached() {
      finish(resolve, { position: posData() })
    }
    function onPath(res) {
      if (res && res.status === 'noPath') {
        finish(reject, new Error('找不到可行路径（可能太远或被地形阻挡）'))
      } else if (res && res.status === 'noPathToGoal') {
        // 尽力而为：如果已经很近就当作成功
        const p = posData()
        const d = Math.hypot(p.x - args.x, p.y - args.y, p.z - args.z)
        if (d <= Math.max(3, range + 2)) finish(resolve, { position: p, approx: true })
        else finish(reject, new Error('无法规划到目标的完整路径'))
      }
    }

    bot.once('goal_reached', onReached)
    bot.pathfinder.on('path_update', onPath)
    try {
      bot.pathfinder.setGoal(goal)
    } catch (e) {
      finish(reject, e)
    }
  })
}

async function doMove(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const direction = args.direction || 'forward'
  const valid = { forward: 'forward', back: 'back', left: 'left', right: 'right' }
  const control = valid[direction]
  if (!control) throw new Error('direction 必须是 forward/back/left/right')
  const durationMs = Math.max(100, Math.min(args.duration_ms ?? 1000, 10000))

  if (args.yaw !== undefined && args.pitch !== undefined) {
    bot.entity.yaw = +args.yaw
    bot.entity.pitch = +args.pitch
  }
  bot.setControlState(control, true)
  if (args.sprint) bot.setControlState('sprint', true)
  if (args.jump) bot.setControlState('jump', true)
  await sleep(durationMs)
  cancelCurrent()
  return { position: posData() }
}

async function doLook(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  if (args.x !== undefined) {
    await bot.lookAt(new Vec3(args.x, args.y, args.z), true)
  } else if (args.yaw !== undefined) {
    await bot.look(+args.yaw, +(args.pitch ?? 0), true)
  } else {
    throw new Error('需要 (x,y,z) 或 yaw/pitch')
  }
  return { yaw: Math.round(((bot.entity.yaw * 180) / Math.PI + 360) % 360) }
}

async function doDig(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const target = new Vec3(Math.floor(args.x), Math.floor(args.y), Math.floor(args.z))
  const block = bot.blockAt(target)
  if (!block) throw new Error('目标方块未加载（距离太远）')
  if (block.name === 'air' || block.name === 'cave_air') throw new Error(`目标处没有方块(${block.name})`)
  if (target.distanceTo(bot.entity.position) > 5.5) throw new Error('距离目标方块太远，请先靠近 (<= 5 格)')

  await equipBestTool(block)
  const timeoutSec = Math.min(args.timeout ?? 30, 120)
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('挖掘超时')), timeoutSec * 1000)
    currentOperation = { cancel: () => { clearTimeout(t); reject(new Error('已取消')) } }
    bot
      .dig(block)
      .then(() => { clearTimeout(t); resolve() })
      .catch((e) => { clearTimeout(t); reject(e) })
  })
  return { dug: block.name, position: posData() }
}

async function doDigDown(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const count = Math.max(1, Math.min(args.count ?? 1, 5))
  const dug = []
  const start = bot.entity.position
  for (let i = 0; i < count; i++) {
    const target = new Vec3(Math.floor(start.x), Math.floor(start.y) - 1 - i, Math.floor(start.z))
    const block = bot.blockAt(target)
    if (!block || block.name === 'air' || block.name === 'bedrock') break
    await equipBestTool(block)
    await bot.dig(block)
    dug.push(block.name)
    await sleep(200)
  }
  return { dug, position: posData() }
}

async function doPlace(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const target = new Vec3(Math.floor(args.x), Math.floor(args.y), Math.floor(args.z))
  const existing = bot.blockAt(target)
  if (existing && existing.boundingBox === 'block') throw new Error(`目标位置已有方块(${existing.name})`)

  // 找一个相邻的实体方块作为放置参考面
  const dirs = [
    [1, 0, 0], [-1, 0, 0],
    [0, 1, 0], [0, -1, 0],
    [0, 0, 1], [0, 0, -1],
  ]
  let ref = null
  let face = null
  for (const d of dirs) {
    const b = bot.blockAt(target.offset(d[0], d[1], d[2]))
    if (b && b.boundingBox !== 'empty') {
      ref = b
      face = new Vec3(-d[0], -d[1], -d[2])
      break
    }
  }
  if (!ref) throw new Error('目标位置没有相邻的实体方块可以贴放')

  const itemName = args.block
  let item = null
  if (itemName) {
    item =
      bot.inventory.items().find((i) => i.name === itemName) ||
      bot.inventory.items().find((i) => i.name.includes(itemName))
    if (!item) throw new Error(`背包里没有 ${itemName}`)
  } else {
    item = bot.inventory.items()[0]
    if (!item) throw new Error('背包是空的')
  }

  await bot.equip(item, 'hand')
  await bot.lookAt(target.offset(0.5, 0.5, 0.5), true)
  try {
    await bot.placeBlock(ref, face)
  } catch (e) {
    throw new Error(`放置失败: ${e.message}`)
  }
  await sleep(200)
  return { placed: item.name, at: { x: args.x, y: args.y, z: args.z } }
}

async function doAttack(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const times = Math.max(1, Math.min(args.times ?? 5, 20))
  let targetName = (args.target || 'nearest_hostile').toLowerCase()

  // 优先拿剑/斧当武器
  const weapon =
    bot.inventory.items().find((i) => i.name.includes('sword')) ||
    bot.inventory.items().find((i) => i.name.includes('axe'))
  if (weapon) {
    try { await bot.equip(weapon, 'hand') } catch (e) { /* ignore */ }
  }

  const hits = []
  for (let i = 0; i < times; i++) {
    let ent = null
    for (const e of Object.values(bot.entities)) {
      if (e === bot.entity || !e.position) continue
      const dist = e.position.distanceTo(bot.entity.position)
      if (dist > 4) continue
      if (targetName === 'nearest_hostile') {
        if (e.kind && String(e.kind).includes('Hostile')) { ent = e; break }
      } else if ((e.name || '').toLowerCase().includes(targetName)) {
        ent = e
        break
      }
    }
    if (!ent) break
    try {
      await bot.lookAt(ent.position.offset(0, (ent.height || 1) * 0.9, 0), true)
      bot.attack(ent)
      hits.push(ent.name || ent.displayName || ent.type)
    } catch (e) { /* ignore */ }
    await sleep(600)
    if (ent.isValid === false) break
  }
  if (!hits.length) throw new Error('附近 4 格内没有找到攻击目标')
  return { attacked: hits.length, targets: [...new Set(hits)], position: posData() }
}

async function doCollect(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const name = String(args.block || args.item || '').toLowerCase()
  if (!name) throw new Error('需要 block 参数（要收集的方块名）')
  const wantCount = Math.max(1, Math.min(args.count ?? 1, 32))
  const radius = Math.max(8, Math.min(args.radius ?? 32, 64))

  const mcData = require('minecraft-data')(bot.version)
  let blockData = mcData.blocksByName[name]
  if (!blockData) {
    blockData = Object.values(mcData.blocksByName).find((b) => b.name.includes(name))
  }
  if (!blockData) throw new Error(`未知方块: ${name}`)

  const { Movements, goals } = require('mineflayer-pathfinder')
  let movements
  try { movements = new Movements(bot) } catch (e) { movements = new Movements(bot, bot.registry) }
  movements.canDig = true
  bot.pathfinder.setMovements(movements)

  let collected = 0
  const deadline = Date.now() + Math.min(args.timeout ?? 180, 420) * 1000
  currentOperation = { cancel: () => {} } // 通过 busyCancelRequested 在循环里软取消

  for (let i = 0; i < wantCount; i++) {
    if (busyCancelRequested || Date.now() > deadline) break
    const found = bot.findBlock({ matching: blockData.id, maxDistance: radius, count: 1 })
    if (!found) break
    // 走到方块旁
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => { cleanup(); resolve() /* 尽力而为，直接尝试挖 */ }, 60 * 1000)
      function cleanup() { clearTimeout(t); bot.removeListener('goal_reached', onReach) }
      function onReach() { cleanup(); resolve() }
      bot.once('goal_reached', onReach)
      try {
        bot.pathfinder.setGoal(new goals.GoalNear(found.position.x, found.position.y, found.position.z, 2))
      } catch (e) { cleanup(); resolve() }
    })
    if (busyCancelRequested) break
    // 挖
    const fresh = bot.blockAt(found.position)
    if (!fresh || fresh.name !== blockData.name) continue
    await equipBestTool(fresh)
    try {
      await bot.dig(fresh)
      collected++
    } catch (e) { continue }
    await sleep(400)
  }

  // 等掉落物捡起
  await sleep(1200)
  if (!collected) throw new Error(`周围 ${radius} 格内没有找到可采集的 ${name}`)
  return { collected, wanted: wantCount, position: posData() }
}

async function doCraft(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const itemName = String(args.item || '').toLowerCase()
  const count = Math.max(1, Math.min(args.count ?? 1, 64))
  const mcData = require('minecraft-data')(bot.version)

  let itemData = mcData.itemsByName[itemName]
  if (!itemData) {
    itemData = Object.values(mcData.itemsByName).find((i) => i.name.includes(itemName))
  }
  if (!itemData) throw new Error(`未知物品: ${itemName}`)

  // 找附近的工作台（16 格内），需要的话走过去
  let table = null
  try {
    table = bot.findBlock({ matching: mcData.blocksByName.crafting_table.id, maxDistance: 16 })
  } catch (e) { table = null }
  if (table) {
    const dist = table.position.distanceTo(bot.entity.position)
    if (dist > 3) {
      await doGoto({ x: table.position.x, y: table.position.y, z: table.position.z, range: 2, timeout: 60 })
    }
  }

  const recipes = bot.recipesFor(itemData.id, null, 1, table)
  if (!recipes.length) {
    throw new Error(
      `没有 ${itemName} 的可用配方（可能缺少材料${table ? '' : '，或需要站在工作台旁边（16格内没找到工作台）'}）`
    )
  }
  try {
    await bot.craft(recipes[0], count, table)
  } catch (e) {
    throw new Error(`合成失败: ${e.message}`)
  }
  return { crafted: itemName, count, used_table: !!table }
}

async function doEquip(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const name = String(args.item || '').toLowerCase()
  const item =
    bot.inventory.items().find((i) => i.name === name) ||
    bot.inventory.items().find((i) => i.name.includes(name))
  if (!item) throw new Error(`背包里没有 ${name}`)
  await bot.equip(item, args.slot || 'hand')
  return { equipped: item.name }
}

async function doEat() {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const foods = bot.inventory.items().filter((i) => /apple|bread|cooked_|porkchop|beef|chicken|mutton|carrot|potato|beetroot|melon_slice|cookie|sweet_berries|golden_|cod|salmon|rabbit|soup|stew|rabbit_stew/.test(i.name))
  if (!foods.length) throw new Error('背包里没有可食用的食物')
  const food = foods[0]
  await bot.equip(food, 'hand')
  try {
    await bot.consume()
  } catch (e) {
    throw new Error(`进食失败: ${e.message}`)
  }
  return { ate: food.name, health: Math.round((bot.health ?? 0) * 10) / 10, food: Math.round((bot.food ?? 0) * 10) / 10 }
}

async function doToss(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const name = String(args.item || '').toLowerCase()
  const count = Math.max(1, Math.min(args.count ?? 1, 64))
  const item =
    bot.inventory.items().find((i) => i.name === name) ||
    bot.inventory.items().find((i) => i.name.includes(name))
  if (!item) throw new Error(`背包里没有 ${name}`)
  await bot.toss(item.type, null, Math.min(count, item.count ?? count))
  return { tossed: item.name, count: Math.min(count, item.count ?? count) }
}

function doFollow(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const username = args.username
  const range = Math.max(1, Math.min(args.range ?? 3, 16))
  const target = bot.players[username]
  if (!target || !target.entity) throw new Error(`没找到玩家 ${username}（或不在视野内）`)

  const { goals } = require('mineflayer-pathfinder')
  if (followTimer) clearInterval(followTimer)
  const { Movements } = require('mineflayer-pathfinder')
  let movements
  try { movements = new Movements(bot) } catch (e) { movements = new Movements(bot, bot.registry) }
  bot.pathfinder.setMovements(movements)

  followTimer = setInterval(() => {
    const t = bot.players[username]
    if (!t || !t.entity) return
    const p = t.entity.position
    try {
      bot.pathfinder.setGoal(new goals.GoalNear(p.x, p.y, p.z, range), true) // dynamic=true 允许动态目标
    } catch (e) { /* ignore */ }
  }, 800)
  followTimer.unref?.()

  return { following: username, range }
}

function doFollowStop() {
  if (followTimer) {
    clearInterval(followTimer)
    followTimer = null
  }
  if (bot && bot.pathfinder) bot.pathfinder.setGoal(null)
  return { stopped: true }
}

async function doRespawn() {
  if (!bot) throw new Error('机器人未连接服务器')
  if (bot.health > 0) throw new Error('机器人还活着，不需要重生')
  // 版本相关的包字段，多试几种
  const attempts = [
    () => bot._client.write('client_command', { actionId: 0, actionId_2: 0 }),
    () => bot._client.write('client_command', { actionId: 0 }),
    () => bot._client.write('client_command', { action: 0 }),
  ]
  let lastErr = null
  for (const fn of attempts) {
    try {
      fn()
      await sleep(800)
      return { respawned: true, health: Math.round((bot.health ?? 0) * 10) / 10 }
    } catch (e) {
      lastErr = e
    }
  }
  throw new Error(`重生失败: ${lastErr ? lastErr.message : 'unknown'}`)
}

async function doJump(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const times = Math.max(1, Math.min(args.times ?? 1, 10))
  for (let i = 0; i < times; i++) {
    bot.setControlState('jump', true)
    await sleep(250)
    bot.setControlState('jump', false)
    await sleep(150)
  }
  return { position: posData() }
}

async function doMap(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const { renderTopDown } = require('./lib/topdown')
  const result = renderTopDown(bot, {
    radius: args.radius ?? 16,
    scale: args.scale ?? 8,
    timeBudgetMs: args.time_budget_ms ?? 25000,
  })
  return {
    png_base64: result.png.toString('base64'),
    center: result.center,
    radius: result.radius,
    truncated: result.truncated,
  }
}

async function doChat(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const message = String(args.message || '')
  if (!message) throw new Error('消息不能为空')
  if (message.length > 256) throw new Error('消息太长（>256 字符）')
  bot.chat(message)
  return { sent: true }
}

async function doCommand(args) {
  if (!isOnline()) throw new Error('机器人未连接服务器')
  const command = String(args.command || '')
  if (!command.startsWith('/')) throw new Error('命令必须以 / 开头')
  bot.chat(command)
  return { sent: true }
}

// 上游实际可用的正式版本（过滤快照/rc），供诊断版本兼容问题
function latestPlayableVersions(count = 8) {
  try {
    const mcData = require('minecraft-data')
    const list = (mcData.supportedVersions?.pc || []).filter((v) => !/[a-zA-Z]/.test(v))
    return list.slice(-count).reverse()
  } catch (e) {
    return []
  }
}

// ================= 命令分发 =================

const HANDLERS = {
  ping: async () => ({
    alive: true,
    online: isOnline(),
    username: bot?.username ?? null,
    viewer: viewerStarted,
    busy: busyAction,
  }),
  versions: async () => ({
    latest_supported: latestPlayableVersions(8),
    viewer_supported: (() => {
      try {
        return require('prismarine-viewer').supportedVersions.slice(-6)
      } catch (e) {
        return []
      }
    })(),
  }),
  connect: doConnect,
  disconnect: doDisconnect,
  status: async () => doStatus(),
  observe: async (a) => doObserve(a),
  inventory: async () => doInventory(),
  players: async () => doPlayers(),
  chat: (a) => doChat(a),
  command: (a) => doCommand(a),
  goto: (a) => withBusy('goto', () => doGoto(a)),
  move: (a) => withBusy('move', () => doMove(a)),
  look: (a) => doLook(a),
  dig: (a) => withBusy('dig', () => doDig(a)),
  dig_down: (a) => withBusy('dig_down', () => doDigDown(a)),
  place: (a) => withBusy('place', () => doPlace(a)),
  attack: (a) => withBusy('attack', () => doAttack(a)),
  collect: (a) => withBusy('collect', () => doCollect(a)),
  craft: (a) => withBusy('craft', () => doCraft(a)),
  equip: (a) => doEquip(a),
  eat: (a) => withBusy('eat', () => doEat()),
  toss: (a) => doToss(a),
  follow: (a) => doFollow(a),
  follow_stop: (a) => doFollowStop(),
  respawn: (a) => doRespawn(),
  jump: (a) => withBusy('jump', () => doJump(a)),
  map: (a) => withBusy(null, () => doMap(a)),
  stop: async () => {
    cancelCurrent()
    doFollowStop()
    return { stopped: true }
  },
}

async function handle(msg) {
  const { id, cmd, args } = msg
  if (!id || !cmd) return
  const handler = HANDLERS[cmd]
  if (!handler) {
    reply(id, false, null, `未知命令: ${cmd}`)
    return
  }
  try {
    const data = await handler(args || {})
    if (cmd === 'stop') doFollowStop()
    reply(id, true, data, null)
  } catch (e) {
    reply(id, false, null, String((e && e.message) || e))
  }
}

// ---- stdin 协议循环 ----
const rl = readline.createInterface({ input: process.stdin, terminal: false })
rl.on('line', (line) => {
  const text = line.trim()
  if (!text) return
  let msg
  try {
    msg = JSON.parse(text)
  } catch (e) {
    console.error('[protocol] 无法解析的行:', text.slice(0, 200))
    return
  }
  if (msg.cmd === 'shutdown') {
    doDisconnect().finally(() => process.exit(0))
    return
  }
  handle(msg).catch((e) => {
    console.error('[handler] 异常:', e)
    if (msg.id) reply(msg.id, false, null, String(e))
  })
})
rl.on('close', () => {
  // Python 端退出：清场并自杀，防止孤儿进程
  console.error('[bridge] stdin 已关闭，准备退出')
  try {
    doDisconnect().finally(() => process.exit(0))
  } catch (e) {
    process.exit(0)
  }
  setTimeout(() => process.exit(0), 3000).unref()
})

process.on('uncaughtException', (e) => {
  console.error('[uncaught]', e)
})
process.on('unhandledRejection', (e) => {
  console.error('[unhandled]', e)
})

console.error('[bridge] Node 桥接端已启动，等待指令...')
emit('ready', { pid: process.pid, node: process.version })
