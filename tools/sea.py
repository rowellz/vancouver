# Step 2: the sea. OSM coastlines are walls on a 4 m raster of the whole map; the sea is what floods from open
# water in the Strait of Georgia. Writes data/sea.npz (uint8 mask, row 0 = north) with its origin and cell size.
# Run: uv run --with numpy,scipy,pillow python3 tools/sea.py
import glob, math, os, pickle, sys
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
sys.path.insert(0, os.path.dirname(__file__))
from common import extent, DATA

C = 4.0
X0, Y0, X1, Y1 = extent()
W, H = int((X1 - X0) / C) + 1, int((Y1 - Y0) / C) + 1
px = lambda p: ((p[0] - X0) / C, (Y1 - p[1]) / C)

seen, coast = set(), []
for f in glob.glob(f'{DATA}/feat/*.pkl'):
    for wid, coords, refs, tags in pickle.load(open(f, 'rb')).get('lines', []):
        if tags.get('natural') == 'coastline' and wid not in seen:
            seen.add(wid); coast.append(coords)
print('coastline ways', len(coast), 'raster', W, 'x', H, flush=True)
wall = Image.new('L', (W, H), 0); d = ImageDraw.Draw(wall)
for c in coast: d.line([px(p) for p in c], fill=1, width=2)
d.rectangle((0, 0, W - 1, H - 1), outline=1)  # the map's edge closes coastlines that run off it
# coastlines the extract cut just short of the edge: run their loose ends out to it
ends = {}
for c in coast:
    for p in (c[0], c[-1]): k = (round(p[0], 1), round(p[1], 1)); ends[k] = ends.get(k, 0) + 1
for (x, y), cnt in ends.items():
    if cnt != 1: continue
    dist = {'w': x - X0, 'e': X1 - x, 's': y - Y0, 'n': Y1 - y}
    side = min(dist, key=dist.get)
    if dist[side] < 300:
        q = {'w': (X0, y), 'e': (X1, y), 's': (x, Y0), 'n': (x, Y1)}[side]
        d.line([px((x, y)), px(q)], fill=1, width=2)
wall = np.array(wall, bool)
lab, n = ndimage.label(~wall)
# OSM coastlines have the land on their left and the water on their right: every region between them votes
votes = np.zeros((n + 1, 2))
for c in coast:
    for (x1, y1), (x2, y2) in zip(c, c[1:]):
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1: continue
        nx, ny = (y2 - y1) / L, -(x2 - x1) / L
        for t in np.arange(0, L, 8):
            mx, my = x1 + (x2 - x1) * t / L, y1 + (y2 - y1) * t / L
            for side, k in ((1, 0), (-1, 1)):
                qx, qy = px((mx + nx * 6 * side, my + ny * 6 * side))
                if 0 <= int(qy) < H and 0 <= int(qx) < W: votes[lab[int(qy), int(qx)], k] += 1
is_sea = votes[:, 0] > votes[:, 1]
is_sea[0] = False
sea = is_sea[lab]
# a region with many votes both ways is land and sea joined through a gap in the coastline: shrink it until
# the gap closes, let the pieces vote again, then grow them back
for k in [k for k, v in enumerate(votes) if k and v.min() > 50]:
    reg = lab == k
    for it in (3, 6, 12, 24):
        er = ndimage.binary_erosion(reg, iterations=it)
        sl, m = ndimage.label(er)
        if m < 2: continue
        sv = np.zeros((m + 1, 2))
        for c in coast:
            for (x1, y1), (x2, y2) in zip(c, c[1:]):
                L = math.hypot(x2 - x1, y2 - y1)
                if L < 1: continue
                nx, ny = (y2 - y1) / L, -(x2 - x1) / L
                for t in np.arange(0, L, 8):
                    mx, my = x1 + (x2 - x1) * t / L, y1 + (y2 - y1) * t / L
                    for side, kk in ((1, 0), (-1, 1)):
                        qx, qy = px((mx + nx * (6 + it * C) * side, my + ny * (6 + it * C) * side))
                        if 0 <= int(qy) < H and 0 <= int(qx) < W and sl[int(qy), int(qx)]: sv[sl[int(qy), int(qx)], kk] += 1
        seaPieces = sv[:, 0] > sv[:, 1]; seaPieces[0] = False
        if not seaPieces.any() or seaPieces[1:].all(): continue
        core = seaPieces[sl]
        # grow back: every pixel of the region goes to the nearest piece
        _, (ii, jj) = ndimage.distance_transform_edt(sl == 0, return_indices=True)
        grown = core[ii, jj] & reg
        sea = (sea & ~reg) | grown
        print('split region', k, 'at erosion', it, 'into', m, 'pieces,', int(seaPieces.sum()), 'sea', flush=True)
        break
sea |= ndimage.binary_dilation(sea) & wall
mixed = [(k, int(v[0]), int(v[1])) for k, v in enumerate(votes) if v.min() > 50]
print('mixed regions (sea, land votes):', mixed[:10], flush=True)
print('sea fraction', round(float(sea.mean()), 3), flush=True)
np.savez_compressed(f'{DATA}/sea.npz', sea=sea.astype(np.uint8), x0=X0, y1=Y1, cell=C)
Image.fromarray((sea * 255).astype(np.uint8)).resize((W // 8, H // 8)).save(f'{DATA}/sea-preview.png')
