# Downloads NRCan HRDEM (LiDAR, 2 m mosaic, OGL-Canada) surface (DSM) and terrain (DTM) models for the
# whole map, reprojected onto the world grid, one 4096 m tile per file: data/hr/<i>_<j>.npz (dsm, dtm as
# float32 2048x2048, row 0 = north, NaN = no data). Tiles already on disk are skipped, so it can resume.
# Run: uv run --with rasterio,numpy python3 tools/hrdem.py [processes]
import os, sys
from multiprocessing import Pool
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from common import LON_O, LAT_O, KX, KY, L1 as T, HR as RES, all_tiles, retry, DATA

N = int(T / RES)
URL = '/vsicurl/https://canelevation-dem.s3.ca-central-1.amazonaws.com/hrdem-mosaic-2m/2_3-mosaic-2m-{}.tif'
os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('GDAL_HTTP_MULTIRANGE', 'YES')
os.environ.setdefault('GDAL_HTTP_MERGE_CONSECUTIVE_RANGES', 'YES')
os.environ.setdefault('VSI_CACHE', 'TRUE')


def fetch(ij):
    try: return retry(lambda: fetch_once(ij), 4, 5)
    except Exception as e: return ij, f'failed: {e}'


def fetch_once(ij):
    import rasterio
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import Affine
    i, j = ij
    out = f'{DATA}/hr/{i}_{j}.npz'
    if os.path.exists(out): return ij, 'cached'
    # the destination grid in lon/lat, linear in the world frame (equirectangular), north row first
    lon0, lat1 = LON_O + i * T / KX, LAT_O + (j + 1) * T / KY
    tr = Affine((T / KX) / N, 0, lon0, 0, -(T / KY) / N, lat1)
    res = {}
    for kind in ('dsm', 'dtm'):
        dst = np.full((N, N), np.nan, np.float32)
        with rasterio.open(URL.format(kind)) as src:
            reproject(rasterio.band(src, 1), dst, src_nodata=src.nodata, dst_transform=tr, dst_crs='EPSG:4326', dst_nodata=np.nan,
                      resampling=Resampling.bilinear, num_threads=2)
        res[kind] = dst
    if np.isnan(res['dtm']).all(): np.savez_compressed(out, empty=np.zeros(1)); return ij, 'empty'
    np.savez_compressed(out, dsm=res['dsm'].astype(np.float32), dtm=res['dtm'].astype(np.float32))
    return ij, 'ok'


if __name__ == '__main__':
    os.makedirs(f'{DATA}/hr', exist_ok=True)
    ts = all_tiles()
    print(len(ts), 'tiles', flush=True)
    with Pool(int(sys.argv[1]) if len(sys.argv) > 1 else 12) as p:
        for n, (ij, st) in enumerate(p.imap_unordered(fetch, ts), 1):
            print(n, ij, st, flush=True)
