// Weather and time of day, synced to the real weather in the city (city.place: Vancouver by default) by default, with a picker.
// Weather data: Open-Meteo (https://open-meteo.com), CC BY 4.0, current conditions refreshed every 15 minutes.
// Sun position: the NOAA general solar position equations (fractional-year series), no library.
// Sets `city.weather = { kind, intensity, isNight, hour, live, wind, atmosphere }` and emits 'weather' whenever it
// changes. `atmosphere` ({ horizon, zenith, fogNear, fogFar }) is the look for a plugin's perspective camera
// (city.view.useCamera): a sky gradient and a THREE.Fog for it to apply. Under a perspective camera this plugin also
// draws a sky dome (sun, moon and stars, clouds, lightning bolts), and the rain and snow are a world-space volume
// around the camera instead of the map's screen-space layer, with splashes. The ground gets wet or snowy in both
// views (city.surface).
import { vancouver, getJSON, NOISE_GLSL } from '../../core/util.js'

let LAT = 49.2827, LON = -123.1207 // downtown Vancouver, or the city's own place (city.place.latlon)
const API = () => `https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&current=temperature_2m,weather_code,cloud_cover,precipitation,wind_speed_10m,wind_direction_10m&wind_speed_unit=ms`
const KEY = 'vancouver.weather'
const RAD = Math.PI / 180

// WMO weather code -> [kind, intensity, description]
const WMO = {
  0: ['clear', 0, 'clear sky'], 1: ['clear', 0.25, 'mainly clear'], 2: ['cloudy', 0.5, 'partly cloudy'], 3: ['overcast', 0.8, 'overcast'],
  45: ['fog', 0.65, 'fog'], 48: ['fog', 0.8, 'rime fog'],
  51: ['drizzle', 0.3, 'light drizzle'], 53: ['drizzle', 0.5, 'drizzle'], 55: ['drizzle', 0.7, 'heavy drizzle'],
  56: ['drizzle', 0.4, 'freezing drizzle'], 57: ['drizzle', 0.7, 'freezing drizzle'],
  61: ['rain', 0.35, 'light rain'], 63: ['rain', 0.6, 'rain'], 65: ['rain', 0.9, 'heavy rain'],
  66: ['rain', 0.4, 'freezing rain'], 67: ['rain', 0.8, 'freezing rain'],
  71: ['snow', 0.35, 'light snow'], 73: ['snow', 0.6, 'snow'], 75: ['snow', 0.9, 'heavy snow'], 77: ['snow', 0.3, 'snow grains'],
  80: ['rain', 0.4, 'light showers'], 81: ['rain', 0.65, 'showers'], 82: ['rain', 0.95, 'violent showers'],
  85: ['snow', 0.5, 'snow showers'], 86: ['snow', 0.85, 'heavy snow showers'],
  95: ['storm', 0.7, 'thunderstorm'], 96: ['storm', 0.85, 'thunderstorm, hail'], 99: ['storm', 1, 'thunderstorm, hail'],
}
export function fromOpenMeteo(c) {
  const [kind, base, desc] = WMO[c.weather_code] || ['cloudy', 0.5, 'cloudy']
  const p = c.precipitation || 0 // mm in the last 15 minutes
  let intensity = base
  if (kind === 'clear' || kind === 'cloudy' || kind === 'overcast') intensity = (c.cloud_cover ?? base * 100) / 100
  else if (kind !== 'fog') intensity = Math.max(base, Math.min(1, Math.sqrt((p * 4) / 8))) // mm/h, 8 mm/h = downpour
  return { kind, intensity, desc, temp: c.temperature_2m, cloud: (c.cloud_cover ?? 50) / 100, wind: c.wind_speed_10m ?? 3, windFrom: c.wind_direction_10m ?? 225 }
}

// Sun elevation and azimuth (radians, azimuth clockwise from north) at a UTC time; NOAA, accurate to ~0.1°.
export function solar(ms, lat = LAT, lon = LON) {
  const d = new Date(ms), doy = Math.floor((ms - Date.UTC(d.getUTCFullYear(), 0, 1)) / 864e5) + 1
  const hr = d.getUTCHours() + d.getUTCMinutes() / 60 + d.getUTCSeconds() / 3600
  const g = (2 * Math.PI / 365) * (doy - 1 + (hr - 12) / 24), { cos, sin } = Math
  const eq = 229.18 * (0.000075 + 0.001868 * cos(g) - 0.032077 * sin(g) - 0.014615 * cos(2 * g) - 0.040849 * sin(2 * g))
  const decl = 0.006918 - 0.399912 * cos(g) + 0.070257 * sin(g) - 0.006758 * cos(2 * g) + 0.000907 * sin(2 * g) - 0.002697 * cos(3 * g) + 0.00148 * sin(3 * g)
  const ha = ((hr * 60 + eq + 4 * lon) / 4 - 180) * RAD, la = lat * RAD
  const el = Math.asin(sin(la) * sin(decl) + cos(la) * cos(decl) * cos(ha))
  const az = Math.atan2(sin(ha), cos(ha) * sin(la) - Math.tan(decl) * cos(la)) + Math.PI
  return { el, az }
}

// Vancouver wall-clock time: hour of day and the UTC time of local midnight
export function vanTime(ms) {
  const p = Object.fromEntries(Object.entries(vancouver(ms).p).map(([k, v]) => [k, +v]))
  const offset = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000
  return { hour: p.hour + p.minute / 60, midnight: Date.UTC(p.year, p.month - 1, p.day) - offset }
}

