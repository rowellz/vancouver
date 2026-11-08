#!/bin/sh
# The whole map bake, start to finish, inside the bake container (tools/bake.Dockerfile), for the city in CITY
# (tools/cities/<city>.py, default vancouver): fetch → LiDAR → split → sea → tiles → heights, into /w/out, then swap
# into /tiles (the live volume), index.json last. Downloads are kept in /w/data between runs; pass FRESH=1 to
# re-download OpenStreetMap and the city's data. Vancouver lives in data/ and /tiles itself, another city in
# data/<city>/ and /tiles/<city>/ (the page's ?city=<city>).
set -eu
cd /w
P=${PROCS:-4}
CITY=${CITY:-vancouver}
SUB=$([ "$CITY" = vancouver ] || echo "/$CITY")
export CITY DATA=data$SUB
T=/tiles$SUB
mkdir -p "$DATA" out "$T"
python3 tools/fetch.py                # OpenStreetMap cut to the map, the city's trees and the like
python3 tools/elevation.py "${ELEV_PROCS:-$P}"  # resumable: only missing LiDAR tiles are fetched (ELEV_PROCS: its own workers)
python3 tools/osm_split.py
python3 tools/sea.py
[ -f "$DATA/roofs.npz" ] || echo "no $DATA/roofs.npz (tools/roofs.py): roofs keep their palette colours"
[ -f "$DATA/facades.npz" ] || echo "no $DATA/facades.npz (tools/facades.py): walls keep their palettes and the page's own rules"
rm -rf out/0 out/1
OUT=out python3 tools/tiles.py "$P"   # also caches $DATA/mrdem32.npz, which heights.py needs
OUT=out python3 tools/heights.py
# swap in: new tiles first, then the index that points browsers at them, then drop tiles no longer baked
rsync -a --exclude index.json out/ "$T/"
cp out/index.json "$T/index.json"
rsync -a --delete --exclude index.json --exclude heights.bin out/0/ "$T/0/"
rsync -a --delete out/1/ "$T/1/"
echo "baked $(ls out/0 | wc -l) detail and $(ls out/1 | wc -l) overview tiles"
