

import json
import logging as lg
import hashlib
from   pathlib import Path
import requests
import time

# ~~~ < include dir hack > ~~~
# normalize include dir root
py_root = Path(__file__).resolve().parents[1]  # data_analysis/src/
if __package__ is None:
    import sys
    sys.path.insert(0, str(py_root.parent))
    __package__ = py_root.name + ".roadinfo"
    print("roadinfo None")
elif __package__ == "":
    __package__ = py_root.name + ".roadinfo"
    print("roadinfo ''")
print(f"roadinfo {__package__=}")
print(f"roadinfo {py_root=}")
# ~~~ </include dir hack > ~~~

if __name__ == "__main__":
    # demo run, see below
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors
    import matplotlib.pyplot as plt
    import numpy as np

    from plot_road import plot_road

    lg.basicConfig(level=lg.INFO, handlers=[lg.StreamHandler()])
lg = lg.getLogger(__name__)


cachedir = Path(__file__).parent / "valhalla_cache"
valhalla_url = "https://valhalla1.openstreetmap.de/trace_attributes"


# -----------------------------------------------------------------------------
# setup

if not cachedir.is_dir():
    if cachedir.exists():
        raise RuntimeError(
            f"valhalla cache dir {cachedir} exists but is not a directory")
    lg.info(f"creating valhalla cache '{cachedir}'...")
    cachedir.mkdir()
else:
    lg.info(f"using valhalla cache '{cachedir}'...")


# -----------------------------------------------------------------------------
# public

def get_info(file: Path) -> dict:
    """Get full information about each road segment from Valhalla api. 
    Will read from cached file if available, or make a http query otherwise.
    Args:
        file: path to a .geojson file containing ["geometry"]["LineString"]
    Returns:
        dict provided by valhalla api corresponding to given LineString
    """
    # require a raw file for reliable hashes.
    # Otherwise, data would need to be sorted and serialized reproducibly -
    # and good luck with floats where operations may have changed otherwise
    # unnoticable bits.
    with open(file, 'r') as f:
        s = f.read()
        geo = json.loads(s)
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()

    info = _get_cache(h)
    if not(info is None):
        return info
    
    info = _get_api(geo)
    _set_cache(h, info)
    return info


def get_speed(info: dict) -> list(tuple):
    """get speed from valhalla info dict
    Args:
        info: dict provided by valhalla api for one LineString
    Returns:
        list of (speed, speed_limit) at every point in the LineString. 
            speed and/or speed_limit be None if value is not found in info.
            It would appear that the end node speed applies to a segment 
            between 2 coordinate points (-> use speeds[1:] rather than [:-1])
    """
    speeds = []
    for e in info["matched_points"]:
        mt = e["type"]
        if mt == "unmatched":
            speeds.append((None, None))
            continue
        idx = e.get("edge_index")
        idx = idx - 2*(idx & int(2**63))  # 64b signed
        edge = info["edges"][idx]
        speeds.append((edge.get("speed"), edge.get("speed_limit")))
    return speeds


# -----------------------------------------------------------------------------
# private

def _get_cache(of_hash: str) -> dict|None:
    """read valhalla info dict from cached file on disk if existing"""
    fn = cachedir / of_hash
    if fn.is_file():
        with (cachedir / of_hash).open('r') as file:
            lg.info(f"reading {of_hash} from cache")
            return json.load(file)
    else:
        return None


def _set_cache(of_hash: str, info: dict) -> None:
    """write valhalla info dict to cache on disk. Cannot overwrite existing"""
    fn = cachedir / of_hash
    if fn.exists():
        raise FileExistsError(f"cannot overwrite hash file '{fn}'")
    with fn.open('w') as file:
        json.dump(info, file)
    lg.info(f"wrote {of_hash} to cache ({int(fn.stat().st_size/1e3)}kB)")
    return


def _get_api(geo: dict):
    """get valhalla info dict with a http request"""
    geom = geo["geometry"]
    if geom["type"] != "LineString":
        raise RuntimeError("Expected a LineString")
    coords = geom["coordinates"]

    shape = [{"lat": lat, "lon": lon} for lon, lat, *alt in coords]

    payload = {
        "shape": shape,
        "costing": "auto",
        "shape_match": "map_snap"
    }

    t0 = time.monotonic()
    r = requests.post(valhalla_url, json=payload)
    dt = time.monotonic() - t0
    if r.status_code >= 300:
        lg.error(f"HTTP code {r.status_code}: {r.text} (after {dt:.3f}sec)")
    lg.info(f"HTTP {r.status_code} in {dt:.3f}sec")
    r.raise_for_status()

    return r.json()


if __name__ == "__main__":
    ROOT = Path(__file__).parents[3]
    fp = ROOT / "data/roadinfo/test_segment_sasolburg.geojson"
    info = get_info(fp)
    speed = get_speed(info)

    plot_road(fp, speed, max_speed=None)

    plt.show()