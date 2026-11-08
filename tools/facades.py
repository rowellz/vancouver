# Facade looks per building footprint (the walls, as roofs.py does the roofs), into data/facades.npz; tiles.py looks
# them up with facade_lookup(ring). Every source is optional, a missing one is skipped:
#   data/facades/tax2026.csv + parcels.geojson: the City of Vancouver property tax report (year built, strata units)
#     on its parcel polygons: the building's era, which decides concrete or glass, balconies, window bands
#   data/facades/heritage.geojson: the Vancouver Heritage Register (evaluation group A/B/C)
#   data/lidar2022/las/*.las: the city's 2022 LiDAR (about 50 points/m2, walls included): the storey height from the
#     rhythm of the wall returns (its point colours clip to white in the sun and go sky-blue in the shade: not used)
#   LOOKS below: well-known buildings, from their descriptions and public photos
#   Mapillary street photos (CC BY-SA 4.0; tools/mapillary.py, data/mapillary/walls.npz): wall colours seen from the street
# Fetch: tools/fetch.sh (tax report, parcels, heritage); the LiDAR's 1 km zips (data/lidar2022/urls.json) sit behind a
# browser check: download them by hand into data/lidar2022/ and `unzip -j -o '*.zip' '*.las' -d las`.
# Run: uv run --with numpy,shapely,pyproj python3 tools/facades.py [procs]      (... check: self-test)
import csv, glob, json, math, os, struct, sys, time
from multiprocessing import Pool
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from common import xy, latlon, L1, save_rings, load_feat, DATA
from roofs import ring_db, ring_match

OUT = f'{DATA}/facades.npz'
SRC = f'{DATA}/facades'
LAS = f'{DATA}/lidar2022/las'
MLY = f'{DATA}/mapillary/walls.npz'  # tools/mapillary.py
CITY = (-3, 1, -3, 0)  # the 4 km feature buckets (i0, i1, j0, j1) that cover the City of Vancouver and UBC
NONE, GLASS, SOLID = 1, 2, 3  # balcony kinds (0: tiles.py's rules decide)

# Well-known buildings: OSM ways (buildings or parts) -> wall colour (as seen), facade pattern (public/core/tile-worker.js:
# 1 punched windows, 2 curtain wall, 3 glass balconies, 9 glass condo, 10 window bands), balconies, storey height (m).
LOOKS = [
    # Vancouver House (BIG, 2020): white aluminium frames, every flat a projecting glass-railed balcony, flat white soffits
    dict(name='Vancouver House', ids=[742009401, 1092241825], wall=0xeceeec, pattern=3, balc=GLASS, storey=3.05),
    # Shangri-La (James K.M. Cheng, 2008; mapped as the Hyatt next door): a deep blue glass curtain wall on dark frames
    dict(name='Living Shangri-La', ids=[91998136, 360939772, 360939773, 360939774, 360939775], wall=0x344660, pattern=2, balc=NONE, storey=3.2),
    # Harbour Centre (1977): a grid of beige precast concrete with deep-set square dark windows, the Lookout on top
    dict(name='Harbour Centre', ids=[363385368, 363385948], wall=0xc9bea6, pattern=1, balc=NONE, storey=3.8),
    # Sun Tower (1912): red-brown brick over a cream terracotta base and trim, a green copper dome
    dict(name='Sun Tower', ids=[299782896, 360621717, 360621719, 360621720], wall=0x8e4a36, pattern=1, balc=NONE, storey=3.6),
    # Woodward's W (Henriquez, 2010): dark glass between red-orange slab edges and piers, glass balconies all round
    dict(name="Woodward's W", ids=[139571559, 363410790, 360670338, 360670339], wall=0xb4553e, pattern=3, balc=GLASS, storey=2.9),
    # Telus Garden Residences (Henriquez, 2016): blue-green glass with white slab edges, glass balconies all round
    dict(name='Telus Garden Residences', ids=[1097852702], wall=0xf0f2f2, pattern=9, balc=GLASS, storey=2.9),
    # Hotel Vancouver (1939): light grey-tan Haddington Island stone, punched windows, the green copper chateau roof
    dict(name='Fairmont Hotel Vancouver', ids=[139571554, 360433181, 360433183, 360433184, 360433185, 361655262], wall=0xc6c0b0, pattern=1, balc=NONE, storey=3.4),
]
FIELDS = ('year', 'units', 'heritage', 'wall', 'wsrc', 'storey', 'balc', 'facing', 'pattern')


