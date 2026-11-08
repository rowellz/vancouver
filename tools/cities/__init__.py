# The city the bake makes: CITY=<name> (default vancouver) picks tools/cities/<name>.py. A city module sets the map's
# frame (ORIGIN, KREF_LAT, SC, BBOX), where its data comes from (OSM, ELEVATION, DEM_FALLBACK, DOWNLOADS, trees(),
# sections()), the look of its neighbourhoods (ZONES, GLASS_P, BRICK_ZONES, HOUSES_KIND) and what the page shows
# (PAGE: title, timezone, start view, plugins, credits; written into tiles/index.json). Its bake inputs live in
# DATA: data/ for Vancouver (as always), data/<city>/ for the others; its tiles in public/tiles/ for Vancouver,
# public/tiles/<city>/ for the others (the page opens one with ?city=<city>).
import importlib, os

NAME = os.environ.get('CITY') or 'vancouver'
city = importlib.import_module(f'cities.{NAME}')
DATA = os.environ.get('DATA') or ('data' if NAME == 'vancouver' else f'data/{NAME}')
TILES = 'public/tiles' if NAME == 'vancouver' else f'public/tiles/{NAME}'
