// 俯视地图渲染器 v2：以机器人为中心渲染周围地形。
// v2 增强：
//   1. 性能：按区块缓存 Chunk 对象 + 空 Section 快速跳过(区块未加载列直接跳过整列)
//   2. 视觉：基于相邻列高差的伪立体光照、水体深度暗化、朝向箭头罗盘、加载区棋盘格提示
//   3. 元数据：返回已加载列比例，供上层在"区块未加载"时给出友好提示
// 纯数据实现(不依赖 WebGL/canvas)，任何环境都能出图。
'use strict'

const { encodePNG } = require('./png')

// Vec3 兼容获取(bot.blockAt 需要)
let Vec3 = null
try {
  const v = require('vec3')
  Vec3 = v.Vec3 || v
} catch (e) {
  Vec3 = null
}

// 常见方块基色(近似)，未收录的按灰阶处理
const BLOCK_COLORS = {
  grass_block: [98, 158, 68],
  dirt: [134, 96, 67],
  coarse_dirt: [119, 85, 59],
  rooted_dirt: [110, 80, 55],
  podzol: [91, 66, 39],
  farmland: [143, 104, 72],
  stone: [125, 125, 125],
  cobblestone: [127, 127, 127],
  deepslate: [80, 80, 84],
  cobbled_deepslate: [90, 90, 94],
  bedrock: [85, 85, 85],
  gravel: [127, 124, 123],
  sand: [219, 207, 163],
  red_sand: [190, 102, 33],
  sandstone: [216, 203, 155],
  water: [63, 96, 198],
  lava: [207, 91, 19],
  ice: [145, 183, 253],
  packed_ice: [141, 180, 250],
  snow: [249, 254, 254],
  snow_block: [249, 254, 254],
  oak_leaves: [72, 112, 48],
  spruce_leaves: [61, 89, 61],
  birch_leaves: [104, 130, 70],
  jungle_leaves: [70, 122, 32],
  leaves: [72, 112, 48],
  oak_log: [102, 81, 49],
  spruce_log: [58, 37, 16],
  birch_log: [215, 203, 143],
  jungle_log: [87, 113, 43],
  oak_planks: [162, 130, 78],
  spruce_planks: [114, 84, 48],
  birch_planks: [192, 175, 121],
  crafting_table: [119, 84, 48],
  furnace: [110, 110, 110],
  chest: [162, 114, 32],
  glass: [200, 230, 240],
  iron_ore: [138, 127, 118],
  deepslate_iron_ore: [95, 92, 94],
  gold_ore: [140, 127, 103],
  deepslate_gold_ore: [91, 84, 74],
  diamond_ore: [109, 138, 133],
  deepslate_diamond_ore: [82, 100, 103],
  coal_ore: [105, 105, 105],
  netherrack: [97, 38, 38],
  end_stone: [219, 222, 158],
  obsidian: [21, 18, 30],
  mycelium: [111, 98, 90],
  clay: [160, 166, 179],
  mud: [60, 57, 62],
  moss_block: [88, 121, 48],
  pumpkin: [198, 113, 25],
  melon: [111, 144, 32],
  hay_block: [166, 141, 26],
  tnt: [174, 63, 44],
  nether_portal: [104, 46, 158],
  planks: [162, 130, 78],
  log: [102, 81, 49],
}

// 透明/空气类方块，扫描高度时跳过
const TRANSPARENT = new Set([
  'air', 'cave_air', 'void_air', 'light', 'torch', 'wall_torch', 'soul_torch',
  'redstone_wire', 'grass', 'tall_grass', 'fern', 'large_fern', 'dead_bush',
  'dandelion', 'poppy', 'blue_orchid', 'allium', 'azure_bluet', 'oxeye_daisy',
  'cornflower', 'lily_of_the_valley', 'wither_rose', 'sunflower', 'lilac',
  'rose_bush', 'peony', 'sugar_cane', 'wheat', 'carrots', 'potatoes', 'beetroots',
  'flowering_azalea', 'brown_mushroom', 'red_mushroom', 'rail', 'powered_rail',
  'detector_rail', 'activator_rail', 'ladder', 'lever', 'tripwire', 'vine',
  'glow_lichen', 'short_grass', 'fire', 'soul_fire', 'cobweb',
])

// 3x5 像素字模(罗盘 "N")
const GLYPH_N = [
  [1, 0, 1],
  [1, 1, 1],
  [1, 1, 1],
  [1, 0, 1],
  [1, 0, 1],
]

/**
 * @param {import('mineflayer').Bot} bot
 * @param {{radius?: number, scale?: number, timeBudgetMs?: number}} opts
 * @returns {Promise<{png: Buffer, center, radius, loadedRatio: number, truncated: boolean}>}
 */
