# The map's 2 m LiDAR surface and terrain, DATA/hr/<i>_<j>.npz, from the city's source (tools/cities/<CITY>.py ELEVATION):
#   hrdem    NRCan HRDEM (tools/hrdem.py), Vancouver
#   nyc2017  the 2017 New York City topobathymetric LiDAR (tools/nyc_lidar.py), New York City
# Resumable: tiles already on disk are skipped. A worker that dies (out of memory) stops the run with an error rather
# than hanging it; run again with fewer processes. Run: CITY=<city> python3 tools/elevation.py [processes]
import importlib, os, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
sys.path.insert(0, os.path.dirname(__file__))
from common import city, all_tiles, retry, DATA

src = importlib.import_module({'hrdem': 'hrdem', 'nyc2017': 'nyc_lidar'}[city.ELEVATION])


def fetch(ij):
    try: return retry(lambda: src.fetch_once(ij), 4, 5)
    except Exception as e: return ij, f'failed: {e}'


if __name__ == '__main__':
    os.makedirs(f'{DATA}/hr', exist_ok=True)
    ts = all_tiles()
    print(len(ts), 'tiles from', city.ELEVATION, flush=True)
    try:
        with ProcessPoolExecutor(int(sys.argv[1]) if len(sys.argv) > 1 else 4) as p:
            for n, f in enumerate(as_completed([p.submit(fetch, ij) for ij in ts]), 1):
                ij, st = f.result()
                print(n, ij, st, flush=True)
    except BrokenProcessPool:
        sys.exit('a LiDAR worker died (out of memory?): the tiles done so far are kept; run again with fewer processes')
