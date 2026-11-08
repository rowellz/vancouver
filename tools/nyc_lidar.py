# The 2017 New York City topobathymetric LiDAR (NOAA Digital Coast, public domain) as the map's 2 m surface (DSM) and
# terrain (DTM) models, one 4096 m tile per file: DATA/hr/<i>_<j>.npz, as tools/hrdem.py writes for Vancouver.
#   DTM: the survey's bare-earth DEM (1 ft GeoTIFFs in NY Long Island state plane, heights in US survey feet), read at
#        its 1.2 m overview and reprojected onto the world grid
#   DSM: the highest return in each 2 m cell of the survey's point clouds (COPC: only the octree levels down to RES
#        are fetched, never whole files), noise and the river bottom left out
# Run through tools/elevation.py (CITY=nyc); tools/nyc_lidar.py <i_j> fetches one tile.
import json, math, os, re, sys, urllib.request
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from common import LON_O, LAT_O, KX, KY, L1 as T, HR, DATA, retry

N = int(T / HR)
S3 = 'https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com'
DEM = f'{S3}/dem/NYC_topobathy_BE_DEM_2017_9307'
VRT = f'{DEM}/NYC_topobathy_BE_DEM_2017_m9307_EPSG-6539.vrt'
COPC = 'laz/geoid18/9306/'
FT = 0.3048006096012192  # US survey foot
RES = 1.0  # m: the point spacing fetched from each point cloud
DROP = (7, 18, 40, 45)  # low and high noise, bathymetric bottom, water column
os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('GDAL_HTTP_MULTIRANGE', 'YES')
os.environ.setdefault('GDAL_HTTP_MERGE_CONSECUTIVE_RANGES', 'YES')
os.environ.setdefault('VSI_CACHE', 'TRUE')
# a stalled download must fail (and be retried), not hang the bake: GDAL's and laspy's (requests) reads time out
os.environ.setdefault('GDAL_HTTP_TIMEOUT', '120')
os.environ.setdefault('GDAL_HTTP_MAX_RETRY', '4')
os.environ.setdefault('GDAL_HTTP_RETRY_DELAY', '5')
import socket
socket.setdefaulttimeout(120)


def _cached(name, make):
    p = f'{DATA}/hr/{name}'
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        v = make(); json.dump(v, open(p + '.tmp', 'w')); os.replace(p + '.tmp', p)
    return json.load(open(p))


def dem_sources():
    """the DEM's GeoTIFFs and their bounds in state plane feet, from the mosaic's VRT"""
    def make():
        x = urllib.request.urlopen(VRT, timeout=60).read().decode()
        gt = [float(v) for v in re.search(r'<GeoTransform>([^<]*)</GeoTransform>', x).group(1).split(',')]
        out = []
        for src in re.findall(r'<ComplexSource>(.*?)</ComplexSource>', x, re.S):
            f = re.search(r'<SourceFilename[^>]*>([^<]*)<', src).group(1)
            d = {k: float(v) for k, v in re.findall(r'(\w+)="([-\d.e+]+)"', re.search(r'<DstRect[^>]*/>', src).group(0))}
            x0, y1 = gt[0] + d['xOff'] * gt[1], gt[3] + d['yOff'] * gt[5]
            out.append((f, x0, y1 + d['ySize'] * gt[5], x0 + d['xSize'] * gt[1], y1))
        return out
    return _cached('nyc_dem_sources.json', make)


def copc_tiles():
    """every point cloud of the survey and its bounds in UTM metres: (key, x0, x1, y0, y1), from the survey's min/max
    list (its file names don't say where a tile is)"""
    def make():
        out = []
        for line in urllib.request.urlopen(f'{S3}/{COPC}minmax_2017_nyc_topobathy_m9306.csv', timeout=60).read().decode().splitlines()[1:]:
            name = line.split(',')[0].strip()
            v = [float(x) for x in re.findall(r'-?\d+\.\d+(?:e[-+]?\d+)?', line[len(name):])]
            if name.endswith('.copc.laz') and len(v) >= 4: out.append((COPC + name, *v[:4]))
        return out
    return _cached('nyc_copc_tiles.json', make)


def grid(i, j):
    """the world tile's destination grid in lon/lat (linear in the world frame), north row first"""
    from rasterio.transform import Affine
    lon0, lat1 = LON_O + i * T / KX, LAT_O + (j + 1) * T / KY
    return Affine((T / KX) / N, 0, lon0, 0, -(T / KY) / N, lat1), (lon0, LAT_O + j * T / KY, LON_O + (i + 1) * T / KX, lat1)


