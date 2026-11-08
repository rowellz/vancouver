# Step 3: the map tiles. For every 4096 m tile with data, writes sixteen 1024 m detail tiles (public/tiles/0/)
# and one 4096 m medium tile (public/tiles/1/), each a small gzipped binary the page streams in as you look
# around. Format: Out below, read by parse() in public/core/tile-worker.js. Inputs: data/feat (osm_split.py), data/hr (hrdem.py),
# data/sea.npz (sea.py), the city's trees (and Vancouver's 2009 LiDAR sections), its coarse DEM where the LiDAR has no data.
# The city: CITY=<name> (tools/cities/), its inputs in DATA, its tiles in OUT (default public/tiles[/<city>]).
# Run: uv run --with numpy,scipy,pillow,shapely,rasterio python3 tools/tiles.py [procs] [i_j ...]
import csv, gzip, json, math, os, pickle, random, struct, sys, time
from multiprocessing import Pool
import numpy as np
import shapely
from shapely.geometry import Polygon, box, LineString
from shapely.ops import unary_union
from PIL import Image, ImageDraw
from scipy import ndimage
sys.path.insert(0, os.path.dirname(__file__))
from common import GROUND, GI, xy, extent, L0, L1, KX, KY, LAT_O, LON_O, HR, KINDS, KI, DRIVE, SHOP_AMENITY, line_kind, hnum, densify, all_tiles, HR_CACHE, hr_lookup, tile_args, load_feat, DATA, TILES, CITY, city
from roofs import roof_lookup
from facades import facade_lookup, NONE as B_NONE, GLASS as B_GLASS, SOLID as B_SOLID

OUT = os.environ.get('OUT', TILES)
VERSION = 2



def ground_class(t):
    g = lambda k: t.get(k, '')
    if g('natural') == 'water' or g('waterway') in ('riverbank', 'dock') or 'water' in t and g('natural') != 'coastline' and g('water') != '' and g('natural') in ('', 'water'):
        return 'water'
    if g('leisure') == 'swimming_pool' or g('amenity') == 'swimming_pool': return 'pool'
    if g('man_made') in ('pier', 'breakwater', 'groyne') or g('leisure') == 'marina' and False: return 'pier'
    if g('leisure') == 'pitch': return 'pitch'
    if g('leisure') == 'track' or g('highway') == 'raceway': return 'track'
    if g('leisure') == 'playground': return 'playground'
    if g('amenity') == 'parking' and g('parking') not in ('underground', 'multi-storey', 'rooftop'): return 'parking'
    if g('highway') == 'pedestrian' or g('area:highway') in ('pedestrian', 'footway') or g('place') == 'square' or g('amenity') in ('marketplace',): return 'plaza'
    if g('aeroway') in ('apron', 'runway', 'taxiway', 'helipad'): return 'apron'
    if g('landuse') in ('construction', 'brownfield', 'landfill', 'quarry'): return 'construction'
    if g('natural') in ('bare_rock', 'scree', 'cliff', 'shingle', 'stone'): return 'rock'
    if g('natural') == 'glacier': return 'snow'
    if g('natural') in ('beach', 'sand', 'dune') or g('golf') == 'bunker' or g('surface') == 'sand' and g('leisure') != 'pitch': return 'sand'
    if g('natural') in ('wood',) or g('landuse') == 'forest': return 'forest'
    if g('natural') == 'wetland' or g('landuse') == 'salt_pond': return 'wetland'
    if g('natural') in ('scrub', 'heath') : return 'scrub'
    if g('leisure') == 'golf_course' or g('golf') in ('fairway', 'green', 'tee', 'rough'): return 'golf'
    if g('leisure') in ('park', 'garden', 'nature_reserve', 'common', 'dog_park', 'recreation_ground') or g('landuse') in ('grass', 'recreation_ground', 'village_green', 'cemetery', 'meadow') \
            or g('natural') in ('grassland',) or g('amenity') == 'grave_yard': return 'grass'
    if g('landuse') in ('farmland', 'orchard', 'vineyard', 'allotments', 'greenhouse_horticulture', 'plant_nursery', 'farmyard', 'animal_keeping'): return 'farmland'
    if g('landuse') in ('industrial', 'railway', 'port', 'military', 'depot') or g('industrial'): return 'industrial'
    if g('landuse') in ('commercial', 'retail') or g('amenity') in ('school', 'university', 'college', 'hospital') and g('landuse') == '': return 'commercial'
    if g('landuse') in ('residential',): return 'residential'
    return None


