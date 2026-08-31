

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
    # print("roadinfo None")
elif __package__ == "":
    __package__ = py_root.name + ".roadinfo"
    # print("roadinfo ''")
# print(f"roadinfo {__package__=}")
# print(f"roadinfo {py_root=}")
# ~~~ </include dir hack > ~~~

try:
    from ..geojson.read_geojson import lonlat2angular
except ImportError:
    from geojson import lonlat2angular

if __name__ == "__main__":
    # demo run, see below
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors
    import matplotlib.pyplot as plt
    import numpy as np

    from plot_road import plot_road, plot_altitude

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
            list of (speed, speed_limit) for every point in the LineString,
            taken from the edge each point was matched onto. Points sit
            mid-edge (median distance_along_edge ~0.5), so where the edge
            changes between two points the road transition lies somewhere
            between them - neither point's value is exact for that segment.
            Difference is negligible in practice (<0.5% of travel time).
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


def scan_cache(fps: list[Path]) -> dict:
    """Scan cache for missing or obsolete files
    Args:
        fps: list of files or directories for which a cached file is expected.
            all files in directories are scanned, subdirectories are ignored
    Returns:
        dict of {
        'missing': list of unmatched in fps,
        'orphans': list of orphaned chache files
        'mapped' : dict of cachefile: fp pairs}
    """
    ls_fp = []
    for fp in fps:
        if fp.is_file():
            ls_fp.append(fp)
        elif fp.is_dir():
            ls_fp.extend([f for f in fp.iterdir() if f.is_file()])

    hashset = {f.name for f in cachedir.iterdir()}

    missing = []
    found = {}
    for fp in ls_fp:
        with open(fp, 'r') as f:
            s = f.read()
        h = hashlib.sha256(s.encode("utf-8")).hexdigest()
        if _get_cache(h) is None:
            missing.append(fp)
        else:
            found[h] = fp
            hashset.remove(h)
    
    return {"missing": missing, "orphans": list(hashset), "mapped": found}


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

    # valhalla refuses to answer queries longer than 200km
    # -> divide into N segments, expecting points at ~ regular intervals
    delta_paths = lonlat2angular(coords)
    distance = np.sum(delta_paths.T[1])
    if distance > 195e3:
        n_split = np.ceil(distance / 150e3).astype(int)
        n_elem = np.ceil(len(coords) / n_split).astype(int)
        parts = [coords[i*n_elem :(i + 1)*n_elem] for i in range(n_split)]
    else:
        # prefer single-query, if possible
        parts = [coords]

    responses = []
    for part in parts:
        shape = [{"lat": lat, "lon": lon} for lon, lat, *alt in part]

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
        responses.append(r.json())

    # idx 0  is copied or returned as-is -> check separately in advance
    for pt in responses[0]["matched_points"]:
        if not("edge_index" in pt):
            lg.warning(f"failed to match point {pt['lon']}, {pt['lat']} "
                f"({pt['type']=})")

    if len(parts) == 1:
        # no combining necessary
        lg.info(f"returned single-query result")
        return responses[0]

    combined = responses.pop(0)
    offset = len(combined["edges"])
    lg.info(f"starting multi-query result with {offset} edges")
    while responses:
        resp = responses.pop(0)
        for pt in resp["matched_points"]:
            if "edge_index" in pt:
                pt["edge_index"] += offset
            else:
                lg.warning(f"failed to match point {pt['lon']}, {pt['lat']} "
                    f"({pt['type']=})")
        combined["edges"].extend(resp["edges"])
        combined["matched_points"].extend(resp["matched_points"])
        offset = len(combined["edges"])
        lg.info(f"updated multi-query result to {offset} edges")
    lg.info(f"returned multi-query result")
    return combined


if __name__ == "__main__":
    ROOT = Path(__file__).parents[3]

    # scan = scan_cache([ROOT / "data/roadinfo/"])
    # print("missing:")
    # for m in scan["missing"]:
    #     print(f"- {m}")
    # print("orphans:")
    # for o in scan["orphans"]:
    #     print(f"x {o}")
    # print("found:")
    # for h, fp in scan["mapped"].items():
    #     print(f"+ {h}: {fp}")
    # exit(0)

    day = 1

    n = 1
    fp = ROOT / f"data/roadinfo/day{day}_route{n}.geojson"
    fig, ax = plt.subplots()
    fig2, ax2 = plt.subplots()
    dist = 0
    while fp.is_file():
        info = get_info(fp)
        speed = get_speed(info)
        fig, ax = plot_road(fp, speed, min_speed=20, max_speed=120, fig=fig, ax=ax)
        fig2, ax2, dist = plot_altitude(fp, dist, fig2, ax2, label=f"day{day}_{n}")
        fp = ROOT / f"data/roadinfo/day{day}_route{(n := n+1)}.geojson"

    fp = ROOT / f"data/roadinfo/day{day}_loop.geojson"
    if fp.is_file():
        info = get_info(fp)
        speed = get_speed(info)
        # plot_road(fp, speed, max_speed=100)
        plot_road(fp, speed, min_speed=20, max_speed=120, fig=fig, ax=ax)
        fig2, ax2, *_ = plot_altitude(fp, 0, fig2, ax2, label=f"day{day}_L")


    fp = ROOT / f"data/roadinfo/manual_day1.geojson"
    if fp.is_file():
        info = get_info(fp)
        speed = get_speed(info)
        # plot_road(fp, speed, max_speed=100)
        plot_road(fp, speed, min_speed=20, max_speed=120, fig=fig, ax=ax, linestyle=":")
        fig2, ax2, *_ = plot_altitude(fp, 0, fig2, ax2, label=f"manual", linestyle=":")


    ax2.legend()
    ax2.grid(which="both")

    plt.show()
