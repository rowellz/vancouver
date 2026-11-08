# Roof colours from the City of Vancouver 2022 orthophoto. For every building footprint (OSM buildings and parts, and the
# city's 2009 LiDAR sections) inside the photo's coverage, samples the 0.5 m image (tools/ortho.sh) inside the footprint,
# 1 m in from its edges, drops tree and shadow pixels and keeps the dominant colour (and a second one on big roofs),
# into data/roofs.npz. tiles.py looks colours up with roof_lookup(ring); no npz, no colours.
# Run: uv run --with numpy,scipy,pillow,shapely,rasterio python3 tools/roofs.py [procs]
#      ... tools/roofs.py preview [yaletown|coalharbour|gastown|lat,lon]  (ortho vs sampled colours, data/ortho/preview-*.png)
#      ... tools/roofs.py check   (self-test of the colour maths and the lookup)
import glob, json, math, os, sys, time
from multiprocessing import Pool
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from common import xy, latlon, L1, save_rings, load_feat, DATA

TIF = f'{DATA}/ortho/tif'
OUT = f'{DATA}/roofs.npz'
RES = 0.5       # metres per pixel of the tifs
CHUNK = 400.0   # work unit, metres (UTM)
GREEN, WHITE = 1, 2  # flags: rooftop vegetation, white membrane
AREAS = {'yaletown': (49.2755, -123.1215), 'coalharbour': (49.2895, -123.1235), 'gastown': (49.2838, -123.1080)}


# ---------------------------------------------------------------- colour
def lin(c):
    c = np.asarray(c, np.float32) / 255
    return np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)


