# What the bake tools share. The map's frame: world metres east (x) and north (y) of the city's origin (downtown
# Vancouver; tools/cities/<CITY>.py), equirectangular. Heights are real elevations in metres (Vancouver: CGVD2013, from
# NRCan HRDEM / MRDEM; Manhattan: NAVD88, from the 2017 NYC LiDAR / USGS 3DEP).
# Also: OpenStreetMap line kinds, the per-tile inputs on disk (data/feat, data/hr), ring databases, retries.
# Light on purpose (the standard library; numpy only inside the functions that need it): importing it costs
# nothing, unlike tiles.py, which loads its inputs.
import math, os, pickle, time

from cities import city, NAME as CITY, DATA, TILES  # CITY=<name>: tools/cities/<name>.py (default vancouver)

LAT_O, LON_O = city.ORIGIN
KX = getattr(city, 'KX', None) or 111320 * math.cos(math.radians(city.KREF_LAT)) / city.SC  # metres per degree of longitude
KY = getattr(city, 'KY', None) or 110574 / city.SC                                           # metres per degree of latitude
BBOX = city.BBOX                                     # S, W, N, E
L0, L1, L2 = 1024, 4096, 16384                       # tile sizes per level of detail, metres
HR = 2.0                                             # HRDEM cell (data/hr), metres


def xy(lat, lon):
    return ((lon - LON_O) * KX, (lat - LAT_O) * KY)


def latlon(x, y):
    return (LAT_O + y / KY, LON_O + x / KX)


def extent():
    x0, y0 = xy(BBOX[0], BBOX[1]); x1, y1 = xy(BBOX[2], BBOX[3])
    return x0, y0, x1, y1


