# Step 1 of the map bake: one pass over the Lower Mainland OSM extract, sorting what the map draws into
# buckets per 4096 m tile (data/feat/<i>_<j>.pkl). Coordinates are world metres (see common.py).
# Run: uv run --with osmium,shapely,numpy python3 tools/osm_split.py
import os, pickle, sys, time
from collections import defaultdict
import osmium
sys.path.insert(0, os.path.dirname(__file__))
from common import xy, L1, extent, SHOP_AMENITY, DATA

SRC = f'{DATA}/lm.osm.pbf'
KEEP_BUILDING = ('building', 'building:part', 'height', 'min_height', 'building:levels', 'building:min_level', 'roof:shape', 'roof:height',
                 'roof:levels', 'roof:colour', 'building:colour', 'building:material', 'roof:material', 'roof:direction', 'roof:orientation',
                 'name', 'amenity', 'shop', 'tourism', 'layer', 'heritage', 'historic', 'start_date', 'building:architecture', 'office')
# shops and the like as points: a building with one inside (or at its door) gets a storefront ground floor
SHOPFRONT = ('shop', 'office', 'craft')
GROUND_KEYS = ('landuse', 'natural', 'leisure', 'amenity', 'aeroway', 'man_made', 'waterway', 'water', 'place', 'surface', 'golf', 'sport', 'highway', 'area:highway', 'railway', 'tourism', 'parking', 'bridge')
LINE_KEYS = ('highway', 'railway', 'aeroway', 'waterway', 'natural', 'man_made', 'barrier', 'route', 'power')
KEEP_LINE = ('turn:lanes', 'turn:lanes:forward', 'turn:lanes:backward', 'highway', 'railway', 'aeroway', 'waterway', 'natural', 'man_made', 'barrier', 'name', 'ref', 'lanes', 'lanes:forward', 'lanes:backward', 'oneway',
             'junction', 'bridge', 'tunnel', 'layer', 'level', 'width', 'surface', 'sidewalk', 'cycleway', 'cycleway:left', 'cycleway:right', 'cycleway:both', 'service',
             'access', 'foot', 'bicycle', 'maxspeed', 'bridge:structure', 'covered', 'usage', 'gauge', 'electrified', 'crossing', 'footway', 'area', 'power')


def keep(tags, names):
    return {k: tags[k] for k in names if k in tags}


buckets = defaultdict(lambda: defaultdict(list))


EX = extent()
I0, J0, I1, J1 = int(EX[0] // L1), int(EX[1] // L1), int(EX[2] // L1), int(EX[3] // L1)


def put(kind, bbox, item):
    x0, y0, x1, y1 = bbox
    for i in range(max(I0, int(x0 // L1)), min(I1, int(x1 // L1)) + 1):
        for j in range(max(J0, int(y0 // L1)), min(J1, int(y1 // L1)) + 1):
            buckets[(i, j)][kind].append(item)


def ring(r):
    return [xy(n.lat, n.lon) for n in r]


def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


t0 = time.time()
n = defaultdict(int)
fp = osmium.FileProcessor(SRC).with_areas().with_locations()
for o in fp:
    tags = o.tags
    if o.is_area():
        if not any(k in tags for k in ('building', 'building:part') + GROUND_KEYS): continue
        polys = []
        try:
            for outer in o.outer_rings():
                polys.append((ring(outer), [ring(inner) for inner in o.inner_rings(outer)]))
        except Exception: continue
        if not polys: continue
        bb = bbox([p for out, _ in polys for p in out])
        oid = o.orig_id() * (1 if o.from_way() else -1)
        if 'building' in tags or 'building:part' in tags:
            put('buildings', bb, (oid, polys, keep(tags, KEEP_BUILDING))); n['buildings'] += 1
        else:
            t = {k: v for k, v in tags}
            put('areas', bb, (oid, polys, t)); n['areas'] += 1
    elif o.is_way():
        if not any(k in tags for k in LINE_KEYS): continue
        if tags.get('area') == 'yes': continue
        try: pts = [(xy(nd.lat, nd.lon), nd.ref) for nd in o.nodes]
        except osmium.InvalidLocationError: continue
        if len(pts) < 2: continue
        coords = [p for p, _ in pts]
        bb = bbox(coords)
        if tags.get('natural') == 'coastline':  # tiles.py cuts the sea 8 m past a tile's edges: a coastline that ends
            bb = (bb[0] - 16, bb[1] - 16, bb[2] + 16, bb[3] + 16)  # just past one must be in that tile too, or its shore has a gap
        put('lines', bb, (o.id, coords, [r for _, r in pts], keep(tags, KEEP_LINE))); n['lines'] += 1
    elif o.is_node():
        if not tags: continue
        t = tags
        if t.get('natural') == 'tree' or t.get('highway') in ('traffic_signals', 'crossing', 'stop', 'street_lamp') or t.get('railway') in ('station', 'stop', 'level_crossing') \
                or t.get('public_transport') or t.get('amenity') in ('bench', 'fountain', 'bicycle_rental') or t.get('man_made') in ('lighthouse', 'tower', 'mast') or t.get('power') == 'tower':
            p = xy(o.location.lat, o.location.lon)
            put('nodes', (p[0], p[1], p[0], p[1]), (o.id, p, {k: v for k, v in t})); n['nodes'] += 1
        elif any(k in t for k in SHOPFRONT) or t.get('amenity') in SHOP_AMENITY:
            p = xy(o.location.lat, o.location.lon)
            put('nodes', (p[0], p[1], p[0], p[1]), (o.id, p, keep(t, ('shop', 'office', 'craft', 'amenity')))); n['shops'] += 1
print({k: v for k, v in n.items()}, round(time.time() - t0), 's', flush=True)

os.makedirs(f'{DATA}/feat', exist_ok=True)
for (i, j), b in buckets.items():
    with open(f'{DATA}/feat/{i}_{j}.pkl', 'wb') as f: pickle.dump(dict(b), f, protocol=5)
print(len(buckets), 'buckets written', round(time.time() - t0), 's')