def lin_lab(c):
    x = c @ np.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192], [0.1805, 0.0722, 0.9505]], np.float32)
    x /= np.array([0.95047, 1.0, 1.08883], np.float32)
    f = np.where(x > 0.008856, np.cbrt(x), 7.787 * x + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def srgb_lab(c): return lin_lab(lin(c))


def lab_srgb(lab):
    lab = np.asarray(lab, np.float32)
    fy = (lab[..., 0] + 16) / 116; fx = fy + lab[..., 1] / 500; fz = fy - lab[..., 2] / 200
    f = np.stack([fx, fy, fz], -1)
    x = np.where(f > 0.2069, f ** 3, (f - 16 / 116) / 7.787) * np.array([0.95047, 1.0, 1.08883], np.float32)
    c = x @ np.array([[3.2406, -0.9689, 0.0557], [-1.5372, 1.8758, -0.2040], [-0.4986, 0.0415, 1.0570]], np.float32)
    c = np.where(c > 0.0031308, 1.055 * np.clip(c, 0, None) ** (1 / 2.4) - 0.055, 12.92 * c)
    return np.clip(np.round(c * 255), 0, 255).astype(np.uint8)


# White balance: the 2022 photo is warm. Its grey pixels (asphalt, concrete; median over the downtown tiles) sit at
# a*,b* = CAST; asphalt and concrete really are a little warm (NEUTRAL). Calibration knob, per flight if needed.
CAST, NEUTRAL = (-0.5, 11.0), (0.0, 3.0)
WB = lin(lab_srgb([60, *NEUTRAL])) / lin(lab_srgb([60, *CAST]))


def roof_of(px):
    """px: (n, 3) uint8 pixels inside the eroded footprint -> (lab, lab2, share2, flags, n_used) or None."""
    raw = srgb_lab(px)
    lab = lin_lab(np.clip(lin(px) * WB, 0, 1))
    a, b = raw[:, 1], raw[:, 2]
    veg = (a < -7) & (b > -5)  # trees over the roof, or a green roof
    flags = GREEN if veg.mean() > 0.35 else 0
    # shadows: blue-grey and dark (skylight only), or much darker than the roof's lit part. A black roof in the sun
    # stays: it is as warm as the rest of the photo, and its lit part is dark too.
    lit = np.percentile(raw[~veg, 0], 80) if (~veg).any() else 0
    shade = ((raw[:, 0] < 32) & (b < 3)) | (raw[:, 0] < min(lit * 0.62, lit - 12))
    keep = lab[~veg & ~shade]
    if len(keep) < max(4, 0.15 * len(lab)):
        if flags: keep = lab[veg]  # a green roof
        else: return None          # in a tower's shadow: no idea
    # two clusters in Lab, deterministic start at the darker and lighter quartiles
    c = np.array([np.percentile(keep, 25, axis=0), np.percentile(keep, 75, axis=0)], np.float32)
    for _ in range(8):
        k = ((keep[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
        c = np.array([np.median(keep[k == j], axis=0) if (k == j).any() else c[j] for j in (0, 1)], np.float32)
    share = np.array([(k == 0).mean(), (k == 1).mean()])
    d, s = (0, 1) if share[0] >= share[1] else (1, 0)
    if np.linalg.norm(c[0] - c[1]) < 15 or share[s] < 0.3 or len(keep) < 800:  # one colour (or a small roof): the median
        main, second, sh2 = np.median(keep, axis=0), np.median(keep, axis=0), 0.0
    else: main, second, sh2 = c[d], c[s], float(share[s])
    if main[0] > 80 and math.hypot(main[1], main[2]) < 10: flags |= WHITE
    return main, second, sh2, flags, len(keep)


# ---------------------------------------------------------------- geometry
def to_utm(x, y):
    from rasterio.warp import transform
    lat, lon = latlon(np.asarray(x, float), np.asarray(y, float))
    e, n = transform('EPSG:4326', 'EPSG:3157', np.atleast_1d(lon).tolist(), np.atleast_1d(lat).tolist())
    return np.array(e), np.array(n)


def tif_bounds():
    import rasterio
    out = []
    for f in sorted(glob.glob(f'{TIF}/*.tif')):
        with rasterio.open(f) as s: out.append((f, tuple(s.bounds)))
    return out


def footprints(cover):
    """All footprints (world-xy rings) whose centre is in the photo's coverage. cover: list of UTM boxes."""
    import shapely
    from shapely.geometry import box
    from shapely.ops import unary_union
    cu = unary_union([box(*b) for _, b in cover])
    # world-xy bounding box of the coverage, to pick the 4 km feature buckets
    e0, n0, e1, n1 = cu.bounds
    from rasterio.warp import transform
    lon, lat = transform('EPSG:3157', 'EPSG:4326', [e0, e1, e0, e1], [n0, n0, n1, n1])
    wx, wy = zip(*[xy(la, lo) for la, lo in zip(lat, lon)])
    rings, src = [], []
    for i in range(int(min(wx) // L1), int(max(wx) // L1) + 1):
        for j in range(int(min(wy) // L1), int(max(wy) // L1) + 1):
            for oid, prs, t in load_feat(i, j).get('buildings', []):
                for outer, inners in prs:
                    if len(outer) >= 4: rings.append(outer); src.append(1 if 'building' not in t else 0)
    for f in json.load(open(f'{DATA}/bf2009.geojson'))['features']:
        g = f['geometry']
        for poly in (g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']] if g['type'] == 'Polygon' else []):
            r = poly[0]
            lo = [p[0] for p in r]; la = [p[1] for p in r]
            if not (lat[0] - 0.01 < min(la) and max(la) < lat[3] + 0.01 and lon[0] - 0.01 < min(lo) and max(lo) < lon[3] + 0.01): continue
            rings.append([xy(b, a) for a, b in r]); src.append(2)
    # centres in UTM, keep the covered ones
    cx = np.array([np.mean([p[0] for p in r]) for r in rings]); cy = np.array([np.mean([p[1] for p in r]) for r in rings])
    ce, cn = to_utm(cx, cy)
    inside = shapely.contains_xy(cu, ce, cn)
    return [(rings[k], src[k], ce[k], cn[k]) for k in np.flatnonzero(inside)]


def sample_chunk(job):
    """job: (cover, [(ring_xy, src, ce, cn)]) -> list of rows."""
    import rasterio
    from rasterio.merge import merge
    from PIL import Image, ImageDraw
    from scipy import ndimage
    cover, fps = job
    if not fps: return []
    # all vertices to UTM at once
    allx = np.concatenate([[p[0] for p in f[0]] for f in fps]); ally = np.concatenate([[p[1] for p in f[0]] for f in fps])
    ue, un = to_utm(allx, ally)
    off = np.cumsum([0] + [len(f[0]) for f in fps])
    e0, n0, e1, n1 = ue.min() - 2, un.min() - 2, ue.max() + 2, un.max() + 2
    e0, n0 = math.floor(e0 / RES) * RES, math.floor(n0 / RES) * RES
    e1, n1 = math.ceil(e1 / RES) * RES, math.ceil(n1 / RES) * RES
    srcs = [rasterio.open(f) for f, b in cover if b[0] < e1 and b[2] > e0 and b[1] < n1 and b[3] > n0]
    img, _ = merge(srcs, bounds=(e0, n0, e1, n1), res=RES, nodata=0)
    for s in srcs: s.close()
    img = np.moveaxis(img[:3], 0, -1)  # rows from the north
    have = img.any(-1)
    H, W = have.shape
    rows = []
    for k, (ring, src, ce, cn) in enumerate(fps):
        px = (ue[off[k]:off[k + 1]] - e0) / RES; py = (n1 - un[off[k]:off[k + 1]]) / RES
        bx0, by0 = max(0, int(px.min()) - 1), max(0, int(py.min()) - 1)
        bx1, by1 = min(W, int(px.max()) + 2), min(H, int(py.max()) + 2)
        if bx1 - bx0 < 2 or by1 - by0 < 2: continue
        m = Image.new('L', (bx1 - bx0, by1 - by0), 0)
        ImageDraw.Draw(m).polygon(list(zip(px - bx0, py - by0)), fill=1)  # ponytail: outer ring only, courtyards count as roof; pass inners if big blocks look off
        m = np.array(m, bool)
        if m.sum() < 4 or have[by0:by1, bx0:bx1][m].mean() < 0.9: continue
        for it in (2, 1):  # 1 m in from the edges, less on small footprints
            m2 = ndimage.binary_erosion(m, iterations=it)
            if m2.sum() >= 8: m = m2; break
        r = roof_of(img[by0:by1, bx0:bx1][m])
        if r is None: continue
        main, second, sh2, flags, n = r
        rows.append((ring, src, main, second, sh2, flags))
    return rows


def chunks(fps, cover):
    by = {}
    for f in fps: by.setdefault((int(f[2] // CHUNK), int(f[3] // CHUNK)), []).append(f)
    return [(cover, by[k]) for k in sorted(by)]


def bake(procs):
    t0 = time.time()
    cover = tif_bounds()
    if not cover: sys.exit(f'no tifs in {TIF}: run tools/ortho.sh first')
    fps = footprints(cover)
    print(len(cover), 'ortho tiles,', len(fps), 'footprints inside', f'{time.time() - t0:.0f}s', flush=True)
    with Pool(procs) as pool: res = pool.map(sample_chunk, chunks(fps, cover), chunksize=1)
    rows = [r for rr in res for r in rr]
    save_rings(OUT, [r[0] for r in rows], src=np.array([r[1] for r in rows], np.uint8),
                        rgb=lab_srgb(np.array([r[2] for r in rows])), rgb2=lab_srgb(np.array([r[3] for r in rows])),
                        share2=np.array([round(r[4] * 100) for r in rows], np.uint8), flags=np.array([r[5] for r in rows], np.uint8))
    print(len(rows), 'roofs ->', OUT, f'{os.path.getsize(OUT) / 1e6:.1f} MB', f'{time.time() - t0:.0f}s', flush=True)


# ---------------------------------------------------------------- lookup (tiles.py)
def ring_db(path):
    """an npz of footprints (rc: ring points, ro: ring offsets) and per-footprint fields -> dict with an STRtree, or False"""
    import shapely
    if not os.path.exists(path): return False
    d = np.load(path)
    ro = d['ro']; ids = np.repeat(np.arange(len(ro) - 1), np.diff(ro))
    polys = shapely.make_valid(shapely.polygons(shapely.linearrings(d['rc'], indices=ids)))
    return dict(polys=polys, tree=shapely.STRtree(polys), **{k: d[k] for k in d.files if k not in ('rc', 'ro')})


def ring_match(db, ring):
    """the footprint in db that is this ring (best overlap), else the smallest one under its centre, else None"""
    import shapely
    from shapely.geometry import Polygon
    q = shapely.make_valid(Polygon(ring))
    hits = db['tree'].query(q, predicate='intersects')
    if not len(hits): return None
    inter = shapely.area(shapely.intersection(db['polys'][hits], q))
    iou = inter / (shapely.area(db['polys'][hits]) + q.area - inter)
    k = int(np.argmax(iou))
    if iou[k] < 0.3:  # a piece of a bigger sampled footprint (a tower split off its podium): the one under its centre
        inside = shapely.contains_xy(db['polys'][hits], *q.representative_point().coords[0])
        if not inside.any(): return None
        k = int(np.flatnonzero(inside)[np.argmin(shapely.area(db['polys'][hits][inside]))])
    return int(hits[k])


_DB = None


def roof_lookup(ring):
    """ring: world-xy footprint -> (0xRRGGBB, 0xRRGGBB second or 0, share2 %, flags) or None outside the photo."""
    global _DB
    if _DB is None: _DB = ring_db(OUT)
    if not _DB: return None
    h = ring_match(_DB, ring)
    if h is None: return None
    db = _DB
    hexc = lambda c: int(c[0]) << 16 | int(c[1]) << 8 | int(c[2])
    return hexc(db['rgb'][h]), hexc(db['rgb2'][h]) if db['share2'][h] else 0, int(db['share2'][h]), int(db['flags'][h])


def roof_colour_xy(ring):
    r = roof_lookup(ring)
    return r[0] if r else None


def roof_colour(ring_lonlat):
    """ring of (lon, lat) -> 0xRRGGBB or None."""
    return roof_colour_xy([xy(la, lo) for lo, la in ring_lonlat])


# ---------------------------------------------------------------- preview
def preview(name, half=160):
    import rasterio
    from rasterio.merge import merge
    from PIL import Image, ImageDraw
    lat, lon = AREAS[name] if name in AREAS else map(float, name.split(','))
    cx, cy = xy(lat, lon)
    ce, cn = to_utm([cx], [cy]); ce, cn = ce[0], cn[0]
    e0, n0, e1, n1 = ce - half, cn - half, ce + half, cn + half
    cover = tif_bounds()
    srcs = [rasterio.open(f) for f, b in cover if b[0] < e1 and b[2] > e0 and b[1] < n1 and b[3] > n0]
    img, _ = merge(srcs, bounds=(e0, n0, e1, n1), res=RES, nodata=0)
    wb = lin(np.moveaxis(img[:3], 0, -1)) * WB  # the photo white-balanced like the samples
    ortho = Image.fromarray(np.clip(np.where(wb > 0.0031308, 1.055 * np.clip(wb, 0, None) ** (1 / 2.4) - 0.055, 12.92 * wb) * 255, 0, 255).astype(np.uint8))
    flat = ortho.copy()  # sampled colours painted over the photo itself, so they compare honestly
    db = ring_db(OUT)  # (was _db(), which did not exist: preview crashed)
    import shapely
    from shapely.geometry import box
    hits = db['tree'].query(box(cx - half * 1.2, cy - half * 1.2, cx + half * 1.2, cy + half * 1.2))
    hits = sorted(hits, key=lambda h: -shapely.area(db['polys'][h]))  # small parts on top of their buildings
    dr = ImageDraw.Draw(flat); dro = ImageDraw.Draw(ortho)
    for h in hits:
        p = db['polys'][h]
        for g in getattr(p, 'geoms', [p]):
            if g.geom_type != 'Polygon': continue
            x, y = np.array(g.exterior.coords).T
            ue, un = to_utm(x, y)
            pts = list(zip((ue - e0) / RES, (n1 - un) / RES))
            c = tuple(int(v) for v in db['rgb'][h])
            dr.polygon(pts, fill=c)
            if db['share2'][h] >= 25:  # second colour as a dot
                q = g.representative_point(); qe, qn = to_utm([q.x], [q.y])
                X, Y = (qe[0] - e0) / RES, (n1 - qn[0]) / RES
                dr.ellipse((X - 5, Y - 5, X + 5, Y + 5), fill=tuple(int(v) for v in db['rgb2'][h]), outline=(40, 40, 40))
            dro.line(pts + pts[:1], fill=(255, 0, 255), width=1)
    W, H = ortho.size
    out = Image.new('RGB', (W * 2 + 8, H), (255, 255, 255)); out.paste(ortho, (0, 0)); out.paste(flat, (W + 8, 0))
    p = f'{DATA}/ortho/preview-{name}.png'
    out.save(p); print(p)


def check():
    import pickle
    lab = srgb_lab(np.array([[200, 60, 40], [30, 30, 30], [240, 240, 235]], np.uint8))
    assert np.abs(lab_srgb(lab).astype(int) - [[200, 60, 40], [30, 30, 30], [240, 240, 235]]).max() <= 1
    assert roof_colour([(-123.0, 49.40), (-122.999, 49.40), (-122.999, 49.401), (-123.0, 49.401)]) is None  # outside the photo
    cx, cy = xy(*AREAS['yaletown'])
    bs = [r[0] for _, prs, t in pickle.load(open(f'{DATA}/feat/-1_-1.pkl', 'rb'))['buildings'] for r in prs
          if 'building' in t and math.hypot(r[0][0][0] - cx, r[0][0][1] - cy) < 300]
    t0 = time.time(); got = [roof_colour_xy(r[:-1]) for r in bs]; dt = time.time() - t0
    assert sum(g is not None for g in got) > 0.8 * len(got), 'downtown buildings should mostly have a colour'
    print(f'ok: {sum(g is not None for g in got)}/{len(got)} downtown buildings coloured, {dt / len(bs) * 1e6:.0f} us per lookup')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'check': check()
    elif len(sys.argv) > 1 and sys.argv[1] == 'preview':
        for n in sys.argv[2:] or list(AREAS): preview(n)
    else: bake(int(sys.argv[1]) if len(sys.argv) > 1 else os.cpu_count())
