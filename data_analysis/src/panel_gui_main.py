

# panel serve panel_gui_main.py

import importlib
import pandas as pd
from   pathlib import Path
import panel as pn
import plotly.graph_objects as go
pn.extension('plotly')

from gui_stuff.panel_test3 import make_plot_panel

import backend.dynamic as volatile

ROOT = Path(__file__).parent


@pn.cache
def get_data() -> pd.DataFrame:
    return pd.read_pickle(ROOT / "main_cols.pkl")
df = get_data()


css_vbutton = {
    "writing-mode": "vertical-rl", "transform": "rotate(180deg)",
    # "height": "120px", "width": "20px",
}

def reload_backend(event):
    print(f"was {volatile.dynamic_1.dynamic_function()}")
    importlib.reload(volatile)
    print(f"is  {volatile.dynamic_1.dynamic_function()}")

def local_dyncamic_func():
    return volatile.dynamic_1.dynamic_function()

def local_dyncamic_print(*args, **kwargs):
    print(volatile.dynamic_1.dynamic_function())

# HEADER LAYOUT ===============================================================
header_layout = pn.Column(sizing_mode="stretch_width")

reload_backend_btn = pn.widgets.Button(name="reload backend scripts")
reload_backend_btn.on_click(reload_backend)
header_layout.append(pn.widgets.StaticText(value=local_dyncamic_func()))



# DYNAMIC LAYOUT ==============================================================
# Column that can be extended, containing Rows that can also be extended
extendable_column = pn.Column()
def add_row(event):
    def add_panel(event):
        def rm_panel(event):
            extendable_row.remove(panel)
        panel = pn.Column()
        panel.styles = {'border': '1px solid black'}
        rm_panel_btn = pn.widgets.Button(name=f"remove panel")
        rm_panel_btn.on_click(rm_panel)
        panel.append(make_plot_panel(rm_panel_btn))
        extendable_row.append(panel)
    def rm_row(event):
        extendable_column.remove(main_row)
    rm_row_btn = pn.widgets.Button(name=f"remove row")
    rm_row_btn.styles = css_vbutton
    rm_row_btn.on_click(rm_row)
    add_panel_btn = pn.widgets.Button(name=f"Add panel")
    add_panel_btn.styles = css_vbutton
    add_panel_btn.on_click(add_panel)
    extendable_row = pn.Row()
    main_row = pn.Row(sizing_mode="stretch_width")
    main_row.styles = {'border': '1px solid black'}
    main_row.append(rm_row_btn)
    main_row.append(extendable_row)
    main_row.append(add_panel_btn)
    extendable_column.append(main_row)



# MAIN LAYOUT =================================================================
add_row_btn = pn.widgets.Button(name="add row")
add_row_btn.on_click(add_row)

main_layout = pn.Column(sizing_mode="stretch_width")
main_layout.append(header_layout)
main_layout.append(extendable_column)
main_layout.append(add_row_btn)

sidebar=[]
sidebar.append(reload_backend_btn)
sidebar.append(pn.widgets.StaticText(value="Timestamp tags?"))
sidebar.append(pn.widgets.StaticText(value="add now [label]"))
sidebar.append(pn.widgets.StaticText(value="add any [label] [datetime]"))
sidebar.append(pn.widgets.StaticText(value="find by name"))
sidebar.append(pn.widgets.StaticText(value="> copy found as timestamp"))
sidebar.append(pn.widgets.StaticText(value="> delete found"))
# * do the "facebook delete": mark as deleted, hide as if deleted, but do 
# not actually remove from dataset.


# HOSTING =====================================================================
template = pn.template.MaterialTemplate(
    site="Panel",
    title="Dynamic Panel",
    sidebar=sidebar,
    main=main_layout,
)
template.servable()
