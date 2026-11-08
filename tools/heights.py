# A coarse height grid for the whole map (64 m cells), so heights are known everywhere before any tile has
# loaded: public/tiles/heights.bin = 'HGT1', x0, y1, cell (float32), w, h (uint32), then w*h int16 decimetres,
# row 0 north. Terrain from HRDEM (data/hr) where there is some, else MRDEM; the sea is flat at 0 m.
# Run: uv run --with numpy,scipy,rasterio python3 tools/heights.py
import glob, os, struct, sys, gzip
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(__file__))
from common import extent, L1, DATA, TILES

C = 64.0
X0, Y0, X1, Y1 = extent()
W, H = int((X1 - X0) / C) + 2, int((Y1 - Y0) / C) + 2
mr = np.load(f'{DATA}/mrdem32.npz')
z = ndimage.zoom(np.nan_to_num(mr['z'], nan=0.0), 0.5, order=1)[:H, :W]
z = np.pad(z, ((0, max(0, H - z.shape[0])), (0, max(0, W - z.shape[1]))), mode='edge')
for f in glob.glob(f'{DATA}/hr/*.npz'):
    d = np.load(f)
    if 'dtm' not in d: continue
    i, j = map(int, os.path.basename(f)[:-4].split('_'))
    dtm = d['dtm']
    k = int(C / 2)  # 2 m cells per 64 m cell
    small = np.nanmean(dtm[: dtm.shape[0] // k * k, : dtm.shape[1] // k * k].reshape(dtm.shape[0] // k, k, dtm.shape[1] // k, k), axis=(1, 3))
    c0, r0 = int(round((i * L1 - X0) / C)), int(round((Y1 - (j + 1) * L1) / C))
    for r in range(small.shape[0]):
        for c in range(small.shape[1]):
            rr, cc = r0 + r, c0 + c
            if 0 <= rr < H and 0 <= cc < W and np.isfinite(small[r, c]): z[rr, cc] = small[r, c]
sea = np.load(f'{DATA}/sea.npz')
S = sea['sea'].astype(bool)
step = int(C / float(sea['cell']))
seaC = S[: S.shape[0] // step * step, : S.shape[1] // step * step].reshape(S.shape[0] // step, step, S.shape[1] // step, step).mean(axis=(1, 3)) > 0.5
seaC = np.pad(seaC, ((0, max(0, H - seaC.shape[0])), (0, max(0, W - seaC.shape[1]))))[:H, :W]
z = np.where(seaC, 0.0, z)
out = b'HGT1' + struct.pack('<fffII', X0, Y1, C, W, H) + np.clip(np.round(z * 10), -32768, 32767).astype('<i2').tobytes()
OUT = os.environ.get('OUT', TILES)
os.makedirs(OUT, exist_ok=True)
open(f'{OUT}/heights.bin', 'wb').write(gzip.compress(out, 9))
print(W, H, 'max', float(z.max()), 'bytes', os.path.getsize(f'{OUT}/heights.bin'))

# ---- base.png: the whole map's colours at 1 px = 64 m, drawn under the tiles so nothing is ever black while they
# load: water, forest (LiDAR canopy), town (building cover), land
import pickle
from PIL import Image, ImageDraw
F = 8.0  # rasterize footprints at 8 m, then average into 64 m cells
k = int(C / F)
bw, bh = W * k, H * k
img = Image.new('L', (bw, bh), 0); d = ImageDraw.Draw(img)
seen = set()
for f in glob.glob(f'{DATA}/feat/*.pkl'):
    for oid, prs, t in pickle.load(open(f, 'rb')).get('buildings', []):
        if oid in seen: continue
        seen.add(oid)
        for outer, _ in prs:
            if len(outer) >= 3: d.polygon([((x - X0) / F, (Y1 - y) / F) for x, y in outer], fill=255)
built = np.array(img, np.float32)[: H * k, : W * k].reshape(H, k, W, k).mean(axis=(1, 3)) / 255
canopy = np.zeros((H, W), np.float32)
for f in glob.glob(f'{DATA}/hr/*.npz'):
    dd = np.load(f)
    if 'dtm' not in dd: continue
    i, j = map(int, os.path.basename(f)[:-4].split('_'))
    chm = np.nan_to_num(dd['dsm'] - dd['dtm'])
    kk = int(C / 2)
    cf = (chm[: chm.shape[0] // kk * kk, : chm.shape[1] // kk * kk] > 5).reshape(chm.shape[0] // kk, kk, chm.shape[1] // kk, kk).mean(axis=(1, 3))
    c0, r0 = int(round((i * L1 - X0) / C)), int(round((Y1 - (j + 1) * L1) / C))
    rr, cc = slice(max(0, r0), min(H, r0 + cf.shape[0])), slice(max(0, c0), min(W, c0 + cf.shape[1]))
    canopy[rr, cc] = cf[rr.start - r0:rr.stop - r0, cc.start - c0:cc.stop - c0]
have = np.zeros((H, W), bool)
for f in glob.glob(f'{DATA}/hr/*.npz'):
    i, j = map(int, os.path.basename(f)[:-4].split('_'))
    c0, r0 = int(round((i * L1 - X0) / C)), int(round((Y1 - (j + 1) * L1) / C))
    have[max(0, r0):r0 + int(L1 / C), max(0, c0):c0 + int(L1 / C)] = True
canopy = np.where((canopy < 0.02) & (z > 250), 0.85, canopy)  # mountains outside the LiDAR: forest
canopy = np.clip(canopy - built, 0, 1)
wimg = Image.new('L', (bw, bh), 0); wd = ImageDraw.Draw(wimg)
for f in glob.glob(f'{DATA}/feat/*.pkl'):
    for oid, prs, t in pickle.load(open(f, 'rb')).get('areas', []):
        if not (t.get('natural') == 'water' or t.get('waterway') == 'riverbank' or ('water' in t and t.get('natural') in (None, 'water'))): continue
        for outer, inners in prs:
            if len(outer) >= 3: wd.polygon([((x - X0) / F, (Y1 - y) / F) for x, y in outer], fill=255)
            for h in inners:
                if len(h) >= 3: wd.polygon([((x - X0) / F, (Y1 - y) / F) for x, y in h], fill=0)
seaC = seaC | (np.array(wimg, np.float32)[: H * k, : W * k].reshape(H, k, W, k).mean(axis=(1, 3)) > 127)
land, forest, town, water = (np.array(v, np.float32) for v in ([126, 160, 92], [56, 98, 54], [176, 176, 170], [40, 98, 150]))
col = land[None, None] * (1 - canopy - built)[..., None] + forest * canopy[..., None] + town * np.clip(built * 2.5, 0, 1)[..., None]
col = col / np.maximum(1e-6, (1 - canopy - built + canopy + np.clip(built * 2.5, 0, 1)))[..., None]
col = np.where(seaC[..., None], water, col)
Image.fromarray(np.clip(col, 0, 255).astype(np.uint8)).save(f'{OUT}/base.png', optimize=True)
print('base.png', os.path.getsize(f'{OUT}/base.png'))