# ---------------------------------------------------------------- roads and other lines (kinds: common.py)
def road_shape(kind, t):
    """(lanes total, width m, oneway, sidewalk band)"""
    ow = t.get('oneway') in ('yes', '1', '-1', 'true') or t.get('junction') == 'roundabout' or kind == 'motorway' and t.get('oneway') != 'no'
    lanes = hnum(t.get('lanes'))
    default = {'motorway': 4, 'trunk': 4, 'primary': 4, 'secondary': 2, 'tertiary': 2, 'residential': 2, 'service': 1, 'living': 1}.get(kind)
    if lanes is None and default: lanes = default if not ow else max(1, default // 2)
    w = hnum(t.get('width'))
    if not w:
        w = {'motorway': 3.7, 'trunk': 3.6, 'primary': 3.4, 'secondary': 3.3, 'tertiary': 3.3, 'residential': 4.0, 'service': 4.5, 'living': 5.0}.get(kind, 0) * (lanes or 1)
        w = w or {'pedestrian': 7, 'footway': 2.2, 'cycleway': 2.6, 'path': 1.6, 'steps': 2.2, 'track': 3.2, 'rail': 3.4, 'lightrail': 3.4, 'runway': 45, 'taxiway': 23}.get(kind, 3)
        if kind in ('motorway', 'trunk', 'primary') : w += 1.5  # shoulders
    sw = t.get('sidewalk', '')
    band = kind in ('primary', 'secondary', 'tertiary', 'residential', 'living') and sw not in ('no', 'none', 'separate') and t.get('sidewalk:both') != 'separate' \
        and not (t.get('sidewalk:left') == 'separate' and t.get('sidewalk:right') == 'separate')
    return int(lanes or 0), w, ow, band


# ---------------------------------------------------------------- binary writer
class Out:
    def __init__(self, ox, oy, size):
        self.ox, self.oy, self.k = ox, oy, 32766 / (2 * size)  # int16 units per metre: ±2 tile sizes (long pieces reach out), far edge exactly 16383 (must match tile-worker.js)
        self.b = bytearray()
        self.b += b'VTL2' + struct.pack('<HHff', VERSION, 0, ox, oy) + struct.pack('<f', size)

    def q(self, x, y):
        return (max(-32768, min(32767, int(round((x - self.ox) * self.k)))), max(-32768, min(32767, int(round((y - self.oy) * self.k)))))

    def section(self, tag, payload):
        self.b += tag.encode() + struct.pack('<I', len(payload)) + payload

    def bytes(self):
        return gzip.compress(bytes(self.b), 7)


# ---------------------------------------------------------------- shared inputs (loaded in the parent; children fork)
EX = extent()
SEA = np.load(f'{DATA}/sea.npz')
SEA_M, SEA_X0, SEA_Y1, SEA_C = SEA['sea'].astype(bool), float(SEA['x0']), float(SEA['y1']), float(SEA['cell'])


def mrdem_grid():
    """the city's coarse DEM (DEM_FALLBACK: MRDEM 30 m in Vancouver) for the whole map, sampled on a 32 m world grid
    (fallback where the LiDAR has no data)"""
    path = f'{DATA}/mrdem32.npz'
    if os.path.exists(path):
        d = np.load(path); return d['z'], float(d['x0']), float(d['y1'])
    import rasterio
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import Affine
    x0, y0, x1, y1 = EX; c = 32.0
    w, h = int((x1 - x0) / c) + 2, int((y1 - y0) / c) + 2
    tr = Affine((c / KX), 0, LON_O + x0 / KX, 0, -(c / KY), LAT_O + y1 / KY)
    z = np.full((h, w), np.nan, np.float32)
    url, zscale = city.DEM_FALLBACK
    with rasterio.open(url) as src:
        reproject(rasterio.band(src, 1), z, src_nodata=src.nodata, dst_transform=tr, dst_crs='EPSG:4326', dst_nodata=np.nan, resampling=Resampling.bilinear)
    z *= zscale
    np.savez_compressed(path, z=z, x0=x0, y1=y1)
    return z, x0, y1


MR, MR_X0, MR_Y1 = mrdem_grid()


TREES = city.trees(DATA)  # the city's tree inventory: (x, y, genus, common name, height)
SECTIONS = city.sections(DATA)  # vector-exact building sections, where a city has them: (ring, height, roof type)
print('inputs ready', len(TREES), 'city trees', len(SECTIONS), 'lidar sections', flush=True)


# ---------------------------------------------------------------- tree species -> shape and autumn colour
# shapes: 0 round, 1 columnar, 2 spreading, 3 weeping, 4 fir, 5 cedar, 6 pine, 7 palm
SHAPE = {
    'ACER': 0, 'PRUNUS': 2, 'MALUS': 2, 'TILIA': 1, 'FRAXINUS': 0, 'QUERCUS': 2, 'CARPINUS': 1, 'BETULA': 3, 'SALIX': 3, 'ULMUS': 2,
    'AESCULUS': 2, 'FAGUS': 2, 'PLATANUS': 2, 'LIQUIDAMBAR': 1, 'GLEDITSIA': 2, 'GINKGO': 1, 'POPULUS': 1, 'CERCIDIPHYLLUM': 0,
    'PYRUS': 1, 'SORBUS': 0, 'CORNUS': 0, 'MAGNOLIA': 0, 'STYRAX': 0, 'PARROTIA': 0, 'NYSSA': 1, 'ZELKOVA': 2, 'CATALPA': 2,
    'THUJA': 5, 'CHAMAECYPARIS': 5, 'CALOCEDRUS': 5, 'CUPRESSOCYPARIS': 5, 'SEQUOIADENDRON': 4, 'SEQUOIA': 4, 'METASEQUOIA': 4,
    'PSEUDOTSUGA': 4, 'PICEA': 4, 'ABIES': 4, 'TSUGA': 4, 'TAXODIUM': 4, 'CEDRUS': 6, 'PINUS': 6, 'ARAUCARIA': 6, 'TAXUS': 5,
    'TRACHYCARPUS': 7, 'ARBUTUS': 2,
}
# colour families (indices into the page's palette, see public/core/trees.js): 0 green, 1 yellow, 2 orange, 3 red, 4 brown,
# 5 dark conifer, 6 blue-green conifer, 7 forest green, 8 bare-ish (late), 9 cherry
AUTUMN = {
    'ACER': (3, 2, 1), 'PRUNUS': (2, 3), 'MALUS': (1, 0), 'TILIA': (1, 0), 'FRAXINUS': (1, 8), 'QUERCUS': (4, 0), 'CARPINUS': (1, 0),
    'BETULA': (1,), 'SALIX': (0, 1), 'ULMUS': (1, 0), 'AESCULUS': (4, 2), 'FAGUS': (4, 2), 'PLATANUS': (4, 1), 'LIQUIDAMBAR': (3, 2),
    'GLEDITSIA': (1,), 'GINKGO': (1,), 'POPULUS': (1,), 'CERCIDIPHYLLUM': (2, 1), 'PYRUS': (3, 2), 'SORBUS': (2, 3), 'CORNUS': (3,),
    'PARROTIA': (2, 3), 'NYSSA': (3,), 'ZELKOVA': (2, 4), 'STYRAX': (1,), 'MAGNOLIA': (0, 4), 'ARBUTUS': (0,),
}


def tree_look(genus, rnd):
    g = (genus or '').upper()
    shape = SHAPE.get(g)
    if shape is None: shape = 0
    if shape >= 4:
        return shape, 6 if g in ('PICEA', 'CEDRUS') else 5
    if g == 'PRUNUS': return shape, 9  # cherries and plums: autumn's red, blossom in spring (the page's palette)
    fam = AUTUMN.get(g)
    if fam is None: return shape, 0 if rnd.random() < 0.6 else rnd.choice((1, 2, 4))
    return shape, fam[0] if rnd.random() < 0.7 else rnd.choice(fam + (0,))


# ---------------------------------------------------------------- per 4096 m tile
def build(ij):
    i, j = ij
    t0 = time.time()
    rnd = random.Random(i * 7919 + j)
    feat = load_feat(i, j)
    X0, Y0 = i * L1, j * L1
    # rasters on the tile's 2 m grid (row 0 = north)
    N = int(L1 / HR)
    hp = f'{DATA}/hr/{i}_{j}.npz'
    dsm = dtm = None
    if os.path.exists(hp):
        d = np.load(hp)
        if 'dtm' in d: dsm, dtm = d['dsm'], d['dtm']
    gx = X0 + (np.arange(N) + 0.5) * HR; gy = Y0 + L1 - (np.arange(N) + 0.5) * HR
    # MRDEM fallback, bilinear from the 32 m grid
    fx = (gx - MR_X0) / 32 - 0.5; fy = (MR_Y1 - gy) / 32 - 0.5
    mr = ndimage.map_coordinates(np.nan_to_num(MR, nan=0.0), np.meshgrid(fy, fx, indexing='ij'), order=1, mode='nearest').astype(np.float32)
    if dtm is None: dtm = mr.copy(); dsm = None
    else:
        hole = np.isnan(dtm)
        dtm = np.where(hole, mr, dtm)
        if dsm is not None: dsm = np.where(np.isnan(dsm), dtm, dsm)
    # sea on the tile's grid
    sr = ((SEA_Y1 - gy) / SEA_C).astype(int).clip(0, SEA_M.shape[0] - 1); sc = ((gx - SEA_X0) / SEA_C).astype(int).clip(0, SEA_M.shape[1] - 1)
    sea = SEA_M[np.ix_(sr, sc)]
    if sea.all() and not feat.get('lines') and not feat.get('buildings'): return ij, 'sea', 0
    pix = lambda x, y: ((x - X0) / HR, (Y0 + L1 - y) / HR)

    # ---- water polygons (OSM lakes, rivers) to flatten the terrain and to draw
    areas = feat.get('areas', [])
    polys = {}  # class -> list of shapely polygons (world metres)
    for oid, prs, t in areas:
        k = ground_class(t)
        if not k: continue
        for outer, inners in prs:
            if len(outer) < 4: continue
            try:
                pg = Polygon(outer, [h for h in inners if len(h) >= 4])
                if not pg.is_valid: pg = pg.buffer(0)
            except Exception: continue
            if pg.is_empty: continue
            polys.setdefault(k, []).append(pg)
    # sea polygons from the mask (vectorized at 4 m inside this tile, smoothed)
    import rasterio.features as rf
    from rasterio.transform import Affine
    r0, c0 = int((SEA_Y1 - (Y0 + L1)) / SEA_C) - 2, int((X0 - SEA_X0) / SEA_C) - 2
    n4 = int(L1 / SEA_C) + 4
    sub = np.zeros((n4, n4), np.uint8)
    rs, cs = max(0, r0), max(0, c0)
    re, ce = min(SEA_M.shape[0], r0 + n4), min(SEA_M.shape[1], c0 + n4)
    if re > rs and ce > cs: sub[rs - r0:re - r0, cs - c0:ce - c0] = SEA_M[rs:re, cs:ce]
    if sub.any():
        # exact shorelines: cut the tile by the OSM coastlines and keep the pieces the sea mask says are water
        tb_ = box(X0 - 8, Y0 - 8, X0 + L1 + 8, Y0 + L1 + 8)
        coast = [LineString(c) for _, c, _, t in feat.get('lines', []) if t.get('natural') == 'coastline' and len(c) > 1]
        seap = []
        if coast:
            cut = unary_union([g.intersection(tb_) for g in coast] + [tb_.exterior])
            for face in shapely.polygonize(list(cut.geoms) if hasattr(cut, 'geoms') else [cut]).geoms:
                pnt = face.representative_point()
                r_, c_ = int((SEA_Y1 - pnt.y) / SEA_C), int((pnt.x - SEA_X0) / SEA_C)
                if 0 <= r_ < SEA_M.shape[0] and 0 <= c_ < SEA_M.shape[1] and SEA_M[r_, c_]: seap.append(face)
        if not seap and sub.mean() > 0.5: seap = [tb_]
        polys.setdefault('water', []).extend(seap)
    # flatten water surfaces: sea at 0 m, lakes/rivers at their shore level
    water_r = np.zeros((N, N), bool)
    if polys.get('water'):
        img = Image.new('L', (N, N), 0); dr = ImageDraw.Draw(img)
        for p in polys['water']:
            for g in (p.geoms if hasattr(p, 'geoms') else [p]):
                dr.polygon([pix(*c) for c in g.exterior.coords], fill=1)
                for h in g.interiors: dr.polygon([pix(*c) for c in h.coords], fill=0)
        water_r = np.array(img, bool)
    water_r |= sea
    dtm = flat_water(dtm, sea, water_r)

    # bridge decks and viaducts overhead: the LiDAR surface there is the deck, so no roofs or tree crowns from it
    under = bridge_mask(feat.get('lines', []), areas, X0, Y0, N)
    if dsm is not None: dsm = np.where(under, dtm, dsm)

    def height_at(x, y):  # bilinear on the 2 m DTM
        fx_, fy_ = (x - X0) / HR - 0.5, (Y0 + L1 - y) / HR - 0.5
        return float(ndimage.map_coordinates(dtm, [[fy_], [fx_]], order=1, mode='nearest')[0])

    # ---- canopy as forest: where the LiDAR sees trees over 6 m in a sizeable patch, the ground is woods (most of the
    # North Shore, Pacific Spirit, Stanley Park and every big back-yard grove is mapped only partly in OSM)
    if dsm is not None:
        chm = ndimage.uniform_filter(np.nan_to_num(dsm - dtm), 5)
        wood = ndimage.binary_opening(chm > 6, iterations=2) & ~water_r
        lab, nl = ndimage.label(wood)
        if nl:
            sizes = ndimage.sum(wood, lab, range(nl + 1)) * HR * HR
            wood = (sizes[lab] > 2500) & wood
            tr = Affine(HR, 0, X0, 0, -HR, Y0 + L1)
            for g, v in rf.shapes(wood.astype(np.uint8), mask=wood, transform=tr):
                p = shapely.geometry.shape(g).simplify(3).buffer(2, join_style=1).buffer(-2, join_style=1)
                if not p.is_empty and p.area > 2500: polys.setdefault('forest', []).append(p)
    else:  # no LiDAR here: high ground that OSM leaves blank is forest
        hi = ndimage.binary_opening((dtm > 250) & ~water_r, iterations=3)
        if hi.any():
            import rasterio.features as rf
            from rasterio.transform import Affine
            tr = Affine(HR, 0, X0, 0, -HR, Y0 + L1)
            for g, v in rf.shapes(hi.astype(np.uint8), mask=hi, transform=tr):
                p = shapely.geometry.shape(g).simplify(8)
                if p.area > 20000: polys.setdefault('forest', []).append(p)
    # built-up places and sports fields keep their own look: cut them out of the canopy forest
    if polys.get('forest'):
        keep = unary_union([p for k in ('pitch', 'track', 'parking', 'water', 'pool', 'golf', 'apron', 'construction', 'sand') for p in polys.get(k, [])] or [Polygon()])
        if not keep.is_empty: polys['forest'] = [f.difference(keep) for f in polys['forest']]

    # ---- buildings
    from shapely.strtree import STRtree
    sp = [p for _, p, t in feat.get('nodes', []) if 'shop' in t or 'office' in t or 'craft' in t or t.get('amenity') in SHOP_AMENITY]
    shops = STRtree(shapely.points(np.array(sp))) if sp else None
    ind = unary_union(polys['industrial']) if polys.get('industrial') else None
    if ind is not None: shapely.prepare(ind)
    sl = [LineString(c) for _, c, _, t in feat.get('lines', []) if line_kind(t) in ('residential', 'tertiary', 'secondary', 'primary', 'living') and len(c) > 1]
    builds = buildings(feat.get('buildings', []), dsm, dtm, X0, Y0, rnd, under, shops, ind, STRtree(sl) if sl else None)
    fp = Image.new('L', (N, N), 0); dfp = ImageDraw.Draw(fp)
    for b in builds:
        dfp.polygon([pix(*c) for c in b['ring']], fill=1)
    fp = np.array(fp, bool)

    # ---- lines
    lines = road_pieces(feat.get('lines', []), dsm, dtm, X0, Y0)
    # streets as ground: carriageways, sidewalks and boulevards, paths and rail beds, unioned per class so junctions
    # are clean (bridges are raised decks, drawn from the lines)
    # every piece that touches this tile, not only those it owns: a street crossing the tile edge is paved on
    # both sides of it
    for L in road_pieces(feat.get('lines', []), dsm, dtm, X0, Y0, own=False):
        if L['z'] or len(L['pts']) < 2: continue
        k = L['kind']; ls = LineString(L['pts'])
        cls = {'motorway': 'arterial', 'trunk': 'arterial', 'primary': 'arterial', 'secondary': 'arterial', 'tertiary': 'road', 'residential': 'road',
               'service': 'road', 'living': 'road', 'pedestrian': 'walk', 'footway': 'walk', 'steps': 'walk', 'path': 'trail', 'track': 'trail',
               'cycleway': 'cycle', 'rail': 'ballast', 'lightrail': 'ballast', 'runway': 'apron', 'taxiway': 'apron'}.get(k)
        if not cls or L.get('crossing'): continue  # a crossing's footway lies on the road: the road wins, the zebra is paint
        w = L['w'] if cls != 'ballast' else 3.4
        polys.setdefault(cls, []).append(ls.buffer(w / 2, quad_segs=3, cap_style='round' if cls in ('road', 'arterial', 'walk') else 'flat'))
        if L['band']:
            blvd = 2.2 if k in ('residential', 'tertiary') else 0.0
            walk = 3.0 if k in ('primary', 'secondary', 'trunk') else 2.0
            outer = ls.buffer(w / 2 + blvd + walk, quad_segs=3, cap_style='flat')
            polys.setdefault('sidewalk', []).append(outer)
            if blvd: polys.setdefault('blvd', []).append(ls.buffer(w / 2 + blvd, quad_segs=3, cap_style='flat'))

    # ---- docks: piers and floating docks mapped as lines (a marina's walkways and fingers) are decks on the water;
    # the boats berthed along them in marinas
    docks = []
    for _, c, _, t in feat.get('lines', []):
        if t.get('man_made') != 'pier' or len(c) < 2 or t.get('highway') and t['highway'] not in ('footway', 'path'): continue
        ls = LineString(c)
        w = hnum(t.get('width')) or (3.0 if ls.length > 60 else 2.0)
        docks.append((ls, w))
        polys.setdefault('pier', []).append(ls.buffer(w / 2, quad_segs=2, cap_style='flat', join_style='mitre'))
    for _, prs, t in areas:  # docks mapped as areas (a walkway and its fingers, the water between them holes): their edges
        if t.get('man_made') == 'pier':
            docks += [(LineString(r), 0.0) for o, ins in prs for r in [o, *ins] if len(r) >= 4]
    boats = berths(docks, polys, areas, X0, Y0, rnd) if docks else []

    # ---- where no trunk may stand: carriageways and paths (as drawn), rail, pitches, tracks, courts, car parks, beaches, pools
    # (crowns may still hang over a path; only the trunk is kept off it)
    nogo = Image.new('L', (N, N), 0); dn = ImageDraw.Draw(nogo)
    for L in lines:
        if L['z']: continue
        pad = 0.0 if L['kind'] in ('footway', 'path', 'steps', 'track', 'cycleway') else 0.6
        dn.line([pix(*c) for c in L['pts']], fill=1, width=max(1, int(round((L['w'] + pad) / HR))), joint='curve')
    for k in ('pitch', 'track', 'parking', 'sand', 'pool', 'apron', 'plaza', 'playground', 'construction'):
        for p in polys.get(k, []):
            for g in (p.geoms if hasattr(p, 'geoms') else [p]):
                if g.geom_type == 'Polygon': dn.polygon([pix(*c) for c in g.exterior.coords], fill=1)
    nogo = np.array(nogo, bool)

    # ---- trees from the canopy
    trees = canopy_trees(dsm, dtm, fp, water_r, X0, Y0, rnd, polys, nogo)
    trees, moved, dropped = off_streets(trees, polys, lambda x, y: fp[min(N - 1, max(0, int((Y0 + L1 - y) / HR))), min(N - 1, max(0, int((x - X0) / HR)))])

    # ---- points the simulation uses: signals, crossings, stops, street lamps, benches, transit stops
    POI = {'traffic_signals': 0, 'crossing': 1, 'stop': 2, 'street_lamp': 3, 'bench': 4, 'bus_stop': 5, 'give_way': 6}
    pois = []
    for nid, (x, y), t in feat.get('nodes', []):
        k = t.get('highway') if t.get('highway') in POI else 'bench' if t.get('amenity') == 'bench' else 'bus_stop' if t.get('public_transport') == 'platform' and t.get('bus') == 'yes' else None
        if k and X0 <= x < X0 + L1 and Y0 <= y < Y0 + L1: pois.append((POI[k], nid, x, y))

    # ---- write the 16 detail tiles and the medium tile
    n = 0
    for a in range(4):
        for b in range(4):
            tx, ty = X0 + a * L0, Y0 + b * L0
            out = Out(tx, ty, L0)
            ti_, tj_ = i * 4 + a, j * 4 + b
            terr = terrain_grid(dtm, a * L0, (3 - b) * L0, L0, 129)  # 8 m: embankments, cuts and seawalls under the roads
            if not write_tile(out, tx, ty, L0, terr, polys, lines, builds, trees, detail=True, pois=pois, boats=boats, water_only=sea[(3 - b) * 512:(4 - b) * 512, a * 512:(a + 1) * 512].all()):
                continue
            os.makedirs(f'{OUT}/0', exist_ok=True)
            open(f'{OUT}/0/{ti_}_{tj_}.bin', 'wb').write(out.bytes()); n += 1
    out = Out(X0, Y0, L1)
    terr = terrain_grid(dtm, 0, 0, L1, 65)
    write_tile(out, X0, Y0, L1, terr, polys, lines, builds, trees, detail=False, water_only=sea.all(), pois=[])
    os.makedirs(f'{OUT}/1', exist_ok=True)
    open(f'{OUT}/1/{i}_{j}.bin', 'wb').write(out.bytes())
    return ij, 'ok', round(time.time() - t0, 1), n, len(builds), len(trees), f'trunks off streets: {moved} moved, {dropped} dropped'


def flat_water(dtm, sea, water):
    """the sea flat at 0 m (so neighbouring tiles meet exactly), lakes and rivers at their shore level; piers, wharves
    and seawalls inside the water stand well above it and keep their height (Canada Place, the seawall walks)"""
    deck = ndimage.binary_opening(dtm > 2.5, iterations=2)
    dtm = np.where(sea & ndimage.binary_erosion(sea, iterations=1) & ~deck, 0.0, dtm)
    low = water & ndimage.binary_erosion(water, iterations=2)
    if low.any():
        lab, nl = ndimage.label(low)
        lvl = ndimage.percentile(dtm, lab, np.arange(1, nl + 1), 10) if hasattr(ndimage, 'percentile') else ndimage.minimum(dtm, lab, np.arange(1, nl + 1))
        lvl = np.concatenate([[0], np.asarray(lvl)])
        low &= ~ndimage.binary_opening(dtm > lvl[lab] + 2.5, iterations=2)
        dtm = np.where(low, np.minimum(dtm, lvl[lab]), dtm)
    return dtm


def terrain_grid(dtm, ox, oy, size, n):
    """n x n heights (row 0 = south edge... no: row 0 = north) over size metres starting at raster offset (ox, oy) metres from the NW corner"""
    step = size / (n - 1)
    xs = (ox + np.arange(n) * step) / HR - 0.5
    ys = (oy + np.arange(n) * step) / HR - 0.5
    return ndimage.map_coordinates(dtm, np.meshgrid(ys, xs, indexing='ij'), order=1, mode='nearest').astype(np.float32)


# ---------------------------------------------------------------- buildings
# Shapes come from the LiDAR surface inside each OSM footprint: up to four roof levels (podium, tower, penthouse,
# terraces) extruded one on top of the other, pitched roofs fitted to the surface (gable, cross gable, hip, shed),
# rooftop plant where the surface shows it. Looks come from OSM tags (material, colours, heritage, shops) and from
# where in town the building stands.
# style ids: the page's palettes and facade patterns (public/core/tile-worker.js PALETTES)
HOUSE, APT, OFFICE, BRICK, COMM, CIVIC, GLASS, CONCRETE, SHED, BIGBOX, STONE, CHARACTER, SPECIAL = range(13)
STYLE = {
    'house': HOUSE, 'detached': HOUSE, 'semidetached_house': HOUSE, 'bungalow': HOUSE, 'cabin': HOUSE, 'garage': HOUSE, 'garages': HOUSE, 'shed': HOUSE, 'terrace': HOUSE,
    'apartments': APT, 'residential': APT, 'dormitory': APT, 'hotel': APT,
    'office': OFFICE, 'commercial': COMM, 'retail': COMM, 'kiosk': COMM, 'service': COMM, 'parking': CONCRETE,
    'industrial': SHED, 'warehouse': SHED, 'manufacture': SHED, 'hangar': SHED, 'storage': SHED, 'factory': SHED, 'barn': SHED, 'farm_auxiliary': SHED,
    'supermarket': BIGBOX, 'mall': BIGBOX,
    'school': CIVIC, 'university': CIVIC, 'college': CIVIC, 'hospital': CIVIC, 'public': CIVIC, 'civic': CIVIC, 'government': CIVIC, 'train_station': CIVIC,
    'stadium': CIVIC, 'transportation': CIVIC, 'temple': CIVIC, 'church': BRICK, 'chapel': BRICK, 'cathedral': STONE,
}
HOUSES = {'house', 'detached', 'semidetached_house', 'bungalow', 'cabin', 'terrace'}
RESID = {'apartments', 'residential', 'dormitory', 'yes'}
MATERIAL = {'brick': BRICK, 'masonry': BRICK, 'clay': BRICK, 'stone': STONE, 'granite': STONE, 'sandstone': STONE, 'limestone': STONE, 'marble': STONE,
            'concrete': CONCRETE, 'cement_block': CONCRETE, 'plaster': CONCRETE, 'stucco': CONCRETE, 'glass': OFFICE, 'mirror': OFFICE, 'concrete/glass': OFFICE,
            'concrete;glass': OFFICE, 'metal': SHED, 'steel': SHED, 'metal_plates': SHED, 'aluminium': SHED, 'wood': CHARACTER, 'timber_framing': CHARACTER,
            'vinyl_siding': CHARACTER}
ROOF_MAT = {'metal': 0xa8b0b4, 'tin': 0xa8b0b4, 'steel': 0xa8b0b4, 'copper': 0x5f9a84, 'roof_tiles': 0xa8583c, 'tile': 0xa8583c, 'tiles': 0xa8583c, 'slate': 0x4a5058,
            'asphalt': 0x50545a, 'asphalt_shingle': 0x585450, 'shingle': 0x585450, 'concrete': 0xc0c0bc, 'glass': 0x8ab4c8, 'grass': 0x6a9a50, 'plants': 0x6a9a50,
            'gravel': 0xa8a49c, 'tar_paper': 0x4a4c50, 'eternit': 0x8a8c8c, 'wood': 0x7a6048, 'thatch': 0xa08850}
ROOF = {'flat': 0, 'gabled': 1, 'hipped': 2, 'pyramidal': 3, 'dome': 4, 'skillion': 5, 'round': 6, 'half-hipped': 2, 'gambrel': 1, 'mansard': 2, 'onion': 4}
CROSS, FLIP = 16, 32  # roof byte bits: ridge along the footprint's short axis; a shed roof's high side on the other edge
COLOURS = {'white': 0xf2f0ea, 'black': 0x303236, 'grey': 0x9a9c9e, 'gray': 0x9a9c9e, 'red': 0xb04a3a, 'brown': 0x7a5a48, 'beige': 0xd8c8a8, 'yellow': 0xe0c060,
           'blue': 0x5a80b0, 'green': 0x5a8a5a, 'silver': 0xc0c4c8, 'tan': 0xc8b090, 'cream': 0xeee4cc, 'orange': 0xd08040, 'darkgrey': 0x5a5c60,
           'lightgrey': 0xc8cacc, 'maroon': 0x7a2a2a, 'pink': 0xe0a8a8, 'teal': 0x4a9a9a}
# rough outlines in world metres (common.py) of places whose building stock has a look of its own; first match wins
# (the city's ZONES, tools/cities/<city>.py)
ZONES = [(k, Polygon(pts)) for k, pts in city.ZONES]


def zone(c):
    for k, p in ZONES:
        if shapely.contains_xy(p, c[0], c[1]): return k
    return None


def colour(v):
    if not v: return 0
    v = v.strip().lower().replace(' ', '').replace('_', '')
    if v.startswith('#') and len(v) in (4, 7):
        h = v[1:]
        if len(h) == 3: h = ''.join(c * 2 for c in h)
        try: return int(h, 16) | 0x1000000
        except ValueError: return 0
    return (COLOURS[v] | 0x1000000) if v in COLOURS else 0


def obb(r):
    best = None
    for a_ in range(len(r)):
        (x1, y1), (x2, y2) = r[a_ - 1], r[a_]
        ang = math.atan2(y2 - y1, x2 - x1); ca, sa = math.cos(ang), math.sin(ang)
        us = [x * ca + y * sa for x, y in r]; vs = [-x * sa + y * ca for x, y in r]
        ar = (max(us) - min(us)) * (max(vs) - min(vs))
        if best is None or ar < best[0]: best = (ar, ang, min(us), max(us), min(vs), max(vs))
    ar, ang, u0, u1, v0, v1 = best
    uc, vc = (u0 + u1) / 2, (v0 + v1) / 2
    cx, cy = uc * math.cos(ang) - vc * math.sin(ang), uc * math.sin(ang) + vc * math.cos(ang)
    hl, hw = (u1 - u0) / 2, (v1 - v0) / 2
    if hw > hl: ang += math.pi / 2; hl, hw = hw, hl
    return cx, cy, ang, hl, hw, ar


def ring_area(r):
    return abs(sum(r[k][0] * r[k - 1][1] - r[k - 1][0] * r[k][1] for k in range(len(r)))) / 2


def inset(r, d):
    s = 1 if sum(r[k - 1][0] * r[k][1] - r[k][0] * r[k - 1][1] for k in range(len(r))) > 0 else -1
    out = []
    for k in range(len(r)):
        (x0, y0), (x1, y1), (x2, y2) = r[k - 1], r[k], r[(k + 1) % len(r)]
        def nrm(ax, ay, bx, by):
            l = math.hypot(bx - ax, by - ay) or 1
            return (-(by - ay) / l * s, (bx - ax) / l * s)
        n1, n2 = nrm(x0, y0, x1, y1), nrm(x1, y1, x2, y2)
        mx, my = n1[0] + n2[0], n1[1] + n2[1]; ml = math.hypot(mx, my)
        if ml < 1e-6: out.append((x1, y1)); continue
        kk = min(3 * d, d / max(0.2, (mx * n1[0] + my * n1[1]) / ml))
        out.append((x1 + mx / ml * kk, y1 + my / ml * kk))
    return out


def bridge_mask(lines, areas, X0, Y0, N):
    """where a bridge deck or viaduct is overhead (roads, rail, SkyTrain guideways, widened by a few metres): there the
    LiDAR surface is the deck, not a roof or a tree crown"""
    img = Image.new('L', (N, N), 0); d = ImageDraw.Draw(img)
    pix = lambda x, y: ((x - X0) / HR, (Y0 + L1 - y) / HR)
    for _, coords, _, t in lines:
        if t.get('bridge') in (None, 'no') or (hnum(t.get('layer')) or 0) < 0: continue
        k = line_kind(t)
        w = road_shape(k, t)[1] if k else 4.0
        d.line([pix(*c) for c in coords], fill=1, width=max(1, int(round((w + 8) / HR))), joint='curve')
    for _, prs, t in areas:
        if t.get('man_made') == 'bridge':
            for outer, _ in prs:
                if len(outer) >= 3: d.polygon([pix(*c) for c in outer], fill=1)
    return np.array(img, bool)


def height_levels(v):
    """1-4 roof heights inside a footprint (1D k-means of the LiDAR surface heights), lowest first"""
    best = np.array([float(np.percentile(v, 60))])
    top = float(np.percentile(v, 97))
    if len(v) < 30 or top - float(np.percentile(v, 5)) < 3: return best
    prev = float(((v - np.median(v)) ** 2).sum())
    for k in (2, 3, 4):
        c = np.percentile(v, np.linspace(12, 94, k))
        for _ in range(10):
            lab = np.abs(v[:, None] - c[None]).argmin(1)
            c = np.array([np.median(v[lab == j]) if (lab == j).any() else c[j] for j in range(k)])
        c = np.sort(c)
        lab = np.abs(v[:, None] - c[None]).argmin(1)
        sse = float(((v - c[lab]) ** 2).sum())
        if np.bincount(lab, minlength=k).min() < 6 or np.diff(c).min() < max(2.5, 0.06 * top) or sse > 0.4 * prev: break
        best, prev = c, sse
    return best


def squared(g, ang):
    """a raster outline squared up: the building-aligned rectangle when it nearly fills it, else simplified"""
    g = g.buffer(0)
    if g.is_empty: return g
    from shapely import affinity
    rot = affinity.rotate(g, -ang, origin=(0, 0), use_radians=True)
    x0, y0, x1, y1 = rot.bounds
    if g.area > 0.78 * (x1 - x0) * (y1 - y0): return affinity.rotate(box(x0, y0, x1, y1), ang, origin=(0, 0), use_radians=True)
    mrr = g.minimum_rotated_rectangle
    if g.area > 0.82 * mrr.area: return mrr
    return g.simplify(1.2)


def polys_of(g, min_area):
    return [p for p in (g.geoms if hasattr(g, 'geoms') else [g]) if p.geom_type == 'Polygon' and p.area >= min_area]


def fit_roof(r, z, ok, fx, shape=''):
    """a pitched roof fitted to the LiDAR surface in the footprint's own frame: (roof byte, eaves, ridge, pitch) or None"""
    rr, cc = np.nonzero(ok)
    if len(rr) < 10: return None
    zz = z[rr, cc]
    cx, cy, ang, hl, hw, _ = obb(r)
    ang %= math.pi  # the page's frame (tile-worker.js roof): "+v" is the same side there
    if hw < 2: return None
    x = fx[0] + (cc + 0.5) * HR - cx; y = fx[1] - (rr + 0.5) * HR - cy
    ca, sa = math.cos(ang), math.sin(ang)
    u, v = x * ca + y * sa, -x * sa + y * ca
    sse0 = float(((zz - zz.mean()) ** 2).sum()) + 1e-6
    cand = {'gable': (np.abs(v), hw, 1, 0), 'cross': (np.abs(u), hl, 1, CROSS), 'hip': (np.maximum(np.abs(v), np.abs(u) - (hl - hw)), hw, 2, 0),
            'shed': (v, hw, 5, 0), 'shedx': (u, hl, 5, CROSS)}
    allow = {'gabled': ('gable', 'cross'), 'gambrel': ('gable', 'cross'), 'hipped': ('hip',), 'half-hipped': ('hip',), 'pyramidal': ('hip',),
             'skillion': ('shed', 'shedx')}.get(shape, tuple(cand))
    best = None
    for name in allow:
        f, run, code, bits = cand[name]
        (c0, k), *_ = np.linalg.lstsq(np.stack([np.ones_like(f), -f], 1), zz, rcond=None)
        if code != 5 and k <= 0: continue
        sse = float(((zz - (c0 - k * f)) ** 2).sum()) * (1.15 if bits else 1.0)  # across the short side: only when clearly better
        if best is None or sse < best[0]: best = (sse, c0, k, run, code, bits)
    if not best: return None
    sse, c0, k, run, code, bits = best
    rise = abs(k) * run * (2 if code == 5 else 1)
    spread = float(np.percentile(zz, 95) - np.percentile(zz, 10))
    # a clean fit, or a busy house roof (dormers, cross gables) that is still much more pitched than flat
    if not shape and (rise < 1.0 or sse > (0.85 if spread > 2.0 and hl * hw < 120 else 0.5) * sse0): return None
    if rise > 10: return None
    if code == 5:
        eaves, ridge = c0 - abs(k) * run, c0 + abs(k) * run
        if k < 0: bits |= FLIP  # higher towards +v (or +u)
    else: eaves, ridge = c0 - k * run, c0
    if eaves < 2.2: return None
    if code == 2 and hl / hw < 1.15: code = 3
    return code | bits, float(eaves), float(ridge), float(abs(k))


def roof_units(z, part, h, fx, ang, hidden):
    """rooftop plant, lift overruns and stair towers: small blobs standing 1-6 m proud of a flat roof, away from its
    edge (an overhanging tree crown touches the edge). -> [(x, y, along, across, height, angle)]"""
    m = part & ~hidden
    inner = ndimage.binary_erosion(m, iterations=1)
    res = np.where(inner, z - h, 0)
    blob = inner & (res > 1.0) & (res < 6)
    lab, n = ndimage.label(blob)
    if not n: return []
    edge = part & ~ndimage.binary_erosion(part, iterations=2)
    ca, sa = math.cos(ang), math.sin(ang)
    out = []
    for k, sl in enumerate(ndimage.find_objects(lab)):
        sel = lab[sl] == k + 1
        nb = int(sel.sum())
        if nb < 2 or nb > 40 or edge[sl][sel].any(): continue
        rr, cc = np.nonzero(sel)
        rr = rr + sl[0].start; cc = cc + sl[1].start
        x = fx[0] + (cc + 0.5) * HR; y = fx[1] - (rr + 0.5) * HR
        u, v = x * ca + y * sa, -x * sa + y * ca
        uc, vc = (u.max() + u.min()) / 2, (v.max() + v.min()) / 2
        out.append((nb, uc * ca - vc * sa, uc * sa + vc * ca, float(u.max() - u.min() + HR * 0.8), float(v.max() - v.min() + HR * 0.8),
                    float(np.clip(np.percentile(res[rr, cc], 75), 0.8, 5.0)), ang))
    out.sort(key=lambda o: -o[0])
    return [o[1:] for o in out[:16]]


def from_lidar(r, a, z, ok, fx, shape, rnd, roofm):
    """parts of one building from the LiDAR surface (heights above its base): [dict(ring, h, mn, roof, ridge, pitch, units)]"""
    v = z[ok]
    p97 = float(np.percentile(v, 97))
    # houses, sheds, halls: one pitched roof if the surface fits one (big ones only if the outline is near a rectangle)
    if p97 < 20 and (a < 700 or a < 5000 and a > 0.85 * obb(r)[5]) and shape in ('', 'gabled', 'hipped', 'skillion', 'pyramidal', 'half-hipped', 'gambrel'):
        f = fit_roof(r, z, roofm, fx, shape)
        if f: return [dict(ring=r, h=f[1], mn=0, roof=f[0], ridge=f[2], pitch=f[3], units=[])]
    if shape and shape != 'flat': return None  # the mapper's roof, on the old path
    c = height_levels(v)
    ang = obb(r)[2]
    foot = Polygon(r).buffer(0)
    import rasterio.features as rf
    from rasterio.transform import Affine
    tr = Affine(HR, 0, fx[0], 0, -HR, fx[1])
    th = [(c[i - 1] + c[i]) / 2 for i in range(1, len(c))]
    lvl_h = lambda sel, d: float(np.percentile(z[sel], 70)) if sel.sum() >= 3 else float(d)
    open_ = False
    if c[0] < 2.5 and len(c) > 1:  # the lowest "roof" is the ground: a courtyard or a plaza inside the outline
        if (v < th[0]).mean() < 0.3: c, th = c[1:], th[1:]
        else: open_ = True
    elif c[0] < 2.2: return None
    sel0 = ok & (z < th[0]) if th else ok
    parts, parents = [], []
    if not open_:
        h0 = lvl_h(sel0, c[0])
        parts.append(dict(ring=r, h=h0, mn=0, roof=0, ridge=0, mask=ok))
        parents.append((foot, h0, ok))
    else: parents.append((foot, 0.0, ok))
    for i in range(1, len(c)):
        R = ok & (z > th[i - 1])
        if R.sum() > 40: R = ndimage.binary_opening(R)
        lab, n = ndimage.label(R)
        new = []
        for k, sl in enumerate(ndimage.find_objects(lab)):
            comp = lab == k + 1
            if comp[sl].sum() < 6: continue
            g = unary_union([shapely.geometry.shape(gg) for gg, _ in rf.shapes(comp.astype(np.uint8), mask=comp, transform=tr)])
            g = squared(g, ang)
            if g.is_empty: continue
            pt = g.representative_point()
            par = next((p for p in parents if p[0].contains(pt)), None) or max(parents, key=lambda p: p[0].intersection(g).area)
            pg, ph, _ = par
            # reach out to the outline below where the raster stopped short of it
            band = pg.difference(pg.buffer(-2.5, join_style=2))
            g = g.union(g.buffer(2.5, join_style=2).intersection(band)).intersection(pg)
            hi = th[i] if i < len(th) else np.inf
            h = lvl_h(comp & (z <= hi), c[i])
            if h - ph < 1.5: continue
            for p in polys_of(g, 20):
                p = p.simplify(0.3)
                parts.append(dict(ring=list(p.exterior.coords)[:-1], h=h, mn=ph if not open_ or i > 1 else 0.0, roof=0, ridge=0, mask=comp))
                new.append((p, h, comp))
        parents = new + parents
    if not parts: return None
    # rooftop plant on each flat roof, minus what the next level up covers
    for p in parts:
        hidden = np.zeros_like(ok)
        for q in parts:
            if q is not p and q['mn'] >= p['h'] - 0.5: hidden |= q['mask']
        p['units'] = roof_units(z, p['mask'], p['h'], fx, ang, hidden) if a >= 120 else []
        p['pitch'] = 0
        del p['mask']
    return parts


def house_kind(info, main):
    """Vancouver's two classic houses, told apart by their roofs: the character house (1900s-1930s, steep gables, wood
    siding, a porch) and the Vancouver Special (1965-85, two storeys, a shallow gable, stucco over a brick base).
    A city of row houses (HOUSES_KIND 'rowhouse': Manhattan's brownstones and brick town houses) has brick ones."""
    btype, a = info['btype'], info['a']
    if city.HOUSES_KIND == 'rowhouse': return BRICK if btype in HOUSES and main['h'] <= 20 else None
    if not (btype in HOUSES or btype == 'yes' and 60 < a < 400) or main['h'] > 11: return None
    year = info['year']
    if year and year < 1940: return CHARACTER
    if not main['roof']: return None
    pitch, rise = main['pitch'], (main['ridge'] or 0) - main['h']
    if pitch >= 0.45 and rise >= 1.8: return CHARACTER
    if pitch < 0.36 and main['h'] >= 4.6 and 110 < a < 330 and (main['roof'] & 15) in (1, 2): return SPECIAL
    return None


def start_year(t):
    s = str(t.get('start_date', ''))
    for k in range(len(s) - 3):
        if s[k:k + 4].isdigit(): return int(s[k:k + 4])
    return None


def style_of(info, h, rnd, kind):
    t, btype, a = info['t'], info['btype'], info['a']
    mat = (t.get('building:material') or '').strip().lower()
    year = info['year']  # OSM start_date, else the tax report's year built
    tower = h > 30 and btype in RESID | {'hotel'}
    zn = info['zone']
    if mat in MATERIAL:
        s = MATERIAL[mat]
        if s == OFFICE and tower and btype != 'hotel': return GLASS
        if s == CHARACTER and h > 12: return COMM
        return s
    if kind is not None: return kind
    heritage = t.get('heritage') or t.get('historic') in ('building', 'yes', 'church', 'monument', 'manor', 'house') or year and year < 1930 \
        or info['fac'].get('heritage') and (year or 0) < 1940  # on the city's register (it lists modernist ones too)
    if heritage and btype not in HOUSES and h < 45:
        return STONE if btype in ('civic', 'public', 'government', 'cathedral') or t.get('amenity') in ('bank', 'townhall', 'courthouse', 'place_of_worship') else BRICK
    if zn in city.BRICK_ZONES and h < city.BRICK_ZONES[zn][0] and btype not in HOUSES and rnd.random() < city.BRICK_ZONES[zn][1]: return BRICK
    s = STYLE.get(btype)
    if zn and zn == city.SHEDS_ZONE and h < 16 and s in (None, COMM, SHED, BIGBOX): return SHED
    if s == SHED or info['ind'] and a > 300 and h < 20 and s in (None, COMM): return SHED
    if (btype in ('retail', 'commercial', 'supermarket') or t.get('shop') in ('mall', 'supermarket', 'department_store', 'doityourself', 'wholesale')) and a > 2500 and h < 16:
        return BIGBOX
    if s is None: s = HOUSE if h < 9 else APT if h > 14 else COMM
    if s == APT and h > 24 and btype != 'hotel':
        p = city.GLASS_P.get(zn, city.GLASS_P[None])
        if year: p = 0.0 if year < 1985 else 1.0 if year >= 1995 else p
        return GLASS if rnd.random() < p else CONCRETE
    if s == OFFICE and year and year < 1975: return CONCRETE
    return s


FLOOR = {OFFICE: 3.9, BRICK: 3.8, COMM: 4.2, CIVIC: 4.2, STONE: 4.6, BIGBOX: 5.0, SHED: 5.0, CONCRETE: 2.9}


def floor_h(style, top, lv, shop):
    """storey height, so window rows line up with the real floors (OSM building:levels when mapped)"""
    if lv and lv >= 1 and top > 4:
        if shop and lv > 1: return float(np.clip((top - 4.5) / (lv - 1), 2.6, 6.0))
        return float(np.clip(top / lv, 2.6, 6.0))
    return FLOOR.get(style, 2.9)


def buildings(items, dsm, dtm, X0, Y0, rnd, under=None, shops=None, ind=None, streets=None):
    """under: bridge decks overhead (bridge_mask); shops: STRtree of shop/office points; ind: industrial land;
    streets: STRtree of street lines (which way a house faces)"""
    N = dtm.shape[0]
    pix = lambda x, y: ((x - X0) / HR, (Y0 + L1 - y) / HR)
    own = lambda c: X0 <= c[0] < X0 + L1 and Y0 <= c[1] < Y0 + L1
    rows = []
    parts_c = []
    for oid, prs, t in items:
        if 'building:part' in t and 'building' not in t:
            for outer, _ in prs: parts_c.append(np.mean(outer[:-1], axis=0))
    parts_c = np.array(parts_c) if parts_c else np.zeros((0, 2))
    seen = set()
    for oid, prs, t in items:
        for outer, inners in prs:
            r = outer[:-1] if outer[0] == outer[-1] else outer
            if len(r) < 3 or len(r) > 400: continue
            c = (sum(p[0] for p in r) / len(r), sum(p[1] for p in r) / len(r))
            if not own(c): continue
            if t.get('building') == 'tower' and under is not None:  # a bridge's pylon (stal̕əw̓asəm, Port Mann) is the bridge's
                cx_, cy_ = map(int, pix(*c))
                if under[max(0, cy_ - 10):cy_ + 11, max(0, cx_ - 10):cx_ + 11].any(): continue
            a = ring_area(r)
            if a < 4: continue
            is_part = 'building:part' in t and 'building' not in t
            if not is_part and len(parts_c):
                xs = [p[0] for p in r]; ys = [p[1] for p in r]
                sel = parts_c[(parts_c[:, 0] > min(xs)) & (parts_c[:, 0] < max(xs)) & (parts_c[:, 1] > min(ys)) & (parts_c[:, 1] < max(ys))]
                if len(sel) and any(Polygon(r).contains(shapely.geometry.Point(p)) for p in sel[:20]): continue  # parts carry the shape
            key = (round(c[0] * 2), round(c[1] * 2), round(a / 4))
            if key in seen: continue
            seen.add(key)
            rows.append((r, t, a, c, is_part))

    def measure(r):
        """the LiDAR inside a ring: (heights above base on the 2 m grid, usable cells, NW corner, base) or None"""
        xs = [pix(*p) for p in r]
        bx0, by0 = max(0, int(min(p[0] for p in xs)) - 1), max(0, int(min(p[1] for p in xs)) - 1)
        bx1, by1 = min(N, int(max(p[0] for p in xs)) + 2), min(N, int(max(p[1] for p in xs)) + 2)
        if bx1 <= bx0 or by1 <= by0: return None
        m = Image.new('L', (bx1 - bx0, by1 - by0), 0)
        ImageDraw.Draw(m).polygon([(x - bx0, y - by0) for x, y in xs], fill=1)
        m = full = np.array(m, bool)
        if m.sum() > 12: m2 = ndimage.binary_erosion(m); m = m2 if m2.sum() > 4 else m
        ground = dtm[by0:by1, bx0:bx1][m] if m.any() else np.array([height_fallback(dtm, xs)])
        base = float(np.percentile(ground, 10)) if len(ground) else 0.0
        fx = (X0 + bx0 * HR, Y0 + L1 - by0 * HR)
        if dsm is None or not m.any(): return None, None, fx, base, None
        free = ~under[by0:by1, bx0:bx1] if under is not None else np.ones_like(m)
        ok = m & free
        if ok.sum() < max(3, 0.4 * m.sum()): return None, None, fx, base, None  # mostly under a bridge deck: the surface there is the deck
        z = dsm[by0:by1, bx0:bx1] - base
        return z, ok, fx, base, full & free & (z > 1.5)  # last: every roof cell, eaves included (for roof pitch fits)

    out = []
    for r, t, a, c, is_part in rows:
        mz = measure(r)
        if mz is None: continue
        z, ok, fx, base, roofm = mz
        chm = z[ok] if z is not None else None
        tag_h = hnum(t.get('height')); lv = hnum(t.get('building:levels'))
        mn = hnum(t.get('min_height')) or (hnum(t.get('building:min_level')) or 0) * 3.1
        shape = t.get('roof:shape', '')
        btype = t.get('building', t.get('building:part', 'yes'))
        shop = bool(t.get('shop') or t.get('office') or t.get('amenity') in SHOP_AMENITY) or (shops is not None and len(shops.query(Polygon(r).buffer(3), predicate='intersects')) > 0)
        fac = facade_lookup(r) or {}  # tools/facades.py: year built, heritage, wall colour, storey, balconies
        info = dict(t=t, btype=btype, a=a, shop=shop, lv=lv, ind=ind is not None and shapely.contains_xy(ind, c[0], c[1]), zone=zone(c),
                    fac=fac, year=start_year(t) or fac.get('year') or None, ring=r)
        gid = rnd.randrange(1, 1 << 30)
        parts = None
        if chm is not None and not is_part and not mn and np.percentile(chm, 90) > 2.2:
            p97 = float(np.percentile(chm, 97))
            if not (tag_h and abs(tag_h - p97) > max(6, 0.3 * tag_h)):  # else rebuilt since the LiDAR flew: the tags win
                parts = from_lidar(r, a, z, ok, fx, shape, rnd, roofm)
        if parts is None:
            h, ridge = None, None
            if tag_h: h = tag_h
            elif chm is not None and len(chm) >= 3 and np.percentile(chm, 90) > 2.2: h = float(np.percentile(chm, 90))
            elif lv: h = lv * 3.1 + 1.0
            if h is None: h = 3.0 if btype in ('garage', 'shed', 'carport', 'roof') else (rnd.uniform(5, 8) if btype in ('house', 'detached') else 7.0)
            if lv and not tag_h and (chm is None or len(chm) < 3): h = lv * 3.1 + 1
            rf_ = ROOF.get(shape, 0) if shape else 0
            if rf_:
                rh = hnum(t.get('roof:height')) or (hnum(t.get('roof:levels')) or 0) * 2.8 or min(4.0, math.sqrt(a) * 0.25)
                ridge, h = h, max(2.4, h - rh)
                if z is not None and rf_ in (1, 2, 5):  # the mapper's roof shape, the LiDAR's ridge direction
                    f = fit_roof(r, z, roofm, fx, shape)
                    if f: rf_ = f[0]
            parts = [dict(ring=r, h=h, mn=mn, roof=rf_, ridge=ridge or 0, pitch=0, units=None)]
        for p in parts:
            p.update(base=base, gid=gid, info=info, a=ring_area(p['ring']))
            if p['units'] is None and z is not None and not p['roof']: p['units'] = []  # LiDAR here: no made-up plant
            out.append(p)
    # City of Vancouver 2009 LiDAR sections are vector-exact: where they agree with today's heights, they win
    if SECTIONS:
        apply_sections(out, X0, Y0, rnd, measure, under)
    groups = {}
    for p in out: groups.setdefault(p['gid'], []).append(p)
    for gid, ps in groups.items():
        main = max(ps, key=lambda p: p['a'])
        info, t = main['info'], main['info']['t']
        top = max(p['h'] for p in ps)
        kind = house_kind(info, main)
        style = style_of(info, top, rnd, kind)
        shop = info['shop'] and style not in (HOUSE, CHARACTER, SPECIAL, SHED) and top >= 5
        tagc = colour(t.get('roof:colour'))
        matc = ROOF_MAT.get((t.get('roof:material') or '').lower(), 0) and ROOF_MAT[t['roof:material'].lower()] | 0x1000000
        mainc = 0 if tagc else photo_roof(main)
        fac = info['fac']
        fh = fac.get('storey') or floor_h(style, top, info['lv'], shop)  # the LiDAR's storey rhythm, else levels, else the style's
        gh = max(p['ridge'] or p['h'] for p in ps)
        # walls: the mapper's colour, else a known building's, else the sunlit walls in the 2022 LiDAR (not on glass: that
        # is the sky in it), else the palette
        wall = colour(t.get('building:colour'))
        if not wall and fac.get('wall') and (fac['wsrc'] == 2 or style not in (GLASS, OFFICE)): wall = fac['wall'] | 0x1000000
        if not wall and info['zone'] and info['zone'] == city.SHEDS_ZONE and style == SHED:  # Granville Island's painted tin
            wall = rnd.choice((0xb84a3a, 0xd8a838, 0x3a6a9a, 0x4a7a5a, 0x8a8c8e, 0xc86a3a, 0x5a8aa0)) | 0x1000000
        res = info['btype'] != 'hotel' and (style in (APT, GLASS) or style == CONCRETE and info['btype'] in RESID and top > 24)
        fx = facade_fx(style, res, top, info['year'], info['btype'], fac, main['ring'])
        d = rnd.uniform(0.03, 0.3)
        if style == CHARACTER and streets is not None and main['roof'] and main['a'] < 300:
            ps.append(porch(main, streets))
        for p in ps:  # roof colour: the mapper's, else the 2022 photo's (this part's, else the building's), else the material's
            roofc = tagc or (photo_roof(p) if p is not main else mainc) or mainc or matc
            p.update(style=style, wall=wall, roofc=roofc, fh=fh, shop=shop, res=res, gh=gh, fx=fx, ring=inset(p['ring'], d), h=p['h'] + rnd.random() * 0.1)
            del p['info']
    return out


def octant(ang):
    """compass octant (0 east, 2 north, ...) of a direction in radians"""
    return round(ang / (math.pi / 4)) % 8


def facade_fx(style, res, top, year, btype, fac, ring):
    """(balcony kind, facade pattern, balcony facing octants) from the building's era unless facades.py knows better;
    0s leave it to the page's own rules (and old tiles carry none). Kinds and patterns: tools/facades.py LOOKS."""
    kind, pat, facing = fac.get('balc', 0), fac.get('pattern', 0), fac.get('facing', 0)
    if year and res and top > 9 and not kind:
        if year < 1950: kind = B_NONE  # walk-ups and old apartment blocks
        elif year < 1986:  # 1960s-80s concrete towers: solid-fronted balconies along the long sides
            kind = B_SOLID
            ang = obb(ring)[2] + math.pi / 2
            facing = (1 << octant(ang)) | (1 << octant(ang + math.pi))
        else: kind = B_GLASS  # Vancouverism: glass-railed balconies all round
    if year and not pat:
        if style == GLASS: pat = 3 if year < 2006 else 9  # green glass and balconies, then window walls
        elif style == APT and top > 20: pat = 1 if year < 1986 else 3
        elif style == CONCRETE and btype not in RESID and 1955 <= year < 1986: pat = 10  # window bands between concrete
    return kind, pat, facing


def photo_roof(p):
    """the roof's colour in the city's 2022 orthophoto (tools/roofs.py, data/roofs.npz), as a tagged colour, or 0"""
    r = roof_lookup(p['ring'])
    if not r or (p['roof'] and r[3] & 1): return 0  # green over a pitched roof is the garden's trees, not the roof
    return r[0] | 0x1000000


def porch(main, streets):
    """a character house's front porch: its roof on posts across the end that faces the street"""
    cx, cy, ang, hl, hw, _ = obb(main['ring'])
    st = streets.geometries[streets.nearest(shapely.geometry.Point(cx, cy))]
    q = st.interpolate(st.project(shapely.geometry.Point(cx, cy)))
    ca, sa = math.cos(ang), math.sin(ang)
    s = 1 if (q.x - cx) * ca + (q.y - cy) * sa > 0 else -1
    u0, u1, w = s * hl, s * (hl + 2.2), hw * 0.75
    ring = [(cx + u * ca - v * sa, cy + u * sa + v * ca) for u, v in ((u0, -w), (u1, -w), (u1, w), (u0, w))]
    return dict(main, ring=ring, h=min(3.2, main['h']), mn=min(3.2, main['h']) - 0.45, roof=0, ridge=0, pitch=0, units=[], a=ring_area(ring))


def height_fallback(dtm, xs):
    x, y = xs[0]
    return float(dtm[int(min(dtm.shape[0] - 1, max(0, y))), int(min(dtm.shape[1] - 1, max(0, x)))])


SEC_INDEX = None


def apply_sections(out, X0, Y0, rnd, measure, under):
    """swap a building's parts for the city's 2009 LiDAR roof sections when they still match it; today's LiDAR gives
    each section its height, roof pitch and plant"""
    global SEC_INDEX
    if SEC_INDEX is None:
        SEC_INDEX = {}
        for r, hg, rt in SECTIONS:
            cx, cy = sum(p[0] for p in r) / len(r), sum(p[1] for p in r) / len(r)
            SEC_INDEX.setdefault((int(cx // L1), int(cy // L1)), []).append((r, hg, rt, cx, cy))
    secs = SEC_INDEX.get((int(X0 // L1), int(Y0 // L1)), [])
    if not secs: return
    from shapely.strtree import STRtree
    groups = {}
    for p in out: groups.setdefault(p['gid'], []).append(p)
    gids = list(groups)
    bases = [max(groups[g], key=lambda p: p['a']) for g in gids]
    tree = STRtree([Polygon(b['ring']).buffer(0) for b in bases])
    N = L1 / HR
    by_g = {}
    for r, hg, rt, cx, cy in secs:
        if under is not None:
            i_, j_ = int((cx - X0) / HR), int((Y0 + L1 - cy) / HR)
            if 0 <= i_ < N and 0 <= j_ < N and under[j_, i_]: continue  # measured off a bridge deck
        hits = tree.query(shapely.geometry.Point(cx, cy), predicate='within')
        if len(hits): by_g.setdefault(int(hits[0]), []).append((r, hg, rt))
    drop, add = set(), []
    for k, segs in by_g.items():
        b = bases[k]
        ps = groups[gids[k]]
        gh = max(p['ridge'] or p['h'] for p in ps)
        cov = sum(ring_area(s[0]) for s in segs) / max(1.0, b['a'])
        top = max(s[1] for s in segs)
        if cov < 0.6 or b['mn'] > 0 or abs(top - gh) > max(6, 0.3 * gh): continue  # rebuilt since 2009, or a mismatch
        new = []
        for r, hg, rt in segs:
            p = dict(b, ring=r, h=hg, roof=0, ridge=0, mn=0, pitch=0, units=b['units'] if b['units'] is None else [], a=ring_area(r))
            mz = measure(r)
            z, ok = (mz[0], mz[1]) if mz else (None, None)
            if z is not None and ok.sum() >= 3:
                v = z[ok]
                if rt == 'pitched' and hg < 18:
                    f = fit_roof(r, z, mz[4], mz[2])
                    if f: p.update(roof=f[0], h=f[1], ridge=f[2], pitch=f[3])
                if not p['roof']:
                    p['h'] = float(np.percentile(v, 70)) if abs(np.percentile(v, 70) - hg) < max(4, 0.25 * hg) else hg
                    if p['a'] >= 60: p['units'] = roof_units(z, ok, p['h'], mz[2], obb(r)[2], np.zeros_like(ok))
            elif rt == 'pitched' and hg < 16:
                cx, cy, ang, hl, hw, ar = obb(r)
                if ring_area(r) / ar > 0.75 and hw > 2: p.update(roof=1 if hl / hw > 1.15 else 2, ridge=hg, h=max(2.6, hg - min(hw * 0.9, 3.5)))
            new.append(p)
        drop |= {id(p) for p in ps}
        add += new
    out[:] = [p for p in out if id(p) not in drop] + add


# ---------------------------------------------------------------- trees
def off_streets(trees, polys, in_building):
    """no trunk on a carriageway, a cycle track or a sidewalk's walking zone, as the ground draws them (the streets'
    pieces above): a street tree stands in its boulevard, else in the tree pits along the kerb; one that can't is
    dropped. Crowns may still overhang the street. -> (trees, moved, dropped)"""
    from shapely.strtree import STRtree
    from shapely.ops import nearest_points
    carr = polys.get('road', []) + polys.get('arterial', []) + polys.get('cycle', [])
    if not trees or not carr: return trees, 0, 0
    walk, blvd = polys.get('sidewalk', []), polys.get('blvd', [])
    C, Wk, Bv = STRtree(carr), STRtree(walk or [Polygon()]), STRtree(blvd or [Polygon()])
    pts = shapely.points(np.array([(t[0], t[1]) for t in trees]))
    hit = lambda T, **k: set(T.query(pts, **k)[0].tolist())
    on_road, on_walk, on_blvd, pit = hit(C, predicate='intersects'), hit(Wk, predicate='intersects'), hit(Bv, predicate='intersects'), hit(C, predicate='dwithin', distance=1.2)
    out, moved, dropped = [], 0, 0
    for i, t in enumerate(trees):
        if i not in on_road and (i not in on_walk or i in on_blvd or i in pit):
            out.append(t); continue
        p = pts[i]
        roads = unary_union([carr[k] for k in C.query(p.buffer(14))])
        spot = None
        strips = [blvd[k] for k in Bv.query(p.buffer(8))] if blvd else []
        if strips:  # the planting strip between the kerb and the sidewalk
            g = unary_union(strips).difference(roads.buffer(0.5)).buffer(-0.4)
            if not g.is_empty: spot = nearest_points(g, p)[0]
        if spot is None or spot.distance(p) > 8:  # a tree pit by the kerb
            spot = nearest_points(roads.buffer(0.9).boundary, p)[0]
        if spot.distance(p) > 8 or roads.distance(spot) < 0.5 or in_building(spot.x, spot.y): dropped += 1; continue
        out.append((spot.x, spot.y, *t[2:])); moved += 1
    return out, moved, dropped


def crown_widths(chm, canopy, at, hs):
    """crown diameter (m) of each tree at grid cell at[k] (height hs[k]): the canopy cells nearer to it than to any other
    crown, within reach of its height, as a disc of the same area"""
    if not at: return []
    mark = np.zeros(chm.shape, np.int32)
    for k, (y, x) in enumerate(at): mark[y, x] = k + 1
    d, (iy, ix) = ndimage.distance_transform_edt(mark == 0, return_indices=True)
    own = mark[iy, ix] - 1
    reach = (0.45 * np.asarray(hs, np.float32) + 2.0).clip(3, 13) / HR
    mine = canopy & (d <= reach[own])
    area = np.bincount(own[mine], minlength=len(at)) * HR * HR
    return [float(np.clip(2 * math.sqrt(a / math.pi), 0.35 * h + 1.5, 1.1 * h + 3)) for a, h in zip(area, hs)]


def crown_q(cw):
    """a crown width in the high nibble of a tree's colour byte (0 = not known: the page sizes it by its height)"""
    return int(np.clip(round((cw - 2.0) / 1.4), 1, 15))


def canopy_trees(dsm, dtm, fp, water, X0, Y0, rnd, polys, nogo):
    if dsm is None: return []
    N = dtm.shape[0]
    chm = ndimage.gaussian_filter(np.nan_to_num(dsm - dtm), 0.7)
    blocked = ndimage.binary_dilation(fp, iterations=1) | water | nogo
    ok = (chm > 3.0) & ~(ndimage.binary_dilation(fp, iterations=1) | water)
    d_free, (fi, fj) = ndimage.distance_transform_edt(blocked, return_indices=True)
    mx = ndimage.maximum_filter(chm, size=5)
    peaks = ok & (chm >= mx - 1e-3)
    ys, xs = np.nonzero(peaks)
    hs = chm[ys, xs]
    # thin crowded peaks: keep the tallest within the crown radius
    order = np.argsort(-hs)
    taken = np.zeros((N // 2 + 2, N // 2 + 2), np.int32) - 1
    keep, crown_at = [], []
    for k in order:
        y, x, h = ys[k], xs[k], hs[k]
        rad = max(1.5, min(7.5, 0.22 * h + 1.0)) * 0.7 / HR
        y0, x0 = max(0, int(y - rad)), max(0, int(x - rad))
        if peaks[y0:int(y + rad) + 1, x0:int(x + rad) + 1].sum() > 1:
            ring = peaks[y0:int(y + rad) + 1, x0:int(x + rad) + 1]
        cy, cx = y // 2, x // 2
        if taken[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2].max() >= 0 and h < 12: continue
        if blocked[y, x]:  # a crown over the road: its trunk stands on the nearest kerb, if one is close
            if d_free[y, x] * HR > 5: continue
            y, x = fi[y, x], fj[y, x]
        taken[cy, cx] = len(keep)
        keep.append((X0 + (x + 0.5) * HR, Y0 + L1 - (y + 0.5) * HR, float(h)))
        crown_at.append((ys[k], xs[k]))
    # crown widths: every canopy cell belongs to the nearest kept crown (within reach of its height), so neighbouring
    # crowns meet the way the canopy does and a street of trees reads as one green mass, not a row of lollipops
    cw = crown_widths(chm, (chm > 3.0) & ~fp & ~water, crown_at, [k[2] for k in keep])  # crowns may overhang roofs
    # species: the city's inventory where it has the tree, else a guess from the crown and the land cover
    near = {}
    for x, y, genus, common, h in TREES:
        if X0 - 5 <= x < X0 + L1 + 5 and Y0 - 5 <= y < Y0 + L1 + 5: near.setdefault((int(x // 4), int(y // 4)), []).append((x, y, genus, h))
    out = []
    used = set()
    for (x, y, h), w in zip(keep, cw):
        g = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for c in near.get((int(x // 4) + dx, int(y // 4) + dy), []):
                    if math.hypot(c[0] - x, c[1] - y) < 4.5 and id(c) not in used: g = c; break
                if g: break
            if g: break
        if g:
            used.add(id(g)); shape, col = tree_look(g[2], rnd)
        else:
            py, px_ = int((Y0 + L1 - y) / HR), int((x - X0) / HR)
            ring = chm[max(0, py - 2):py + 3, max(0, px_ - 2):px_ + 3]
            peaky = (h - ring.mean()) / max(1, h)
            conifer = h > 24 or peaky > 0.22 or h > 14 and rnd.random() < 0.5
            if conifer: shape, col = rnd.choice((4, 4, 4, 5, 5, 6)), rnd.choice((5, 5, 7, 6))
            else: shape, col = rnd.choice((0, 0, 0, 2, 1, 3)), rnd.choice((0, 0, 0, 0, 7, 1, 2, 4))
        out.append((x, y, shape, col | crown_q(w) << 4, min(63.0, h)))
    # young street trees the LiDAR can't see yet
    for cell, lst in near.items():
        for c in lst:
            if id(c) in used or c[3] > 9: continue
            if not (X0 <= c[0] < X0 + L1 and Y0 <= c[1] < Y0 + L1): continue
            py, px_ = int((Y0 + L1 - c[1]) / HR), int((c[0] - X0) / HR)
            if 0 <= py < N and 0 <= px_ < N and blocked[py, px_]: continue
            shape, col = tree_look(c[2], rnd)
            out.append((c[0], c[1], shape, col | crown_q(0.45 * c[3] + 1.5) << 4, c[3]))
    return out


def berths(docks, polys, areas, X0, Y0, rnd):
    """boats alongside the docks of marinas: one each side of a finger, then along the walkways wherever one fits
    (never over a dock, another boat or the shore). -> [(x, y, heading, length, kind)]"""
    from shapely import affinity
    marina = [Polygon(o).buffer(40) for _, prs, t in areas if t.get('leisure') == 'marina' for o, _ in prs if len(o) >= 4]
    if not marina: return []
    marina = unary_union(marina)
    water = unary_union(polys.get('water', [])) if polys.get('water') else None
    if water is None: return []
    shapely.prepare(water); shapely.prepare(marina)
    deck = unary_union([d.buffer(w / 2 + 0.3, cap_style='flat') for d, w in docks])
    shapely.prepare(deck)
    out, placed = [], {}  # placed: 20 m cell -> hulls (with a gap round them)
    for ls, w in sorted(docks, key=lambda d: d[0].length):  # fingers first: they decide the berths
        if not marina.intersects(ls): continue
        cs = list(ls.coords)
        for (ax, ay), (bx, by) in zip(cs, cs[1:]):
            seg = math.hypot(bx - ax, by - ay)
            if seg < 5: continue
            ux, uy = (bx - ax) / seg, (by - ay) / seg
            step = seg if seg < 26 else 0  # a finger: one berth per side, the length of the finger; a walkway: a row
            for side in (1, -1):
                s0 = 0.0
                while s0 < seg - 4:
                    ln = min(seg * 0.9, rnd.uniform(7, 15)) if step else rnd.uniform(8, 16)
                    if step: s0 = (seg - ln) / 2
                    beam = 0.32 * ln
                    off = w / 2 + beam / 2 + 0.6
                    cx, cy = ax + ux * (s0 + ln / 2) - uy * off * side, ay + uy * (s0 + ln / 2) + ux * off * side
                    hull = affinity.rotate(box(-ln / 2, -beam / 2, ln / 2, beam / 2), math.atan2(uy, ux), use_radians=True, origin=(0, 0))
                    hull = affinity.translate(hull, cx, cy)
                    cell = (int(cx // 20), int(cy // 20))
                    near = [q for i in (-1, 0, 1) for j in (-1, 0, 1) for q in placed.get((cell[0] + i, cell[1] + j), ())]
                    if rnd.random() < 0.85 and water.contains(hull) and not deck.intersects(hull) and not any(hull.intersects(q) for q in near):
                        placed.setdefault(cell, []).append(hull.buffer(0.4))
                        sail = ln > 8.5 and rnd.random() < 0.5
                        col = 0 if rnd.random() < 0.8 else rnd.randrange(1, 4)  # white, navy, dark green, blue
                        out.append((cx, cy, math.atan2(uy, ux) % (2 * math.pi), ln, (1 if sail else 0) | col << 1))
                    if step: break
                    s0 += ln + 1.2
    return out


# ---------------------------------------------------------------- roads, paths, rail
TURN_BITS = {'left': 1, 'slight_left': 2, 'through': 4, 'slight_right': 8, 'right': 16, 'reverse': 32, 'merge_to_left': 64, 'merge_to_right': 64, 'sharp_left': 1, 'sharp_right': 16}


def turn_lanes(v):
    """OSM turn:lanes 'left|through;right|' -> one bitmask per lane (0 = unmarked), at most 8 lanes"""
    if not v: return []
    out = []
    for lane in v.split('|')[:8]:
        m = 0
        for d in lane.split(';'): m |= TURN_BITS.get(d.strip(), 0)
        out.append(m)
    return out if any(out) else []


def road_pieces(items, dsm, dtm, X0, Y0, own=True):
    """pieces split at junctions: dict(kind, pts [(x,y)], z [..] or None, lanes, width, oneway, band, layer, a, b (node ids), name)"""
    use = {}
    ways = []
    for wid, coords, refs, t in items:
        k = line_kind(t)
        if not k: continue
        if t.get('tunnel') in ('yes', 'building_passage', 'culvert') or t.get('covered') == 'yes' and k in ('rail', 'lightrail'): continue
        if t.get('area') == 'yes': continue
        new = sorted((e[2] for e in APPROACH.get(wid, ()) if e[2]), key=lambda n: n[:2], reverse=True)
        if new:  # an approach touches down between two nodes: a node there
            coords, refs = list(coords), list(refs)
            for seg, f, x, y, nid in new: coords.insert(seg + 1, (x, y)); refs.insert(seg + 1, nid)
        ways.append((wid, coords, refs, t, k))
        for r in refs: use[r] = use.get(r, 0) + 1
        use[refs[0]] += 1; use[refs[-1]] += 1
    out = []
    for wid, coords, refs, t, k in ways:
        lanes, w, ow, band = road_shape(k, t)
        if t.get('oneway') == '-1': coords, refs = coords[::-1], refs[::-1]
        bridge = t.get('bridge') not in (None, 'no')
        turns = (turn_lanes(t.get('turn:lanes:forward') or (t.get('turn:lanes') if ow else None)), turn_lanes(t.get('turn:lanes:backward')))
        if t.get('oneway') == '-1': turns = (turn_lanes(t.get('turn:lanes')), [])
        ap = [(*sorted((refs.index(r0), refs.index(r1))), *rest) for r0, r1, _, *rest in APPROACH.get(wid, ()) if r0 in refs and r1 in refs]
        stops = {e for lo, hi, *_ in ap for e in (lo, hi)}  # an approach is pieces of its own, raised
        cut = [0] + [n for n in range(1, len(refs) - 1) if use.get(refs[n], 0) >= 2 or n in stops] + [len(refs) - 1]
        for a, b in zip(cut, cut[1:]):
            pts = coords[a:b + 1]
            mid = pts[len(pts) // 2]
            if own and not (X0 <= mid[0] < X0 + L1 and Y0 <= mid[1] < Y0 + L1): continue
            z = None
            raised = any(lo <= a and b <= hi for lo, hi, *_ in ap)
            if not own:  # surfaces only: every piece touching the tile, bridges just flagged
                z = [0.0] if bridge or raised else None
            elif bridge or k == 'lightrail' and t.get('bridge'):
                pts = densify(pts, 25)  # a long straight span needs points along it to carry its arch
                z = deck_heights(wid, pts)
            elif raised:
                pts = densify(pts, 12)
                z = approach_heights(ap, a, b, pts)
            out.append(dict(turns=turns, own=mid, kind=k, pts=pts, z=z, lanes=lanes, w=w, ow=ow, band=band, layer=int(hnum(t.get('layer')) or 0), a=refs[a], b=refs[b],
                            name=t.get('name', ''), drive=k in DRIVE and t.get('access') not in ('private', 'no'),
                            crossing=k in ('footway', 'cycleway', 'path') and (t.get('footway') == 'crossing' or t.get('cycleway') == 'crossing') and t.get('crossing') not in ('unmarked', 'no')))
    return out


# ---------------------------------------------------------------- bridge decks
# Every bridge's deck profile is worked out once for the whole map (so a bridge reads the same from every tile it
# crosses), from the LiDAR surface across the deck: a low percentile across its width (cars, railings, cables and
# trusses sit above the deck), samples far above the along-bridge profile dropped, the grade clamped, then smoothed.
# Sidewalks and cycle decks mapped beside a road deck take that deck's height; a long bridge the LiDAR does not have
# yet (stal̕əw̓asəm, 2024) or one under the trees spans between its abutments. Cached in data/decks<version>.pkl.
DECK_VERSION = 3
DECK_CACHE = f'{DATA}/decks{DECK_VERSION}.pkl'
GRADE = 0.07  # steepest deck grade kept, rise per metre
FOOT = {'footway', 'cycleway', 'path', 'pedestrian', 'steps', 'track'}
DECKS = None
APPROACH = {}  # at-grade way id -> [(deck end node, touchdown node, new node or None, points, distance along, height)]


def deck_samples(coords, w):
    """samples every 4 m along a way: (points, distance along, low surface across the deck, the lower of the ground
    25 m out to either side)"""
    P = np.array(densify(coords, 4), float)
    s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
    t = np.gradient(P, axis=0); t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-6)[:, None]
    n = np.stack([-t[:, 1], t[:, 0]], 1)
    half = max(1.5, w * 0.4)
    across = np.linspace(-half, half, max(3, int(half) + 1)); along = np.array([-2.0, 0.0, 2.0])
    q = P[:, None, None, :] + n[:, None, None, :] * across[None, :, None, None] + t[:, None, None, :] * along[None, None, :, None]
    v = hr_lookup(q[..., 0], q[..., 1], 0).reshape(len(P), -1)
    v[v < hr_lookup(q[..., 0], q[..., 1], 1).reshape(len(P), -1) - 2] = np.nan  # no deck below the ground (the survey's water glitches: -600 m)
    with np.errstate(all='ignore'):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            low = np.nanpercentile(v, 20, axis=1)
    off = w / 2 + 25
    side = np.fmin(hr_lookup(*(P + n * off).T, 1), hr_lookup(*(P - n * off).T, 1))
    return P, s, low, side


def wooded(P, low, w):
    """a footbridge's LiDAR is the canopy over it: tall trees beside it, and the "deck" well above the ground at both
    of its ends (where a real one meets the path)"""
    t = np.gradient(P, axis=0) if len(P) > 1 else np.array([[1.0, 0.0]])
    t = t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-6)[:, None]; n = np.stack([-t[:, 1], t[:, 0]], 1) * (w / 2 + 6)
    side = np.concatenate([hr_lookup(*(P + n).T, 0) - hr_lookup(*(P + n).T, 1), hr_lookup(*(P - n).T, 0) - hr_lookup(*(P - n).T, 1)])
    gap = low[[0, -1]] - hr_lookup(P[[0, -1], 0], P[[0, -1], 1], 1)
    with np.errstate(all='ignore'):
        return bool(np.nanmedian(side) > 8 and np.all(gap > 3)) if np.isfinite(side).any() else False


def robust_deck(s, z):
    """drop what stands above the deck (trusses, stay cables, pylons), clamp the grade, smooth; None if no samples"""
    ok = np.isfinite(z)
    if not ok.any(): return None
    z = np.interp(s, s[ok], z[ok])
    if len(z) > 4:
        z = ndimage.median_filter(z, 5, mode='nearest')  # lone glitches (expansion joints, a gap) either way
        # a running fit along the deck (straight over ~200 m, then a curve over ~400 m, so crests and sags are kept);
        # what stands well above it (trusses, stay cables, pylons) is dropped, then again with a tighter margin
        keep = np.ones(len(z), bool)
        u = (s - s.mean()) / 1000.0
        for margin, win, deg in ((6.0, 51, 1), (3.0, 51, 1), (1.5, 51, 1), (1.5, 101, 2), (1.5, 101, 2)):
            f = lambda a: ndimage.uniform_filter1d(a, min(len(z), win), mode='nearest')
            w = keep.astype(float)
            M = np.stack([np.stack([f(w * u ** (i + j)) for j in range(deg + 1)], -1) for i in range(deg + 1)], -2)
            r = np.stack([f(w * z * u ** i) for i in range(deg + 1)], -1)
            ok_ = np.abs(np.linalg.det(M)) > 1e-12
            fit = z.copy()
            if ok_.any():
                c = np.linalg.solve(M[ok_], r[ok_][..., None])[..., 0]
                fit[ok_] = sum(c[:, i] * u[ok_] ** i for i in range(deg + 1))
            keep = (z < fit + margin) & (z > fit - 4)
            if keep.sum() < 2: break
        if keep.sum() >= 2: z = np.interp(s, s[keep], z[keep])
    ds = np.diff(s)
    for _ in range(2):  # lower envelope of slope GRADE (cuts spikes), then the upper one (fills dips)
        for k in range(1, len(z)): z[k] = min(z[k], z[k - 1] + GRADE * ds[k - 1])
        for k in range(len(z) - 2, -1, -1): z[k] = min(z[k], z[k + 1] + GRADE * ds[k])
    for k in range(1, len(z)): z[k] = max(z[k], z[k - 1] - GRADE * ds[k - 1])
    for k in range(len(z) - 2, -1, -1): z[k] = max(z[k], z[k + 1] - GRADE * ds[k])
    return ndimage.uniform_filter1d(z, 5, mode='nearest') if len(z) >= 5 else z


def deck_profiles():
    """{way id: (way coords, distance along of each sample, deck height)} for every bridge on the map"""
    global DECKS
    if DECKS is not None: return DECKS
    import glob
    files = sorted(glob.glob(f'{DATA}/feat/*.pkl'))
    key = (DECK_VERSION, max((os.path.getmtime(f) for f in files), default=0))
    if os.path.exists(DECK_CACHE):
        d = pickle.load(open(DECK_CACHE, 'rb'))
        if d.get('key') == key: DECKS = d['decks']; APPROACH.update(d['approach']); return DECKS
    t0 = time.time()
    ways = {}
    for f in files:
        for wid, coords, refs, t in pickle.load(open(f, 'rb')).get('lines', []):
            if t.get('bridge') in (None, 'no') or len(coords) < 2: continue
            k = line_kind(t)
            if k and t.get('tunnel') not in ('yes', 'building_passage', 'culvert'): ways[wid] = (coords, refs, t, k)
    raw = {}
    for wid, (coords, refs, t, k) in sorted(ways.items(), key=lambda kv: (int(kv[1][0][0][0] // L1), int(kv[1][0][0][1] // L1))):
        P, s, low, gnd = deck_samples(coords, road_shape(k, t)[1])
        raw[wid] = (P, s, low, gnd)
    layer = {wid: int(hnum(t.get('layer')) or 0) for wid, (c, r, t, k) in ways.items()}
    prof = {}
    missing, canopy = set(), set()
    for wid, (P, s, low, gnd) in raw.items():
        L = s[-1]
        mid = (s > 0.15 * L) & (s < 0.85 * L)
        g = np.nan_to_num(gnd, nan=0.0)
        if layer[wid] >= 1 and L > 80 and mid.any() and np.mean(~np.isfinite(low[mid]) | (low[mid] - g[mid] < 2)) > 0.6:
            missing.add(wid)  # the surface is no higher than the land and water beside it: the deck is newer than the survey
        elif ways[wid][3] in FOOT | {'service', 'residential', 'tertiary'} and L < 400 and wooded(P, low, road_shape(ways[wid][3], ways[wid][2])[1]):
            missing.add(wid); canopy.add(wid)  # a small bridge under the trees: the LiDAR sees their crowns, not the deck
        else:
            z = robust_deck(s, low)
            if z is not None: prof[wid] = z
    # ends: a node shared by bridge ways gets one height
    ends = {}
    for wid, (coords, refs, t, k) in ways.items():
        for e, r in ((0, refs[0]), (-1, refs[-1])): ends.setdefault((r, layer[wid]), []).append((wid, e))
    # missing decks: from abutment to abutment (where they meet a deck we have, or the ground) with a gentle crest
    for wid in missing:
        coords, refs, t, k = ways[wid]
        P, s, low, gnd = raw[wid]
        hs = []
        for e, r in ((0, refs[0]), (-1, refs[-1])):
            nb = [prof[w][e2] for w, e2 in ends[(r, layer[wid])] if prof.get(w) is not None]
            hs.append(float(np.mean(nb)) if nb else float(np.nan_to_num(hr_lookup(P[e, :1], P[e, 1:], 1)[0], nan=0.0)))
        prof[wid] = None  # filled below, once neighbours along the same missing bridge are known
        ways[wid] = (coords, refs, t, k, hs)
    # chain the missing ways of one bridge (shared nodes) so the crest spans the whole river, not each way
    comp = {wid: wid for wid in missing}
    def root(a):
        while comp[a] != a: comp[a] = comp[comp[a]]; a = comp[a]
        return a
    for (r, lay), lst in ends.items():
        ms = [w for w, _ in lst if w in missing]
        for a, b in zip(ms, ms[1:]): comp[root(a)] = root(b)
    groups = {}
    for wid in missing: groups.setdefault(root(wid), []).append(wid)
    for g in list(groups.values()):
        pts = np.concatenate([raw[w][0] for w in g])
        wet = sum(4 * np.sum(~np.isfinite(raw[w][2]) | (raw[w][2] < 2.5)) for w in g)  # metres of it at water level
        if not canopy.intersection(g) and (np.ptp(pts[:, 0]) ** 2 + np.ptp(pts[:, 1]) ** 2 < 300 ** 2 or wet < 100):  # a new river crossing, not odd LiDAR
            for w in g:
                missing.discard(w); ways[w] = ways[w][:4]
                z = robust_deck(raw[w][1], raw[w][2].copy())
                if z is not None: prof[w] = z
                else: prof.pop(w, None)
            groups = {k: v for k, v in groups.items() if v is not g}
    for g in groups.values():
        pts = np.concatenate([raw[w][0] for w in g])
        # the bridge's axis: its two farthest-apart points; each end's height from the ways' outer ends nearest it
        a = pts[np.argmax(np.hypot(*(pts - pts.mean(0)).T))]; b = pts[np.argmax(np.hypot(*(pts - a).T))]
        ax = (b - a) / max(1e-6, np.hypot(*(b - a))); Lb = float(np.hypot(*(b - a)))
        ha, hb = [], []
        for w in g:
            coords, refs, t, k, hs = ways[w]
            for e, h in zip((0, -1), hs):
                if sum(1 for w2, _ in ends[(refs[e], layer[w])] if w2 in missing) > 1: continue  # inner joint, not an abutment
                (ha if np.dot(raw[w][0][e] - a, ax) < Lb / 2 else hb).append(h)
        h0 = float(np.mean(ha)) if ha else 0.0; h1 = float(np.mean(hb)) if hb else h0
        crest = 0.016 * Lb if not canopy.issuperset(g) else min(1.0, 0.016 * Lb)  # end grades about 6%; a footbridge barely arches
        for w in g:
            u = np.clip((raw[w][0] - a) @ ax / max(Lb, 1e-6), 0, 1)
            prof[w] = h0 + (h1 - h0) * u + crest * 4 * u * (1 - u)
    for wid in missing: ways[wid] = ways[wid][:4]
    # sidewalks and cycle decks beside a road deck (within 25 m, same layer) ride on it
    from shapely.strtree import STRtree
    roads = {}
    for wid, (coords, refs, t, k) in ways.items():
        if k in DRIVE and prof.get(wid) is not None: roads.setdefault(layer[wid], []).append(wid)
    trees = {lay: (STRtree([LineString(ways[w][0]) for w in ws]), ws) for lay, ws in roads.items()}
    for wid, (coords, refs, t, k) in ways.items():
        if k not in FOOT or layer[wid] not in trees or prof.get(wid) is None: continue
        tree, ws = trees[layer[wid]]
        P = raw[wid][0]
        pts = shapely.points(P)
        idx, dist = tree.query_nearest(pts, max_distance=25, return_distance=True, all_matches=False)
        if not len(idx[0]): continue
        z = prof[wid].copy()
        for i_, j_ in zip(*idx):
            rw = ws[j_]; line = LineString(ways[rw][0])
            z[i_] = np.interp(line.project(pts[i_]), raw[rw][1], prof[rw])
        z = robust_deck(raw[wid][1], z) if len(idx[0]) < len(P) else z
        prof[wid] = ndimage.uniform_filter1d(z, 9, mode='nearest') if len(z) >= 9 else z  # beside two decks: no hopping
    # one height where bridge ways meet: the mean of their ends, eased in along each way
    fix, joints = {}, {}
    for (r, lay), lst in ends.items(): joints.setdefault(r, []).extend(lst)  # whatever their layers: a shared node is a joint
    for r, lst in joints.items():
        lst = [(w, e) for w, e in lst if prof.get(w) is not None]
        if len(lst) < 2: continue
        m = float(np.mean([prof[w][e] for w, e in lst]))
        for w, e in lst: fix.setdefault(w, [0.0, 0.0])[0 if e == 0 else 1] = m - prof[w][e]
    def ease(fix):
        for w, (d0, d1) in fix.items():
            s = raw[w][1]; u = s / max(s[-1], 1e-6)
            prof[w] = prof[w] + d0 * (1 - u) + d1 * u
    ease(fix)
    def at_nodes(w):  # (node, x, y, deck height) at each node of a deck way
        c = np.asarray(ways[w][0], float); sv = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(c, axis=0).T))])
        return zip(ways[w][1], c[:, 0], c[:, 1], np.interp(sv, raw[w][1], prof[w]))
    # a deck that branches off another one's middle (a ramp, a boardwalk's fork) meets it at its height there
    inner = {r: h for w in prof if prof[w] is not None for k_, (r, x, y, h) in enumerate(at_nodes(w)) if 0 < k_ < len(ways[w][1]) - 1}
    ease({w: [inner[ways[w][1][0]] - prof[w][0] if ways[w][1][0] in inner else 0.0, inner[ways[w][1][-1]] - prof[w][-1] if ways[w][1][-1] in inner else 0.0]
          for w in prof if prof[w] is not None and (ways[w][1][0] in inner or ways[w][1][-1] in inner)})
    DECKS = {w: (np.asarray(ways[w][0], np.float64), raw[w][1].astype(np.float32), np.asarray(prof[w], np.float32)) for w in prof if prof[w] is not None}
    tops = {}  # every node of a deck: where an at-grade way goes on from it
    for w in DECKS:
        for r, x, y, h in at_nodes(w): tops.setdefault(r, []).append((x, y, h))
    APPROACH.clear(); APPROACH.update(approaches(files, {r: np.mean(v, axis=0) for r, v in tops.items()}))
    HR_CACHE.clear()
    try: pickle.dump({'key': key, 'decks': DECKS, 'approach': APPROACH}, open(DECK_CACHE, 'wb'), protocol=5)
    except OSError: pass
    print('bridge decks', len(DECKS), 'of', len(ways), 'ways,', len(missing), 'not in the LiDAR,', len(APPROACH), 'approaches', round(time.time() - t0), 's', flush=True)
    return DECKS


def approaches(files, tops):
    """At-grade ways that go on from a deck's end well above the ground: the structure goes on past OpenStreetMap's
    bridge tag (a viaduct's last spans, a ramp on a retained fill, the guideway down to a tunnel portal, stairs off a
    footbridge, a connector across a bridge). From the deck's end they follow the low LiDAR surface along the way, no
    steeper than a ramp (a stair for steps), until they meet the ground; on into the next way if they don't, or to the
    deck at its far end. tops: deck end node -> (x, y, deck height). -> {way id: [(deck end node, touchdown node, the
    touchdown node to add to the way (segment, fraction along it, x, y, node id) or None, points, along, height)]}"""
    cell = 512
    near = {(int(x // cell) + a, int(y // cell) + b) for x, y, _ in tops.values() for a in (-1, 0, 1) for b in (-1, 0, 1)}
    tops = {r: h for r, (x, y, h) in tops.items()}
    at = {}  # node -> [(way id, ref index)] for at-grade ways near a deck end
    ways, blds = {}, []
    for f in files:
        for wid, coords, refs, t in pickle.load(open(f, 'rb')).get('lines', []):
            if wid in ways or len(coords) < 2 or t.get('bridge') not in (None, 'no') or t.get('area') == 'yes': continue
            k = line_kind(t)
            if not k or t.get('tunnel') in ('yes', 'building_passage', 'culvert') or t.get('covered') == 'yes' and k in ('rail', 'lightrail'): continue
            if t.get('indoor') or t.get('level', '0') != '0' or t.get('highway') == 'corridor': continue  # inside a building
            if not any((int(x // cell), int(y // cell)) in near for x, y in coords[::4] + coords[-1:]): continue
            ways[wid] = (coords, refs, k, road_shape(k, t)[1])
            for n_, r in enumerate(refs): at.setdefault(r, []).append((wid, n_))
        for _, prs, t in pickle.load(open(f, 'rb')).get('buildings', []):
            for outer, _ in prs:
                if len(outer) >= 4 and (int(outer[0][0] // cell), int(outer[0][1] // cell)) in near: blds.append(Polygon(outer).buffer(0))
    from shapely.strtree import STRtree
    blds = STRtree(blds)
    out = {}
    seen = set()
    queue = [(r, h, 400.0) for r, h in tops.items() if r in at]
    while queue:
        r, h0, budget = queue.pop()
        for wid, k0 in at.get(r, []):
            coords, refs, kind, w = ways[wid]
            for order in (list(range(k0, len(refs))), list(range(k0, -1, -1))):  # on from the deck end, either way
                if len(order) < 2 or (wid, k0, order[1]) in seen: continue
                seen.add((wid, k0, order[1]))
                pts = [coords[n_] for n_ in order]
                P, s, low, _ = deck_samples(pts, w)
                g = hr_lookup(P[:, 0], P[:, 1], 1)
                g = np.where(np.isfinite(g), g, MR[((MR_Y1 - P[:, 1]) / 32).astype(int).clip(0, MR.shape[0] - 1), ((P[:, 0] - MR_X0) / 32).astype(int).clip(0, MR.shape[1] - 1)])
                if not np.isfinite(g[0]) or h0 - g[0] < 1.0: continue
                if len(blds.query(shapely.points(P[min(2, len(P) - 1)]), predicate='within')): continue  # into a building (a skywalk, a station)
                vert = np.concatenate([[0], np.cumsum([max(1, int(math.dist(a, b) / 4)) for a, b in zip(pts, pts[1:])])])
                G = 0.8 if kind == 'steps' else 0.03 if kind == 'rail' else 0.06 if kind == 'lightrail' else 0.10 if kind in FOOT else 0.08
                far = tops.get(refs[order[-1]])
                if far is not None and s[-1] < 150:  # a connector between two decks: straight from one to the other
                    if abs(far - h0) > (0.5 if kind in FOOT else 0.1) * s[-1]: continue  # too steep for a ramp: stairs, a lift
                    z, down = h0 + (far - h0) * s / max(s[-1], 1e-6), None
                else:  # a footway's surface beyond its bridge is a building or the trees as often as a ramp: it just comes down
                    z, down = ramp(s, low, g, h0, G, 0 if kind in FOOT else 300)
                if down is not None and (down < 0 or s[down] < 6): continue  # down at once: stairs or a lift, not a ramp
                end, ins, keep = refs[order[-1]], None, len(P)
                if down is not None:  # the approach ends where it touches down: at a node of the way, else at a new one there
                    vi = int(np.searchsorted(vert, down))
                    if vert[vi] - down <= 1: end, keep = refs[order[vi]], vert[vi] + 1; z[down + 1:keep] = g[down + 1:keep]
                    elif down - vert[vi - 1] <= 1: end, keep = refs[order[vi - 1]], vert[vi - 1] + 1
                    else:
                        seg = min(order[vi - 1], order[vi])
                        f = math.dist(coords[seg], P[down]) / max(1e-6, math.dist(coords[seg], coords[seg + 1]))
                        end, keep = (1 << 52) + wid * 64 + len(out.get(wid, ())), down + 1  # a node id of our own, below 2^53 for the page
                        ins = (seg, f, *P[down], end)
                out.setdefault(wid, []).append((refs[order[0]], end, ins, P[:keep].copy(), s[:keep].astype(np.float32), z[:keep].astype(np.float32)))
                if down is None and refs[order[-1]] not in tops and s[-1] < budget:
                    queue.append((refs[order[-1]], float(z[-1]), budget - s[-1]))
    return out


def ramp(s, low, g, h0, G, follow):
    """heights down from a deck end at h0 along samples s (low LiDAR surface, ground g; g's gaps are filled in place):
    on the surface while it goes on smoothly (up to `follow` metres), no steeper than G, never above the surface (the
    LiDAR would have seen it) nor below the ground -> (heights, NaN past the touchdown; index of the touchdown, None if it
    never does, -1 if the LiDAR sees open ground well below it: whatever holds the deck end up, it isn't a ramp)"""
    z = np.full(len(s), np.nan); z[0] = c = h0
    ok = np.isfinite(low)
    low = np.where(ok, ndimage.median_filter(np.where(ok, low, -1e4), 5, mode='nearest'), np.nan)  # a lone tree or sign over it
    for m in range(1, len(s)):
        ds = s[m] - s[m - 1]
        if not np.isfinite(g[m]): g[m] = g[m - 1] if np.isfinite(g[m - 1]) else c
        if s[m] > follow or not np.isfinite(low[m]) or abs(low[m] - low[m - 1]) > 1.5: follow = -1  # gone, or treetops: down from here
        c = np.clip(low[m], c - G * ds, c + 0.25 * G * ds) if follow > 0 else c - G * ds
        if np.isfinite(low[m]) and low[m] < c - 0.75 and low[m] < g[m] + 0.5:  # the LiDAR sees the open ground under it: no structure
            z[m] = g[m]
            return z, (m if c - g[m] < 3 else -1)
        c = max(c, g[m])
        z[m] = c
        if c - g[m] < 0.3: return z, m
    return z, None


def approach_heights(ap, a, b, pts):
    """an at-grade piece's heights where it is part of one of its way's approaches (from ref a to ref b), else None"""
    for lo, hi, P, s, z in ap:
        if lo <= min(a, b) and max(a, b) <= hi:
            line = LineString(P)
            return [float(np.interp(line.project(shapely.geometry.Point(p)), s, z)) for p in pts]
    return None


def deck_heights(wid, pts):
    """a bridge piece's deck height at each of its points (from deck_profiles), or None"""
    d = deck_profiles().get(wid)
    if d is None: return None
    coords, s, z = d
    line = LineString(coords)
    # samples were taken every 4 m along the densified way: map a point to distance along, then to height
    return [float(np.interp(line.project(shapely.geometry.Point(p)), s, z)) for p in pts]


# ---------------------------------------------------------------- tile writer
def pack_ring(out, ring):
    """ring -> count + delta-encoded int16 points (first absolute)"""
    q = [out.q(x, y) for x, y in ring]
    b = bytearray(struct.pack('<I', len(q)))
    px_, py_ = 0, 0
    for x, y in q:
        dx, dy = x - px_, y - py_
        b += struct.pack('<hh', max(-32768, min(32767, dx)), max(-32768, min(32767, dy)))
        px_, py_ = x, y
    return b


def write_tile(out, tx, ty, size, terr, polys, lines, builds, trees, detail, water_only, pois=(), boats=()):
    tb = box(tx, ty, tx + size, ty + size)
    n = terr.shape[0]
    out.section('TERR', struct.pack('<H', n) + np.clip(np.round(terr * 10), -32768, 32767).astype('<i2').tobytes())
    # ground: per class, the union clipped to the tile, as polygons (rings); the page triangulates and drapes them
    gb = bytearray(); runs = 0
    tol, min_area = (0.25, 4) if detail else (4.0, 600)
    wat = [p for p in polys.get('water', []) if p.intersects(tb)]
    water_u = unary_union(wat) if wat else None
    land_base = tb.difference(water_u) if wat else tb  # land and water meet edge to edge, never overlap
    # per class: the union clipped to the tile and simplified, small bits dropped (the class below fills in)
    per = {}
    for k in GROUND:
        src = [land_base] if k == 'land' else polys.get(k, [])
        cand = [p for p in src if p.intersects(tb)]
        if not cand: continue
        try: g = unary_union(cand).intersection(tb)
        except Exception: g = unary_union([c.buffer(0) for c in cand]).intersection(tb)
        if wat and k not in ('water', 'pier', 'land') and k not in ('road', 'arterial', 'sidewalk', 'blvd', 'walk', 'cycle', 'trail', 'ballast'): g = g.difference(water_u)
        g = g.simplify(tol)
        if k != 'land':
            parts = [p for p in (g.geoms if hasattr(g, 'geoms') else [g]) if p.geom_type == 'Polygon' and p.area >= min_area]
            if not parts: continue
            g = unary_union(parts)
        per[k] = g
    # one non-overlapping layer: each class keeps what no class above it covers (no z-fighting anywhere)
    covered = None
    for k in reversed(GROUND):
        if k not in per: continue
        g = per[k]
        mine = g if covered is None else g.difference(covered)
        covered = g if covered is None else covered.union(g)
        per[k] = mine
    for k in GROUND:
        if k not in per: continue
        g = per[k]
        geoms = [p for p in (g.geoms if hasattr(g, 'geoms') else [g]) if p.geom_type == 'Polygon' and p.area >= 0.01]
        if not geoms: continue
        body = bytearray()
        for p in geoms:
            rings = [list(p.exterior.coords)[:-1]] + [list(r.coords)[:-1] for r in p.interiors]
            rings = [r for r in rings if len(r) >= 3]
            body += struct.pack('<H', len(rings))
            for r in rings: body += pack_ring(out, r)
        gb += struct.pack('<BI', GI[k], len(geoms)) + body
        runs += 1
    out.section('GRND', struct.pack('<H', runs) + bytes(gb))
    # lines, with a node table so pieces can join across tiles
    sel = []
    for L in lines:
        if not detail and (KI[L['kind']] >= KI['service'] and L['kind'] not in ('rail', 'lightrail', 'runway', 'taxiway')): continue
        mid = L['own']  # the point that decided which 4 km tile owns the piece
        if tx <= mid[0] < tx + size and ty <= mid[1] < ty + size: sel.append(L)
    nodes = {}
    for L in sel:
        for nid in (L['a'], L['b']): nodes.setdefault(nid, len(nodes))
    lb = bytearray(struct.pack('<I', len(nodes)))
    for nid in nodes: lb += struct.pack('<Q', nid)
    lb += struct.pack('<I', len(sel))
    for L in sel:
        pts = L['pts'] if detail or L['z'] or len(L['pts']) <= 2 else list(LineString(L['pts']).simplify(1.5).coords)
        z = L['z'] if L['z'] and len(pts) == len(L['pts']) else None  # bridge decks in overview tiles too
        tf, tb_ = L.get('turns', ([], []))
        flags = (1 if L['ow'] else 0) | (2 if L['band'] else 0) | (4 if z else 0) | (8 if L['drive'] else 0) | (16 if L.get('crossing') else 0) | (32 if tf or tb_ else 0)
        lb += struct.pack('<BBBbHII', KI[L['kind']], min(255, L['lanes']), flags, max(-8, min(7, L['layer'])), int(min(655, L['w']) * 100), nodes[L['a']], nodes[L['b']])
        lb += pack_ring(out, pts)
        if z: lb += np.round(np.array(z) * 10).astype('<i2').tobytes()
        if tf or tb_: lb += bytes([len(tf)] + tf + [len(tb_)] + tb_)  # turn lanes, left to right in each direction of travel
    out.section('LINE', bytes(lb))
    # buildings: compact header, optional fields behind flags
    bb = bytearray(); nb = 0
    for b in builds:
        r = b['ring']
        c = (sum(p[0] for p in r) / len(r), sum(p[1] for p in r) / len(r))
        if not (tx <= c[0] < tx + size and ty <= c[1] < ty + size): continue
        if not detail:
            if b['a'] < 60 and b['gh'] < 12 or b['mn'] > 0 and b['a'] < 150: continue  # overview: no sheds, no penthouses
            p = Polygon(r)
            if len(r) > 4:
                _, _, _, _, _, ar = obb(r)
                if p.area / ar > 0.86: r = list(p.minimum_rotated_rectangle.exterior.coords)[:-1]
                else:
                    sp = p.simplify(1.5)
                    if sp.geom_type == 'Polygon' and len(sp.exterior.coords) > 3: r = list(sp.exterior.coords)[:-1]
        b['mn'] = max(0.0, b['mn'] or 0.0); b['h'] = max(0.5, b['h'])
        units = b.get('units') if detail else None
        fl = (1 if b['mn'] else 0) | (2 if b['roof'] else 0) | (4 if b['wall'] else 0) | (8 if b['roofc'] else 0) | (16 if abs(b['gh'] - b['h']) > 0.5 else 0) \
            | 32 | (64 if units is not None else 0) | (128 if any(b.get('fx', ())) else 0)
        bb += struct.pack('<BBhH', fl, b['style'], int(round(b['base'] * 10)), int(min(6000, b['h'] * 10)))
        if fl & 1: bb += struct.pack('<H', int(max(0, min(6000, b['mn'] * 10))))
        if fl & 2: bb += struct.pack('<BH', b['roof'], int(min(6000, b['ridge'] * 10)))
        if fl & 4: bb += struct.pack('<I', b['wall'] & 0xffffff)
        if fl & 8: bb += struct.pack('<I', b['roofc'] & 0xffffff)
        if fl & 16: bb += struct.pack('<HI', int(min(6000, b['gh'] * 10)), b['gid'])
        # facade: storey height (1/20 m) and bits (1 storefront ground floor, 2 homes: balconies allowed)
        bb += struct.pack('<BB', int(min(255, round(b['fh'] * 20))), (1 if b['shop'] else 0) | (2 if b['res'] else 0))
        if fl & 128:  # facade looks (tools/facades.py): balcony kind | pattern << 4, balcony facing octants (0 = all round)
            kind, pat, facing = b['fx']
            bb += struct.pack('<BB', kind | pat << 4, facing)
        if fl & 64:  # rooftop plant the LiDAR saw (none = a clean roof): centre, size along/across, height, angle
            bb += struct.pack('<B', len(units))
            for x, y, su, sv, uh, ang in units:
                bb += struct.pack('<hhBBBB', *out.q(x, y), int(min(255, su * 10)), int(min(255, sv * 10)), int(min(255, uh * 20)), int((ang % math.pi) / math.pi * 256) & 255)
        bb += pack_ring(out, r)
        nb += 1
    out.section('BLDG', struct.pack('<I', nb) + bytes(bb))
    tr_ = bytearray(); nt = 0
    for x, y, shape, col, h in trees:
        if not (tx <= x < tx + size and ty <= y < ty + size): continue
        if not detail and (h < 9 or (hash((round(x), round(y))) & 3)): continue
        tr_ += struct.pack('<hhBBB', *out.q(x, y), shape, col, int(min(63, h) * 4)); nt += 1
    out.section('TREE', struct.pack('<I', nt) + bytes(tr_))
    if detail:  # boats at their berths: centre, heading (1/256 turn), length (1/10 m), kind (1 sail) | colour << 1
        bt = [b for b in boats if tx <= b[0] < tx + size and ty <= b[1] < ty + size]
        out.section('BOAT', struct.pack('<I', len(bt)) + b''.join(struct.pack('<hhBBB', *out.q(x, y), int(a / (2 * math.pi) * 256) & 255, int(min(255, ln * 10)), k) for x, y, a, ln, k in bt))
    pb = bytearray(); npo = 0
    for k, nid, x, y in pois:
        if tx <= x < tx + size and ty <= y < ty + size: pb += struct.pack('<BQhh', k, nid, *out.q(x, y)); npo += 1
    out.section('POIS', struct.pack('<I', npo) + bytes(pb))
    return runs > 0 or nb > 0 or len(sel) > 0


def safe_build(ij):
    import traceback
    try: return build(ij)
    except Exception: return ij, 'FAILED', traceback.format_exc().splitlines()[-1]


def write_index():
    ls = lambda lv: sorted(f[:-4] for f in os.listdir(f'{OUT}/{lv}') if f.endswith('.bin')) if os.path.isdir(f'{OUT}/{lv}') else []
    json.dump({'version': VERSION, 'origin': [LAT_O, LON_O], 'k': [KX, KY], 'extent': EX, 'sizes': [L0, L1], 'ground': GROUND, 'kinds': KINDS,
               'l0': ls(0), 'l1': ls(1), 'built': int(time.time()), **({'city': city.PAGE} if city.PAGE else {})}, open(f'{OUT}/index.json', 'w'))

def selftest():
    """synthetic LiDAR: a gable whose ridge runs across the footprint, a hip, a shed, and a tower on a podium"""
    ring = [(0, 0), (16, 0), (16, 10), (0, 10)]  # long axis along x; cells of the 2 m grid, NW corner at (0, 10)
    xs, ys = np.meshgrid((np.arange(8) + 0.5) * HR, 10 - (np.arange(5) + 0.5) * HR)
    ok = np.ones(xs.shape, bool)
    f = fit_roof(ring, 8 - 0.5 * np.abs(xs - 8), ok, (0, 10))  # ridge along y (the short side)
    assert f and f[0] & 15 == 1 and f[0] & CROSS and abs(f[2] - 8) < 0.3, f
    f = fit_roof(ring, 8 - 0.5 * np.maximum(np.abs(ys - 5), np.abs(xs - 8) - 3), ok, (0, 10))
    assert f and f[0] & 15 == 2, f
    f = fit_roof(ring, 4 + 0.4 * ys, ok, (0, 10))  # high side north: +v when the long axis points east
    assert f and f[0] & 15 == 5 and f[0] & FLIP, f
    z = np.full((20, 20), 12.0); z[5:12, 6:13] = 60.0; z[8:10, 8:10] = 64.0
    assert len(height_levels(z.ravel())) == 2, height_levels(z.ravel())
    # a deck cresting 30 m up (6% at the ends), stay cables 30-60 m above it near a pylon, an expansion-joint gap
    sd = np.arange(0, 2000, 4.0); deck = 10 + 30 * (1 - ((sd - 1000) / 1000) ** 2)
    seen = deck.copy(); seen[(sd > 900) & (sd < 1050)] += 30 + 30 * np.sin((sd[(sd > 900) & (sd < 1050)] - 900) / 50); seen[300] -= 8
    assert np.abs(robust_deck(sd, seen) - deck).max() < 1.5, np.abs(robust_deck(sd, seen) - deck).max()
    # elevation: a pier (4 m) in the sea keeps its height, the sea around it (wave noise) is flat at 0
    sea = np.ones((40, 40), bool); dt = np.random.default_rng(1).normal(0, 0.3, (40, 40)); dt[10:20, 5:35] = 4.0
    f = flat_water(dt, sea, sea)
    assert np.allclose(f[12:18, 8:32], 4.0) and np.all(f[25:38, 2:38] == 0), (f[12:18, 8:32].min(), np.abs(f[25:38]).max())
    # an approach: the viaduct goes on flat at 12 m for 40 m past the bridge tag, then a 5% ramp down to the ground
    # (a tree over it doesn't lift it); under the trees it comes down at its own grade; where the LiDAR sees only the
    # ground past the deck end there is no ramp (stairs, a lift)
    sa = np.arange(0, 400, 4.0); gr = np.full(len(sa), 2.0); lw = np.maximum(2.0, 12.0 - 0.05 * np.maximum(0, sa - 40)); lw[20] += 9
    z, down = ramp(sa, lw, gr.copy(), 12.0, 0.08, 300)
    assert down is not None and np.allclose(z[:11], 12, atol=0.01) and abs(sa[down] - 240) < 12 and np.nanmax(np.diff(z)) <= 1e-9, (down, z[:20])
    z, down = ramp(sa, np.full(len(sa), 30.0) + 5 * (np.arange(len(sa)) % 2), gr.copy(), 12.0, 0.08, 150)
    assert abs(sa[down] - 10 / 0.08) < 8, down
    z, down = ramp(sa, np.full(len(sa), 2.0), gr.copy(), 12.0, 0.08, 150)
    assert down == -1, down
    z, down = ramp(sa, np.full(len(sa), 2.0), gr.copy(), 4.0, 0.08, 150)  # a deck end a little high: down to the ground
    assert down == 1 and np.isfinite(z[:2]).all(), (down, z[:3])
    # crowns: two 12 m trees in a 20 x 40 m patch of canopy share it, up to their reach; a lone tree in a small patch keeps a small crown
    can = np.zeros((40, 40), bool); can[10:20, 5:25] = True; can[30:33, 30:33] = True
    w = crown_widths(np.full((40, 40), 12.0), can, [(15, 10), (15, 20), (31, 31)], [12.0, 12.0, 6.0])
    assert 13 < w[0] < 17 and 13 < w[1] < 17 and w[2] < 7.5 and crown_q(w[0]) > crown_q(w[2]), w
    print('selftest ok')


if __name__ == '__main__':
    if sys.argv[1:] == ['selftest']: selftest(); sys.exit()
    procs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    want = tile_args(sys.argv[2:]) or all_tiles()
    t0 = time.time()
    deck_profiles()  # once, before the workers fork
    if procs == 1:
        for ij in want: print(safe_build(ij), flush=True)
    else:
        with Pool(procs) as p:
            for r in p.imap_unordered(safe_build, want): print(r, round(time.time() - t0), 's', flush=True)
    write_index()

