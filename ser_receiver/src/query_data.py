

import logging as lg
from   pathlib import Path


def data_from_timestamps(ts_dict: dict) -> dict:
    """require input: {"start_t": <timestamp>, "stop_t": <timestamp>}.
    Scans csv files in data/datapoints to send all data in requrested 
    timeframe.
    """
    try:
        start = int(ts_dict["start_t"])
        stop  = int(ts_dict["stop_t"])
    except Exception as e:
        lg.error(f"data_from_timestamps() called with  invalid parameter: "
            f"{type(e)} {e}")
        return {"error": f"{type(e)} {e}"}
    
    csv_dir = Path(__file__).parent.parent.parent / "data/datapoints"
    csv_files = [fp for fp in csv_dir.iterdir() if 
        fp.is_file() and fp.suffix == ".csv"]

    metadata = {
        "req start": start,
        "req stop": stop,
        "errors": [],
    }
    csvstr = ""

    if not(start == 0 and stop == 0):
        metadata["errors"].append("start/stop params not yet supported")

    n_lines = 0
    for f in csv_files:
        # filter for actually targeted files
        try:
            name, yyyymmdd, hour = f.stem.split('_')
            if not name == "data":
                continue
        except Exception:
            continue

        # todo: resolve start, stop and only read relevant files

        # todo: length check -> terminate after some MB of data and
        # put info of last timestamp into metadata. Client must follow up
        # with another request if they actually want all the data

        # todo: gzip on csvstr (got 15MB csv down to 1.2MB)

        with open(f, 'r') as file:
            lines = file.readlines()
            csvstr += ''.join(lines)
            n_lines += len(lines)

    metadata["n_lines"] = n_lines
    metadata["csvstr_size"] = len(csvstr)  
    # ^ in python chars -> ca. nr. of Bytes, need actual encoding to be exact

    return {"metadata": metadata, "csvstr": csvstr}
