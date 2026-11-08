#!/usr/bin/env python3
"""A city's GTFS (+ OSM guideways) -> compact files for public/plugins/transit/data/ (Vancouver: TransLink, SkyTrain;
CITY=<city>: its TRANSIT feeds, tools/cities/<city>.py, into public/plugins/transit/data/<city>/).

    [CITY=<city>] uv run --with shapely,numpy python3 tools/transit_data.py [gtfs.zip] [lm.osm.pbf]

Without a zip it downloads the city's feeds (TRANSIT['feeds']; several are merged, their ids prefixed). Needs the
`osmium` CLI. A feed without shape_dist_traveled has its stops placed along their shapes.

Data: "Route and arrival data used in this product or service is provided by permission of TransLink. TransLink
assumes no responsibility for the accuracy or currency of the Data used in this product or service." (TransLink
GTFS terms). Guideway bridge/tunnel tags and stations: (c) OpenStreetMap contributors, ODbL.

Output (all little varint streams, see `Out` and public/plugins/transit/index.js `decode`):
  manifest.json   routes, day patterns (wk = Mon-Thu, fr, sa, su) + dates that differ, stations, chunk list
  rail.bin        SkyTrain, West Coast Express, SeaBus: shapes (with height codes), stop patterns, timings, trips
  bus_I_J.bin     buses, grouped by the 8 km cell their route's middle lies in
  lines/          for driving a bus (the drive plugin): index.json lists each bus route's directions; R.json (R: the
                  route's index in the manifest) has per direction its usual pattern with every stop, not just the
                  timepoints: {head, shape: [x, n, dx, dn, ...] (0.5 m steps, as below), stops:
                  [[name, code, metres along]], prof: [[half minutes from each stop to the next, the first 0]], trips: [start - previous
                  start, profile, day mask, ...]}
Positions are metres east/north of ORIGIN (equirectangular, the core's own projection), in 0.5 m steps.
Each shape point has a height code = zigzag(lift in 0.25 m) * 3 + mode; mode 0 = lift over the terrain,
1 = bridge (straight between the terrain at the bridge's two ends, plus lift), 2 = tunnel (hidden).
"""
import collections, csv, datetime, io, json, math, os, subprocess, sys, tempfile, urllib.request, zipfile
import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.strtree import STRtree
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LAT_O, LON_O, KX, KY, xy, city, CITY, DATA, extent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = city.TRANSIT  # the city's feeds, kinds of line, stations and credit (tools/cities/<city>.py)
OUT = os.path.join(ROOT, 'public/plugins/transit/data' + ('' if CITY == 'vancouver' else f'/{CITY}'))
PBF = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, DATA, 'lm.osm.pbf')
UNIT = CFG['dist_unit']  # metres per shape_dist_traveled unit, None: the feed has none
ORIGIN, K, P = (LAT_O, LON_O), (KX, KY), xy  # the map's frame (common.py): metres per degree lon, lat
Q = 0.5  # position step, m
CELL = 8000  # bus chunk size, m

IDS = ('route_id', 'trip_id', 'shape_id', 'service_id', 'stop_id', 'parent_station')  # prefixed per feed when merging


def zz(v): return (v << 1) ^ (v >> 63)


class Out:
    def __init__(self): self.b = bytearray()
    def u(self, v):
        v = int(v); assert v >= 0, v
        while True:
            if v < 0x80: self.b.append(v); return
            self.b.append((v & 0x7f) | 0x80); v >>= 7
    def s(self, v): self.u(zz(int(v)))


def rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig'))


def rows_all(zs, name, need=True):
    """a table from every feed, ids prefixed with the feed's key ('' for one feed: as they are)"""
    for pre, z in zs:
        if name not in z.namelist():
            if need: raise KeyError(f'{name} missing')
            continue
        for r in rows(z, name):
            if pre:
                for k in IDS:
                    if r.get(k): r[k] = f'{pre}:{r[k]}'
            yield r


