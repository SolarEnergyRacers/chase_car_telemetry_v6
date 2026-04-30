from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QObject

from   datetime import datetime
import io
import os
from   pathlib  import Path
import threading
import queue

import requests
import logging as lg

from datainput import DataInput, CANFrame
from datapoint import DataPoint
from panda_server import Panda_server
from query_data   import data_from_timestamps

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

        self.csvdir = Path(__file__).parent.parent.parent / "data/datapoints"
        if not self.csvdir.is_dir():
            self.csvdir.mkdir()
        if opt["CAN"]["debug_timestamp"]["enable"]:
            # create dead file and point write_csv() to initializer method
            self.__write_csv_true = self.write_csv
            self.write_csv = self.initial_faketime_setup
            self.csvfile = io.StringIO(); self.csvfile.close()
        else:
            now = datetime.now().strftime("%Y-%m-%d_%H")
            self.csvfile = open(self.csvdir / f"data_{now}.csv", 'a')
        self.csvfile.linecounter = 0
        self.csvfile.lock = threading.Lock()
        # ^ useful, but not magic - still need to actually validate

        def responder_func(timestamps: dict) -> dict:
            """Wrapper function before csv files are read:
            If possible, flush data to disk to ensure file reads are up to date
            """
            if self.csvfile.lock.acquire(timeout=1.0):
                try:
                    self.csvfile.flush()
                    os.fsync(self.csvfile)
                except Exception as e:
                    lg.warning("responder_func() failed to flush csv file.")
                finally:
                    self.csvfile.lock.release()
            return data_from_timestamps(timestamps)

        try:
            self.server = Panda_server(opt, responder_func)
        except Exception as err:
            lg.error(err)
            lg.error("Panda socket creation failed.")
    
    def shutdown_panda(self):
        if not self.csvfile.closed: 
            self.csvfile.close()  # don't care if locked, we're done.
        os.sync()
        return self.server.shutdown()

    def handle_new_input(self, input_val):
        di = CANFrame(self.opt, input_val)
        self.uploadDataInput(di)

    def uploadDataInput(self, di: DataInput):
        lg.debug("uploading Datapoints")
        self.uploadDatapoints(di.asDatapoints())

    def uploadDatapoints(self, datapoints: list[DataPoint]):
        emitted = False

        csv_ok = self.csvfile.lock.acquire(timeout=1.0)
        try:
            for dp in datapoints:
                lg.debug(dp.__dict__)
                self.server.publish(dp.__dict__)
                if csv_ok and self.write_csv(dp):
                    """lazy evaluation; write_csv() is gated by csv_ok"""
                else:
                    lg.error(f" csv error, dropping dp: {dp.__dict__}")
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
        finally:
            if csv_ok:
                self.csvfile.lock.release()

    def write_csv(self, dp: DataPoint):
        if not "value" in dp.fields:
            lg.warning(f"ignoring datapoint missing 'value' field: {dp}")
            return
        line  = f"{dp.time},{dp.measurement},"
        line += f"{dp.fields["value"]},"
        tags_sanitized = f"{dp.tags}".replace('"', "'")
        line += f'"{tags_sanitized}"\n'
        if not(self.csvfile.closed) and self.csvfile.writable:
            try:
                self.csvfile.write(line)
                self.csvfile.linecounter += 1
                if self.csvfile.linecounter > 10000:
                    # guarantee disk write every 10k lines (~ 4-5 min.)
                    self.csvfile.flush()
                    os.fsync(self.csvfile)
                    self.csvfile.linecounter = 0
                return True
            except Exception as e:
                lg.error(f"csv write: {type(e)} {e}")
                return False
        else:
            lg.error(f"csv file not writable")
        return False

    def initial_faketime_setup(self, dp: DataPoint):
        """wrapper for write_csv that sets file name based on timestamp of dp,
        then replaces write_csv() with the proper, unwrapped call.
        """
        now = dp.time / 1000.
        now = datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H")
        lg.debug(f"setup csv fake time: {dp.time} -> {now}.")

        linecounter = self.csvfile.linecounter
        lock        = self.csvfile.lock
        self.csvfile = open(self.csvdir / f"data_{now}.csv", 'a')
        self.csvfile.linecounter = 0
        self.csvfile.lock = lock
        if not linecounter == 0:
            lg.error(f"fake time csv setup noticed linecounter={linecounter} "
                "before first write, which is impossible")

        self.write_csv = self.__write_csv_true

        return self.write_csv(dp)