// Time-of-day looks by true sun elevation (degrees); interpolated. tint/desat go to the post grade.
const TOD = [
  { el: -14, tint: [0.36, 0.42, 0.66], desat: 0.3, sun: 0xb8c8ff, sunI: 1.5, sky: 0x8a9ac8, gnd: 0x3a3848, hemiI: 1.5 },
  { el: -6, tint: [0.5, 0.5, 0.74], desat: 0.2, sun: 0xc0b8f0, sunI: 1.3, sky: 0x9a90c0, gnd: 0x45404c, hemiI: 1.6 },
  { el: -1, tint: [0.84, 0.66, 0.7], desat: 0.08, sun: 0xff8050, sunI: 1.4, sky: 0xc09cc0, gnd: 0x5a4a48, hemiI: 1.6 },
  { el: 4, tint: [1.02, 0.8, 0.68], desat: 0, sun: 0xff9a58, sunI: 2.3, sky: 0xdcb4b4, gnd: 0x7a6658, hemiI: 1.7 },
  { el: 9, tint: [1.02, 0.92, 0.84], desat: 0, sun: 0xffc890, sunI: 2.3, sky: 0xe2dce6, gnd: 0x86786a, hemiI: 1.9 },
  { el: 16, tint: [1, 1, 1], desat: 0, sun: 0xfff1dc, sunI: 2.3, sky: 0xe8eef8, gnd: 0x8a8070, hemiI: 2.0 }, // the core's own light
]
// Weather looks: share of direct sun kept, grade, haze (fog veil), cloud shadows
const LOOK = {
  clear: { sun: 1, tint: [1, 1, 1], desat: 0, haze: 0 },
  cloudy: { sun: 0.25, tint: [0.93, 0.95, 0.99], desat: 0.28, haze: 0.03 },
  overcast: { sun: 0.3, tint: [0.86, 0.88, 0.94], desat: 0.32, haze: 0.06 },
  fog: { sun: 0.2, tint: [0.95, 0.96, 0.98], desat: 0.35, haze: 0.55 },
  drizzle: { sun: 0.2, tint: [0.76, 0.8, 0.9], desat: 0.36, haze: 0.08, hazeCol: [0.62, 0.66, 0.74] },
  rain: { sun: 0.1, tint: [0.62, 0.68, 0.82], desat: 0.45, haze: 0.1, hazeCol: [0.56, 0.61, 0.7] },
  storm: { sun: 0.05, tint: [0.38, 0.42, 0.58], desat: 0.52, haze: 0.08, hazeCol: [0.4, 0.44, 0.54] },
  snow: { sun: 0.3, tint: [0.92, 0.97, 1.08], desat: 0.5, haze: 0.32, hazeCol: [0.95, 0.97, 1] },
}
// street-level fog (metres) per weather at full intensity; clear air reaches the far end of the streamed map. three.js
// fades fog in with a smoothstep from near to far, so a near below zero is what veils the middle distance
const FOG = { clear: [160, 900], cloudy: [120, 760], overcast: [60, 560], fog: [-60, 240], drizzle: [20, 480], rain: [-40, 360], storm: [-50, 250], snow: [-40, 280] }
// street-level sky per weather: day horizon, zenith, night horizon, zenith (the clear sky is pulled towards it), and
// the clouds' lit and shaded colours by day
const SKY = {
  cloudy: [0xc9d1d8, 0x9fb2c4, 0x2c323c, 0x1e232b, 0xf6f8fa, 0xaab4c0],
  overcast: [0xb9c0c6, 0x9ea7b0, 0x262b33, 0x1a1e25, 0xc4cad0, 0x8f98a2],
  fog: [0xc9ced2, 0xc4c9ce, 0x4c4e56, 0x3e4048, 0xc9ced2, 0xc4c9ce],
  drizzle: [0xa9b0b8, 0x8e97a2, 0x232830, 0x181c22, 0xa8b0b8, 0x76808c],
  rain: [0x969ea8, 0x7a8492, 0x1f242c, 0x15191f, 0x8e96a0, 0x5c6672],
  storm: [0x5d6573, 0x434b59, 0x161a21, 0x0e1116, 0x5a6274, 0x2a303c],
  snow: [0xd2d7dc, 0xbcc4cc, 0x3a404a, 0x2a2f37, 0xdde2e7, 0xaab2bb],
}
const PICKS = { clear: 0.1, cloudy: 0.8, rain: 0.6, storm: 0.9, snow: 0.6, fog: 0.7 } // manual choices: intensity
const ICON = { clear: '☀︎', night: '☾︎', cloudy: '⛅︎', overcast: '☁︎', fog: '≋', drizzle: '☂︎', rain: '☂︎', storm: '⚡︎', snow: '❄︎' }