def project_stops(line, pts):
    """metres along `line` of each point, never going back (a loop's second pass is looked for ahead)"""
    from shapely.ops import substring
    out, prev = [], 0.0
    for p in pts:
        g = shapely.Point(p); d = line.project(g)
        if d < prev - 1:
            sub = substring(line, prev, line.length)
            d = prev + sub.project(g) if sub.length > 0 else prev
        prev = max(d, prev); out.append(prev)
    return np.array(out)


def secs(t):
    h, m, s = t.strip().split(':'); return int(h) * 3600 + int(m) * 60 + int(s)


def simplify_keep(pts, tol, keep):
    """Douglas-Peucker on [(x, n, orig)], never dropping indices in keep."""
    n = len(pts); out = np.zeros(n, bool); out[0] = out[-1] = True
    for k in keep: out[k] = True
    a = np.array([(p[0], p[1]) for p in pts])
    stack = []
    idx = np.flatnonzero(out)
    for i, j in zip(idx, idx[1:]): stack.append((i, j))
    while stack:
        i, j = stack.pop()
        if j - i < 2: continue
        d = a[j] - a[i]; L = math.hypot(*d) or 1e-9
        seg = a[i + 1:j] - a[i]
        dist = np.abs(seg[:, 0] * d[1] - seg[:, 1] * d[0]) / L
        k = int(np.argmax(dist))
        if dist[k] > tol: m = i + 1 + k; out[m] = True; stack += [(i, m), (m, j)]
    return [p for p, o in zip(pts, out) if o]


def densify(pts, step):
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        L = math.dist(a[:2], b[:2]); n = max(1, math.ceil(L / step))
        for k in range(1, n + 1):
            t = k / n; out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t))
    return out


def runs_along(line, ways, tree, maxd):
    """Stretches of `line` that run alongside one of `ways` (not just crossing it): [(s0, s1, way index)]."""
    res = []
    for wi in tree.query(line, predicate='dwithin', distance=maxd):
        w = ways[wi][0]
        n = max(2, int(w.length / 5) + 1)
        pts = shapely.points(np.array([w.interpolate(t * w.length).coords[0] for t in np.linspace(0, 1, n)]))
        near = shapely.distance(line, pts) <= maxd
        s = shapely.line_locate_point(line, pts)
        step = w.length / (n - 1)
        i = 0
        while i < n:
            if not near[i]: i += 1; continue
            j = i
            while j + 1 < n and near[j + 1]: j += 1
            along, span = (j - i) * step, s[j] - s[i]
            if along >= 20 and abs(span) >= 0.6 * along: res.append((min(s[i], s[j]), max(s[i], s[j]), wi))
            i = j + 1
    return res


def merge(iv, gap=15):
    iv = sorted(iv); out = []
    for a, b, *r in iv:
        if out and a <= out[-1][1] + gap: out[-1][1] = max(out[-1][1], b)
        else: out.append([a, b, *r])
    return out


def osm_lines(filters, tag_test):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(['osmium', 'tags-filter', '-O', '-o', f'{d}/f.pbf', PBF, *filters], check=True)
        subprocess.run(['osmium', 'export', '-O', '-f', 'geojsonseq', '-o', f'{d}/f.geojsonseq', f'{d}/f.pbf'], check=True)
        feats = [json.loads(l.strip('\x1e')) for l in open(f'{d}/f.geojsonseq')]
    out = []
    for f in feats:
        g, t = f['geometry'], f['properties']
        if g['type'] == 'LineString' and (c := tag_test(t)) is not None:
            out.append((LineString([P(la, lo) for lo, la in g['coordinates']]), c, t))
        elif g['type'] == 'Point' and tag_test(t) is not None:
            out.append((P(g['coordinates'][1], g['coordinates'][0]), tag_test(t), t))
    return out


