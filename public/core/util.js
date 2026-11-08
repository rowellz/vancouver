// Small helpers shared by the core, its tile workers and the plugins (no DOM, no three.js: tools/*.mjs import
// plugins that use them under node)

export const clamp = (v, a, b) => Math.max(a, Math.min(b, v))
export const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t) }
export const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a)) // an angle into -π .. π
export const EASE = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2) // cubic in-out

// GLSL: a hash and smooth value noise over 2D, for the procedural shaders
export const NOISE_GLSL = `
  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float noise(vec2 p) { vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y); }`

// a seeded random number generator (mulberry32): () => 0 .. 1
export function mulberry32(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// bilinear sample of an n×n height grid h over `size` metres (row 0 north) at tile-local (x, y north)
export function sampleGrid(h, n, size, x, y) {
  const c = size / (n - 1), fx = Math.min(n - 1.0001, Math.max(0, x / c)), fy = Math.min(n - 1.0001, Math.max(0, (size - y) / c))
  const i = fx | 0, j = fy | 0, u = fx - i, v = fy - j
  return h[j * n + i] * (1 - u) * (1 - v) + h[j * n + i + 1] * u * (1 - v) + h[(j + 1) * n + i] * (1 - u) * v + h[(j + 1) * n + i + 1] * u * v
}

// a circle (x, y, r) against a box (centre cx, cy, unit forward fx, fy, half length hl and width hw; right is
// (fy, -fx)): how to push the circle out { x, y } (one reused object: read it before asking again), or null
const PUSH = { x: 0, y: 0 }
export function circleBox(x, y, r, cx, cy, fx, fy, hl, hw) {
  const dx = x - cx, dy = y - cy, lf = dx * fx + dy * fy, lr = dx * fy - dy * fx
  let ef = lf - clamp(lf, -hl, hl), er = lr - clamp(lr, -hw, hw), d = Math.hypot(ef, er), k
  if (d < 1e-6) { // centre inside: out through the nearest side
    const pf = hl - Math.abs(lf), pr = hw - Math.abs(lr)
    if (pf < pr) { ef = Math.sign(lf) || 1; er = 0; k = r + pf } else { ef = 0; er = Math.sign(lr) || 1; k = r + pr }
  } else if (d < r) k = (r - d) / d
  else return null
  PUSH.x = (ef * fx + er * fy) * k; PUSH.y = (ef * fy - er * fx) * k
  return PUSH
}

// is (x, y) inside the ring pts ([[x, y], …])? (even-odd)
export function inPoly(x, y, pts) {
  let c = false
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const a = pts[i], b = pts[j]
    if ((a[1] > y) !== (b[1] > y) && x < ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]) + a[0]) c = !c
  }
  return c
}

export const esc = (s) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`) // for innerHTML

// fetch JSON, failing on an HTTP error rather than parsing its page
export const getJSON = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`); return r.json() })

// The city's wall-clock time at epoch ms (Vancouver's, or the city's own: setTimeZone, from tiles/index.json):
// { ymd: 'YYYYMMDD', sec: seconds since local midnight, wd: 'Mon', hm: 'HH:MM', p: the parts }
const fmt = (timeZone) => new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'short' })
let FMT = fmt('America/Vancouver')
export function setTimeZone(tz) { FMT = fmt(tz) }
export function vancouver(ms) {
  const p = {}
  for (const { type, value } of FMT.formatToParts(ms)) p[type] = value
  return { ymd: p.year + p.month + p.day, sec: +p.hour * 3600 + +p.minute * 60 + +p.second + (((ms % 1000) + 1000) % 1000) / 1000, wd: p.weekday, hm: `${p.hour}:${p.minute}`, p }
}
