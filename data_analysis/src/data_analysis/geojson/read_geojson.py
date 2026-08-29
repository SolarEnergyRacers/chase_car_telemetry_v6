

import pandas as pd
import logging
import json
import numpy as np
from   pathlib import Path

lg = logging.getLogger(__name__)

# todo: test featureCollection


def _get_feature(feat: dict):
    if not "type" in feat:
        raise ValueError("geometry object missing 'type' field")
    gtype = feat["type"]
    if gtype == "LineString" or gtype == "Polygon" or gtype == "MultiPoint":
        # polygons have rules and are weird when containing holes, but we don't
        # care about this here.
        # No information about type is available in final data, 
        # multipoint cannot be distinguished from line
        return np.array(feat["coordinates"], dtype=float)
    else:
        raise NotImplementedError(
            f"geometry type '{gtype}' is not supported")


def _get_featureCollection(feats: list):
    if not isinstance(feats, list):
        raise TypeError(
            f"FeatureCollection 'features' is {type(geo)} instead of list")
    retvals = []
    for feat in feats:
        retvals.append(_get_feature(feat))
    return retvals


def get_paths(geo: dict):
    if not "type" in geo:
        raise ValueError(
            "toplevel object missing 'type' field, geojson not recognized")
    if geo["type"] == "Feature":
        if not "geometry" in geo:
            raise ValueError("Feature object missing 'geometry' field")
        return [_get_feature(geo["geometry"])]
    elif geo["type"] == "FeatureCollection":
        if not "features" in geo:
            raise ValueError(
                "FeatureCollection object missing 'features' field")
        return _get_featureCollection(geo["features"])


def resolve_geo_to_coords(geo: any, altitude: str = "keep") -> np.ndarray:
    """Take geojson, or part of it, and get its LineString property
    Args:
        geo: path to geojson, geojson dict, df, or coordinates as list / array
            (latter case is returned unmodified)
        altitude: 
            'keep': return if available, don't care if missing from geo;
            'need': raise ValueError if missing from geo;
            'drop': only return coordinates without altitude inforomation
    Returns:
        best attempt at extracted array of coordinates from geo, as np.ndarray
    """
    if altitude in ("keep", "drop"):
        inp_w = (2, 3)
    elif altitude == "need":
        inp_w = (3, )
    else:
        raise ValueError(
            f"altitude must be 'keep', 'need' or 'drop', but is '{altitude}'")

    # from file
    if isinstance(geo, str):
        geo = Path(geo)
    if isinstance(geo, Path) and geo.is_file():
        with open(geo, 'r') as f:
            geo_ = json.load(f)
        lg.info(f"resolve_geo_to_coords(): read {geo} from disk")
        geo = geo_

    # from dict
    if isinstance(geo, dict):
        geo = geo.get("geometry", geo)
        if "type" in geo:
            if geo["type"] != "LineString":
                # umap.openstreetmap writes LineString; ignore other options
                raise RuntimeError("Expected a LineString")
            geo = geo["coordinates"]

    # from array-like
    if isinstance(geo, list) or isinstance(geo, tuple):
        geo = np.array(geo, dtype=float)
    if isinstance(geo, pd.DataFrame):
        # order of known matters. 
        known = ["longitude", "lon", "latitude", "lat", "altitude", "alt"]
        cols = [key for key in known if key in geo.columns]
        if not len(cols) >= 2:
            raise KeyError(
                f"geo dataframe is missing required columns. "
                f"Valid are {known}, but got {list(geo.columns)} instead.")
        if not( cols[0].startswith("lon") and cols[1].startswith("lat") ):
            raise KeyError(
                f"geo dataframe is malformed. Need lon,lat,(alt) info "
                f"in that order, but found {cols} instead.")
        if len(cols) == 3 and not cols[2].startswith("alt"):
            raise KeyError(
                f"geo dataframe is malformed. Need lon,lat,(alt) info "
                f"in that order, but found {cols} instead.")
        lg.info(f"geo from df with cols {cols}")
        geo = np.array(geo[cols])
    if isinstance(geo, np.ndarray):
        msg = "geo is array, but"
        if not len(geo.shape) == 2: 
            raise TypeError(
                f"{msg} {len(geo.shape)}D instead of 2D")
        if not geo.shape[1] in inp_w: 
            raise TypeError(
                f"{msg} shape is {geo.shape} instead of (x,2) or (x,3)")
        if not (    np.issubdtype(geo.dtype, np.floating)
                or isinstance(geo[0, 0], float)):
            raise TypeError(
                f"{msg} not of float type ({type(geo[0, 0])} at [0,0])")
        coords = geo
        if altitude == "drop":
            coords = coords[:, :2]
        lg.info(f"found coords of shape {coords.shape}")
        return coords

    # giving up
    raise ValueError("could not interpret geo as geojson(-part)")


