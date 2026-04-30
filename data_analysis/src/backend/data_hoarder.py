

import io
import json
import queue

import logging as lg
import numpy as np
import pandas as pd
import streamlit as st

from panda_client import Panda_client


class DataPoint2:
    parameter: str  # = "measurement" + infos from tags
    value: object   # = fields["value"]
    timestamp: int  # in ms



full_frame = pd.DataFrame()
"""Contains the complete set of data from both csv requests and live updates"""

main_cols = pd.DataFrame()
"""Table of main car data where "parameter" column was resolved"""

current_errors = []
"""List of errors that happened recently. Frequently overwritten"""

gurke = 0


with open("options.json", "r") as opt_file:
    opt = json.load(opt_file)

# global variables expected to be instantiated once at startup and then
# survive F5, disconnects, etc. Will however rerun on any python file change.
# Stray pandas (lost reference) do not seem to cause issues so far. 
# No idea how long they live tho.
pipe = queue.Queue()
server = Panda_client(opt, pipe)


def weak_dtype_convert(data: str):
    """float default; bool fallback 
    -> require all bool to fail float() coversion"""
    try:
        return float(data)
    except ValueError:
        return bool(data)


def request_all_data():
    if "all_data" in st.session_state.keys():
        st.session_state.all_data += 1
    else:
        st.session_state.all_data = 0
    global gurke
    gurke += 1
    st.session_state.gurke = gurke

    global full_frame, current_errors
    big_panda = server.request_datapoints(0, gurke)
    metadata: dict = big_panda["metadata"]
    csv_chonker: str = big_panda["csvstr"]

    current_errors = metadata["errors"]

    df = pd.read_csv(io.StringIO(csv_chonker), 
        names=["timestamp", "parameter", "value", "tags"],
        dtype={"timestamp": np.int64, "parameter": str, "tags": str},
        converters={"value":weak_dtype_convert}
    )

    # todo: validate start / stop timestamp, request missing if applicable

    full_frame = df
    derive_dataframes(df)
    return


# @st.cache
def derive_dataframes(df: pd.DataFrame):
    global main_cols

    batt_details_cols  = {
        "pcb_temp", 
        "min_voltage", "max_voltage", 
        "min_temp", "max_temp"}
    for s in df["parameter"].values:
        if s.startswith("cell_") or s.startswith("cmu_") or s.startswith("bmu_"):
            batt_details_cols.add(s)
    lg.debug(f"moving these columns to batt_details {batt_details_cols}")
    batt_details = df[df["parameter"].isin(batt_details_cols)]

    errors_cols = {
        "can_power_low",
        "measurement_untrusted", "err_cont_12v_supp",
        "err_cont_1_driver", "err_cont_2_driver", "err_cont_3_driver",
        "pack_isolation_fail", "contactor_stuck", "soc_invalid", "unexpected_cell",
        "vehicle_comm_timeout",
    }
    lg.debug(f"moving these columns to errors {errors_cols}")
    errors = df[df["parameter"].isin(errors_cols)]

    # long -> wide format
    for ffs in batt_details_cols:
        df = df[df["parameter"] != ffs]
        # need to drop dupes only differentiated by tags
    df = df.pivot(columns="parameter", index="timestamp", values="value")
    # todo: aggregate instad of just reshape?
    # (not done atm to catch systematic errors by assuming 1ms resolution is
    # good enough to be unique index per parameter.)
    # todo: downsample (LPF)?

    # cleanup
    # df = df.drop(labels=batt_details_cols, axis="columns")
    df = df.drop(labels=errors_cols,       axis="columns")
    for col in df:
        try:
            ds = df[col]
            df[col] = df[col].astype(type(ds[ds.first_valid_index()]))
        except KeyError:
            lg.debug(f"column '{col}' only contains null values.")

    main_cols = df[-10000:]
