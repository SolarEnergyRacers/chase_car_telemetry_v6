

import pandas as pd
import streamlit as st

from backend import *

gurke2 = 0


def main_st():
    st.button("get all data", on_click=data_hoarder.request_all_data)

    if "all_data" in st.session_state:
        st.text(f"all data: {st.session_state['all_data']}")
    else:
        st.error("no data")
    
    st.text(f"gugus: {st.session_state.get('gurke', 'gewese')}")

    global gurke2
    gurke2 += 1
    st.text(f"gugus2: {gurke2}")

    # st.text(str(data_hoarder.full_frame))
    plotdata: pd.DataFrame = data_hoarder.main_cols
    print(plotdata.info())
    plotdata = plotdata.loc[:, ["batt_volt", "batt_curr"]]
    plotdata = plotdata.dropna(how='all')
    print(plotdata.info())
    st.line_chart(plotdata) #, y=["speed", "batt_volt", "batt_curr"])

    for err in data_hoarder.current_errors:
        st.error(err)