async function renderTopDown (bot, opts = {}) {
  const radius = Math.max(4, Math.min(48, opts.radius ?? 20))
  const scale = Math.max(2, Math.min(16, opts.scale ?? 7))
  const size = radius * 2 + 1
  const width = size * scale
  const height = size * scale

  const minY = typeof bot.game?.minY === 'number' ? bot.game.minY : -64
  const worldHeight = typeof bot.game?.height === 'number' ? bot.game.height : 384
  const maxY = minY + worldHeight

  const px = Buffer.alloc(width * height * 4, 255)
  const botX = Math.floor(bot.entity.position.x)
  const botY = Math.floor(bot.entity.position.y)
  const botZ = Math.floor(bot.entity.position.z)

  // 只扫机器人物理附近的高度带：上方少许(树冠/屋檐) + 下方较多(地形起伏)
  const scanTop = Math.min(maxY - 1, botY + 20)
  const scanBottom = Math.max(minY, botY - 48)
  const deadline = Date.now() + Math.min(opts.timeBudgetMs ?? 20000, 35000)

  const heightMap = new Int16Array(size * size).fill(-32000)
  const colorMap = new Array(size * size).fill(null)
  const loadedMap = new Uint8Array(size * size)
  let hMin = 32000
  let hMax = -32000
  let loadedColumns = 0
  let truncated = false

  // ---- 预取区块列：mineflayer 的 world.getColumn 返回 Promise，必须异步预取 ----
  const chunkCache = new Map() // "cx,cz" -> column 或 null
  const keys = new Set()
  for (let dz = -radius; dz <= radius; dz++) {
    for (let dx = -radius; dx <= radius; dx++) {
      const cx = (botX + dx) >> 4
      const cz = (botZ + dz) >> 4
      keys.add(cx + ',' + cz)
    }
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  await Promise.race([
    Promise.all(
      [...keys].map((k) => {
        const [cx, cz] = k.split(',').map(Number)
        return Promise.resolve()
          .then(() => bot.world.getColumn(cx, cz))
          .then((col) => chunkCache.set(k, col ?? null))
          .catch(() => chunkCache.set(k, null))
      })
    ),
    sleep(8000),
  ])
  const getColumn = (x, z) => {
    const entry = chunkCache.get((x >> 4) + ',' + (z >> 4))
    return entry === undefined ? null : entry
  }

  const blockAtFast = (x, y, z, column) => {
    try {
      if (column && Vec3) return column.getBlock(new Vec3(x & 15, y, z & 15))
      return bot.blockAt(new Vec3(x, y, z))
    } catch (e) {
      return null
    }
  }

  const sectionEmpty = (column, y) => {
    // 注意：prismarine-chunk 1.18+ 的 isEmpty() 实测对部分格式返回错误的 true
    // (有方块的 section 也被判空 → 整段跳过 → 全黑图)。因此只在"明确可证为空"
    // 时才跳过：solidBlockCount===0 且无 data。否则老实逐块扫描(有时间预算兜底)。
    try {
      const section = column && column.sections ? column.sections[Math.floor((y - minY) / 16)] : null
      if (!section) return true
      const solid = typeof section.solidBlockCount === 'number' ? section.solidBlockCount : null
      const hasData = !!section.data
      if (solid === 0 && !hasData) return true
    } catch (e) { /* 判断失败则老实扫 */ }
    return false
  }

  for (let dz = -radius; dz <= radius; dz++) {
    if (dz % 8 === 0 && Date.now() > deadline) { truncated = true; break }
    for (let dx = -radius; dx <= radius; dx++) {
      const x = botX + dx
      const z = botZ + dz
      const idx = (dz + radius) * size + (dx + radius)
      const column = getColumn(x, z)

      // 完全没有该区块 → 未加载，快速跳过
      if (!column) {
        heightMap[idx] = -32000
        continue
      }
      loadedMap[idx] = 1
      loadedColumns++

      let top = null
      let topY = scanBottom - 1
      let y = scanTop
      while (y >= scanBottom) {
        // 空 section 整段跳过(16 层)
        if (sectionEmpty(column, y)) { y -= 16; continue }
        const block = blockAtFast(x, y, z, column)
        if (block && block.name && !TRANSPARENT.has(block.name) && block.name !== 'air' && block.name !== 'cave_air') {
          top = block
          topY = y
          break
        }
        y--
      }
      heightMap[idx] = topY
      colorMap[idx] = top
      if (top) {
        if (topY < hMin) hMin = topY
        if (topY > hMax) hMax = topY
      }
    }
  }

  // 已加载列太少 → 上层应该提示用户稍后再试，但仍输出地图(大部分未加载棋盘格)
  const loadedRatio = loadedColumns / (size * size)
  const hRange = Math.max(1, hMax - hMin)
  const anyLoaded = loadedColumns > 0

  // ---- 第二遍：着色 + 伪立体光照 ----
  const lumAt = (idx) => 0.62 + 0.38 * ((heightMap[idx] - hMin) / hRange)

  for (let dz = 0; dz < size; dz++) {
    for (let dx = 0; dx < size; dx++) {
      const idx = dz * size + dx
      const block = colorMap[idx]
      const loaded = loadedMap[idx] === 1
      let r, g, b

      if (loaded && block) {
        const base = BLOCK_COLORS[block.name] || (() => {
          // 未知方块：按名称 hash 出稳定伪彩色，便于区分
          let hsh = 0
          for (let i = 0; i < block.name.length; i++) hsh = (hsh * 31 + block.name.charCodeAt(i)) | 0
          return [96 + (hsh & 63), 96 + ((hsh >> 6) & 63), 96 + ((hsh >> 12) & 63)]
        })()
        ;[r, g, b] = base

        // 相对高度明暗
        let lum = lumAt(idx)
        // 伪立体光照：与右上相邻列比较高差
        const nIdx = (dz > 0 ? idx - size : idx) // 北邻
        const eIdx = (dx > 0 ? idx - 1 : idx) // 西邻
        const nH = loadedMap[nIdx] === 1 ? heightMap[nIdx] : null
        const eH = loadedMap[eIdx] === 1 ? heightMap[eIdx] : null
        const my = heightMap[idx]
        if (nH !== null && my > nH) lum += 0.10
        if (nH !== null && my < nH) lum -= 0.10
        if (eH !== null && my > eH) lum += 0.07
        if (eH !== null && my < eH) lum -= 0.07

        // 水体：按"水面下深度"渐暗，模拟深水
        if (block.name === 'water') {
          lum = 0.85 + 0.15 * ((my - hMin) / hRange)
        }

        r = Math.round(r * lum)
        g = Math.round(g * lum)
        b = Math.round(b * lum)
        if (block.name === 'lava') {
          // 岩浆发光：提高红亮
          r = Math.min(255, r + 60)
          g = Math.max(0, g - 20)
          b = Math.max(0, b - 40)
        }
      } else if (loaded) {
        // 已加载但顶方块为空(如虚空/全透明列)
        r = 18; g = 20; b = 24
      } else {
        // 未加载区块：棋盘格提示
        const chk = ((dx + dz) >> 3) & 1
        r = chk ? 22 : 30; g = chk ? 24 : 32; b = chk ? 30 : 40
      }

      for (let sy = 0; sy < scale; sy++) {
        const py = (dz * scale + sy) * width
        for (let sx = 0; sx < scale; sx++) {
          const offset = (py + dx * scale + sx) * 4
          px[offset] = Math.min(255, Math.max(0, r))
          px[offset + 1] = Math.min(255, Math.max(0, g))
          px[offset + 2] = Math.min(255, Math.max(0, b))
          px[offset + 3] = 255
        }
      }
    }
  }

  // ---- 机器人标记：红色方块 + 白点 + 朝向线 ----
  const cx = radius * scale + Math.floor(scale / 2)
  const cy = cx
  const markSize = Math.max(4, scale)
  drawRect(px, width, height, cx - markSize, cy - markSize, markSize * 2, markSize * 2, [225, 38, 38])
  drawRect(px, width, height, cx - 1, cy - 1, 2, 2, [255, 255, 255])
  // 朝向线(mineflayer yaw: 0=+Z/南)
  const yaw = typeof bot.entity.yaw === 'number' ? bot.entity.yaw : 0
  const dirX = -Math.sin(yaw)
  const dirZ = -Math.cos(yaw)
  drawLine(px, width, height, cx, cy,
    Math.round(cx + dirX * markSize * 2.2), Math.round(cy + dirZ * markSize * 2.2), [255, 210, 60])

  // ---- 罗盘 "N" (右上角) ----
  const nx = width - scale - 8
  const ny = 6
  for (let gy = 0; gy < 5; gy++) {
    for (let gx = 0; gx < 3; gx++) {
      if (GLYPH_N[gy][gx]) {
        drawRect(px, width, height, nx + gx * scale, ny + gy * scale, scale, scale, [235, 240, 245])
      }
    }
  }
  drawRect(px, width, height, nx + scale - Math.floor(scale / 2), ny - scale, scale, scale, [225, 60, 60]) // 指北点

  const png = encodePNG(width, height, px)
  return {
    png,
    center: { x: botX, y: botY, z: botZ },
    radius,
    loadedRatio: +loadedRatio.toFixed(2),
    truncated,
  }
}

function drawRect (px, width, height, x0, y0, w, h, rgb) {
  for (let y = y0; y < y0 + h; y++) {
    if (y < 0 || y >= height) continue
    for (let x = x0; x < x0 + w; x++) {
      if (x < 0 || x >= width) continue
      const offset = (y * width + x) * 4
      px[offset] = rgb[0]
      px[offset + 1] = rgb[1]
      px[offset + 2] = rgb[2]
      px[offset + 3] = 255
    }
  }
}

function drawLine (px, width, height, x0, y0, x1, y1, rgb) {
  const steps = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1)
  for (let i = 0; i <= steps; i++) {
    const x = Math.round(x0 + ((x1 - x0) * i) / steps)
    const y = Math.round(y0 + ((y1 - y0) * i) / steps)
    if (x >= 0 && x < width && y >= 0 && y < height) {
      const offset = (y * width + x) * 4
      px[offset] = rgb[0]
      px[offset + 1] = rgb[1]
      px[offset + 2] = rgb[2]
      px[offset + 3] = 255
    }
  }
}

module.exports = { renderTopDown }