def dtm_tile(i, j):
    import rasterio
    from rasterio.warp import reproject, transform_bounds, Resampling
    tr, ll = grid(i, j)
    bx0, by0, bx1, by1 = transform_bounds('EPSG:4326', 'EPSG:6539', *ll)
    dst = np.full((N, N), np.nan, np.float32)
    for f, x0, y0, x1, y1 in dem_sources():
        if x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1: continue
        with rasterio.open(f'/vsicurl/{DEM}/{f}') as full:
            lv = min(1, len(full.overviews(1)) - 1)  # 4 ft cells, or the coarsest the file has (small ones have fewer)
        with rasterio.open(f'/vsicurl/{DEM}/{f}', **({'OVERVIEW_LEVEL': lv} if lv >= 0 else {})) as src:
            reproject(rasterio.band(src, 1), dst, src_nodata=src.nodata, dst_transform=tr, dst_crs='EPSG:4326', dst_nodata=np.nan,
                      init_dest_nodata=False, resampling=Resampling.bilinear, num_threads=2)
    return dst * FT


def dsm_tile(i, j):
    import laspy
    from laspy.copc import Bounds
    from pyproj import Transformer
    _, (lon0, lat0, lon1, lat1) = grid(i, j)
    to_utm = Transformer.from_crs('EPSG:4326', 'EPSG:6347', always_xy=True)  # NAD83(2011) / UTM 18N: the point clouds' frame
    back = Transformer.from_crs('EPSG:6347', 'EPSG:4326', always_xy=True)
    ux, uy = to_utm.transform([lon0, lon1, lon0, lon1], [lat0, lat0, lat1, lat1])
    q = Bounds(mins=np.array([min(ux), min(uy)]), maxs=np.array([max(ux), max(uy)]))
    dsm = np.full(N * N, -np.inf, np.float32)
    n = 0
    for key, x0, x1, y0, y1 in copc_tiles():
        if x1 < q.mins[0] or x0 > q.maxs[0] or y1 < q.mins[1] or y0 > q.maxs[1]: continue
        with laspy.CopcReader.open(f'{S3}/{key}') as r:
            p = r.query(bounds=q, resolution=RES)
        if not len(p): continue
        keep = ~np.isin(np.asarray(p.classification), DROP)
        ux_, uy_, z = np.asarray(p.x)[keep], np.asarray(p.y)[keep], np.asarray(p.z, np.float32)[keep]
        del p  # every dimension of every point: the big one
        # UTM -> world cell: affine over one 760 m point cloud (exact to well under a centimetre), not per point
        ax, ay = [x0, x1, x0], [y0, y0, y1]
        lon, lat = back.transform(ax, ay)
        W = np.array([[(lo - LON_O) * KX - i * T, (la - LAT_O) * KY - j * T] for lo, la in zip(lon, lat)])
        A = np.linalg.solve(np.array([[a, b, 1.0] for a, b in zip(ax, ay)]), W)  # rows: d/dx, d/dy, offset
        c = ((ux_ * A[0, 0] + uy_ * A[1, 0] + A[2, 0]) / HR).astype(np.int32)
        r_ = ((T - (ux_ * A[0, 1] + uy_ * A[1, 1] + A[2, 1])) / HR).astype(np.int32)
        del ux_, uy_
        ok = (c >= 0) & (c < N) & (r_ >= 0) & (r_ < N)
        np.maximum.at(dsm, r_[ok].astype(np.int64) * N + c[ok], z[ok])
        n += int(ok.sum())
        del c, r_, z, ok
    dsm = dsm.reshape(N, N)
    dsm[~np.isfinite(dsm)] = np.nan
    # cells no return fell in (dark roofs, glass, the RES spacing): the highest neighbour, twice
    from scipy import ndimage
    for _ in range(2):
        hole = np.isnan(dsm)
        if not hole.any() or hole.all(): break
        near = ndimage.maximum_filter(np.nan_to_num(dsm, nan=-1e9), 3)
        dsm = np.where(hole & (near > -1e8), near, dsm)
    return dsm, n


def fetch_once(ij):
    i, j = ij
    out = f'{DATA}/hr/{i}_{j}.npz'
    if os.path.exists(out): return ij, 'cached'
    dtm = retry(lambda: dtm_tile(i, j), 3, 10)
    if np.isnan(dtm).all(): np.savez_compressed(out, empty=np.zeros(1)); return ij, 'empty'
    dsm, n = retry(lambda: dsm_tile(i, j), 3, 10)
    np.savez_compressed(out + '.tmp.npz', dsm=dsm.astype(np.float32), dtm=dtm.astype(np.float32))
    os.replace(out + '.tmp.npz', out)
    return ij, f'ok, {n} points'


if __name__ == '__main__':
    from common import tile_args
    os.makedirs(f'{DATA}/hr', exist_ok=True)
    for ij in tile_args(sys.argv[1:]): print(fetch_once(ij), flush=True)
