// 俯视地图渲染器：以机器人所在位置为中心，把周围世界渲染成一张俯视 PNG。
// 纯数据实现（不依赖 WebGL / canvas），在任何环境都能出图，作为网页渲染截屏的兜底方案。
'use strict'

const { encodePNG } = require('./png')

// 常见方块的基色（近似色，够用于"看懂战况"）。未收录的方块按灰阶处理。
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
  water: [60, 92, 198],
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
}

// 透明/空气类方块，扫描高度时直接跳过
const TRANSPARENT = new Set([
  'air', 'cave_air', 'void_air', 'light', 'torch', 'wall_torch', 'soul_torch',
  'redstone_wire', 'grass', 'tall_grass', 'fern', 'large_fern', 'dead_bush',
  'dandelion', 'poppy', 'blue_orchid', 'allium', 'azure_bluet', 'oxeye_daisy',
  'cornflower', 'lily_of_the_valley', 'wither_rose', 'sunflower', 'lilac',
  'rose_bush', 'peony', 'sugar_cane', 'wheat', 'carrots', 'potatoes', 'beetroots',
  'flowering_azalea', 'brown_mushroom', 'red_mushroom', 'rail', 'powered_rail',
  'detector_rail', 'activator_rail', 'ladder', 'lever', 'tripwire', 'vine',
  'glow_lichen', 'short_grass', 'fire', 'soul_fire', 'cobweb', 'snow' /* 薄雪层 */,
])

/**
 * @param {import('mineflayer').Bot} bot
 * @param {{radius?: number, scale?: number}} opts
 * @returns {{png: Buffer, center: {x:number,y:number,z:number}, radius: number}}
 */
function renderTopDown(bot, opts = {}) {
  const radius = Math.max(4, Math.min(64, opts.radius ?? 24))
  const scale = Math.max(2, Math.min(16, opts.scale ?? 8))
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

  // 先扫一遍拿到高度范围，用于亮度归一化
  const heightMap = new Int16Array(size * size).fill(-32000)
  const colorMap = new Array(size * size).fill(null)
  let hMin = 32000
  let hMax = -32000

  for (let dz = -radius; dz <= radius; dz++) {
    for (let dx = -radius; dx <= radius; dx++) {
      const x = botX + dx
      const z = botZ + dz
      let top = null
      let topY = minY - 1
      for (let y = maxY; y >= minY; y--) {
        let block = null
        try {
          block = bot.blockAt(new (require('vec3').Vec3)(x, y, z))
        } catch (e) {
          block = null
        }
        if (!block) continue
        if (block.name === null || block.name === undefined) continue
        if (TRANSPARENT.has(block.name)) continue
        if (block.name === 'air' || block.name === 'cave_air') continue
        top = block
        topY = y
        break
      }
      const idx = (dz + radius) * size + (dx + radius)
      heightMap[idx] = topY
      colorMap[idx] = top
      if (top) {
        if (topY < hMin) hMin = topY
        if (topY > hMax) hMax = topY
      }
    }
  }

  const hRange = Math.max(1, hMax - hMin)

  for (let dz = 0; dz < size; dz++) {
    for (let dx = 0; dx < size; dx++) {
      const idx = dz * size + dx
      const block = colorMap[idx]
      const topY = heightMap[idx]

      let r = 26, g = 28, b = 34 // 未加载区块：深灰蓝
      if (block) {
        const base = BLOCK_COLORS[block.name]
        if (base) {
          ;[r, g, b] = base
        } else {
          const gray = 128
          r = g = b = gray
        }
        // 按相对高度做明暗，制造地形起伏感
        const lum = 0.55 + 0.45 * ((topY - hMin) / hRange)
        r = Math.round(r * lum)
        g = Math.round(g * lum)
        b = Math.round(b * lum)
        // 水面稍微降饱和提示"这是水"
        if (block.name === 'water' || block.name === 'lava') {
          r = Math.round(r * 0.9 + 20)
          g = Math.round(g * 0.9 + 10)
        }
      }

      // 写入 scale x scale 的像素块
      for (let sy = 0; sy < scale; sy++) {
        const py = (dz * scale + sy) * width
        for (let sx = 0; sx < scale; sx++) {
          const offset = (py + dx * scale + sx) * 4
          px[offset] = Math.min(255, r)
          px[offset + 1] = Math.min(255, g)
          px[offset + 2] = Math.min(255, b)
          px[offset + 3] = 255
        }
      }
    }
  }

  // 机器人标记：中心一个红色方块 + 白色中心点
  const cx = radius * scale + Math.floor(scale / 2)
  const cy = cx
  drawRect(px, width, height, cx - scale, cy - scale, 2 * scale, 2 * scale, [220, 40, 40])
  drawRect(px, width, height, cx - 1, cy - 1, 2, 2, [255, 255, 255])

  const png = encodePNG(width, height, px)
  return {
    png,
    center: { x: botX, y: botY, z: botZ },
    radius,
  }
}

function drawRect(px, width, height, x0, y0, w, h, rgb) {
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

module.exports = { renderTopDown }
