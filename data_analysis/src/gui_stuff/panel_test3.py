


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
        # "yaxis": {"title": (params[0] if params else "")},  
        # ^ fixes a layout issue, no idea why
        "legend": {"orientation": "h"},
        # "autosize": True,
        "margin": {"l": 0, "r": 0, "t": 0, "b": 0},
    }, template=None)
    return fig


def add_plot(event):
    # global plot_layouts
    param_widget = pn.widgets.MultiChoice(name="parameter", value=["batt_volt", "batt_curr"], options=list(df.columns), sizing_mode="stretch_width")
    dt_start = pn.widgets.DatetimePicker(name="From", value=df.index.min(), sizing_mode="stretch_width")
    dt_stop  = pn.widgets.DatetimePicker(name="To",   value=df.index.max(), sizing_mode="stretch_width")
    dt_range = pn.Row(dt_start, dt_stop, sizing_mode="stretch_width")
    # bound_plot = pn.bind(make_plot, params=param_widget, start=dt_start, stop=dt_stop)
    bound_plot = pn.pane.Plotly(
        pn.bind(make_plot, params=param_widget, start=dt_start, stop=dt_stop),
        sizing_mode="stretch_width",config={"responsive": True})
    # plot_layouts.append([param_widget, dt_range, bound_plot])
    # render()
    global dynamic
    # dynamic.extend([dt_range, bound_plot])
    # dynamic.append(pn.Column(param_widget, dt_range, bound_plot, width=500, sizing_mode="fixed"))
    dynamic.append(pn.Column(param_widget, dt_range, bound_plot, sizing_mode="stretch_width"))


main_layout = []
plot_layouts = []

add_plot_btn = pn.widgets.Button(name="add plot")
add_plot_btn.on_click(add_plot)


# def render():
#     global main_layout
#     main_layout = []
#     main_layout.append(add_plot_btn)
#     for pl in plot_layouts:
#         main_layout.extend(pl)


# render()


# # sideways button:
# btn = pn.widgets.Button(name="Add panel", button_type="primary")
# btn.styles = {
#     "writing-mode": "vertical-rl", "transform": "rotate(180deg)",
#     "height": "120px", "width": "40px",
# }
# main_layout.append(btn)


container = pn.Column(sizing_mode="stretch_width")
main_layout = pn.Row(container, sizing_mode="stretch_width")

container.append(add_plot_btn)
dynamic = pn.Column()
container.append(dynamic)

template = pn.template.MaterialTemplate(
    site="Panel",
    title="Panel Test 3",
    main=main_layout,
)
# template.main.styles = {
#     "width": "100vw",
#     "max-width": "100vw",
#     "padding": "0",
#     "margin": "0",
# }
template.servable()