export default function weather(city) {
  if (city.place?.latlon) [LAT, LON] = city.place.latlon
  const where = city.place?.short || 'Vancouver'
  const { THREE } = city
  const { sun, hemi } = city.light, U = city.post.uniforms
  const saved = { sunC: sun.color.clone(), sunI: sun.intensity, sky: hemi.color.clone(), gnd: hemi.groundColor.clone(), hemiI: hemi.intensity, dir: sun.position.clone().sub(sun.target.position).normalize() }

  let state = { mode: 'live', hour: null } // mode: 'live' or a kind; hour: null = live time
  try { Object.assign(state, JSON.parse(localStorage.getItem(KEY)) || {}) } catch {}
  let live = null, fetchedAt = 0
  const v3 = new THREE.Vector3(), c3 = new THREE.Color(), cx = new THREE.Color(), ca = new THREE.Color(), cb = new THREE.Color()

  // ---------- atmosphere: one full-screen quad that multiplies the image (cloud shadows) and veils it (haze),
  // with the ground position reconstructed per pixel (the camera is orthographic, so it is affine in the screen)
  const atmo = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), new THREE.ShaderMaterial({
    uniforms: { uG0: { value: new THREE.Vector2() }, uGx: { value: new THREE.Vector2() }, uGy: { value: new THREE.Vector2() }, uDrift: { value: new THREE.Vector2() },
      uCov: { value: 0 }, uShade: { value: 0 }, uHaze: { value: 0 }, uHazeCol: { value: new THREE.Vector3() } },
    vertexShader: `uniform vec2 uG0, uGx, uGy; varying vec2 vG;
      void main() { vG = uG0 + uGx * position.x + uGy * position.y; gl_Position = vec4(position.xy, 0.0, 1.0); }`,
    fragmentShader: `uniform vec2 uDrift; uniform float uCov, uShade, uHaze; uniform vec3 uHazeCol; varying vec2 vG;
      ${NOISE_GLSL}
      float fbm(vec2 p) { float s = 0.0, a = 0.5; for (int k = 0; k < 4; k++) { s += a * noise(p); p = p * 2.07 + 17.3; a *= 0.5; } return s; }
      void main() {
        float shade = 1.0;
        if (uShade > 0.0) {
          float c = fbm((vG + uDrift) / 1100.0), t = 0.62 - 0.32 * uCov;
          shade = 1.0 - uShade * smoothstep(t - 0.05, t + 0.09, c);
        }
        float f = 0.0;
        if (uHaze > 0.0) f = clamp(uHaze * (0.55 + 0.9 * fbm((vG + uDrift * 0.4) / 2600.0 + 5.0)), 0.0, 0.92);
        gl_FragColor = vec4(uHazeCol * f, shade * (1.0 - f)); // blended as src + dst * src.a
      }`,
    blending: THREE.CustomBlending, blendEquation: THREE.AddEquation, blendSrc: THREE.OneFactor, blendDst: THREE.SrcAlphaFactor,
    transparent: true, depthTest: false, depthWrite: false,
  }))
  atmo.frustumCulled = false; atmo.renderOrder = -1000; atmo.visible = false
  city.overlay.add(atmo)

  // ---------- rain and snow: one instanced quad mesh in screen space, anchored to the ground as the view pans
  const share = () => city.quality?.tier.weather ?? 1 // of the drops, by graphics quality
  const MAXP = 12000, MINZ = matchMedia('(pointer: coarse)').matches ? 0.06 : 0.025 // particles from this zoom in (phones: closer)
  const pg = new THREE.InstancedBufferGeometry()
  pg.setAttribute('position', new THREE.Float32BufferAttribute([0, -1, 0, 1, -1, 0, 1, 1, 0, 0, 1, 0], 3))
  pg.setIndex([0, 1, 2, 0, 2, 3])
  const seeds = new Float32Array(MAXP * 3)
  for (let i = 0; i < seeds.length; i++) seeds[i] = Math.random()
  pg.setAttribute('seed', new THREE.InstancedBufferAttribute(seeds, 3))
  pg.instanceCount = 0
  const drops = new THREE.Mesh(pg, new THREE.ShaderMaterial({
    uniforms: { uRes: { value: new THREE.Vector2() }, uOff: { value: new THREE.Vector2() }, uDir: { value: new THREE.Vector2(0, -1) }, uT: { value: 0 },
      uSpeed: { value: 900 }, uLen: { value: 18 }, uWid: { value: 1.2 }, uSnow: { value: 0 }, uCol: { value: new THREE.Vector4(1, 1, 1, 0.4) } },
    vertexShader: `attribute vec3 seed; uniform vec2 uRes, uOff, uDir; uniform float uT, uSpeed, uLen, uWid, uSnow; varying vec2 vUv;
      void main() {
        vec2 span = uRes + 120.0, d = uDir;
        float k = 0.75 + 0.5 * seed.z; // nearer drops: faster, longer
        vec2 p = seed.xy * span + uOff + d * uSpeed * k * uT;
        p.x += uSnow * sin(uT * (0.7 + seed.z) + seed.x * 40.0) * 10.0 * k;
        p = mod(p, span) - 60.0;
        float len = mix(uLen * k, uWid * k, uSnow);
        p += -d * position.x * len + vec2(-d.y, d.x) * position.y * uWid * k * 0.5;
        vUv = position.xy;
        gl_Position = vec4(p / uRes * 2.0 - 1.0, 0.0, 1.0);
      }`,
    fragmentShader: `uniform float uSnow; uniform vec4 uCol; varying vec2 vUv;
      void main() {
        float a = uSnow > 0.5 ? 1.0 - smoothstep(0.35, 1.0, length(vec2(vUv.x * 2.0 - 1.0, vUv.y)))
          : (1.0 - vUv.x) * (1.0 - vUv.y * vUv.y);
        gl_FragColor = vec4(uCol.rgb, uCol.a * a);
      }`,
    transparent: true, depthTest: false, depthWrite: false, side: THREE.DoubleSide, forceSinglePass: true,
  }))
  drops.frustumCulled = false; drops.renderOrder = 1000; drops.visible = false
  city.overlay.add(drops)

  // ---------- rain and snow under a plugin's perspective camera: drops fixed in the world inside a box that wraps
  // around the camera (so they have parallax; the box sits a little ahead, where the camera looks), falling with the
  // gusting wind. All of it moves in the vertex shader: the CPU only integrates the wind into one offset per frame.
  const persp = () => !!city.view.camera.isPerspectiveCamera
  const coarse = matchMedia('(pointer: coarse)').matches
  const g3 = new THREE.InstancedBufferGeometry()
  g3.setAttribute('position', new THREE.Float32BufferAttribute([0, -1, 0, 1, -1, 0, 1, 1, 0, 0, 1, 0], 3))
  g3.setIndex([0, 1, 2, 0, 2, 3])
  g3.setAttribute('seed', pg.attributes.seed)
  g3.instanceCount = 0
  const fall3 = new THREE.Mesh(g3, new THREE.ShaderMaterial({
    uniforms: { uC: { value: new THREE.Vector3() }, uWind: { value: new THREE.Vector3() }, uDir: { value: new THREE.Vector3(0, -1, 0) }, uFall: { value: 0 }, uT: { value: 0 },
      uBox: { value: 30 }, uLen: { value: 1.2 }, uWid: { value: 0.012 }, uPix: { value: 0.002 }, uSnow: { value: 0 }, uCol: { value: new THREE.Vector4(1, 1, 1, 0.4) } },
    vertexShader: `attribute vec3 seed; uniform vec3 uC, uWind, uDir; uniform float uFall, uT, uBox, uLen, uWid, uPix, uSnow; varying vec2 vUv; varying float vA;
      void main() {
        float k = (16.0 + floor(seed.z * 9.0)) / 20.0; // fall speeds 0.8..1.2 whose wrap period (20 boxes) is whole boxes
        vec3 p = seed * uBox + uWind;
        p.y -= uFall * k;
        if (uSnow > 0.5) { // flakes sway and tumble
          p.x += sin(uT * (0.5 + seed.z) + seed.y * 40.0) * 0.45;
          p.z += cos(uT * (0.4 + seed.x) + seed.z * 30.0) * 0.45;
        }
        vec3 r = mod(p - uC, uBox) - uBox * 0.5; // the box around uC; each drop keeps its place in the world
        p = uC + r;
        vec3 toCam = cameraPosition - p; float dist = length(toCam); toCam /= dist;
        vec3 side = normalize(cross(uDir, toCam));
        vec3 along = uSnow > 0.5 ? normalize(cross(toCam, side)) : uDir;
        float w0 = uWid * (0.7 + 0.6 * seed.x), w = max(w0, dist * uPix); // never thinner than a pixel: fainter instead
        float len = uSnow > 0.5 ? w * 2.2 : uLen * k;
        p += along * (position.x - 0.5) * len + side * position.y * w;
        vec3 a = abs(r) / (uBox * 0.5);
        vA = (1.0 - smoothstep(0.7, 1.0, max(max(a.x, a.y), a.z))) * smoothstep(1.2, 4.0, dist) * (uSnow > 0.5 ? pow(w0 / w, 1.5) : w0 / w);
        vUv = position.xy;
        gl_Position = projectionMatrix * viewMatrix * vec4(p, 1.0);
      }`,
    fragmentShader: `uniform float uSnow; uniform vec4 uCol; varying vec2 vUv; varying float vA;
      void main() {
        float a = uSnow > 0.5 ? 1.0 - smoothstep(0.25, 1.0, length(vec2(vUv.x * 2.0 - 1.0, vUv.y))) : (1.0 - abs(vUv.y)) * smoothstep(0.0, 0.5, vUv.x);
        gl_FragColor = vec4(uCol.rgb, uCol.a * a * vA);
      }`,
    transparent: true, depthTest: true, depthWrite: false, side: THREE.DoubleSide, forceSinglePass: true,
  }))
  fall3.frustumCulled = false; fall3.renderOrder = 1000; fall3.visible = false
  city.overlay.add(fall3)
  // splashes: rings on the ground around the player, each one respawning at a hashed new spot every cycle, on a
  // plane fitted to the ground there (the CPU samples three heights per frame)
  const SPL = 600
  const gs = new THREE.InstancedBufferGeometry()
  gs.setAttribute('position', new THREE.Float32BufferAttribute([-1, 0, -1, 1, 0, -1, 1, 0, 1, -1, 0, 1], 3))
  gs.setIndex([0, 2, 1, 0, 3, 2])
  gs.setAttribute('seed', pg.attributes.seed)
  gs.instanceCount = 0
  const splash = new THREE.Mesh(gs, new THREE.ShaderMaterial({
    uniforms: { uT: { value: 0 }, uC: { value: new THREE.Vector3() }, uG: { value: new THREE.Vector2() }, uR: { value: 14 }, uCol: { value: new THREE.Vector4(1, 1, 1, 0.5) } },
    vertexShader: `attribute vec3 seed; uniform float uT, uR; uniform vec3 uC; uniform vec2 uG; varying vec2 vUv; varying float vE;
      void main() {
        float ph = uT / (0.35 + 0.25 * seed.y) + seed.z * 7.0, cyc = floor(ph);
        vE = fract(ph);
        vec2 h = fract(sin(vec2(cyc * 12.9898 + seed.x * 78.233, cyc * 39.346 + seed.y * 11.135)) * 43758.5453);
        vec2 xz = uC.xz + vec2(cos(h.x * 6.2832), sin(h.x * 6.2832)) * uR * sqrt(h.y);
        vec3 at = vec3(xz.x, uC.y + dot(uG, xz - uC.xz) + 0.04, xz.y);
        vUv = position.xz;
        gl_Position = projectionMatrix * viewMatrix * vec4(at + position * (0.03 + 0.26 * vE), 1.0);
      }`,
    fragmentShader: `uniform vec4 uCol; varying vec2 vUv; varying float vE;
      void main() { float r = length(vUv); gl_FragColor = vec4(uCol.rgb, uCol.a * (1.0 - vE) * smoothstep(0.5, 0.78, r) * (1.0 - smoothstep(0.84, 1.0, r))); }`,
    transparent: true, depthTest: true, depthWrite: false,
  }))
  splash.frustumCulled = false; splash.renderOrder = 1001; splash.visible = false
  city.overlay.add(splash)

  // ---------- the sky under a perspective camera: a dome around the camera, just inside its far plane, drawn only
  // where nothing else is (transparent, after the opaque map). Gradient into the fog colour at the horizon, the sun
  // or the moon and stars, drifting clouds or a low deck, and lightning bolts; graded with the rest of the scene.
  const dome = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 24), new THREE.ShaderMaterial({
    uniforms: { uHor: { value: new THREE.Color() }, uZen: { value: new THREE.Color() }, uCloud: { value: new THREE.Color() }, uCloudDk: { value: new THREE.Color() },
      uSunDir: { value: new THREE.Vector3(0, 1, 0) }, uSunCol: { value: new THREE.Color() }, uSun: { value: 0 }, uMoonDir: { value: new THREE.Vector3(0, 1, 0) }, uMoon: { value: 0 }, uStars: { value: 0 },
      uCov: { value: 0 }, uDeck: { value: 0 }, uDrift: { value: new THREE.Vector2() }, uBolt: { value: 0 }, uBoltAz: { value: 0 }, uBoltSeed: { value: 0 }, uAlpha: { value: 1 } },
    vertexShader: 'varying vec3 vDir; void main() { vDir = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: `uniform vec3 uHor, uZen, uCloud, uCloudDk, uSunDir, uSunCol, uMoonDir; uniform float uSun, uMoon, uStars, uCov, uDeck, uBolt, uBoltAz, uBoltSeed, uAlpha; uniform vec2 uDrift; varying vec3 vDir;
      float h1(float x) { return fract(sin(x * 127.1) * 43758.5453); }
      ${NOISE_GLSL}
      float fbm(vec2 p) { float s = 0.0, a = 0.5; for (int k = 0; k < 5; k++) { s += a * noise(p); p = p * 2.03 + 17.3; a *= 0.5; } return s; }
      float zig(float x) { float i = floor(x); return mix(h1(i), h1(i + 1.0), fract(x)) - 0.5; } // jagged 1D noise
      void main() {
        vec3 d = normalize(vDir);
        float h = d.y, up = max(h, 0.0);
        vec3 col = mix(uHor, uZen, smoothstep(0.0, 0.55, up));
        float s = max(dot(d, uSunDir), 0.0), m = max(dot(d, uMoonDir), 0.0), cl = 0.0;
        vec3 lights = uSunCol * uSun * (pow(s, 1800.0) * 14.0 + pow(s, 120.0) * 0.6) // sun disc and glow, moon, stars: behind the clouds
          + vec3(0.85, 0.88, 0.95) * uMoon * (smoothstep(0.99955, 0.9997, m) * 1.6 + pow(m, 60.0) * 0.12);
        if (uStars > 0.0) { vec3 g = floor(d * 260.0); lights += vec3(0.8, 0.85, 1.0) * uStars * step(0.996, hash(g.xy + g.z * 7.13)) * smoothstep(0.01, 0.08, up) * 1.4; }
        col += uSunCol * uSun * pow(s, 10.0) * 0.18 * (1.0 - uDeck); // the bright part of the sky around the sun
        if (h > 0.0 && (uCov > 0.0 || uDeck > 0.0)) { // clouds on a plane overhead, thinning into the horizon haze
          vec2 q = d.xz / (h + 0.06) * 0.7 + uDrift;
          float f = fbm(q), th = mix(0.78, 0.3, uCov);
          float c = max(smoothstep(th, th + 0.16, f), uDeck);
          vec3 cc = mix(uCloudDk, uCloud, smoothstep(0.25, 0.85, f + 0.25 * (1.0 - uDeck) * pow(s, 6.0)));
          cl = c * smoothstep(0.0, 0.22, h);
          col = mix(col, cc, cl);
        }
        col += lights * (1.0 - cl) * (1.0 - uDeck); // a deck hides the sun even where it thins at the horizon
        if (uBolt > 0.0 && h > -0.02) { // a bolt from the cloud base down to the horizon, with a branch
          float az = atan(d.x, -d.z), rel = az - uBoltAz; rel = atan(sin(rel), cos(rel));
          float e = asin(clamp(h, -1.0, 1.0)), top = 0.24, x = rel * cos(e);
          float path = 0.035 * zig(e * 14.0 + uBoltSeed) + 0.012 * zig(e * 60.0 + uBoltSeed * 3.1);
          float on = smoothstep(top, top - 0.01, e), dd = abs(x - path);
          float e0 = top * (0.5 + 0.2 * h1(uBoltSeed)), bp = 0.035 * zig(e0 * 14.0 + uBoltSeed) + 0.012 * zig(e0 * 60.0 + uBoltSeed * 3.1);
          float bd = abs(x - (bp + (e0 - e) * (h1(uBoltSeed + 1.0) > 0.5 ? 0.55 : -0.55) + 0.01 * zig(e * 70.0 + uBoltSeed * 5.0)));
          float bon = step(e, e0) * smoothstep(e0 - 0.11, e0 - 0.05, e);
          float b = on * (exp(-dd * 1400.0) * 4.0 + exp(-dd * 70.0) * 0.4) + bon * (exp(-bd * 1800.0) * 2.0 + exp(-bd * 90.0) * 0.2);
          col += vec3(0.86, 0.86, 1.0) * b * uBolt;
        }
        gl_FragColor = vec4(col, uAlpha);
      }`,
    side: THREE.BackSide, transparent: true, depthWrite: false, fog: false,
  }))
  dome.frustumCulled = false; dome.renderOrder = -1; dome.visible = false
  dome.onBeforeRender = (r, sc, cam) => { // centred on the camera, just inside its far plane; the horizon is the fog
    dome.position.copy(cam.position); dome.scale.setScalar(cam.far * 0.9); dome.updateMatrixWorld()
    const u = dome.material.uniforms
    if (sc.fog?.isFog) u.uHor.value.copy(sc.fog.color)
    u.uAlpha.value = THREE.MathUtils.smoothstep(cam.fov, 14, 40) // fades in through the plugin's dive from the map
  }
  city.scene.add(dome)

  // ---------- state -> lights, grade, effects
  const fx = { tint: [1, 1, 1], desat: 0, flash: 0, precip: 0, snow: false, storm: false, wind: [0, 0] } // grade before a lightning flash
  let w = null, emitted = '', anim = null, lastDir = null, nextBolt = 0, flashT = -1, lightLevel = 1, lastFrameAt = 0

  function current() {
    const t = state.hour == null ? Date.now() : vanTime(Date.now()).midnight + state.hour * 3600e3
    const hour = state.hour == null ? vanTime(t).hour : state.hour
    let kind, intensity, cloud
    if (state.mode === 'live' && live) ({ kind, intensity, cloud } = live)
    else if (state.mode === 'live') { kind = 'cloudy'; intensity = 0.5; cloud = 0.5 } // until the first answer
    else { kind = state.mode; intensity = PICKS[kind] ?? 0.5; cloud = kind === 'clear' ? 0.12 : kind === 'cloudy' ? 0.8 : 1 }
    return { t, hour, kind, intensity, cloud, ...solar(t) }
  }

  function lerpKeys(el) {
    const e = el / RAD
    let i = 0
    while (i < TOD.length - 2 && e > TOD[i + 1].el) i++
    const a = TOD[i], b = TOD[i + 1], k = THREE.MathUtils.clamp((e - a.el) / (b.el - a.el), 0, 1)
    const mix = (x, y) => x + (y - x) * k
    const col = (x, y) => ca.setHex(x).lerp(cb.setHex(y), k).clone()
    return { tint: a.tint.map((x, j) => mix(x, b.tint[j])), desat: mix(a.desat, b.desat), sun: col(a.sun, b.sun), sunI: mix(a.sunI, b.sunI), sky: col(a.sky, b.sky), gnd: col(a.gnd, b.gnd), hemiI: mix(a.hemiI, b.hemiI) }
  }

  function apply() {
    const c = current(), tod = lerpKeys(c.el), look = LOOK[c.kind] || LOOK.cloudy
    const elDeg = c.el / RAD, isNight = elDeg < -6
    const wet = c.kind === 'clear' || c.kind === 'cloudy' ? 0 : 0.6 + 0.4 * c.intensity // how far towards the weather look
    // light: the sun, clamped above 10° so the shadows stay readable; through twilight it turns into a soft
    // moon-like key light from the south-south-west (the camera looks north, so its shadows fall into view)
    const night = 1 - THREE.MathUtils.smoothstep(elDeg, -8, 2) // 0 by day, 1 once the sun is 8° below the horizon
    const se = Math.max(c.el, 10 * RAD), mz = 200 * RAD, me = 38 * RAD
    v3.set(Math.sin(c.az) * Math.cos(se), Math.sin(se), -Math.cos(c.az) * Math.cos(se)).multiplyScalar(1 - night)
      .add(new THREE.Vector3(Math.sin(mz) * Math.cos(me), Math.sin(me), -Math.cos(mz) * Math.cos(me)).multiplyScalar(night)).normalize()
    if (!lastDir || lastDir.angleTo(v3) > 0.002) { lastDir = v3.clone(); city.light.setSunDirection(v3) }
    // windows light up through dusk (and earlier on dark, wet days)
    if ('night' in city.light) city.light.night = Math.min(1, 1 - THREE.MathUtils.smoothstep(elDeg, -6, 8) + (c.kind === 'storm' || c.kind === 'rain' ? 0.25 * c.intensity : 0))
    const keep = c.kind === 'clear' || c.kind === 'cloudy' ? 1 - (1 - look.sun) * c.intensity : look.sun + (1 - look.sun) * (1 - wet) * 0.3
    sun.color.copy(tod.sun).lerp(c3.setHex(0xdde4f0), 1 - keep) // diffuse days: whiter, weaker sun
    sun.intensity = tod.sunI * keep
    hemi.color.copy(tod.sky).lerp(c3.setHex(0xc8d0dc).multiplyScalar(tod.hemiI / 2), (1 - keep) * 0.6)
    hemi.groundColor.copy(tod.gnd)
    hemi.intensity = tod.hemiI * (1 + 0.3 * (1 - keep)); hemiBase = null
    // grade
    const cl = c.kind === 'clear' || c.kind === 'cloudy' ? c.intensity : 1
    fx.tint = tod.tint.map((x, j) => x * (1 + (look.tint[j] - 1) * (wet || cl)))
    fx.desat = Math.min(0.8, tod.desat + look.desat * (wet || cl))
    lightLevel = Math.max(0.25, (tod.tint[0] + tod.tint[1] + tod.tint[2]) / 3)
    grade()
    // atmosphere: cloud shadows by day under broken cloud; haze in fog, rain, snow and overcast
    const a = atmo.material.uniforms
    const shadows = c.kind === 'clear' || c.kind === 'cloudy' ? THREE.MathUtils.smoothstep(elDeg, -2, 8) : 0
    a.uCov.value = THREE.MathUtils.clamp(c.cloud, 0.05, 0.9)
    a.uShade.value = shadows * (c.cloud > 0.04 ? 0.16 + 0.1 * c.cloud : 0) // lighter for a few fair-weather clouds
    a.uHaze.value = look.haze * (c.kind === 'fog' ? 0.5 + 0.7 * c.intensity : wet || cl)
    const hz = isNight ? [0.1, 0.12, 0.18] : (look.hazeCol || [0.8, 0.84, 0.9]).map((x, j) => x * Math.min(1, 0.35 + tod.tint[j] * 0.65))
    a.uHazeCol.value.set(...hz)
    // the street-level look for perspective cameras: sky, clouds and fog, from the sun's height and the weather
    const dark = 1 - THREE.MathUtils.smoothstep(elDeg, -8, 2) // (the `night` above is inverted: smoothstep wants min < max)
    const golden = THREE.MathUtils.smoothstep(elDeg, -4, 2) * (1 - THREE.MathUtils.smoothstep(elDeg, 7, 14))
    const S = SKY[c.kind], fair = c.kind === 'clear' || c.kind === 'cloudy', pull = !S ? 0 : fair ? 0.7 * c.intensity : 0.8 + 0.2 * c.intensity
    ca.setHex(0xcfe2ec).lerp(c3.setHex(0xf4cfaa), golden).lerp(c3.setHex(0x283350), dark) // horizon
    cb.setHex(0x84b7dc).lerp(c3.setHex(0x8fa8d4), golden).lerp(c3.setHex(0x0f1628), dark) // zenith
    if (S) { ca.lerp(c3.setHex(S[0]).lerp(cx.setHex(S[2]), dark), pull); cb.lerp(c3.setHex(S[1]).lerp(cx.setHex(S[3]), dark), pull) }
    const D = dome.material.uniforms
    D.uZen.value.copy(cb); D.uHor.value.copy(ca) // (the horizon then follows the fog the camera's plugin applies)
    D.uSunDir.value.set(Math.sin(c.az) * Math.cos(c.el), Math.sin(c.el), -Math.cos(c.az) * Math.cos(c.el))
    D.uSunCol.value.copy(tod.sun); D.uSun.value = THREE.MathUtils.smoothstep(elDeg, -3, 1) * (c.kind === 'fog' ? 0.3 : 1)
    D.uMoonDir.value.set(Math.sin(mz) * Math.cos(me), Math.sin(me), -Math.cos(mz) * Math.cos(me))
    D.uMoon.value = fair ? dark * (1 - 0.8 * c.cloud) : 0
    D.uStars.value = fair ? dark * (1 - c.cloud) : 0
    D.uCov.value = fair ? c.cloud : c.kind === 'fog' ? 0 : 1
    D.uDeck.value = { overcast: 0.7, drizzle: 0.8, rain: 0.85, storm: 0.9, snow: 0.8 }[c.kind] ?? 0
    D.uCloud.value.setHex(S ? S[4] : 0xf6f8fa).lerp(c3.setHex(0xffc49a), fair ? golden * 0.6 : 0).lerp(c3.setHex(0x4a4650), dark) // by night lit from below by the city
    D.uCloudDk.value.setHex(S ? S[5] : 0xaab4c0).lerp(c3.setHex(0xa08088), fair ? golden * 0.5 : 0).lerp(c3.setHex(0x1e2128), dark)
    const [n0, f0] = FOG.cloudy, [n1, f1] = FOG[c.kind] || FOG.cloudy, fk = c.kind === 'clear' || c.kind === 'cloudy' || c.kind === 'fog' ? 1 : 0.5 + 0.5 * c.intensity
    let fogNear = n0 + (n1 - n0) * fk, fogFar = f0 + (f1 - f0) * fk
    if (c.kind === 'fog') { fogNear *= 1.5 - c.intensity; fogFar *= 1.5 - c.intensity }
    fogNear *= 1 - 0.2 * dark; fogFar *= 1 - 0.15 * dark
    const atmosphere = { horizon: ca.getHex(), zenith: cb.getHex(), fogNear: Math.round(fogNear), fogFar: Math.round(fogFar) }
    // particles
    const snow = c.kind === 'snow', rain = c.kind === 'rain' || c.kind === 'storm' || c.kind === 'drizzle'
    fx.precip = rain || snow ? c.intensity * (c.kind === 'drizzle' ? 0.5 : 1) : 0
    fx.snow = snow
    // wind: live, or for a picked rain or storm at least a breeze or a gale, so the drops slant
    const wnd = Math.max(live?.wind ?? 3, state.mode === 'live' ? 0 : c.kind === 'storm' ? 9 : rain ? 4 : 0), from = (live?.windFrom ?? 225) * RAD
    fx.wind = [-Math.sin(from) * wnd, Math.cos(from) * wnd] // towards, x east / z south, m/s
    fx.storm = c.kind === 'storm'
    // the ground: wet streets mirror the sky (by night the city's lights), snow lies on the flat
    if (city.surface) {
      city.surface.wet = rain ? 0.55 + 0.45 * c.intensity : c.kind === 'fog' ? 0.2 : 0
      city.surface.snow = snow ? 0.55 + 0.45 * c.intensity : 0
      city.surface.sky?.copy(ca).lerp(c3.setHex(0x8a6c48), dark * 0.7)
    }
    effects()
    // the contract
    w = { kind: c.kind, intensity: +c.intensity.toFixed(2), isNight, hour: +c.hour.toFixed(2), live: state.mode === 'live' && state.hour == null, wind: fx.wind.map((x) => +x.toFixed(1)), atmosphere }
    city.weather = w
    const key = JSON.stringify(w)
    if (key !== emitted) { emitted = key; city.emit('weather', w) }
    ui()
    city.requestRender()
  }

  let hemiBase = null
  function grade() {
    const f = fx.flash
    if (persp() || hemiBase != null) { // street level: the flash lights the scene too
      if (hemiBase == null) hemiBase = hemi.intensity
      hemi.intensity = hemiBase * (1 + 2.5 * f)
      if (!f) hemiBase = null
    }
    U.tint.value.set(...fx.tint.map((x, j) => x * (1 - f) + f * [2.2, 2.3, 2.7][j]))
    U.desat.value = fx.desat * (1 - f)
  }

  function sizeDrops() { // by zoom: bigger, faster and denser up close; a fine texture from far away
    const f = THREE.MathUtils.clamp(Math.log(city.camera.zoom / 0.025) / Math.log(0.5 / 0.025), 0, 1) // 0 city .. 1 street
    const u = drops.material.uniforms, area = innerWidth * innerHeight
    u.uSnow.value = fx.snow ? 1 : 0
    u.uSpeed.value = fx.snow ? 40 + 60 * f : 420 + 680 * f
    u.uLen.value = 6 + 22 * f
    u.uWid.value = fx.snow ? 2.2 + 4.8 * f : 0.8 + 0.8 * f
    const b = Math.min(1, lightLevel * 1.4)
    u.uCol.value.set(...(fx.snow ? [0.97, 0.98, 1].map((x) => x * Math.max(0.45, b)) : [0.86, 0.9, 1].map((x) => x * Math.max(0.4, b))), fx.snow ? 0.95 : 0.2 + 0.25 * f)
    const per = fx.snow ? 380 : 320 // CSS px² per particle at full intensity
    pg.instanceCount = Math.min(MAXP, Math.round((area / per) * (0.15 + 0.85 * fx.precip) * (0.55 + 0.45 * f) * share()))
  }

  // what is on screen decides what runs
  const dropsWanted = () => !document.hidden && fx.precip > 0 && (persp() || city.camera.zoom >= MINZ)
  function effects() {
    const on = !document.hidden, pOn = dropsWanted(), p = persp()
    drops.visible = pOn && !p
    fall3.visible = pOn && p; splash.visible = fall3.visible && !fx.snow; dome.visible = p
    if (fall3.visible) size3()
    // the cloud shadow / haze layer reconstructs the ground for the orthographic map camera only; at street level
    // the haze is the fog in city.weather.atmosphere
    atmo.visible = !p && (atmo.material.uniforms.uShade.value > 0.001 || atmo.material.uniforms.uHaze.value > 0.001)
    const want = pOn || (on && flashT >= 0)
    if (want && !anim) anim = city.animate()
    else if (!want && anim) { anim(); anim = null }
    city.requestRender()
  }

  function size3() { // density, size and colour by kind and intensity (not per frame)
    const u = fall3.material.uniforms, b = Math.max(fx.snow ? 0.6 : 0.5, Math.min(1, lightLevel * 1.4)), snow = fx.snow
    u.uSnow.value = snow ? 1 : 0
    u.uBox.value = snow ? 26 : 22
    u.uLen.value = 1.0 + 0.8 * fx.precip
    u.uWid.value = snow ? 0.045 : 0.011
    if (snow) u.uCol.value.set(0.97 * b, 0.98 * b, b, 1); else u.uCol.value.set(0.78 * b, 0.84 * b, 0.94 * b, 0.55)
    g3.instanceCount = Math.round(MAXP * (0.25 + 0.75 * fx.precip) * (coarse ? 0.5 : 1) * share())
    gs.instanceCount = snow ? 0 : Math.round(SPL * (0.25 + 0.75 * fx.precip) * share())
    splash.material.uniforms.uCol.value.set(u.uCol.value.x, u.uCol.value.y, u.uCol.value.z, 0.55)
  }

  // per frame: camera-dependent uniforms, the clock, lightning
  const ndc = new THREE.Vector3(), o = new THREE.Vector3(), g = (x, y, out) => { // ground (y = 0) under an NDC point
    o.set(x, y, -1).unproject(city.camera)
    ndc.set(x, y, 1).unproject(city.camera); const t = o.y / (o.y - ndc.y)
    return out.set(o.x + (ndc.x - o.x) * t, o.z + (ndc.z - o.z) * t)
  }
  const t0 = performance.now()
  const offFrame = city.on('frame', (dt) => {
    lastFrameAt = performance.now()
    const t = (lastFrameAt - t0) / 1000, cam = city.camera
    cam.updateMatrixWorld()
    if ((drops.visible || fall3.visible) && !dropsWanted()) effects() // zoomed out past them
    if (dome.visible) { const d = dome.material.uniforms.uDrift.value; d.x -= fx.wind[0] * dt * 0.01; d.y -= fx.wind[1] * dt * 0.01 }
    if (fall3.visible) {
      const C = city.view.camera, u = fall3.material.uniforms, B = u.uBox.value
      C.getWorldDirection(v3)
      u.uC.value.copy(C.position).addScaledVector(v3, B * 0.3)
      u.uT.value = t % 600
      // gusts: the slant swings; the drift is integrated so the drops never jump (wrapped by whole boxes)
      const gust = fx.storm ? 1 + 0.7 * Math.sin(t * 0.9) * Math.sin(t * 0.37 + 1) : 1 + 0.25 * Math.sin(t * 0.5)
      const fall = fx.snow ? 1.1 : 9, ws = (fx.snow || fx.storm ? 0.6 : 0.35) * gust, wx = fx.wind[0] * ws, wz = fx.wind[1] * ws
      u.uWind.value.x = (u.uWind.value.x + wx * dt) % B; u.uWind.value.z = (u.uWind.value.z + wz * dt) % B
      u.uFall.value = (u.uFall.value + fall * dt) % (B * 20)
      u.uDir.value.set(wx, -fall, wz).normalize()
      u.uPix.value = (2 * Math.tan((C.fov * RAD) / 2)) / innerHeight // metres per CSS pixel at 1 m
      if (splash.visible) { // around the player (or ahead of the camera), on the ground's slope there
        const P = city.player, H = city.world.heightAt, su = splash.material.uniforms
        const x = P ? P.x : C.position.x + v3.x * 10, z = P ? P.z : C.position.z + v3.z * 10
        su.uC.value.set(x, P ? P.y : H(x, z), z)
        su.uG.value.set(THREE.MathUtils.clamp((H(x + 4, z) - H(x - 4, z)) / 8, -0.2, 0.2), THREE.MathUtils.clamp((H(x, z + 4) - H(x, z - 4)) / 8, -0.2, 0.2))
        su.uT.value = t % 600
      }
    }
    if (atmo.visible) {
      const a = atmo.material.uniforms, sp = Math.max(3, Math.hypot(...fx.wind) * 2) / Math.max(0.5, Math.hypot(...fx.wind))
      g(0, 0, a.uG0.value); g(1, 0, a.uGx.value).sub(a.uG0.value); g(0, 1, a.uGy.value).sub(a.uG0.value)
      a.uDrift.value.set(-fx.wind[0] * sp * t, -fx.wind[1] * sp * t)
    }
    if (drops.visible) {
      sizeDrops()
      const u = drops.material.uniforms, W = innerWidth, H = innerHeight
      u.uRes.value.set(W, H)
      u.uT.value = t % 600
      // the ground origin's screen position anchors the pattern; the fall direction is the wind-slanted
      // velocity projected to the screen (rain falls ~9 m/s, snow ~1 m/s)
      const p0 = v3.set(0, 0, 0).project(cam), x0 = p0.x, y0 = p0.y
      u.uOff.value.set(x0 * W / 2, y0 * H / 2)
      const fall = fx.snow ? 1.2 : 9, s = Math.min(fx.snow ? 1.5 : 0.7, 1 / fall)
      const tgt = city.controls.target, a = v3.copy(tgt).project(cam), ax = a.x, ay = a.y
      const b = v3.copy(tgt).add(ndc.set(fx.wind[0] * s, -1, fx.wind[1] * s)).project(cam)
      u.uDir.value.set((b.x - ax) * W, (b.y - ay) * H).normalize()
    }
    if (flashT >= 0) { // lightning: a bright flicker, then a second, weaker one
      const e = t - flashT
      fx.flash = e > 0.55 ? 0 : 0.5 * Math.max(0, 1 - e / 0.07) + 0.75 * Math.max(0, 1 - Math.abs(e - 0.2) / 0.1) + 0.3 * Math.max(0, 1 - Math.abs(e - 0.36) / 0.12)
      dome.material.uniforms.uBolt.value = Math.min(1, fx.flash * 2.5)
      grade()
      if (e > 0.55) { flashT = -1; effects() }
    }
  })
  let wasPersp = false
  const offQuality = city.on('quality', () => { if (fall3.visible) size3(); city.requestRender() }) // drops.visible: sized per frame
  const offView = city.on('view', () => { if (wasPersp !== persp() || drops.visible !== (dropsWanted() && !persp())) { wasPersp = persp(); effects() } })
  const onVis = () => { effects(); if (!document.hidden && Date.now() - fetchedAt > 15 * 60e3) refresh() }
  document.addEventListener('visibilitychange', onVis)

  // slow things: the sun every minute, a rare lightning bolt, clouds drifting while nothing else redraws
  let secs = 0
  const tick = setInterval(() => {
    if (document.hidden) return
    const t = (performance.now() - t0) / 1000
    if (fx.storm && flashT < 0 && t > nextBolt) {
      if (nextBolt) { // a strike; at street level its bolt comes down somewhere in view
        flashT = t
        const C = city.view.camera, D = dome.material.uniforms
        if (C.isPerspectiveCamera) {
          C.getWorldDirection(v3)
          const hf = 2 * Math.atan(Math.tan((C.fov * RAD) / 2) * C.aspect)
          D.uBoltAz.value = Math.atan2(v3.x, -v3.z) + (Math.random() - 0.5) * hf * 0.6; D.uBoltSeed.value = Math.random() * 100
        }
        effects()
      }
      nextBolt = t + 4 + Math.random() * 10
    }
    if (state.hour == null && ++secs % 60 === 0) apply()
    else if (atmo.visible && performance.now() - lastFrameAt > 900) city.requestRender()
  }, 1000)

  // ---------- live data
  let timer = 0, busy = false
  async function refresh() {
    clearTimeout(timer)
    timer = setTimeout(refresh, 15 * 60e3)
    if (document.hidden || busy) return
    busy = true
    try {
      const j = await getJSON(API())
      live = fromOpenMeteo(j.current); fetchedAt = Date.now()
      apply()
    } catch (e) { console.warn('weather: Open-Meteo', e); if (!live) { live = false; ui() } } finally { busy = false }
  }

  // ---------- UI
  const style = document.createElement('style')
  style.textContent = `.wx h3 { margin: 10px 0 6px; font-size: 10.5px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
    .wx .now { color: var(--text); }
    .wx .chips { display: flex; flex-wrap: wrap; gap: 4px; }
    .wx .chips button, .wx .row button { font: inherit; color: var(--text); background: rgba(255,255,255,.06); border: 1px solid var(--line); border-radius: 7px; padding: 4px 9px; cursor: pointer; }
    .wx button[aria-pressed="true"] { background: var(--accent); color: #0b1016; border-color: transparent; }
    .wx .row { display: flex; align-items: center; gap: 8px; }
    .wx input[type=range] { flex: 1; accent-color: var(--accent); }
    .wx .clock { font-variant-numeric: tabular-nums; color: var(--text); min-width: 3em; text-align: right; }
    .wx .credit { margin-top: 10px; color: var(--muted); font-size: 11.5px; }`
  document.head.append(style)
  const btn = city.ui.addButton({ label: 'Weather', title: 'Weather and time of day', panel: true })
  const P = btn.panel
  P.classList.add('wx')
  P.innerHTML = `<div class="now"></div>
    <h3>Weather</h3><div class="chips">${['live', 'clear', 'cloudy', 'rain', 'storm', 'snow', 'fog'].map((k) => `<button type="button" data-k="${k}">${k[0].toUpperCase() + k.slice(1)}</button>`).join('')}</div>
    <h3>Time of day</h3><div class="row"><button type="button" data-t="live">Live</button><input type="range" min="0" max="24" step="0.25" aria-label="Hour of day"><span class="clock"></span></div>
    <div class="credit">${where} weather by <a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo.com</a> (CC BY 4.0)</div>`
  const range = P.querySelector('input'), clock = P.querySelector('.clock'), now = P.querySelector('.now')
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)) } catch {} }
  P.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return
    if (b.dataset.k) state.mode = b.dataset.k
    if (b.dataset.t) state.hour = null
    save(); apply()
  })
  range.addEventListener('input', () => { state.hour = +range.value; save(); apply() })
  const hhmm = (h) => `${String(Math.floor(h) % 24).padStart(2, '0')}:${String(Math.floor((h % 1) * 60 + 1e-6)).padStart(2, '0')}`
  function ui() {
    for (const b of P.querySelectorAll('[data-k]')) b.setAttribute('aria-pressed', String(b.dataset.k === state.mode))
    P.querySelector('[data-t]').setAttribute('aria-pressed', String(state.hour == null))
    if (document.activeElement !== range) range.value = String(w.hour)
    clock.textContent = hhmm(w.hour)
    now.textContent = live ? `Live: ${Math.round(live.temp)}°C, ${live.desc}` : live === false ? 'Live: Open-Meteo unreachable, retrying' : `Live: loading ${where} weather…`
    const ic = w.isNight && (w.kind === 'clear' || w.kind === 'cloudy') ? ICON.night : ICON[w.kind]
    btn.set({ icon: ic, label: state.mode === 'live' && live ? `${Math.round(live.temp)}°` : 'Weather' })
  }

  apply()
  refresh()

  return {
    dispose() {
      clearTimeout(timer); clearInterval(tick)
      offFrame(); offView(); offQuality(); document.removeEventListener('visibilitychange', onVis)
      anim?.(); btn.remove(); style.remove()
      for (const m of [atmo, drops, fall3, splash]) { city.overlay.remove(m); m.geometry.dispose(); m.material.dispose() }
      city.scene.remove(dome); dome.geometry.dispose(); dome.material.dispose()
      if (city.surface) { city.surface.wet = 0; city.surface.snow = 0 }
      sun.color.copy(saved.sunC); sun.intensity = saved.sunI
      hemi.color.copy(saved.sky); hemi.groundColor.copy(saved.gnd); hemi.intensity = saved.hemiI
      city.light.setSunDirection(saved.dir)
      U.tint.value.set(1, 1, 1); U.desat.value = 0
      city.requestRender()
    },
  }
}
