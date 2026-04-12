import json

import logging as lg
import sys

from PyQt5 import QtWidgets

from serialhandler import SerialHandler
from datahandler import DataHandler
from mainwindow import MainWindow

if __name__ == "__main__":

    # read config file
    with open("options.json", "r") as opt_file:
        opt = json.load(opt_file)

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
    sys.exit(app.exec_())