def lonlat2angular(path: list):
    # Δλ = longitude2 − longitude1
    # y = sin(Δλ) × cos(latitude2)
    # x = cos(latitude1) × sin(latitude2) − sin(latitude1) × cos(latitude2) × cos(Δλ)
    # Initial bearing = atan2(y, x)
    # Normalized bearing = (bearing + 360) mod 360
    if len(path[0]) == 2:
        has_alt = False
    elif len(path[0]) == 3:
        has_alt = True
    else:
        raise ValueError(
            f"cannot interpret coordinates with {len(path[0])} entries "
            f"(must be 2 for lon/lat, or 3 for lon/lat/height)")
    rad_path = [[np.deg2rad(p[0]), np.deg2rad(p[1]), *p[2:]] for p in path]
    delta_path = []
    for p1, p2 in zip(rad_path[:-1], rad_path[1:]):
        lon1, lat1, *alt1 = p1
        lon2, lat2, *alt2 = p2
        d_lon = lon2 - lon1
        d_lat = lat2 - lat1
        dy = np.sin(d_lon) * np.cos(lat2)
        dx = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(d_lat)
        angle = np.mod(np.atan2(dx, dy), 2*np.pi)
        compass = np.rad2deg(angle)

        r_percent = (np.abs(lat1) / (np.pi / 2))      # crude fat-earth
        r = (1-r_percent)*6.378e6 + r_percent*6.357e6 # compensation
        hav = (
                + 1 
                - np.cos(d_lat) 
                + np.cos(lat1)*np.cos(lat2)*(1-np.cos(d_lon))
            ) / 2
        arc = 2*np.asin((hav)**0.5)
        distance = r*arc

        if has_alt:
            rise = alt2[0] - alt1[0]
            delta_path.append([compass, distance, rise])
        else:
            delta_path.append([compass, distance])
    return np.array(delta_path)


def reverse_angular(path: list, origin: list = None):
    """debug function. show path if it were planar"""
    if origin is None:
        px, py = 0, 0
    else:
        px, py, *h = origin
    points = [[px, py]]
    for p in path:
        a = np.deg2rad(p[0])
        px += np.cos(a) * p[1]
        py += np.sin(a) * p[1]
        points.append([px, py])
    return points


if __name__ == "__main__":
    import json
    ROOT = Path(__file__).parents[3]
    fp = ROOT / "data/roadinfo/test_segment_sasolburg.geojson"

    with open(fp, 'r') as file:
        j = json.load(file)
    paths = get_paths(j)
    paths[0] = paths[0]
    print(paths)
    dpaths = lonlat2angular(paths[0])
    print(dpaths)

    import matplotlib.pyplot as plt
    dpaths = np.array(dpaths)
    print(f"length = {sum(dpaths.T[1])/1e3:.3f}km")
    print(f"{len(dpaths)=}")
    # plt.plot(np.unwrap(dpaths.T[0], period=360))
    # plt.show()
    # plt.plot(dpaths.T[1])
    # plt.show()

    plt.plot(*np.array(reverse_angular(dpaths, paths[0][0])).T)
    plt.axis('equal')
    plt.show()
