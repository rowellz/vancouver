# The map bake's toolbox (osmium, uv, Python deps; laspy and pyproj read the NYC point clouds), for tools/bake-all.sh.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends osmium-tool curl rsync ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_LINK_MODE=copy UV_CACHE_DIR=/cache/uv
RUN uv pip install --system numpy scipy pillow shapely rasterio osmium pyproj 'laspy[lazrs]' requests
WORKDIR /w
