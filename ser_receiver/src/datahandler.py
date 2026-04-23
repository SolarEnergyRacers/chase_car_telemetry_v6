from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QObject

import threading
import queue
from multiprocessing.connection import Listener

# from influxdb_client import InfluxDBClient, Point, WritePrecision
import requests
import logging as lg

from datainput import DataInput, CANFrame
from datapoint import DataPoint
from panda_server import Panda_server


x = 0  # responder demo counter

# Inherits QObject so it can emit signals
class DataHandler(QObject):

    recBattInfo = QtCore.pyqtSignal(DataPoint)
    recSpeedInfo = QtCore.pyqtSignal(DataPoint)
    recCellInfo = QtCore.pyqtSignal(list)
    recBattErrors = QtCore.pyqtSignal(list)
    recPVInfo = QtCore.pyqtSignal(DataPoint)
    recConfirm = QtCore.pyqtSignal(DataPoint)


    def __init__(self, opt: dict):
        QObject.__init__(self)
        self.opt = opt

        try:
            self.panda_queue = queue.Queue(maxsize=1024)  # arbitrary maxsize, just prevent memory leak if stalled
            self.fullQ_err_logged = 0
            def demo_response(timestamps: dict):
                global x
                answer = {"timestamps": timestamps, "data": (x := x+1)}
                print(f"demo response for {timestamps} is {answer}")
                return answer
            self.server = Panda_server(opt, demo_response)


        except Exception as err:
            lg.error(err)
            lg.error("Panda socket creation failed.")
    
    def shutdown_panda(self):
        return self.server.shutdown()

    def handle_new_input(self, input_val):
        di = CANFrame(self.opt, input_val)
        self.uploadDataInput(di)

    def uploadDataInput(self, di: DataInput):
        lg.debug("uploading Datapoints")
        self.uploadDatapoints(di.asDatapoints())

    def uploadDatapoints(self, datapoints: list[DataPoint]):
        emitted = False

        for dp in datapoints:
            lg.debug(dp.__dict__)
            self.server.publish(dp.__dict__)
            if dp.measurement == "speed":
                self.recSpeedInfo.emit(dp)
            elif dp.measurement == "batt_volt":
                self.recBattInfo.emit(dp)
            elif dp.measurement == "min_voltage" and not emitted:
                self.recCellInfo.emit(datapoints)
                emitted = True
            elif dp.measurement == "cell_over_voltage" and not emitted:
                self.recBattErrors.emit(datapoints)
                emitted = True
            elif dp.measurement == "mppt_out_current":
                self.recPVInfo.emit(dp)
            elif dp.measurement == "driver_confirm":
                self.recConfirm.emit(dp)