# ---------------------------------------------------------------- footprints, era, heritage
def footprints():
    """every OSM building and part in the city's buckets: (osm id, world-xy ring, name)"""
    out = []
    for i in range(CITY[0], CITY[1] + 1):
        for j in range(CITY[2], CITY[3] + 1):
            for oid, prs, t in load_feat(i, j).get('buildings', []):
                for outer, inners in prs:
                    r = outer[:-1] if outer[0] == outer[-1] else outer
                    c = np.mean(r, axis=0)
                    if len(r) >= 3 and i * L1 <= c[0] < (i + 1) * L1 and j * L1 <= c[1] < (j + 1) * L1: out.append((oid, r, t.get('name', '')))
    return out


def eras(fps):
    """per footprint: (year built or 0, units on its parcel, heritage group 0 / 1 A / 2 B / 3 C)"""
    import shapely
    n = len(fps)
    year, units, her = np.zeros(n, np.uint16), np.zeros(n, np.uint16), np.zeros(n, np.uint8)
    if not os.path.exists(f'{SRC}/parcels.geojson'): return year, units, her
    tax = {}
    with open(f'{SRC}/tax2026.csv', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f, delimiter=';'):
            y = int(r['year_built'] or 0)
            e = tax.setdefault(r['land_coordinate'], [[], 0])
            if y > 1850: e[0].append(y)
            e[1] += 1
    polys, keys = [], []
    for ft in json.load(open(f'{SRC}/parcels.geojson'))['features']:
        g = ft['geometry']
        if not g or g['type'] != 'Polygon': continue
        polys.append(shapely.Polygon([xy(la, lo) for lo, la in g['coordinates'][0]]))
        keys.append(ft['properties']['tax_coord'])
    tree = shapely.STRtree(polys)
    rings = shapely.make_valid(np.array([shapely.Polygon(r) for _, r, _ in fps]))
    pts = shapely.point_on_surface(rings)
    fi, pi = tree.query(pts, predicate='within')
    parcel = np.full(n, -1); parcel[fi] = pi
    for k in range(n):
        if parcel[k] < 0: continue
        e = tax.get(keys[parcel[k]])
        if e: year[k], units[k] = int(np.median(e[0])) if e[0] else 0, min(e[1], 65535)
    # heritage: the register's point (at the address) -> the footprint under it, else the biggest on its parcel
    hp, hg = [], []
    for ft in json.load(open(f'{SRC}/heritage.geojson'))['features']:
        pr = ft['properties']
        if pr.get('category') != 'HERITAGE BUILDINGS' or pr.get('status') != 'Active' or not ft['geometry']: continue
        lo, la = ft['geometry']['coordinates']
        hp.append(xy(la, lo)); hg.append({'A': 1, 'B': 2, 'C': 3}.get(pr.get('evaluationgroup'), 3))
    ftree = shapely.STRtree(rings)
    hpts = shapely.points(hp)
    hi, ri = ftree.query(hpts, predicate='within')
    for a, b in zip(hi, ri): her[b] = hg[a]
    got = set(hi)
    pa, pb = tree.query(hpts, predicate='within')
    by_parcel = {}
    for k in range(n):
        if parcel[k] >= 0: by_parcel.setdefault(parcel[k], []).append(k)
    for a, b in zip(pa, pb):
        if a in got or b not in by_parcel: continue
        k = max(by_parcel[b], key=lambda k: shapely.area(rings[k]))
        her[k] = her[k] or hg[a]
    return year, units, her


