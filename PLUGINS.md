# Writing a plugin

Everything interactive on top of the city (music, weather, transit, ships, planes, games) is a plugin: an ES
module in `public/plugins/<name>/` whose default export receives the `city` object.

```js
// public/plugins/hello/index.js
export default function hello(city) {
  const { THREE } = city
  const marker = new THREE.Mesh(new THREE.ConeGeometry(4, 12, 4), new THREE.MeshLambertMaterial({ color: 0xff6040 }))
  marker.position.copy(city.world.fromLatLon(49.2827, -123.1207)) // on the ground at that spot
  marker.position.y += 6
  city.scene.add(marker)
  city.requestRender()
  return { dispose() { city.scene.remove(marker); city.requestRender() } }
}
```

Turn it on by adding its path to `public/plugins/index.json`, or try it locally with
`http://localhost:PORT/?plugin=plugins/hello/index.js` (same-origin paths only).

## Rules of the road

- **The page renders on demand.** Nothing is drawn unless something changed. After you change the scene call
  `city.requestRender()`. For animation call `const stop = city.animate()` and `stop()` when you are done (for
  example when your objects are off screen or too small to see); while any plugin animates, every frame is drawn
  and `city.on('frame', (dt, t) => …)` fires each frame.
- **Stay fast.** The target is 60 fps on a mid laptop and a phone. Use one `InstancedMesh` (or one merged
  geometry) per kind of thing, not thousands of meshes. Heavy simulation goes in a Web Worker. Pause work when
  `city.view.zoom` says your things are smaller than a pixel, and when `document.hidden`. Nothing per frame should
  allocate (no closures, arrays or `new Vector3` in a frame handler: reuse scratch objects), and a transparent
  `side: THREE.DoubleSide` material is drawn in two passes, recompiled in between every frame, unless you give it
  `forceSinglePass: true` (the same picture when it is flat or additive).
- **Don't fight the style.** Flat-shaded Lambert materials, the ink outline pass and the shared sun do the look.
  Objects in `city.scene` get outlines and shadows; `city.overlay` is drawn last without them (particles,
  labels, glows).
- **Clean up** in `dispose()`.
- **Credit data** you use (in a comment and, for user-visible data, in your UI).

## The `city` object

