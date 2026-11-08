# New York City (CITY=nyc, ?city=nyc): Manhattan, from the Battery to Inwood, with the shores across the Hudson, the East River and the Harlem River.
import csv, math

TITLE = 'Manhattan in code'
ORIGIN = (40.7580, -73.9855)  # Times Square
_p = math.radians(ORIGIN[0])
KX = 111412.84 * math.cos(_p) - 93.5 * math.cos(3 * _p)  # metres per degree of longitude, at the origin
KY = 111132.954 - 559.822 * math.cos(2 * _p) + 1.175 * math.cos(4 * _p)  # and of latitude
BBOX = (40.695, -74.030, 40.882, -73.900)  # S, W, N, E

OSM = ('https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf', 'ny.osm.pbf')
ELEVATION = 'nyc2017'  # tools/elevation.py: the 2017 NYC topobathymetric LiDAR (tools/nyc_lidar.py)
DEM_FALLBACK = ('/vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt', 1.0)  # USGS 3DEP 1/3", metres
_S, _W, _N, _E = BBOX
DOWNLOADS = [  # the 2015 Street Tree Census (NYC Parks, NYC Open Data), living trees inside the map
    ('trees.csv', 'https://data.cityofnewyork.us/resource/uvpi-gqnh.csv?$limit=500000&$select=latitude,longitude,spc_latin,spc_common,tree_dbh'
                  f"&$where=status=%27Alive%27%20AND%20latitude%20between%20{_S}%20and%20{_N}%20AND%20longitude%20between%20{_W}%20and%20{_E}"),
]


def trees(data):
    """street trees: (x, y, genus, common name, height m); the census has trunk diameters, not heights"""
    from common import xy
    out = []
    with open(f'{data}/trees.csv', encoding='utf-8') as fh:
        for row in csv.DictReader(fh):
            try: lat, lon, dbh = float(row['latitude']), float(row['longitude']), float(row['tree_dbh'] or 0)
            except ValueError: continue
            genus = (row['spc_latin'] or '').split(' ')[0]
            x, y = xy(lat, lon)
            out.append((x, y, genus, row['spc_common'], min(26.0, 2.5 + 0.55 * dbh)))  # dbh in inches
    return out


def sections(data):
    return []


def _m(pts):  # lat, lon -> world metres
    return [((lon - ORIGIN[1]) * KX, (lat - ORIGIN[0]) * KY) for lat, lon in pts]


ZONES = [  # first match wins
    ('core', _m([(40.7484, -74.0080), (40.7700, -73.9935), (40.7640, -73.9630), (40.7440, -73.9720), (40.7390, -73.9890)])),  # Midtown and Hudson Yards: towers
    ('core', _m([(40.7005, -74.0190), (40.7160, -74.0160), (40.7140, -74.0030), (40.7070, -73.9990), (40.7005, -74.0110)])),  # the Financial District
    ('historic', _m([(40.7160, -74.0160), (40.7400, -74.0120), (40.7395, -73.9900), (40.7300, -73.9740), (40.7110, -73.9770), (40.7140, -74.0030)])),  # Tribeca, SoHo, the Villages, the Lower East Side
    ('prewar', _m([(40.7700, -73.9990), (40.8200, -73.9640), (40.8730, -73.9330), (40.8820, -73.9100), (40.8000, -73.9250), (40.7600, -73.9560)])),  # uptown: brick and limestone
]
GLASS_P = {'core': 0.85, 'historic': 0.3, 'prewar': 0.15, None: 0.5}
BRICK_ZONES = {'historic': (40, 0.85), 'prewar': (70, 0.7)}
SHEDS_ZONE = None
HOUSES_KIND = 'rowhouse'  # houses are brick and brownstone row houses

