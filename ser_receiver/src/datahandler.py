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


def panda_socket(panda_queue: queue.Queue):
    # basic implementation for 1 outgoing connection
    listener = Listener(('localhost', 6000), authkey=b"otp tbd")
    running = True
    while running:
        conn = listener.accept()
        while True:
            item = panda_queue.get()
            try:
                conn.send(item)
            except BrokenPipeError:
                lg.warn("panda pipe broken; attempt to reconnect")
                break
            except KeyboardInterrupt:
                lg.info("intercepted KeyboardInterrupt, closing panda pipe")
                conn.close()
                running = 0
                break


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
            self.panda_queue = queue.Queue()
            t = threading.Thread(target=panda_socket, args=(self.panda_queue,), daemon=True)
            t.start()

        except Exception as err:
            lg.error(err)
            lg.error("Panda socket creation failed.")

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
            if not self.opt["influx"]["no_db"]:
                #self.write_api.write(bucket=self.opt["influx"]["bucket"], org=self.opt["influx"]["org"], record = dp.__dict__)

                p = Point(dp.measurement).field("value", dp.fields["value"])

                for tag in dp.tags:
                    p = p.tag(tag, dp.tags[tag])

                self.write_api.write(bucket=self.opt["influx"]["bucket"], org=self.opt["influx"]["org"],  record=p)


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