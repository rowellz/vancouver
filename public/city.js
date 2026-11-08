// The plugin host: one `city` object that every plugin gets. See PLUGINS.md.
// A plugin is an ES module whose default export is `(city) => ({ dispose() })` (dispose optional).
import * as THREE from 'three'

export function createCity({ scene, overlay, camera, renderer, controls, zAt, ll, toLatLon, sun, hemi, setSun, post, requestRender, data, tiles, night, surface, suppressBuildings, ownCamera, quality, place }) {
  const handlers = new Map()
  const own = () => ownCamera?.active
  let animators = 0
  const city = {
    version: 1,
    THREE,
    // which city this is: { name, title, short, latlon: [lat, lon], tz } (tiles/index.json "city"; Vancouver by default)
    place,
    scene, // everything in here gets the ink outlines and shadows
    // graphics quality (core/quality.js): tier.name 'high' | 'low' and its knobs (life, weather: shares of the
    // particles / agents to draw, detail, …); city.on('quality', tier) when it changes
    quality,
    overlay, // drawn last, without outlines (particles, labels, effects)
    camera, renderer, controls,
    canvas: renderer.domElement,
    world: {
      // three.js frame: x east, y up, z south; 1 unit = 1 metre
      fromLatLon(lat, lon) { const [x, n] = ll(lat, lon); return new THREE.Vector3(x, zAt(x, n), -n) },
      toLatLon(x, z) { return toLatLon(x, -z) },
      heightAt(x, z) { return zAt(x, -z) },
      // street furniture's footing: the nearest point { x, z } at least `margin` m off the carriageways and cycle tracks
      // the map draws, within maxD m; null where (x, z) is clear already (or the detail tile isn't loaded)
      offRoad(x, z, margin = 0.6, maxD = 8) { const p = tiles?.offRoad(x, -z, margin, maxD); return p && { x: p.x, z: -p.y } },
      // a [[lat, lon], …] ring where your plugin draws its own building: the generic one there is removed
      suppressBuildings(ring) { suppressBuildings?.(ring) },
      // call fn(tile) for every loaded tile now, and on every tile that loads later; returns an unsubscribe
      eachTile(fn) { tiles?.forEach((t) => fn(t)); return city.on('tile:load', fn) },
    },
    view: {
      // while a plugin has its own camera (useCamera): the map zoom that shows its focus at the same scale
      get zoom() { return own() ? ownCamera.zoom : camera.zoom },
      get center() { return own() ? own().opts.focus.clone() : controls.target.clone() },
      get metresPerPixel() { return 1 / (6 * city.view.zoom) }, // CSS px
      // ground rectangle on screen (x0, z0, x1, z1), generous for the 30° tilt
      get bounds() {
        if (own()) { const f = own().opts.focus, r = own().opts.radius ?? 900; return { x0: f.x - r, x1: f.x + r, z0: f.z - r, z1: f.z + r } }
        const t = controls.target, hw = innerWidth / 2 / (6 * camera.zoom), hh = innerHeight / (6 * camera.zoom)
        return { x0: t.x - hw, x1: t.x + hw, z0: t.z - hh, z1: t.z + hh }
      },
      // the camera the map is drawn with: city.camera, or the one a plugin passed to useCamera
      get camera() { return own()?.camera || camera },
      // draw the map through your own camera (e.g. a PerspectiveCamera for a game): MapControls stop, and tiles
      // stream, shadows fit and fog-aware ink work around opts.focus (a Vector3 on the ground you keep moving):
      // tiles within opts.radius metres (default 900; ≤ 1100 loads detail tiles), shadows within
      // opts.shadowRadius (default 300). Keep the camera's aspect right on resize.
      useCamera(cam, opts) { ownCamera?.use(cam, opts) },
      // back to the map camera, exactly where it was
      release() { ownCamera?.release() },
    },
    light: {
      sun, hemi, setSunDirection: setSun,
      get night() { return night?.value ?? 0 }, // 0 day .. 1 night: lights windows
      set night(v) { if (night) { night.value = Math.max(0, Math.min(1, v)); requestRender() } },
    },
    // weather on the ground: wet (0..1) darkens it and mirrors `sky` (a THREE.Color) in the wet paving, snow (0..1)
    // whitens flat ground, roofs and, less, the roads
    surface: {
      get wet() { return surface?.wet.value ?? 0 },
      set wet(v) { if (surface) { surface.wet.value = Math.max(0, Math.min(1, v)); requestRender() } },
      get snow() { return surface?.snow.value ?? 0 },
      set snow(v) { if (surface) { surface.snow.value = Math.max(0, Math.min(1, v)); requestRender() } },
      get sky() { return surface?.sky.value },
      get seaHole() { return surface?.seaHole.value }, // Vector4 (x, sea level, z, radius): no map sea in there (radius 0: off)
    },
    post, // { uniforms: { gain (vec3), sat (float) } } colour grade of the final image
    data, // static data the core has loaded
    // the streamed map: loaded tiles (tiles.tiles), and 'tile:load' / 'tile:unload' events with each tile's
    // { level, i, j, ox, oy, size, graph: { pieces: [{ kind, lanes, oneway, drive, w, a, b, pts (world x,y pairs), z }] }, terrain, footprints, water }
    tiles,
    on(event, fn) {
      if (!handlers.has(event)) handlers.set(event, new Set())
      handlers.get(event).add(fn)
      return () => handlers.get(event)?.delete(fn)
    },
    emit(event, ...args) {
      for (const fn of handlers.get(event) || []) { try { fn(...args) } catch (e) { console.error(`[${event}]`, e) } }
    },
    requestRender, // draw one more frame (the page renders on demand)
    animate(on = true) { // keep drawing every frame while any plugin animates; returns a stop function
      animators += on ? 1 : 0
      requestRender()
      let stopped = !on
      return () => { if (!stopped) { stopped = true; animators--; requestRender() } }
    },
    get animating() { return animators > 0 },
    ui: makeUI(),
  }
  return city
}

