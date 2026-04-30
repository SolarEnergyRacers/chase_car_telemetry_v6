

from   datetime import datetime, timedelta
from   pathlib import Path
import json
import logging as lg
import serial
import time

REPO = Path(__file__).parent.parent.parent


realtime = 0  # 1 = send data at recorded rate, 0 = send timestamp updates instead
# ser_data = "2024-09-21_05-33-10_ser_comm.csv"          # playback data (in REPO/data)
ser_data = "2024-09-21_06-21-18_ser_comm.csv"           # playback data (in REPO/data)
start_time = datetime.strptime("06:30:15", "%H:%M:%S")  # delta relative to UTF timestamp offset info
stop_time  = datetime.strptime("09:38:30", "%H:%M:%S")  # delta relative to UTF timestamp offset info


def get_metadata(ser_data: str):
    with open(REPO / "data/meta_info.json", "r") as opt_file:
        data_opt = json.load(opt_file)
    if ser_data in data_opt:
        sep = data_opt[ser_data].get("sep", ';')
        cols = data_opt[ser_data]["cols"]
    else:
        lg.warn(f"{ser_data} not found in meta_info.json, using defaults...")
        sep = ";"
        cols = [["timestamp", "%Y-%m-%d_%H:%M:%S.f", 0]]
    
    if cols[0][0] != "timestamp":
        msg  = f"Only data with timestamp in 1st column is supported "
        msg += f"(got {cols[0][0]})"
        raise KeyError(msg)
    if cols[1][0] != "canframe":
        msg  = f"Only data with canframe in 2nd column is supported "
        msg += f"(got {cols[1][0]})"
        raise KeyError(msg)
    return sep, (cols[0][1], cols[0][2])


def main():
    # read config file
    with open("options.json", "r") as opt_file:
        opt = json.load(opt_file)

    # set console logging level
    if opt["app"]["debug"]:
        lg.root.setLevel(lg.DEBUG)
    else:
        lg.root.setLevel(lg.INFO)

    time_type = "at real time" if realtime else "with timetamps"
    lg.info(f"playback file {ser_data} {time_type}.")

    # datetime corrections
    sep, timeinfo = get_metadata(ser_data)
    epoch = datetime(1970, 1, 1) - datetime(1900,1,1)
    ts_data_offset = epoch + timedelta(seconds = timeinfo[1])
    
    start_ts = start_time + ts_data_offset
    stop_ts  = stop_time  + ts_data_offset

    before = -1
    skipcount = 0
    sendcount = 0
    with serial.Serial(opt["serial"]["com"], timeout=0.1) as intf:
        lg.info(f"opened port {opt['serial']['com']}")
        with open(REPO / "data" / ser_data, 'r') as file:
            lg.info(f"reading data from {ser_data}")
            firstline = 1
            for line in file.readlines():
                ts, data, *_ = line.split(sep)
                ts = datetime.strptime(ts, timeinfo[0]) + ts_data_offset
                now = ts.timestamp()
                if (now - before) < - 3600:  # fix 24h wraparound issue
                    lg.debug(f"24H wraparound correction done at line {line}")
                    ts_data_offset += timedelta(hours=24)

                if firstline:
                    firstline = 0
                    rt_offset = datetime.now() - start_ts
                    msg  = f"first line = '{line.rstrip()}', resolved as: "
                    msg += f"ts = {ts} ({ts.strftime("%Y-%m-%d %H:%M:%S.%f")}"
                    msg += f"), data = '{data}', rt_offset = {rt_offset}."
                    lg.debug(msg)

                if ts < start_ts:
                    skipcount += 1
                    continue
                if ts > stop_ts:
                    break

                if realtime:
                    dt = (ts - (datetime.now() - rt_offset)).total_seconds()
                    if dt > 3:
                        lg.debug(f"data blackout for {dt}s")
                    if dt > 0: time.sleep(dt)
                else:
                    addr = int(opt["CAN"]["fake_time"]["base_addr"], base=0)
                    addr |= 0xF800
                    faketime = int(now * 1000) # millisecond resolution
                    intf.write(addr.to_bytes(2) + faketime.to_bytes(8) + b"\n")
                    print(addr.to_bytes(2) + faketime.to_bytes(8) + b"\n")

                ints = [int(x, base=16) for x in data.split(' ')]
                intf.write(b"".join([x.to_bytes(1) for x in ints]))
                intf.flush()
                sendcount += 1
                # if sendcount >= 1:
                #     break

                before = now

    lg.info(f"closed port {opt['serial']['com']}")
    lg.debug(f"skipped {skipcount} lines before start")
    lg.debug(f"sent {sendcount} lines")
    return  # main




if __name__ == "__main__":
    main()
