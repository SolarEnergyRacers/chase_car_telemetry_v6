import json

import logging as lg
import os
import sys

from PyQt5 import QtWidgets

from serialhandler import SerialHandler
from datahandler import DataHandler
from mainwindow import MainWindow

options_file = "options.json"
if not os.path.exists(options_file):
    options_file = os.path.join(os.path.dirname(__file__), "..", options_file)

def main():
    default_opt = {
        "app": {
            "debug": False,
            "baudrate": 115200,
            "port": "COM3",
            "timeout": 1,
        },
        "data": {
            "save_path": "./data",
            "save_interval": 5,
        },
    }
    # read config file

    try:
        with open(options_file, "r") as opt_file:
            opt = json.load(opt_file)
    except FileNotFoundError:
        print(f"Config file {options_file} not found, creating default config file.")
        with open(options_file, "w") as opt_file:
            json.dump(default_opt, opt_file, indent=4)

    # set console logging level
    if opt["app"]["debug"]:
        lg.root.setLevel(lg.DEBUG)
    else:
        lg.root.setLevel(lg.INFO)

    sh = SerialHandler(opt)
    dh = DataHandler(opt)

    app = QtWidgets.QApplication(sys.argv)
    mw = MainWindow()

    sh.new_input.connect(mw.handle_new_input)
    sh.new_input.connect(dh.handle_new_input)

    dh.recSpeedInfo.connect(mw.update_speed)
    dh.recBattInfo.connect(mw.update_batt_info)
    dh.recCellInfo.connect(mw.update_cell_info)
    dh.recBattErrors.connect(mw.update_errors)
    dh.recConfirm.connect(mw.update_confirm)
    dh.recPVInfo.connect(mw.update_pv_info)

    sh.update_status.connect(mw.on_update_com)
    mw.send.connect(sh.send)

    mw.show()
    sh.start()
    ret = app.exec_()
    dh.shutdown_panda()
    sys.exit(ret)


if __name__ == "__main__":
    main()