// html.compact: the phone layout (index.html sets it); toolbar panels are bottom sheets there
const compact = () => document.documentElement.classList.contains('compact')
const CLOSE_ALL = 'ui:closesheets' // a panel, About or the chat opening: every other one closes

// A panel that is a bottom sheet on phones: a close button, and dragging its top edge down closes it.
// `close()` hides it (and whatever goes with that, e.g. a pressed toolbar button).
function sheet(el, close) {
  const x = () => {
    if (el.querySelector(':scope > .sheet-x')) return
    const g = document.createElement('div'), b = document.createElement('button')
    g.className = 'sheet-grip'; g.setAttribute('aria-hidden', 'true')
    g.addEventListener('pointerdown', drag)
    b.type = 'button'; b.className = 'sheet-x'; b.textContent = '×'; b.setAttribute('aria-label', 'Close')
    b.onclick = close
    el.prepend(g, b)
  }
  // the grip (touch-action: none, so the browser doesn't take the touch for scrolling) follows the finger down
  function drag(e) {
    if (!compact()) return
    const y0 = e.clientY, t0 = performance.now()
    let dy = 0
    try { e.target.setPointerCapture(e.pointerId) } catch {}
    document.body.classList.add('sheet-dragging')
    const move = (ev) => { dy = Math.max(0, ev.clientY - y0); el.style.transform = `translateY(${dy}px)` }
    const up = () => {
      removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up)
      document.body.classList.remove('sheet-dragging')
      const fast = dy / Math.max(1, performance.now() - t0) > 0.5
      el.style.transition = 'transform .18s ease-out'
      if (dy > 80 || (dy > 24 && fast)) { el.style.transform = 'translateY(100%)'; setTimeout(() => { close(); el.style.transform = el.style.transition = '' }, 170) }
      else { el.style.transform = ''; setTimeout(() => (el.style.transition = ''), 200) }
    }
    addEventListener('pointermove', move); addEventListener('pointerup', up); addEventListener('pointercancel', up)
  }
  x()
  return x // call again after replacing the element's contents
}

