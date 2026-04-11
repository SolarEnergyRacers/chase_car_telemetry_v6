from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QObject

from influxdb_client import InfluxDBClient, Point, WritePrecision
import requests
import logging as lg

from datainput import DataInput, CANFrame
from datapoint import DataPoint

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
            if not self.opt["influx"]["no_db"]:
                self.client = InfluxDBClient(url="http://"+opt["influx"]["host"]+":"+str(opt["influx"]["port"]),
                                             token=opt["influx"]["token"],
                                             org=opt["influx"]["org"])
                self.write_api = self.client.write_api()
                self.available = True

        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout, ConnectionRefusedError) as err:
            self.available = False
            lg.error(err)
            lg.error("Connection to Influx DB failed")
            lg.error("host=" + opt["influx"]["host"])
            lg.error("port=" + str(opt["influx"]["port"]))

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