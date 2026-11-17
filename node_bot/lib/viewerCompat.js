// viewer 兼容层：把 prismarine-viewer 不支持的游戏版本映射到最接近的受支持版本。
//
// 背景：viewer 浏览器端的 setVersion() 遇到不认识的版本会直接放弃初始化
// (还触发 window.alert)，页面只剩天空色清屏 —— 即"截屏是蓝色块"的根因。
// 区块几何由服务端数据决定，版本映射只影响贴图/模型精度，绝大多数方块仍可正常渲染。
'use strict'

function versionNum (v) {
  const parts = String(v).split('.').map((n) => parseInt(n, 10) || 0)
  return (parts[0] || 0) * 1000000 + (parts[1] || 0) * 1000 + (parts[2] || 0)
}

function mapVersion (version) {
  if (!version) return version
  let supported
  try {
    supported = require('prismarine-viewer').supportedVersions
  } catch (e) {
    return version
  }
  if (!Array.isArray(supported) || !supported.length) return version
  if (supported.includes(version)) return version

  // 找数值上最接近的受支持版本(优先不新于目标的大版本)
  const target = versionNum(version)
  let best = supported[0]
  let bestDiff = Infinity
  for (const v of supported) {
    const d = Math.abs(versionNum(v) - target)
    if (d < bestDiff) {
      bestDiff = d
      best = v
    }
  }
  console.error(`[viewer-compat] 游戏版本 ${version} 不在渲染器支持列表，已映射为 ${best}(近似贴图渲染)`)
  return best
}

module.exports = { mapVersion }
