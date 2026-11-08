# Vancouver and the Lower Mainland, from the Strait of Georgia to Port Coquitlam and White Rock to the North Shore.
import csv, json, os

TITLE = 'Vancouver in code'
# the frame (common.py): world metres east and north of downtown, equirectangular at KREF_LAT, scaled by SC
ORIGIN = (49.28606407, -123.10901045)
KREF_LAT, SC = 49.2845, 0.99744659
BBOX = (48.99, -123.33, 49.48, -122.40)  # S, W, N, E: the Lower Mainland and the North Shore mountains

OSM = ('https://download.geofabrik.de/north-america/canada/british-columbia-latest.osm.pbf', 'bc.osm.pbf')
ELEVATION = 'hrdem'  # tools/elevation.py: NRCan HRDEM 2 m surface and terrain (tools/hrdem.py)
DEM_FALLBACK = ('/vsicurl/https://canelevation-dem.s3.ca-central-1.amazonaws.com/mrdem-30/mrdem-30-dtm.tif', 1.0)  # MRDEM 30 m, metres
OD = 'https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets'
DOWNLOADS = [  # (file in DATA, url), fetched when missing (FRESH=1: again)
    ('trees.csv', f'{OD}/public-trees/exports/csv?delimiter=%3B'),
    ('bf2009.geojson', f'{OD}/building-footprints-2009/exports/geojson'),
]


def trees(data):
    """City of Vancouver public trees: (x, y, genus, common name, height m)"""
    from common import xy
    out = []
    with open(f'{data}/trees.csv', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh, delimiter=';'):
            try: lat, lon = map(float, row['geo_point_2d'].split(','))
            except ValueError: continue
            x, y = xy(lat, lon)
            out.append((x, y, row['genus_name'], row['common_name'], float(row['height_m'] or 0) or 8.0))
    return out


def sections(data):
    """the city's 2009 LiDAR building sections: (ring, height above ground, roof type)"""
    from common import xy
    out = []
    for f in json.load(open(f'{data}/bf2009.geojson'))['features']:
        p = f['properties']; geo = f['geometry']
        polys = geo['coordinates'] if geo['type'] == 'MultiPolygon' else [geo['coordinates']] if geo['type'] == 'Polygon' else []
        for poly in polys:
            r = [xy(b, a) for a, b in poly[0]]
            if len(r) >= 4 and (p.get('hgt_agl') or 0) >= 2: out.append((r, p['hgt_agl'], (p.get('rooftype') or '').lower()))
    return out


# rough outlines in world metres of places whose building stock has a look of its own; first match wins
ZONES = [
    ('historic', [(-230, -60), (350, -60), (1300, -250), (1300, -560), (720, -560), (700, -1000), (250, -800), (-120, -480), (-300, -260)]),  # Gastown, Victory Sq, Chinatown, Railtown: 1890s-1920s brick
    ('historic', [(-629, -872), (-491, -988), (-871, -1448), (-1009, -1332)]),  # Yaletown's warehouse district
    ('granville', [(-2200, -1880), (-1450, -1880), (-1450, -1430), (-2200, -1430)]),  # Granville Island: tin sheds
    ('westend', [(-880, -182), (-1703, -1006), (-1803, -1115), (-2306, -408), (-2700, 200), (-2100, 950), (-1784, 825)]),  # 1960s-70s concrete towers
    ('core', [(-2900, 1300), (400, 300), (400, -500), (-200, -1400), (-1200, -1750), (-2700, -300)]),  # downtown, Coal Harbour, Yaletown: glass condos
]
GLASS_P = {'core': 0.9, 'westend': 0.2, None: 0.55}  # share of residential towers (over 24 m, year unknown) in glass, else concrete
BRICK_ZONES = {'historic': (32, 0.8)}  # zone: (under this height, this share of the buildings is brick)
SHEDS_ZONE = 'granville'  # low commercial buildings here are painted tin sheds
HOUSES_KIND = 'vancouver'  # character houses and Vancouver Specials, told apart by their roofs (tiles.py house_kind)

# what the page shows (tiles/index.json "city"); the Vancouver page's own defaults, so nothing is written for it
PAGE = None