PAGE = {
    'name': 'nyc', 'title': TITLE, 'short': 'New York', 'tz': 'America/New_York', 'latlon': list(ORIGIN),
    'start': [-300, -1200, 0.07],  # world x, north (metres), zoom: Midtown and down to the Village
    'plugins': ['plugins/lofi/lofi.js', 'plugins/weather/index.js', 'plugins/transit/index.js', 'plugins/life/index.js'],
    'about': 'Manhattan drawn entirely in code: every building, street and tree comes from open data and is rendered live in your browser with three.js.',
    'credits': [
        'Streets, buildings, parks: © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> (ODbL)',
        'Street trees: 2015 Street Tree Census, NYC Parks (<a href="https://opendata.cityofnewyork.us/">NYC Open Data</a>)',
        'Subway and bus schedules: MTA New York City Transit (GTFS)',
        'Terrain and LiDAR: 2017 New York City topobathymetric LiDAR, <a href="https://coast.noaa.gov/dataviewer/">NOAA Digital Coast</a>; USGS 3DEP (public domain)',
    ],
}

# transit (tools/transit_data.py -> public/plugins/transit/data/nyc/): MTA New York City Transit's subway and buses on
# their timetables, the trips that reach the map. The bus feed has every NYCT route in the five boroughs.
_GTFS = 'https://rrgtfsfeeds.s3.amazonaws.com'
_A_DIVISION = {'1', '2', '3', '4', '5', '6', '6X', '7', '7X', 'GS'}  # the numbered lines: narrower, shorter cars
# articulated buses: Select Bus Service and the busiest crosstown and avenue routes. ponytail: per-route guess, the
# feed has no vehicle types
_ARTIC = {'M14A+', 'M14D+', 'M15', 'M15+', 'M23+', 'M34+', 'M34A+', 'M60+', 'M79+', 'M86+', 'M101', 'M102', 'M103', 'BX12+', 'BX41+', 'B44+', 'B46+', 'Q44+', 'Q70+'}


def _kind(r):
    if r['route_type'] == '1': return 'subA' if r['route_id'].split(':')[-1] in _A_DIVISION else 'subB'
    if r['route_type'] == '2': return 'subB'  # the Staten Island Railway, off the map
    return 'bus'


def _route(r, short):
    if r['route_type'] in ('1', '2'):
        a = r['route_id'].split(':')[-1] in _A_DIVISION
        cars = 4 if short in ('GS', 'S', 'FS', 'H') else 10 if a or short in ('A', 'C', 'E', 'F', 'N', 'Q', 'R', 'W', 'B', 'D', 'M') else 8
        return {'model': 'nycA' if a else 'nycB', 'cars': cars}
    return {'artic': short in _ARTIC, 'model': 'mta18' if short in _ARTIC else 'mta12'}


TRANSIT = dict(
    feeds=[('sub', f'{_GTFS}/gtfs_subway.zip'), ('bus', f'{_GTFS}/gtfs_m.zip')],
    dist_unit=None,  # no shape_dist_traveled: stops are placed along their shapes
    clip=True,  # only the trips that reach the map
    credit='Schedule data: MTA New York City Transit (GTFS). The MTA assumes no responsibility for the accuracy or currency of the data.',
    kind=_kind,
    route=_route,
    color=lambda r: r['route_color'] or ('0039A6' if r['route_type'] == '3' else '808183'),
    station=lambda t: 'sky' if t.get('station') == 'subway' or t.get('subway') == 'yes' and t.get('railway') == 'station' else None,
    platform=lambda r, k: 150,
    page={  # the plugin's legend: [group, label, kind, colour], and the elevated structure's look
        'groups': [['sky', 'Subway: numbered lines', 'subA', '#ee352e'], ['sky', 'Subway: lettered lines', 'subB', '#0062cf'],
                   ['bus', 'Buses', 'bus', '#0039a6']],
        'structure': {'deck': '#5b6066', 'pier': '#4e5359', 'platform': '#8f8b84'},
        'patterns': ['Weekday', 'Friday', 'Saturday', 'Sunday/holiday'],
    },
)
