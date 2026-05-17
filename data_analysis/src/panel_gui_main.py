

# panel serve panel_gui_main.py

import importlib
import pandas as pd
from   pathlib import Path
import panel as pn
import plotly.graph_objects as go
pn.extension('plotly')

from gui_stuff.panel_test3 import make_plot

# import backend.dynamic #as volatile
import backend.dynamic as volatile

ROOT = Path(__file__).parent


@pn.cache
def get_data() -> pd.DataFrame:
    return pd.read_pickle(ROOT / "main_cols.pkl")
df = get_data()


le_counter = 0

css_vbutton = {
    "writing-mode": "vertical-rl", "transform": "rotate(180deg)",
    # "height": "120px", "width": "20px",
}

def reload_backend(event):
    # global volatile
    print(f"was {volatile.dynamic_1.dynamic_function()}")
    importlib.reload(volatile)
    print(f"is  {volatile.dynamic_1.dynamic_function()}")
    # print(f"was {backend.dynamic.dynamic_1.dynamic_function()}")
    # importlib.reload(backend.dynamic)
    # print(f"is  {backend.dynamic.dynamic_1.dynamic_function()}")

def local_dyncamic_func():
    return volatile.dynamic_1.dynamic_function()

def local_dyncamic_print(*args, **kwargs):
    print(volatile.dynamic_1.dynamic_function())

# HEADER LAYOUT ===============================================================
header_layout = pn.Column(sizing_mode="stretch_width")

reload_backend_btn = pn.widgets.Button(name="reload backend scripts")
reload_backend_btn.on_click(reload_backend)
header_layout.append(reload_backend_btn)
header_layout.append(pn.widgets.StaticText(value=local_dyncamic_func()))



# DYNAMIC LAYOUT ==============================================================
# Column that can be extended, containing Rows that can also be extended
extendable_column = pn.Column()
def add_row(event):
    global le_counter
    le_counter += 1
    def add_panel(event):
        global le_counter
        le_counter += 1
        def rm_panel(event):
            extendable_row.remove(panel)
        panel = pn.Column()
        panel.styles = {'border': '1px solid black'}
        rm_panel_btn = pn.widgets.Button(name=f"remove panel {le_counter}")
        rm_panel_btn.on_click(rm_panel)
        panel.append(rm_panel_btn)
        panel.append(pn.widgets.StaticText(value=local_dyncamic_func()))
        # funny_btn = pn.widgets.Button(name=f"le funny {local_dyncamic_func()}")
        # funny_btn.on_click(local_dyncamic_print)
        # panel.append(funny_btn)
        panel.append(make_plot_panel())
        extendable_row.append(panel)
    def rm_row(event):
        extendable_column.remove(main_row)
    rm_row_btn = pn.widgets.Button(name=f"remove row {le_counter}")
    rm_row_btn.styles = css_vbutton
    rm_row_btn.on_click(rm_row)
    add_panel_btn = pn.widgets.Button(name=f"Add panel {le_counter}+1")
    add_panel_btn.styles = css_vbutton
    add_panel_btn.on_click(add_panel)
    extendable_row = pn.Row()
    main_row = pn.Row(sizing_mode="stretch_width")
    main_row.styles = {'border': '1px solid black'}
    main_row.append(rm_row_btn)
    main_row.append(extendable_row)
    main_row.append(add_panel_btn)
    extendable_column.append(main_row)

def make_plot_panel():
    global df
    param_widget = pn.widgets.MultiChoice(name="parameter", value=["batt_volt", "batt_curr"], options=list(df.columns), sizing_mode="stretch_width")
    dt_start = pn.widgets.DatetimePicker(name="From", value=df.index.min(), sizing_mode="stretch_width")
    dt_stop  = pn.widgets.DatetimePicker(name="To",   value=df.index.max(), sizing_mode="stretch_width")
    dt_range = pn.Row(dt_start, dt_stop, sizing_mode="stretch_width")
    bound_plot = pn.pane.Plotly(
        pn.bind(make_plot, params=param_widget, start=dt_start, stop=dt_stop),
        sizing_mode="stretch_width",config={"responsive": True})

    return pn.Column(param_widget, dt_range, bound_plot, sizing_mode="stretch_width")


# MAIN LAYOUT =================================================================
add_row_btn = pn.widgets.Button(name="add row")
add_row_btn.on_click(add_row)

main_layout = pn.Column(sizing_mode="stretch_width")
main_layout.append(header_layout)
main_layout.append(extendable_column)
main_layout.append(add_row_btn)


# HOSTING =====================================================================
template = pn.template.MaterialTemplate(
    site="Panel",
    title="Dynamic Panel",
    main=main_layout,
)
template.servable()