# ---------------------------------------------------------------- 2022 LiDAR
def las_header(path):
    b = open(path, 'rb').read(375)
    off = struct.unpack('<I', b[96:100])[0]; pf = b[104] & 63; rl = struct.unpack('<H', b[105:107])[0]
    sc = struct.unpack('<3d', b[131:155]); of = struct.unpack('<3d', b[155:179])
    mx, mn = struct.unpack('<6d', b[179:227])[0::2], struct.unpack('<6d', b[179:227])[1::2]
    return off, pf, rl, sc, of, mn, mx


def las_points(path):
    """the LAS file as arrays (x, y, z in metres, class), points sorted into 4 m cells"""
    off, pf, rl, sc, of, mn, mx = las_header(path)
    dt = np.dtype(dict(names=['x', 'y', 'z', 'cls'], formats=['<i4', '<i4', '<i4', 'u1'], offsets=[0, 4, 8, 16 if pf >= 6 else 15], itemsize=rl))
    p = np.memmap(path, dt, 'r', offset=off)
    cls = np.asarray(p['cls']) & 31
    keep = cls != 7  # noise
    x = (p['x'][keep] * sc[0] + of[0]); y = (p['y'][keep] * sc[1] + of[1]); z = (p['z'][keep] * sc[2] + of[2]).astype(np.float32)
    cls = cls[keep]
    nx = int((mx[0] - mn[0]) // 4) + 1
    key = ((y - mn[1]) // 4).astype(np.int64) * nx + ((x - mn[0]) // 4).astype(np.int64)
    o = np.argsort(key, kind='stable')
    return dict(x=x[o], y=y[o], z=z[o], cls=cls[o], key=key[o], nx=nx, x0=mn[0], y0=mn[1])


def las_box(P, e0, n0, e1, n1):
    """indices of the points in a box (via the 4 m cells)"""
    ix0, ix1 = int((e0 - P['x0']) // 4), int((e1 - P['x0']) // 4)
    out = []
    for iy in range(int((n0 - P['y0']) // 4), int((n1 - P['y0']) // 4) + 1):
        a, b = np.searchsorted(P['key'], [iy * P['nx'] + max(0, ix0), iy * P['nx'] + ix1 + 1])
        if b > a: out.append(np.arange(a, b))
    return np.concatenate(out) if out else np.zeros(0, np.int64)


STOREYS = np.arange(2.5, 5.21, 0.02)


def storey_of(z):
    """the storey height that the wall returns repeat at (slab edges, sills, window heads), or 0: Rayleigh test"""
    if len(z) < 200: return 0.0
    z = z[:: max(1, len(z) // 6000)]
    r = np.abs(np.exp(2j * np.pi * z[:, None] / STOREYS[None, :]).mean(0))
    k = int(np.argmax(r))
    return float(STOREYS[k]) if r[k] >= 0.35 and 0 < k < len(STOREYS) - 1 else 0.0


def wall_storey(P, ring_utm):
    """the storey height (or 0) from the points on the footprint's walls. (Their colours are no use: the survey's photos
    clip sunlit walls to white, brick included, and shaded ones are the blue of the sky.)"""
    import shapely
    E, N = ring_utm[:, 0], ring_utm[:, 1]
    idx = las_box(P, E.min() - 6, N.min() - 6, E.max() + 6, N.max() + 6)
    if len(idx) < 200: return 0.0
    X, Y, Z, cls = P['x'][idx], P['y'][idx], P['z'][idx], P['cls'][idx]
    g = cls == 2
    zg = float(np.median(Z[g])) if g.sum() > 20 else float(np.percentile(Z, 2))
    inside = shapely.contains_xy(shapely.Polygon(ring_utm), X, Y)
    if inside.sum() < 20: return 0.0
    top = float(np.percentile(Z[inside], 98)) - zg
    if top < 6: return 0.0
    wallish = (cls == 1) | (cls == 6)
    s = 1 if 0.5 * np.sum(E * np.roll(N, -1) - np.roll(E, -1) * N) > 0 else -1  # ccw: outward normal (dy, -dx)
    zs = []
    for k in range(len(E)):
        ax, ay, bx, by = E[k], N[k], E[(k + 1) % len(E)], N[(k + 1) % len(E)]
        L = math.hypot(bx - ax, by - ay)
        if L < 6: continue
        ux, uy = (bx - ax) / L, (by - ay) / L
        al = (X - ax) * ux + (Y - ay) * uy; dp = (X - ax) * s * uy - (Y - ay) * s * ux
        z = Z - zg
        sel = wallish & (al > 1) & (al < L - 1) & (np.abs(dp) < 3) & (z > max(3.0, 0.25 * top)) & (z < top - 1.5)
        if sel.sum() < max(40, 2 * L): continue
        h, ed = np.histogram(dp[sel], bins=np.arange(-3, 3.01, 0.25))
        f0 = ed[np.argmax(h)] + 0.125  # the wall plane (OSM outlines are often a metre or two off)
        w = sel & (np.abs(dp - f0) < 0.5)
        if w.sum() < max(30, 1.5 * L): continue
        zs.append(z[sel & (np.abs(dp - f0) < 3)])
    return storey_of(np.concatenate(zs)) if zs else 0.0


def lidar_job(job):
    path, items = job
    P = las_points(path)
    return [(k, wall_storey(P, r)) for k, r in items]


def lidar_jobs(fps):
    from pyproj import Transformer
    tr = Transformer.from_crs('EPSG:4326', 'EPSG:3157', always_xy=True)  # NAD83(CSRS) UTM 10, the survey's frame
    files = sorted(glob.glob(f'{LAS}/*.las'))
    if not files: return []
    boxes = [(f, las_header(f)) for f in files]
    jobs = {f: [] for f in files}
    for k, (oid, r, name) in enumerate(fps):
        la, lo = latlon(np.array([p[0] for p in r]), np.array([p[1] for p in r]))
        e, n = tr.transform(lo, la)
        ce, cn = np.mean(e), np.mean(n)
        for f, h in boxes:
            mn, mx = h[5], h[6]
            if mn[0] + 3 <= ce < mx[0] - 3 and mn[1] + 3 <= cn < mx[1] - 3: jobs[f].append((k, np.c_[e, n])); break
    return [(f, it) for f, it in jobs.items() if it]


# ---------------------------------------------------------------- bake and lookup
def bake(procs):
    t0 = time.time()
    fps = footprints()
    n = len(fps)
    print(n, 'footprints', f'{time.time() - t0:.0f}s', flush=True)
    year, units, her = eras(fps)
    print((year > 0).sum(), 'with a year built,', (her > 0).sum(), 'on the heritage register', f'{time.time() - t0:.0f}s', flush=True)
    wall, wsrc, storey = np.zeros(n, np.uint32), np.zeros(n, np.uint8), np.zeros(n, np.uint8)
    balc, facing, pattern = np.zeros(n, np.uint8), np.zeros(n, np.uint8), np.zeros(n, np.uint8)
    jobs = lidar_jobs(fps)
    if jobs:
        with Pool(min(procs, len(jobs))) as pool:
            for res in pool.imap_unordered(lidar_job, jobs):
                for k, st in res:
                    if st: storey[k] = round(st * 20)
        print(f'LiDAR: {len(jobs)} tiles, {(storey > 0).sum()} storey heights', f'{time.time() - t0:.0f}s', flush=True)
    mly = np.zeros((n, 3), 'U20')  # the photos a colour came from, for credit
    m = ring_db(MLY)
    if m:
        got = 0
        for k, (oid, r, name) in enumerate(fps):
            h = ring_match(m, r)
            if h is None: continue
            c = m['rgb'][h]
            wall[k], wsrc[k], mly[k] = int(c[0]) << 16 | int(c[1]) << 8 | int(c[2]), 1, m['images'][h]
            got += 1
        print(f'Mapillary: {got} wall colours', f'{time.time() - t0:.0f}s', flush=True)
    by_id = {}
    for k, (oid, r, name) in enumerate(fps): by_id.setdefault(oid, []).append(k)
    by_name = {}
    for k, (oid, r, name) in enumerate(fps):
        if name: by_name.setdefault(name.lower(), []).append(k)
    for L in LOOKS:
        ks = [k for i in L['ids'] for k in by_id.get(i, [])] or by_name.get(L['name'].lower(), [])
        if not ks: print('LOOKS: not found:', L['name'], flush=True)
        for k in ks:
            if L.get('wall'): wall[k], wsrc[k] = L['wall'], 2
            balc[k], pattern[k] = L.get('balc', 0), L.get('pattern', 0)
            if L.get('storey'): storey[k] = round(L['storey'] * 20)
    save_rings(OUT, [r for _, r, _ in fps], year=year, units=units, heritage=her, wall=wall, wsrc=wsrc, storey=storey,
                        balc=balc, facing=facing, pattern=pattern, mly=mly)
    print(n, 'facades ->', OUT, f'{os.path.getsize(OUT) / 1e6:.1f} MB', f'{time.time() - t0:.0f}s', flush=True)


_DB = None


def facade_lookup(ring):
    """ring: world-xy footprint -> dict(year, units, heritage, wall (0xRRGGBB or 0), wsrc (1 Mapillary, 2 LOOKS),
    storey (m or 0), balc, facing, pattern) or None where facades.npz knows nothing"""
    global _DB
    if _DB is None: _DB = ring_db(OUT)
    if not _DB: return None
    k = ring_match(_DB, ring)
    if k is None: return None
    f = {n: int(_DB[n][k]) for n in FIELDS}
    f['storey'] /= 20
    return f


def check():
    assert abs(storey_of(np.concatenate([np.arange(12) * 3.1 + 5 + np.random.default_rng(1).normal(0, 0.1, 12) for _ in range(40)])) - 3.1) < 0.05
    assert storey_of(np.random.default_rng(2).uniform(5, 60, 3000)) == 0
    # a synthetic 20 m tower: a 12 x 8 m box of wall points with a 3 m storey rhythm
    rng = np.random.default_rng(3)
    walls = []
    for (ax, ay, bx, by, col) in ((0, 0, 12, 0, (60, 70, 80)), (12, 0, 12, 8, (60, 70, 80)), (12, 8, 0, 8, (60, 70, 80)), (0, 8, 0, 0, (170, 90, 70))):
        t = rng.uniform(0, 1, 4000); z = np.concatenate([np.repeat(np.arange(1, 7) * 3.0, 500) + rng.normal(0, 0.15, 3000), rng.uniform(1, 20, 1000)])
        walls.append(np.c_[ax + (bx - ax) * t, ay + (by - ay) * t, z, np.tile(col, (4000, 1))])
    roof = np.c_[rng.uniform(0.5, 11.5, 2000), rng.uniform(0.5, 7.5, 2000), np.full(2000, 20.0), np.full((2000, 3), 120)]
    gnd = np.c_[rng.uniform(-6, 18, 2000), rng.uniform(-6, 14, 2000), np.zeros(2000), np.full((2000, 3), 90)]
    a = np.concatenate(walls + [roof, gnd])
    cls = np.r_[np.full(16000 + 2000, 6), np.full(2000, 2)].astype(np.uint8)
    nx = 100
    key = ((a[:, 1] + 50) // 4).astype(np.int64) * nx + ((a[:, 0] + 50) // 4).astype(np.int64)
    o = np.argsort(key)
    P = dict(x=a[o, 0], y=a[o, 1], z=a[o, 2].astype(np.float32), cls=cls[o], key=key[o], nx=nx, x0=-50, y0=-50)
    st = wall_storey(P, np.array([[0, 0], [12, 0], [12, 8], [0, 8]], float))
    assert abs(st - 3.0) < 0.05, st
    print('facades check ok')


if __name__ == '__main__':
    if sys.argv[1:] == ['check']: check()
    else: bake(int(sys.argv[1]) if len(sys.argv) > 1 else os.cpu_count())
