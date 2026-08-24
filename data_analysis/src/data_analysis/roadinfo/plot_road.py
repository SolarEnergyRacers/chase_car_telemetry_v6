

import logging
import json
from   pathlib import Path
import matplotlib.cm as cm
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np

# ~~~ < include dir hack > ~~~
# normalize include dir root
py_root = Path(__file__).resolve().parents[1]  # data_analysis/src/
if __package__ is None:
    import sys
    sys.path.insert(0, str(py_root.parent))
    __package__ = py_root.name + ".roadinfo"
    # print("plot_road None")
elif __package__ == "":
    __package__ = py_root.name + ".roadinfo"
    # print("plot_road ''")
# print(f"plot_road {__package__=}")
# print(f"plot_road {py_root=}")
# ~~~ </include dir hack > ~~~


try:
    from ..geojson.read_geojson import resolve_geo_to_coords, lonlat2angular
except ImportError:
    from geojson.read_geojson import resolve_geo_to_coords, lonlat2angular

lg = logging.getLogger(__name__)


def plot_road(
    geo: dict,
    speeds: list,
    use_limit: bool = False,
    min_speed: float = None,
    max_speed: float = None,
    fig: plt.Figure = None,
    ax: plt.Axes = None,
    cmap_name: str = "turbo",
    cmap_label: str = "speed [km/h]",
    **kwargs
) -> tuple[Figure, Axes]:
    """plot road speed on new or existing plot
    Args:
        geo: geojson dict
        speeds: get_speeds()-style speed array corresponding to geo
        use_limit: plot speed limit (default is routing speed)
        min_speed: if given, use as min. speed bound in colormap
        max_speed: if given, use as max. speed bound in colormap
        fig, ax: existing plot, or None for new one
        cmap_name: colormap to use (plt.get_cmap() must know it)
        speed_unit: printed on y-axis of colorbar
        **kwargs: forwarded to plt.plot()
    Returns:
        (fig, ax) of plot that was drawn on
    """
    coords = resolve_geo_to_coords(geo, altitude="drop")

    if (fig is None) != (ax is None):
        raise ValueError(
            "either both fig and ax must be given, or None of them")
    if fig is None:
        fig, ax = plt.subplots()
    fixed_linestyle = "linestyle" in kwargs
    fixed_color     = "color"     in kwargs

    speeds = np.array(speeds, dtype=float)
    speeds = speeds[:,1] if use_limit else speeds[:,0]
    minv = np.nanmin(speeds) if min_speed is None else min_speed
    maxv = np.nanmax(speeds) if max_speed is None else max_speed

    if len(ax._colorbars) > 0:
        # re-use colormap - unfortunately cannot update with new min/max, 
        # since previous line(s) are already drawn based on original map
        cbar = ax._colorbars[0]._colorbar
        cmap = cbar.cmap
    else:
        cmap = plt.get_cmap(cmap_name)
        norm = mcolors.Normalize(vmin=minv, vmax=maxv)
        sm = cm.ScalarMappable(norm=norm, cmap=cmap)
        cbar = fig.colorbar(sm, ax=ax)
        cbar.set_label(cmap_label)
    for xya1, xya2, speed in zip(coords[:-1], coords[1:], speeds[1:]):
        x, y, *a = np.vstack([xya1, xya2]).T
        if np.isnan(speed):
            if not(fixed_linestyle): kwargs["linestyle"] = ":"
            if not(fixed_color    ): kwargs["color"    ] = '#DDDDDD'

            dist = lonlat2angular([xya1, xya2])[0, 1]
            lg.warn(f"no speed info for {dist:.0f}m ({xya1} -> {xya2})")
        else:
            if not(fixed_linestyle): kwargs["linestyle"] = "-"
            if not(fixed_color    ): kwargs["color"    ] = cmap(cbar.norm(speed))
        ax.plot(x, y, **kwargs)
    ax.axis('equal')

    return fig, ax

# todo:
# def plot_landmarks(fig, ax, [[lon,lat],[lon,lat]]):
#   """label some known points on map for nicer overview"""


axcache = None
ax2cache = None
def plot_altitude(
    geo: dict, 
    x_offset: float = 0,
    fig: plt.Figure = None, 
    ax: plt.Axes = None, 
    **kwargs
):
    global axcache, ax2cache
    # coords = np.asarray(coords)
    # if not coords.shape[1] == 3:
    #     raise ValueError("cannot interpret data as (lon, lat, alt) points")
    coords = resolve_geo_to_coords(geo, altitude="need")
    
    dpaths = lonlat2angular(coords)
    xaxis = np.cumsum(np.hstack([x_offset, dpaths.T[1]]))

    if (fig is None) != (ax is None):
        raise ValueError(
            "either both fig and ax must be given, or None of them")
    if fig is None:
        fig, ax = plt.subplots()

    inclines = (coords.T[2, 1:] - coords.T[2, :-1]) / dpaths.T[1]

    import scipy.signal as sig
    inclines = sig.savgol_filter(inclines, 10, 3)

    if ax == axcache:
        ax2 = ax2cache
    else:
        _, ax2 = plt.subplots()
        axcache  = ax
        ax2cache = ax2

    ax.plot(xaxis, coords.T[2], **kwargs)
    ax2.step(xaxis[1:], inclines * 100, where="mid", **kwargs)
    ax2.set_ylabel("incline (%)")
    ax2.set_ylim(-10, 10)
    return fig, ax, xaxis[-1]
