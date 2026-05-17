


import pandas as pd
from   pathlib import Path
import panel as pn
import plotly.graph_objects as go
pn.extension('plotly')

ROOT = Path(__file__).parent


@pn.cache
def get_data() -> pd.DataFrame:
    return pd.read_pickle(ROOT / "../main_cols.pkl")
df = get_data()


def make_plot(params: list = [], start = None, stop = None):
    data = df[params]
    data = data[start:stop]

    fig = go.Figure()
    pn.pane.Plotly(fig, margin=0)

    # make space for y-axis
    unit_spacing = 0.04
    lpad = (len(params)) * unit_spacing if params else 0
    fig.update_layout({"xaxis": {"domain": [lpad, 1.0]}})

    for i, col in enumerate(params):
        curve = data[col].dropna(how='all')

        yaxis = "y" if i == 0 else f"y{i+1}"

        fig.add_trace(
            {"x": curve.index, "y": curve, "name": col, "yaxis":yaxis})

        fig.update_layout({f"yaxis{i+1 if i>0 else ''}": {
            "title": col, "overlaying": ("y" if i>0 else None), 
            "position": unit_spacing*(i+1), "title_standoff": 0, "ticklabelstandoff": 0,
        }})

    fig.update_layout({
        "xaxis": {"title": "Time"},
        "legend": {"orientation": "h"},
        "margin": {"l": 0, "r": 0, "t": 0, "b": 0},
        "height": 400,
    }, template=None)
    return fig

def make_plot_panel(rm_btn: pn.widgets.Button):
    global df
    param_widget = pn.widgets.MultiChoice(name="parameter", value=["batt_volt", "batt_curr"], options=list(df.columns), sizing_mode="stretch_width")
    top_row = pn.Row(param_widget, rm_btn)
    dt_start = pn.widgets.DatetimePicker(name="From", value=df.index.min(), sizing_mode="stretch_width")
    dt_stop  = pn.widgets.DatetimePicker(name="To",   value=df.index.max(), sizing_mode="stretch_width")
    dt_range = pn.Row(dt_start, dt_stop, sizing_mode="stretch_width")
    bound_plot = pn.pane.Plotly(
        pn.bind(make_plot, params=param_widget, start=dt_start, stop=dt_stop),
        sizing_mode="stretch_width",config={"responsive": True})

    return pn.Column(top_row, dt_range, bound_plot, sizing_mode="stretch_width")