function makeUI() {
  const bar = document.getElementById('tools')
  // About (index.html) is a sheet too
  const about = document.getElementById('about'), aboutBtn = document.getElementById('aboutBtn')
  if (about && aboutBtn) {
    const close = () => { about.hidden = true; aboutBtn.setAttribute('aria-expanded', 'false') }
    sheet(about, close)
    aboutBtn.addEventListener('click', () => { if (about.hidden) document.dispatchEvent(new Event(CLOSE_ALL)) }, true) // before its own toggle
    document.addEventListener(CLOSE_ALL, close)
  }
  return {
    // a toolbar button at the top right (on phones: the dock at the bottom); with `panel: true` it opens a small
    // panel under it (on phones: a bottom sheet). `set({ icon, label })` changes what the button says.
    addButton({ label, title, icon, onClick, panel }) {
      const b = document.createElement('button')
      b.type = 'button'
      const ic = document.createElement('span'), lb = document.createElement('span')
      ic.className = 'ic'; lb.className = 'lb'
      ic.setAttribute('aria-hidden', 'true')
      const set = ({ icon: i = icon, label: l = label } = {}) => { icon = i; label = l; ic.textContent = i || ''; lb.textContent = l || '' }
      set()
      b.append(ic, lb)
      if (title) b.title = title
      b.setAttribute('aria-label', title || label)
      bar.append(b)
      bar.hidden = false
      let el = null
      if (panel) {
        el = document.createElement('div')
        el.className = 'panel tool-panel'
        el.hidden = true
        el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', label || title)
        document.body.append(el)
        b.setAttribute('aria-expanded', 'false')
        const close = () => { el.hidden = true; b.setAttribute('aria-expanded', 'false') }
        const ensureX = sheet(el, close)
        document.addEventListener(CLOSE_ALL, close)
        b.addEventListener('click', () => {
          const open = el.hidden
          document.dispatchEvent(new Event(CLOSE_ALL))
          if (!open) return
          ensureX() // the plugin may have replaced the panel's contents since
          el.hidden = false; b.setAttribute('aria-expanded', 'true')
          if (compact()) { el.style.top = el.style.right = '' } else { const r = b.getBoundingClientRect(); el.style.top = `${r.bottom + 8}px`; el.style.right = `${Math.max(12, innerWidth - r.right)}px` }
        })
      }
      if (onClick) b.addEventListener('click', onClick)
      return { button: b, panel: el, set, remove() { b.remove(); el?.remove(); bar.hidden = !bar.children.length } }
    },
    // the bottom-sheet behaviour of toolbar panels, for a plugin's own panel: returns a function that puts the
    // close button back (call it after replacing the panel's contents)
    sheet,
    // ask every open panel (toolbar panels, About, a plugin's sheet listening for it) to close
    closeAll() { document.dispatchEvent(new Event(CLOSE_ALL)) },
    onCloseAll(fn) { document.addEventListener(CLOSE_ALL, fn); return () => document.removeEventListener(CLOSE_ALL, fn) },
    get compact() { return compact() },
    toast(text, ms = 3000) {
      const t = document.createElement('div')
      t.className = 'panel toast'
      t.setAttribute('role', 'status')
      t.textContent = text
      document.body.append(t)
      setTimeout(() => t.remove(), ms)
    },
    // a line in the credits strip at the bottom of the page (a link with href): { set(text, href), remove() }
    credit(text, href) {
      let el = document.getElementById('credits')
      if (!el) {
        el = document.createElement('div')
        el.id = 'credits'
        el.className = 'panel'
        Object.assign(el.style, { left: '50%', bottom: '12px', transform: 'translateX(-50%)', padding: '4px 10px', fontSize: '11px', color: 'var(--muted)', zIndex: 15, display: 'flex', gap: '10px', whiteSpace: 'nowrap', maxWidth: 'calc(100% - 24px)', overflow: 'hidden' })
        document.body.append(el)
      }
      const s = document.createElement('a')
      s.target = '_blank'; s.rel = 'noopener'; s.style.color = 'inherit'
      el.append(s)
      const set = (t, h) => { s.textContent = t; if (h) s.href = h }
      set(text, href)
      return { set, remove() { s.remove(); if (!el.children.length) el.remove() } }
    },
  }
}

// Plugins listed in plugins/index.json (or the city's own list, city.place.plugins), plus ?plugin=<path> for local
// development (same origin only).
export async function loadPlugins(city) {
  const list = city.place?.plugins ? [...city.place.plugins] : await fetch('plugins/index.json').then((r) => r.json()).catch(() => [])
  for (const p of new URLSearchParams(location.search).getAll('plugin')) {
    const u = new URL(p, location.href)
    if (u.origin === location.origin) list.push(u.pathname)
    else console.warn('plugin from another origin ignored:', p)
  }
  const loaded = []
  for (const path of list) {
    try {
      const m = await import(new URL(path, location.href).href)
      loaded.push({ path, instance: (await m.default(city)) || {} })
    } catch (e) { console.error('plugin', path, e) }
  }
  return loaded
}
