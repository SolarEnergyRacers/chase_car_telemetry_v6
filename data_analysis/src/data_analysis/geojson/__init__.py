
from pathlib import Path as __Path
geojson_root = __Path(__file__).parents[4] / "data/roadinfo"
del __Path

from .read_geojson import *