def main():
    if len(sys.argv) > 1: zs = [(CFG['feeds'][0][0], zipfile.ZipFile(sys.argv[1]))]
    else:
        zs = []
        for pre, url in CFG['feeds']:
            src = os.path.join(tempfile.mkdtemp(), 'gtfs.zip'); print('downloading', url); urllib.request.urlretrieve(url, src)
            zs.append((pre, zipfile.ZipFile(src)))
    infos = [next(rows(z, 'feed_info.txt')) for _, z in zs]
    feed = {'feed_version': ' + '.join(f['feed_version'] for f in infos), 'feed_start_date': min(f['feed_start_date'] for f in infos),
            'feed_end_date': max(f['feed_end_date'] for f in infos)}

    # ---------- day patterns ----------
    trips = [t for t in rows_all(zs, 'trips.txt')]
    routes = {r['route_id']: r for r in rows_all(zs, 'routes.txt') if r['route_type'] in ('1', '2', '3', '4')}
    trips = [t for t in trips if t['route_id'] in routes]
    if CFG['clip']:  # only trips whose route reaches the map (a feed for a whole region)
        X0, Y0, X1, Y1 = extent(); pad = 1000
        box_ = {}
        for r in rows_all(zs, 'shapes.txt'):
            x, n = P(float(r['shape_pt_lat']), float(r['shape_pt_lon'])); b = box_.get(r['shape_id'])
            box_[r['shape_id']] = (min(b[0], x), min(b[1], n), max(b[2], x), max(b[3], n)) if b else (x, n, x, n)
        on = {k for k, (a, b, c, d) in box_.items() if c > X0 - pad and a < X1 + pad and d > Y0 - pad and b < Y1 + pad}
        n0 = len(trips); trips = [t for t in trips if t.get('shape_id') in on]
        print('clipped to the map:', len(trips), 'of', n0, 'trips')
    tcount = collections.Counter(t['service_id'] for t in trips)
    cal = {r['service_id']: r for r in rows_all(zs, 'calendar.txt')}
    exc = collections.defaultdict(dict)
    for r in rows_all(zs, 'calendar_dates.txt', need=False): exc[r['date']][r['service_id']] = r['exception_type']
    D = lambda s: datetime.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    default = lambda k: [0, 0, 0, 0, 1, 2, 3][D(k).weekday()]
    diff = lambda a, b: sum(tcount[x] for x in a ^ b)
    reps, special = [frozenset()] * 4, {}
    for (pre, _), info in zip(zs, infos):  # each feed by itself (feeds can cover different dates), then merged
        mine = (lambda sid: sid.startswith(pre + ':')) if pre else (lambda sid: True)
        active = {}
        d, d1 = D(info['feed_start_date']), D(info['feed_end_date'])
        while d <= d1:
            k = d.strftime('%Y%m%d')
            s = {sid for sid, c in cal.items() if mine(sid) and c[DAYS[d.weekday()]] == '1' and c['start_date'] <= k <= c['end_date']}
            for sid, e in exc[k].items():
                if mine(sid): (s.add if e == '1' else s.discard)(sid)
            active[k] = frozenset(x for x in s if tcount[x]); d += datetime.timedelta(1)
        fr = []
        for p in range(4):
            c = collections.Counter(s for k, s in active.items() if default(k) == p)
            fr.append((c.most_common(1) or [(frozenset(), 0)])[0][0])
        for k, s in active.items():
            p = min(range(4), key=lambda p: (diff(s, fr[p]), p != default(k)))
            if p != default(k): special.setdefault(k, p)
        reps = [r | f for r, f in zip(reps, fr)]
    print('patterns', [sorted(r) for r in reps], 'special days', special)
    trips = [dict(t, mask=sum(1 << p for p in range(4) if t['service_id'] in reps[p])) for t in trips]
    trips = [t for t in trips if t['mask']]
    tids = {t['trip_id'] for t in trips}

    # ---------- routes ----------
    used = sorted({t['route_id'] for t in trips}, key=lambda r: (routes[r]['route_type'] == '3', routes[r]['route_short_name'].lstrip('0').rjust(4)))
    RI = {r: i for i, r in enumerate(used)}
    rmeta = []
    for r in used:
        x = routes[r]; short = x['route_short_name'].lstrip('0')
        rmeta.append({'name': short or x['route_long_name'], 'long': x['route_long_name'], 'kind': CFG['kind'](x),
                      **CFG['route'](x, short), 'color': '#' + CFG['color'](x)})
    rail_routes = {r for r in used if routes[r]['route_type'] != '3'}

    # ---------- stop times ----------
    stops = {s['stop_id']: s for s in rows_all(zs, 'stops.txt')}
    st = collections.defaultdict(list)
    for r in rows_all(zs, 'stop_times.txt'):
        if r['trip_id'] in tids:
            st[r['trip_id']].append((int(r['stop_sequence']), secs(r['arrival_time']), secs(r['departure_time']),
                                     float(r.get('shape_dist_traveled') or 0), r.get('timepoint', '1') == '1', r['stop_id']))
    shp = collections.defaultdict(list)
    need = {t['shape_id'] for t in trips}
    for r in rows_all(zs, 'shapes.txt'):
        if r['shape_id'] in need:
            shp[r['shape_id']].append((int(r['shape_pt_sequence']), float(r['shape_pt_lat']), float(r['shape_pt_lon']), float(r.get('shape_dist_traveled') or 0)))

    # ---------- OSM: SkyTrain structure, road bridges/tunnels, stations ----------
    def rail_cls(t):
        if t.get('railway') not in ('subway', 'light_rail') or t.get('service'): return None
        lay = int(t.get('layer', '0').split(';')[0] or 0)
        if t.get('tunnel') in ('yes', 'covered', 'building_passage') or lay < 0: return 'tunnel'
        if t.get('bridge:name'): return 'bridge'
        if t.get('bridge') in ('yes', 'viaduct') or lay > 0: return 'elevated'
        return None
    rail_ways = [w for w in osm_lines(['w/railway=subway,light_rail'], rail_cls) if isinstance(w[0], LineString)]
    road_ok = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'service', 'busway',
               'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}
    def road_cls(t):
        if t.get('highway') not in road_ok: return None
        if t.get('tunnel') in ('yes', 'building_passage'): return 'tunnel'
        if t.get('bridge') in ('yes', 'viaduct'): return 'bridge'
        return None
    road_ways = osm_lines(['w/bridge=yes,viaduct', 'w/tunnel=yes,building_passage'], road_cls)
    rail_tree, road_tree = STRtree([w[0] for w in rail_ways]), STRtree([w[0] for w in road_ways])
    stn = osm_lines(['n/railway=station'], CFG['station'])
    print('osm', len(rail_ways), 'rail ways', len(road_ways), 'road bridges/tunnels', len(stn), 'stations')

    # ---------- shapes ----------
    shape_route = {}
    for t in trips: shape_route.setdefault(t['shape_id'], t['route_id'])
    shapes = {}  # shape_id -> dict(pts=[(x, n)], code=[...], orig=[km], cum=[m])
    for sid, raw in shp.items():
        raw.sort()
        if UNIT: pts = [(*P(la, lo), dk * UNIT) for _, la, lo, dk in raw]
        else:  # no shape_dist_traveled: metres along the shape itself (stops are projected onto it, `along`)
            pts, acc = [], 0.0
            for k, (_, la, lo, _) in enumerate(raw):
                p = P(la, lo); acc += math.dist(p, pts[-1][:2]) if pts else 0.0; pts.append((*p, acc))
        rail = shape_route[sid] in rail_routes
        sky = routes[shape_route[sid]]['route_type'] == '1'
        if rail:
            pts = densify(simplify_keep(pts, 0.6, []), 20)
        else:
            pts = simplify_keep(pts, 2.0, [])
        dedup = [pts[0]]
        for p in pts[1:]:
            if math.dist(p[:2], dedup[-1][:2]) >= 1: dedup.append(p)
        pts = dedup if len(dedup) > 1 else pts[:1] + pts[-1:]
        line = LineString([p[:2] for p in pts])
        cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(np.array([p[:2] for p in pts]), axis=0).T))])
        if sky:
            iv = runs_along(line, rail_ways, rail_tree, 12)
            cls = np.array(['ground'] * len(pts), dtype=object)
            for prio in ('elevated', 'bridge', 'tunnel'):
                for a, b, wi in iv:
                    if rail_ways[wi][1] == prio: cls[(cum >= a - 1) & (cum <= b + 1)] = prio
            # OSM often leaves station boxes and short bits untagged: short ground gaps inside a viaduct or a tunnel
            # take the structure around them
            runs, i = [], 0
            while i < len(cls):
                j = i
                while j + 1 < len(cls) and cls[j + 1] == cls[i]: j += 1
                runs.append([i, j]); i = j + 1
            for k in range(1, len(runs) - 1):
                a, b = runs[k]; pa, nb = cls[runs[k - 1][0]], cls[runs[k + 1][0]]
                if cls[a] != 'ground' or cum[b] - cum[a] > 300: continue
                if pa == nb == 'tunnel' or (pa in ('elevated', 'bridge') and nb in ('elevated', 'bridge')):
                    cls[a:b + 1] = 'tunnel' if pa == 'tunnel' else 'elevated'
            target = np.array([{'ground': 0, 'elevated': 8, 'bridge': 8, 'tunnel': -9}[c] for c in cls], float)
            lift = target.copy()
            for _ in range(2):  # ramps: 5 % up to the guideway, 7 % down into tunnels
                for rng in (range(1, len(pts)), range(len(pts) - 2, -1, -1)):
                    for i in rng:
                        j = i - 1 if rng.step == 1 else i + 1
                        ds = abs(cum[i] - cum[j]); lo, hi = lift[j] - 0.07 * ds, lift[j] + 0.05 * ds
                        if cls[i] != 'bridge': lift[i] = min(max(lift[i], lo), hi)
            mode = np.where(lift < -2.5, 2, 0)
            # named bridges (SkyBridge, North Arm): straight between the ends, arched like the core's road bridges
            for a, b in merge([(a, b) for a, b, wi in iv if rail_ways[wi][1] == 'bridge']):
                m = (cum >= a) & (cum <= b)
                mode[m] = 1; lift[m] = 8 + np.minimum(26, np.minimum(cum[m] - a, b - cum[m]) * 0.06)
            code = [zz(int(round(l * 4))) * 3 + int(md) for l, md in zip(lift, mode)]
        elif not rail:
            iv = runs_along(line, road_ways, road_tree, 10)
            bridges = merge([(a, b) for a, b, wi in iv if road_ways[wi][1] == 'bridge'])
            tunnels = merge([(a, b) for a, b, wi in iv if road_ways[wi][1] == 'tunnel'])
            if bridges or tunnels:  # put points at the ends and every 40 m along bridges, so the arch shows
                extra = []
                for a, b in bridges: extra += list(np.arange(a, b, 40)) + [b]
                for a, b in tunnels: extra += [a, b] + ([a + 25, b - 25] if b - a > 60 else [])
                s_all = np.unique(np.concatenate([cum, np.clip(extra, 0, cum[-1])]))
                xs, ns = np.interp(s_all, cum, [p[0] for p in pts]), np.interp(s_all, cum, [p[1] for p in pts])
                og = np.interp(s_all, cum, [p[2] for p in pts])
                pts = list(zip(xs, ns, og)); cum = s_all
            code = []
            for s in cum:
                c = 0
                for a, b in bridges:
                    if a - 0.01 <= s <= b + 0.01: c = zz(int(round(min(26, min(s - a, b - s) * 0.06) * 4))) * 3 + 1
                for a, b in tunnels:  # down 8 m inside long tunnels (hidden below -2.5 m), ramping from the portals
                    if b - a > 60 and a + 24 < s < b - 24: c = zz(-32) * 3 + 2
                code.append(c)
        else:
            code = [0] * len(pts)
        # quantise, then lengths from the quantised points (what the browser will compute)
        qp = [(round(p[0] / Q), round(p[1] / Q)) for p in pts]
        qa = np.array(qp, float) * Q
        qcum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(qa, axis=0).T))])
        shapes[sid] = dict(q=qp, code=code, orig=np.array([p[2] for p in pts]), cum=qcum,
                           bbox=(qa[:, 0].min(), qa[:, 1].min(), qa[:, 0].max(), qa[:, 1].max()))

    # ---------- where each stop is along its shape ----------
    proj = {}
    def along(sts, sid):
        """metres along shape sid (its quantised length) of each of the stop times sts"""
        sh = shapes[sid]
        if UNIT: return np.interp([x[3] * UNIT for x in sts], sh['orig'], sh['cum'])
        key = (sid, tuple(x[5] for x in sts))
        if key not in proj:
            if 'line' not in sh: sh['line'] = LineString(np.array(sh['q'], float) * Q)
            proj[key] = project_stops(sh['line'], [P(float(stops[x[5]]['stop_lat']), float(stops[x[5]]['stop_lon'])) for x in sts])
        return proj[key]

    # ---------- stop patterns, timing profiles, trips ----------
    groups = collections.defaultdict(list)  # chunk -> trips
    route_mid = {}
    for t in trips:
        if t['route_id'] in rail_routes or t['route_id'] in route_mid: continue
        b = shapes[t['shape_id']]['bbox']; route_mid[t['route_id']] = f'{math.floor((b[0] + b[2]) / 2 / CELL)}_{math.floor((b[1] + b[3]) / 2 / CELL)}'.replace('-', 'm')
    for t in trips:
        groups['rail' if t['route_id'] in rail_routes else 'bus_' + route_mid[t['route_id']]].append(t)

    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.endswith('.bin'): os.remove(os.path.join(OUT, f))
    chunks = []
    for name, ts in sorted(groups.items()):
        rail = name == 'rail'
        sids = sorted({t['shape_id'] for t in ts}); SI = {s: i for i, s in enumerate(sids)}
        pats, profs, out_trips = {}, {}, []
        for t in ts:
            sts = sorted(st[t['trip_id']])
            if len(sts) < 2: continue
            sh = shapes[t['shape_id']]
            if not rail: sts = [x for i, x in enumerate(sts) if x[4] or i == 0 or i == len(sts) - 1]
            dist = np.maximum.accumulate(np.round(along(sts, t['shape_id'])))
            pk = (RI[t['route_id']], SI[t['shape_id']], tuple(int(v) for v in dist))
            pi = pats.setdefault(pk, len(pats))
            t0 = sts[0][1]
            arr, dep = [x[1] - t0 for x in sts], [x[2] - t0 for x in sts]
            if rail and t['route_id'] and routes[t['route_id']]['route_type'] == '1':  # SkyTrain: ~20 s at each station
                for i in range(1, len(sts) - 1):
                    if arr[i] == dep[i]:
                        g = min(arr[i] - dep[i - 1], arr[i + 1] - dep[i]); h = min(10, g // 4)
                        arr[i] -= h; dep[i] += h
            prof = [pi]
            last = 0
            for a, b in zip(arr, dep): prof += [a - last, b - a]; last = b
            ki = profs.setdefault(tuple(prof), len(profs))
            out_trips.append((sts[0][1], ki, t['mask']))
        out_trips.sort()
        o = Out()
        o.u(len(sids))
        for s in sids:
            sh = shapes[s]; o.u(len(sh['q'])); px = pz = 0
            for (x, n), c in zip(sh['q'], sh['code']): o.s(x - px); o.s(n - pz); o.u(c); px, pz = x, n
        o.u(len(pats))
        for (ri, si, dist), _ in sorted(pats.items(), key=lambda kv: kv[1]):
            o.u(ri); o.u(si); o.u(len(dist)); last = 0
            for v in dist: o.u(v - last); last = v
        o.u(len(profs))
        for prof, _ in sorted(profs.items(), key=lambda kv: kv[1]):
            for v in prof: o.u(v)  # pattern index, then (travel, dwell) per stop; stop count comes from the pattern
        o.u(len(out_trips)); last = 0
        for s0, ki, m in out_trips: o.u(s0 - last); o.u(ki); o.u(m); last = s0
        fn = f'{name}.bin'
        open(os.path.join(OUT, fn), 'wb').write(o.b)
        bb = [min(shapes[s]['bbox'][0] for s in sids), min(shapes[s]['bbox'][1] for s in sids),
              max(shapes[s]['bbox'][2] for s in sids), max(shapes[s]['bbox'][3] for s in sids)]
        chunks.append({'file': fn, 'bbox': [round(v) for v in bb], 'trips': len(out_trips), 'bytes': len(o.b)})
        print(fn, len(sids), 'shapes', len(pats), 'patterns', len(profs), 'profiles', len(out_trips), 'trips', len(o.b), 'bytes')
        if rail: rail_sids = sids

    # ---------- guideway: SkyTrain centrelines, overlapping shapes drawn once ----------
    covered, guide = set(), []  # guide: (shape, first point, last point)
    cell = lambda a, b, t: (int((a[0] + (b[0] - a[0]) * t) // 15), int((a[1] + (b[1] - a[1]) * t) // 15))
    sky_sids = sorted((s for s in rail_sids if routes[shape_route[s]]['route_type'] == '1'), key=lambda s: -shapes[s]['cum'][-1])
    for s in sky_sids:
        q = np.array(shapes[s]['q']) * Q; mine = set(); start = None
        for i in range(len(q) - 1):
            seen = any((cx + dx, cy + dy) in covered for t in (0.25, 0.5, 0.75) for cx, cy in [cell(q[i], q[i + 1], t)]
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1))
            for t in np.linspace(0, 1, 5): mine.add(cell(q[i], q[i + 1], t))
            if seen:
                if start is not None: guide.append((s, start, i)); start = None
            elif start is None: start = i
        if start is not None: guide.append((s, start, len(q) - 1))
        covered |= mine
    # each run reaches 3 points (~60 m) into what is already drawn, so branches meet the main line
    guide_out = [[rail_sids.index(s), max(0, i0 - 3), min(len(shapes[s]['q']) - 1, i1 + 3)] for s, i0, i1 in guide]

    # ---------- stations: platform along the nearest rail shape, at its height ----------
    sky_lines = [(s, LineString(np.array(shapes[s]['q']) * Q)) for s in rail_sids]
    stations = []
    for (x, n), k, t in stn:
        best = None
        for s, line in sky_lines:
            rk = routes[shape_route[s]]['route_type']
            if (k == 'sky') != (rk == '1') or rk == '4': continue
            dd = line.distance(shapely.Point(x, n))
            if dd < 120 and (not best or dd < best[0]): best = (dd, s, line)
        if not best: continue
        _, s, line = best
        sa = line.project(shapely.Point(x, n)); sh = shapes[s]
        i = int(np.clip(np.searchsorted(sh['cum'], sa), 1, len(sh['cum']) - 1))
        (ax, an), (bx, bn) = sh['q'][i - 1], sh['q'][i]
        px, pn = line.interpolate(sa).coords[0]
        stations.append({'name': t.get('name', ''), 'x': round(px, 1), 'n': round(pn, 1), 'dir': round(math.atan2(bn - an, bx - ax), 3),
                         'shape': rail_sids.index(s), 's': round(sa, 1), 'kind': k,
                         'len': CFG['platform'](routes[shape_route[s]], k)})
    print(len(stations), 'stations', len(guide_out), 'guideway runs')

    manifest = {
        'credit': CFG['credit'],
        'feed': {k: feed[k] for k in ('feed_version', 'feed_start_date', 'feed_end_date')},
        'origin': ORIGIN, 'k': K, 'q': Q,
        'patterns': ['Mon–Thu', 'Friday', 'Saturday', 'Sunday/holiday'], 'special': special,
        'routes': rmeta, 'chunks': chunks, 'guide': guide_out, 'stations': stations,
        **(CFG['page'] or {}),  # another city's legend, looks and pattern names for the page
    }
    json.dump(manifest, open(os.path.join(OUT, 'manifest.json'), 'w'), separators=(',', ':'), ensure_ascii=False)
    write_lines(trips, rail_routes, RI, rmeta, st, shapes, stops, along)
    print('total', sum(c['bytes'] for c in chunks) + os.path.getsize(os.path.join(OUT, 'manifest.json')), 'bytes')


BOUND = ('Northbound ', 'Southbound ', 'Eastbound ', 'Westbound ')


def write_lines(trips, rail_routes, RI, rmeta, st, shapes, stops, along):
    """Every bus route's usual pattern each way, all its stops and its timetable (see the docstring)."""
    out = os.path.join(OUT, 'lines')
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out): os.remove(os.path.join(out, f))
    by = collections.defaultdict(list)
    for t in trips:
        if t['route_id'] not in rail_routes and len(st[t['trip_id']]) >= 2: by[(RI[t['route_id']], t['direction_id'])].append(t)
    pat = lambda t: (t['shape_id'], tuple(x[5] for x in sorted(st[t['trip_id']])))
    files, index = collections.defaultdict(list), collections.defaultdict(list)
    for (ri, d), ts in sorted(by.items()):
        key = collections.Counter(pat(t) for t in ts).most_common(1)[0][0]
        mine = [t for t in ts if pat(t) == key]
        sh, s0 = shapes[key[0]], sorted(st[mine[0]['trip_id']])
        dist = np.maximum.accumulate(np.round(along(s0, key[0])))
        head = collections.Counter(t['trip_headsign'] for t in mine).most_common(1)[0][0]
        name = rmeta[ri]['name']
        if head.split(' ')[0].lstrip('0') == name: head = head.split(' ', 1)[1] if ' ' in head else head
        names = []
        for x, v in zip(s0, dist):
            n = stops[x[5]]['stop_name'].strip()
            for b in BOUND: n = n[len(b):] if n.startswith(b) else n
            names.append([n, stops[x[5]].get('stop_code', ''), int(v)])
        profs, rows = {}, []
        for t in mine:
            s = sorted(st[t['trip_id']])
            off = [round((x[1] - s[0][1]) / 30) for x in s]  # half minutes, as steps from stop to stop
            rows.append((s[0][1], profs.setdefault(tuple(b - a for a, b in zip([0] + off, off)), len(profs)), t['mask']))
        rows.sort()
        flat, last = [], 0
        for a, p, m in rows: flat += [a - last, p, m]; last = a
        shape, px, pn = [], 0, 0
        for x, n in sh['q']: shape += [x - px, n - pn]; px, pn = x, n
        files[ri].append({'head': head, 'shape': shape, 'stops': names, 'prof': [list(p) for p, _ in sorted(profs.items(), key=lambda kv: kv[1])], 'trips': flat})
        index[ri].append([head, len(names), round(float(sh['cum'][-1]) / 100) / 10])
    for ri, dirs in files.items():
        json.dump({'dirs': dirs}, open(os.path.join(out, f'{ri}.json'), 'w'), separators=(',', ':'), ensure_ascii=False)
    json.dump({str(ri): v for ri, v in sorted(index.items())}, open(os.path.join(out, 'index.json'), 'w'), separators=(',', ':'), ensure_ascii=False)
    print(len(files), 'bus lines', sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out)), 'bytes')


if __name__ == '__main__':
    main()