| | |
|---|---|
| `city.version` | API version (1) |
| `city.place` | which city the map is: `{ name, title, short, latlon: [lat, lon], tz }` (Vancouver, or another city baked with `CITY=<name>` and opened with `?city=<name>`, see the README). A plugin made for one city's data (transit, ships, landmarks) is listed only in that city's plugins |
| `city.THREE` | the three.js module the page uses (r170) |
| `city.scene` | the world; x east, y up, z south, 1 unit = 1 metre |
| `city.overlay` | drawn after everything, no outlines or shadows |
| `city.camera` | orthographic camera looking north, 30° down; `camera.zoom` is CSS px per drawing px |
| `city.renderer`, `city.canvas`, `city.controls` | three.js renderer, its canvas, the MapControls |
| `city.world.fromLatLon(lat, lon)` | `THREE.Vector3` on the ground at that point |
| `city.world.toLatLon(x, z)` | `{ lat, lon }` |
| `city.world.heightAt(x, z)` | ground height in metres (real elevation, from the finest loaded tile; 0 where nothing is loaded yet) |
| `city.world.offRoad(x, z, margin = 0.6, maxD = 8)` | for street furniture: the nearest `{ x, z }` at least `margin` m off the carriageways and cycle tracks the map draws (detail tiles), within `maxD` m; `null` where `(x, z)` is clear already or no detail tile is loaded there |
| `city.world.eachTile(fn)` | `fn(tile)` for every loaded map tile now and later (`{ level, i, j, ox, oy, size, graph, terrain, footprints, water, trees }`, see below); returns an unsubscribe |
| `city.world.suppressBuildings(ring)` | `[[lat, lon], …]`: the generic building there is removed because your plugin draws its own |
| `city.view.zoom`, `.center`, `.metresPerPixel`, `.bounds` | what is on screen (`bounds`: `{x0, z0, x1, z1}`) |
| `city.view.useCamera(camera, opts)` | draw the map through your own camera, e.g. a `PerspectiveCamera` for a game (see below) |
| `city.view.release()` | back to the map camera, exactly where it was |
| `city.view.camera` | the camera the map is drawn with now: `city.camera`, or yours |
| `city.light.sun`, `city.light.hemi` | the `DirectionalLight` (casts the shadows) and `HemisphereLight` |
| `city.light.setSunDirection(v)` | move the sun (a `Vector3` pointing towards it); shadows follow |
| `city.light.night` | 0 (day) to 1 (night): lights a share of the windows |
| `city.surface.wet`, `.snow`, `.sky` | weather on the map's ground, roads and roofs: `wet` (0–1) darkens them and makes paving mirror `sky` (a `THREE.Color`) at grazing angles; `snow` (0–1) whitens flat ground and roofs, the roads less (set by the weather plugin) |
| `city.surface.seaHole` | a `Vector4` (x, sea level, z, radius): the map's sea is left out within radius of (x, z), up to 3 m above sea level, where a plugin draws its own water (the sail plugin's waves); radius 0 (the default) turns it off |
| `city.post.uniforms.tint` / `.desat` | grade of the final image: multiply (`Vector3`, 1 = none) and desaturate (0–1) |
| `city.tiles` | the tile streamer (advanced) |
| `city.on(event, fn)` | `'frame'` `(dt, t)`, `'view'` `(view)`, `'quality'` `(tier)`, `'tile:load'` / `'tile:unload'` `(tile)`, `'weather'` `(weather)`, `'player'` `(player)`; returns an unsubscribe function |
| `city.requestRender()`, `city.animate()` | see above |
| `city.ui.addButton({ label, title, icon, onClick, panel })` | a toolbar button at the top right (a dock at the bottom on phones); with `panel: true`, `.panel` is a popover element you fill (a bottom sheet on phones); `.set({ icon, label })` relabels it |
| `city.ui.sheet(el, close)`, `.closeAll()`, `.onCloseAll(fn)`, `.compact` | your own panel behaving like the toolbar's (close button, drag down to close on phones; only one open at a time); `compact` is true in the phone layout (`html.compact` in CSS) |
| `city.ui.toast(text)` | a short message |
| `city.ui.credit(text, href)` | a line in the credits strip at the bottom of the page (a link with `href`); `.set(text, href)`, `.remove()` |
| `city.traffic` | the simulated cars, from the life plugin (see below); `undefined` without it |
| `city.quality.tier` | graphics quality (`core/quality.js`): `name` `'high'` or `'low'` and its knobs, e.g. `life` and `weather` (the share of agents / particles to draw) and `detail`; `city.on('quality', tier)` fires when it changes (the viewer's About → Graphics, or Auto dropping to low on a slow GPU). Scale your counts by it |

`window.city` is the same object, handy from the browser console.

## Map tiles

The map streams in as you look around: 4096 m tiles (`level 1`) for the overview and 1024 m tiles (`level 0`) up
close. New tiles are built and uploaded to the GPU a few MB per frame, so streaming never stalls a frame;
`'tile:load'` fires once a tile is complete. Each tile carries its road graph so traffic, people and games can move on real streets:

- `tile.graph.pieces`: `{ kind, lanes, oneway, drive, band, crossing, turns, w, a, b, pts, z }` (`band`: the road has sidewalks drawn) per road piece between junctions. `kind`
  indexes `motorway, trunk, primary, secondary, tertiary, residential, service, living, pedestrian, footway,
  cycleway, path, steps, track, rail, lightrail, runway, taxiway`; `a`/`b` are OpenStreetMap node ids (they match
  across tiles); `pts` is a flat `Float32Array` of world x, north pairs; `z` is the deck height on bridges (detail
  and overview tiles), else `null` (use `heightAt`). `oneway` pieces run from `a` to `b`. `w` is the carriageway
  width in metres. `crossing`: a footway or cycleway that crosses a street (it lies on the street). `turns`: OSM turn
  lanes or `null`, `{ f, b }` with a bitmask per lane, left to right, for the way `a` → `b` (`f`) and back (`b`;
  empty arrays where not mapped): 1 left, 2 slight left, 4 through, 8 slight right, 16 right, 32 u-turn, 64 merge
  (0: not given).
- `tile.graph.pois`: `{ kind, id, x, y }` points (detail tiles): `kind` 0 traffic signals, 1 crossing, 2 stop sign,
  3 street lamp, 4 bench, 5 bus stop, 6 give way; `id` is the OSM node id (signals on a junction share the id of
  the graph pieces' end there).
- `tile.terrain`: `{ n, h, size }`, an `n`×`n` height grid over the tile, row 0 north.
- `tile.footprints` (detail tiles): building outlines for collisions, `{ pts, start }`: ring `r` is the world x,
  north pairs `pts[start[r]] … pts[start[r + 1] - 1]`. Parts that start more than 2.5 m up (you can drive under
  them) and buildings a plugin replaced with `suppressBuildings` are left out.
- `tile.water` (detail tiles with water): `{ n, mask }`, an `n`×`n` `Uint8Array`, 1 where the cell is water; cell
  `(i, j)` counts east and north from the tile's south-west corner (`ox`, `oy`), `size / n` metres each.
- `tile.trees` (tiles with trees, else `null`): `{ pos, size }`, `Float32Array`s with 3 and 2 numbers a tree: its foot
  relative to the tile's corner (`x - ox`, ground height, `-(north - oy)`) and its crown's width and height. The trunk
  of a tree `w` metres wide is about `0.035 w` in radius (the drive plugin bumps into it). Tiles already loaded when
  you call `eachTile` come as the streamer's own objects, whose `trees` is the billboard mesh: read its geometry's
  `iPos` and `iSize` attributes the same way.

Detail tiles overlap their parent overview tile, so a plugin that wants each road once should use level 0 tiles
(and level 1 only where no level 0 tile is loaded).

## Your own camera

A game can take over the view: `city.view.useCamera(camera, opts)` stops the MapControls and draws the map, the
trees and `city.overlay` through `camera` (orthographic or perspective) until `city.view.release()`, which puts
the map camera back exactly where it was. `opts` is read every frame, so keep moving it rather than calling again:

- `opts.focus` (`Vector3`, required): the ground point the map streams around, usually a little ahead of the player.
- `opts.radius` (metres, default 900): tiles load within this square around the focus; 1100 or less loads the
  detail tiles (and their `graph`, `footprints`, `water`), more shows the overview tiles.
- `opts.shadowRadius` (metres, default 300): the sharp sun shadows cover this square around the focus. With a
  perspective camera the square follows the camera instead, reaching from just behind it to 1.5× the radius ahead,
  and is redrawn each time the camera has moved on a fifth of it, so give moving things a blob shadow instead of
  `castShadow`.

You own the camera: set its pose, `fov`, `aspect` (on resize), `near` and `far` every frame as you need. With a
perspective camera the ink outlines are found from depth properly linearised and fade out over `scene.fog` (a
`THREE.Fog` you set on `city.scene` is used by the map and the trees alike). While your camera is active,
`city.view.zoom` is the map zoom that shows the focus at the same scale, `center` is the focus and `bounds` the
streamed square, and `'view'` fires as the focus moves. A perspective camera also gets what street level needs:
trees within ~190 m are real 3D trees (the billboards beyond stand upright), and those trees and the building faces between the camera and `city.player` (see below) dissolve so the player stays in view, fog fades into the sky texture you set
as `scene.background` rather than one flat colour, detail tiles stay up while new ones arrive past the fog, and
when frames keep coming late the view is drawn at a lower resolution (down to 0.75 CSS px per pixel; `release()`
restores it). Its ink lines are smoothed by FXAA below 1.5x pixel ratio (`view.setAA('auto' | 'fxaa' | 'msaa' | 'off')`
in `main.js` picks it, for quality tiers). Nothing changes for the map view when no plugin has a
camera. The drive plugin is the example: it dives from the map into a chase camera behind a van (or a walker) and back.

`core/play.js` has what such a game needs, shared by the drive and sail plugins: `mapPicker` (a click or tap on the
map to the ground under it), `mapCameraPose`, `poseOf` and `dive` (the dive from the map into your chase camera and
back), `streetSky` (the weather's sky gradient and fog at street level), `touchButtons`, `typing`, `buzz` and
`MOVE_KEYS`, and one shared `AudioContext` (`audioContext()`, with `tone()` and `noise()`): hang your own gain off it.

## Shared code

Plugins may import from `core/` (relative paths, e.g. `'../../core/util.js'`):

- `core/util.js` (no three.js, no DOM): `clamp`, `smooth`, `wrap`, `EASE`, `mulberry32` (seeded random),
  `vancouver(ms)` (the wall clock in the city: Vancouver's, or `city.place.tz`), `getJSON`, `esc` (for `innerHTML`), `inPoly`, `circleBox`,
  `sampleGrid` and `NOISE_GLSL` (hash and value noise for shaders).
- `core/kit.js`: the low-poly model kit (see the player, below); `k.geometry(true)` with `kitMaterial(THREE)` for
  plain vertex-coloured models.
- `core/play.js`: for a plugin with its own camera (above).

## Shared between plugins

- **Weather** (`plugins/weather`): `city.weather = { kind, intensity, isNight, hour, live, wind, atmosphere }`, re-emitted
  as `'weather'` on change. `wind` is `[x, z]` m/s (towards); `atmosphere = { horizon, zenith, fogNear, fogFar }` (hex
  colours, metres) is the street-level look a plugin with a perspective camera applies as its sky gradient and
  `THREE.Fog` (`fogNear` can be below zero: three.js fades fog in with a smoothstep, so that veils the middle
  distance in fog and rain). Under such a camera the weather plugin draws, by itself: a sky dome in `city.scene`
  (inside the camera's far plane, only where nothing else is; its horizon follows `scene.fog.color`; it fades in as
  the camera's `fov` passes 14°–40°, so a dive from the map looks like the map first) with the sun or moon and
  stars, drifting clouds or a low deck, and lightning bolts; rain and snow as a world-space volume around the camera
  with splashes around `city.player`; the scene lit up in lightning. The map view keeps its screen-space layer.
  Both views get wet or snowy ground through `city.surface`.
- **Traffic** (`plugins/life`): `city.traffic.nearby(x, z, r)` → `[{ id, x, y, z, heading, length, width, speed }]`
  (forward = `(sin heading, 0, cos heading)`), `city.traffic.setObstacle(id, obj | null)` (a player's vehicle or walker
  `{ x, z, heading, length, width, speed, y }` that cars brake for; see Traffic below), `city.traffic.bump(id, impulse)` (a car was hit),
  `city.traffic.people(x, z, r, out)` and `city.traffic.poles(x, z, r, out)` (the people and the traffic signal poles
  there), `city.traffic.knock(id, dx, dz, speed)` (a person was bumped into: they stumble, or fall over and get up).
- **The player** (`plugins/drive`, `plugins/sail`): while the user drives, walks, hikes, drives a bus or sails, `city.player = { mode: 'car' |
  'foot' | 'bus' | 'boat', x, y, z, heading, speed, colour, vehicle: 'van' | 'bus' | 'boat' }` (updated every frame, read it when you
  need it), else `null`; `'player'` is emitted on enter, leave and car ⇄ foot. `city.models.van(colour)`,
  `city.models.person(colour)`, `city.models.bus()` (a 12 m city bus, its middle at the origin) and `city.models.boat(colour)` return the same low-poly models (forward −z, origin on the
  ground or the waterline; `update(dt, speed)` runs the walk cycle or bobs the boat), so other players look alike.
  They are built with `core/kit.js`, a small kit for flat-shaded low-poly models (bevelled profiles, boxes,
  rods, wheels merged into one geometry with vertex colours) and their shared look (`shaded(material)`: a little
  darker towards the ground, a cool rim light); the life plugin's street-level cars, people and signals use it too.
  The drive plugin publishes its van and walker with `setObstacle('player-van' | 'player-walker', …)` and collides
  with `city.traffic.nearby` and, when present, `city.multiplayer.nearby(x, z, r)` (same shape).
- **Ships** (`plugins/ships`): `city.ships.nearby(x, z, r)`, the vessels within `r` metres of their hull, in the
  shape of `city.traffic.nearby` (anchored ones with speed 0). The sail plugin bumps into them.
- **The sea while sailing** (`plugins/sail/water.js`): the sail plugin swaps the flat sea within a kilometre of the boat
  for a wave surface driven by the wind (`city.surface.seaHole`); `city.player.y` rides the waves. Its grid size follows
  `city.quality.tier` (`'low'`, and phones: a coarser grid); a tier can set its own as `tier.sea = { n, glints }` (grid
  vertices a side, sun glints on or off).
- **Bridge piers** (`plugins/landmarks`): `city.landmarks.piers`, a flat array of boxes standing in the water,
  `x, z, half length, half width, rot` each (the long axis is `(cos rot, −sin rot)` in x / z).
- **A game on screen**: a plugin that takes over the screen with its own camera and controls adds the classes
  `dlv-on` (and `dlv-touchy` with on-screen touch controls) to `document.body`: the toolbar steps aside and the chat
  button, the music player and toasts make room for the controls.

## Traffic

The life plugin publishes `city.traffic` when it starts and emits `'traffic:ready'` with it, so a game can bump
into the simulated cars and have them brake for the player. Check it exists (or listen for the event): without
the life plugin there is none. Positions are three.js world coordinates; `heading` is the yaw, the forward vector
being `(sin(heading), 0, cos(heading))`.

- `city.traffic.nearby(x, z, r)`: the cars within `r` metres, `[{ id, x, y, z, heading, length, width, speed }]`
  (metres, m/s), from the latest frame. Cheap enough to call every frame; empty while traffic is hidden (zoomed
  out, switched off, tab hidden).
- `city.traffic.setObstacle(id, { x, z, heading, length, width, speed, y })`: something the player drives or
  walks. Cars treat it as a car they must never drive into: they follow it in the lane it is in (every lane or
  junction path that passes within 1.5 m of its outline), pass it by changing lanes and don't enter a junction it
  blocks; people step around it. `y` (optional, its height) limits it to lanes at that level, so a van on a
  bridge deck leaves the street below alone. Call it as often as you like (it reaches the simulation at most 10
  times a second); `setObstacle(id, null)` removes it.
- `city.traffic.bump(id)`: the player hit car `id`; it stops for a few seconds, then drives on.
- `city.traffic.people(x, z, r, out = [])`: the people on foot within `r` metres (walking, waiting, on benches; not the
  cyclists), `[{ id, x, y, z, heading }]` (they face `(sin heading, 0, cos heading)`). Empty while people are hidden
  (zoomed out, switched off). The array `out` is cleared and filled with objects that are reused on the next call, so
  asking every frame makes no garbage: read them right away. The page copies people out of the simulation only while
  someone has asked in the last two seconds.
- `city.traffic.poles(x, z, r, out = [])`: the traffic signal poles within `r` metres, `[{ x, y, z, r }]` (`y` the foot,
  `r` the pole's radius), reused the same way.
- `city.traffic.knock(id, dx, dz, speed)`: the player bumped into person `id`, pushing towards `(dx, dz)` at `speed` m/s:
  a stumble at a walk, faster a fall onto their back (facing what hit them); they get up after a few seconds and walk
  on. People also hop out of the way of an obstacle coming at them faster than 3 m/s, when they can.
