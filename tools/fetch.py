# The bake's downloads for the city (tools/cities/<CITY>.py): its OpenStreetMap extract from Geofabrik, cut to the map
# (DATA/lm.osm.pbf, every run), and its own datasets (DOWNLOADS: trees and the like). Files on disk are kept;
# FRESH=1 downloads them again. Needs curl and the osmium CLI. Run: CITY=<city> python3 tools/fetch.py
import os, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import city, DATA, BBOX

os.makedirs(DATA, exist_ok=True)
url, name = city.OSM
files = [(name, url)] + list(city.DOWNLOADS)
for f, u in files:
    p = f'{DATA}/{f}'
    if os.environ.get('FRESH') == '1' and os.path.exists(p): os.remove(p)
    if os.path.exists(p): continue
    print('downloading', f, flush=True)
    subprocess.run(['curl', '-sS', '-L', '--fail', '-o', p + '.part', u], check=True)
    os.replace(p + '.part', p)
S, W, N, E = BBOX
cut = lambda src, b, strategy: subprocess.run(['osmium', 'extract', '-b', ','.join(map(str, b)), src, '-o', f'{DATA}/lm.osm.pbf', '--overwrite', '-s', strategy])
if cut(f'{DATA}/{name}', (W, S, E, N), 'smart').returncode:
    # the smart strategy's id sets grow with the spread of OSM ids, not the area: for a state like New York they
    # outgrow a small machine and osmium is killed. Then: the map and a wide margin, the simple way (cheap). A way or
    # multipolygon that reaches past the margin comes out incomplete and osm_split.py leaves it out; osm_split.py keeps
    # only what is on the map, so the margin is just a read-through.
    m = 0.1
    print('osmium ran out of memory (smart strategy): taking the map with a margin instead', flush=True)
    cut(f'{DATA}/{name}', (W - m, S - m, E + m, N + m), 'simple').check_returncode()
print('fetched', ', '.join(f for f, _ in files), flush=True)