def all_tiles():
    """every 4096 m tile (i, j) over BBOX"""
    x0, y0, x1, y1 = extent()
    return [(i, j) for i in range(int(x0 // L1), int(x1 // L1) + 1) for j in range(int(y0 // L1), int(y1 // L1) + 1)]


def tile_args(argv):
    """'i_j' command-line arguments -> [(i, j)]"""
    return [tuple(map(int, a.split('_'))) for a in argv]


# ---------------------------------------------------------------- ground classes (draw order = list order; the page's GROUND_COLOURS)
GROUND = ['land', 'residential', 'commercial', 'industrial', 'farmland', 'grass', 'golf', 'scrub', 'wetland', 'forest', 'sand',
          'rock', 'construction', 'apron', 'parking', 'plaza', 'playground', 'pitch', 'track', 'water', 'pier', 'pool', 'snow',
          'trail', 'walk', 'cycle', 'ballast', 'sidewalk', 'blvd', 'road', 'arterial']  # streets last: they win where things overlap
GI = {k: i for i, k in enumerate(GROUND)}


def read_tile(path):
    """a baked tile -> (bytes, ox, oy, size, {section tag: (offset, length)}, ring): ring(o) reads the delta-encoded
    points at o -> (world x, north array, next offset)"""
    import gzip, struct
    import numpy as np
    b = gzip.decompress(open(path, 'rb').read())
    ox, oy, size = struct.unpack_from('<fff', b, 8)
    k = (2 * size) / 32766
    sec, o = {}, 20
    while o < len(b):
        tag = b[o:o + 4].decode(); n = struct.unpack_from('<I', b, o + 4)[0]; sec[tag] = (o + 8, n); o += 8 + n

    def ring(o):
        n = struct.unpack_from('<I', b, o)[0]
        d = np.frombuffer(b, '<i2', 2 * n, o + 4).reshape(-1, 2).astype(np.int64).cumsum(0)
        return d * k + (ox, oy), o + 4 + 4 * n
    return b, ox, oy, size, sec, ring


# ---------------------------------------------------------------- roads and other lines
# kind ids shared with the page (KIND in public/core/tile-worker.js)
KINDS = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'service', 'living', 'pedestrian', 'footway', 'cycleway',
         'path', 'steps', 'track', 'rail', 'lightrail', 'runway', 'taxiway']
KI = {k: i for i, k in enumerate(KINDS)}
DRIVE = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'service', 'living'}
# amenities that count as shops (a storefront ground floor)
SHOP_AMENITY = {'restaurant', 'cafe', 'fast_food', 'bar', 'pub', 'bank', 'pharmacy', 'ice_cream', 'clinic', 'dentist', 'doctors', 'post_office',
                'library', 'cinema', 'theatre', 'nightclub', 'food_court', 'bureau_de_change', 'veterinary', 'marketplace', 'community_centre'}


def line_kind(t):
    h = t.get('highway', ''); r = t.get('railway', ''); a = t.get('aeroway', '')
    if h:
        h = h.replace('_link', '')
        if h in ('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'service', 'pedestrian', 'footway', 'cycleway', 'path', 'steps', 'track'): return h
        if h in ('unclassified', 'road'): return 'residential'
        if h == 'living_street': return 'living'
        if h in ('bridleway', 'corridor'): return 'path'
        return None
    if r in ('rail', 'preserved', 'narrow_gauge') and t.get('usage') != 'industrial' or r == 'rail': return 'rail'
    if r in ('light_rail', 'subway', 'tram', 'monorail'): return 'lightrail'
    if a in ('runway', 'taxiway'): return a
    return None


def hnum(v):
    """a number from an OSM tag ('12 m', '3;4', '2,5'), or None"""
    try: return float(str(v).split(';')[0].replace('m', '').replace(',', '.').strip())
    except (ValueError, TypeError): return None


def densify(pts, step):
    out = [pts[0]]
    for p, q in zip(pts, pts[1:]):
        L = math.dist(p, q); n = max(1, int(L / step))
        for k in range(1, n + 1): out.append((p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n))
    return out


# ---------------------------------------------------------------- per-tile inputs on disk
def load_feat(i, j):
    """the OpenStreetMap features of a 4096 m tile (osm_split.py: data/feat/<i>_<j>.pkl), {} if there are none"""
    p = f'{DATA}/feat/{i}_{j}.pkl'
    if not os.path.exists(p): return {}
    with open(p, 'rb') as f: return pickle.load(f)


HR_CACHE = {}


def hr_tile(i, j):
    """(dsm, dtm) of a 4 km HRDEM tile (hrdem.py: data/hr/<i>_<j>.npz), or None"""
    import numpy as np
    if (i, j) not in HR_CACHE:
        p = f'{DATA}/hr/{i}_{j}.npz'
        d = np.load(p) if os.path.exists(p) else None
        HR_CACHE[(i, j)] = (d['dsm'], d['dtm']) if d is not None and 'dsm' in d and 'dtm' in d else None
        if len(HR_CACHE) > 12: HR_CACHE.pop(next(iter(HR_CACHE)))
    return HR_CACHE[(i, j)]


def hr_lookup(xs, ys, k):
    """LiDAR surface (k=0) or ground (k=1) at world points, NaN where there is none"""
    import numpy as np
    xs, ys = np.asarray(xs, float).ravel(), np.asarray(ys, float).ravel()
    out = np.full(xs.shape, np.nan, np.float32)
    ti, tj = np.floor(xs / L1).astype(int), np.floor(ys / L1).astype(int)
    for i, j in set(zip(ti.tolist(), tj.tolist())):
        a = hr_tile(i, j)
        if a is None: continue
        sel = (ti == i) & (tj == j)
        r = ((((j + 1) * L1) - ys[sel]) / HR).astype(int).clip(0, a[k].shape[0] - 1)
        c = ((xs[sel] - i * L1) / HR).astype(int).clip(0, a[k].shape[1] - 1)
        out[sel] = a[k][r, c]
    return out


def save_rings(path, rings, **cols):
    """a footprint database (roofs.py, facades.py, mapillary.py; read by roofs.ring_db): every ring's points in a row
    (rc), where each starts (ro), and per ring a value of each of cols"""
    import numpy as np
    rc = np.array([p for r in rings for p in r], np.float32)
    ro = np.cumsum([0] + [len(r) for r in rings]).astype(np.int32)
    np.savez_compressed(path, rc=rc, ro=ro, **cols)


def retry(fn, tries=4, wait=5):
    """fn(), tried again after wait, 2 wait, … seconds when it raises; the last error is raised"""
    for k in range(tries):
        try: return fn()
        except Exception:
            if k == tries - 1: raise
            time.sleep(wait * (k + 1))
