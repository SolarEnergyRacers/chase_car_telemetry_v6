import queue
import logging as lg
import serial
import traceback

from serial import SerialException

from PyQt5 import QtGui, QtCore
import time


class SerialHandler(QtCore.QThread):
    new_input = QtCore.pyqtSignal(object)
    update_status = QtCore.pyqtSignal(bool)

    def __init__(self, opt: dict):
        QtCore.QThread.__init__(self)
        self.opt = opt
        self.buffer = bytearray()

        self._com = None
        self.com_available = False
        self._connect_serial()

    def run(self):
        while True:
            try:
                if self.com_available and self._com.inWaiting() > 0:
                    input_val = self._com.read_until()
                    lg.debug(f"Serial input: {input_val} length: {len(input_val)}")
                    if self.opt["comm"]["hex_string"]:
                        self.handle_input_hex(input_val)
                    else:
                        self.handle_input_bytes(input_val)

                elif not self.com_available:
                    self._connect_serial()
                    self.usleep(int(1 * 10e5)) # wait one second before trying to reconnect to serial port

            except serial.SerialException:
                self._com.close()
                self.com_available = False
                self.update_status.emit(self.com_available)
                lg.warning("Serial: SerialException")
                traceback.print_exc()
            except TypeError:
                if self._com is not None:
                    self._com.close()
                self.com_available = False
                self.update_status.emit(self.com_available)
                lg.warning("Serial: TypeError")
                traceback.print_exc()
            except AttributeError:
                self.com_available = False
                self.update_status.emit(self.com_available)
                lg.warning("Serial: AttributeError")
                traceback.print_exc()
                #this is caused if the serial cannot be opened when program starts


    def send(self, out_message):
        if self.com_available:
            self._com.write(out_message.encode("ascii", "ignore"))

    def handle_input_bytes(self, input_val):

        if input_val[0] >= 0xF8 and len(input_val) == 11:
            self.new_input.emit(input_val)
            self.buffer = bytearray()
        elif len(input_val) < 11:
            if len(self.buffer) + len(input_val) == 11 and input_val[-1] == 13: #13 is \n (LF), maybe change to 10 (CR)
                self.new_input.emit(self.buffer + input_val)
                self.buffer = bytearray()
            elif len(self.buffer) + len(input_val) < 11 and len(self.buffer) > 0:
                self.buffer += input_val
            elif len(self.buffer) == 0 and input_val[0] >= 0xF8:
                self.buffer = input_val
            else:
                lg.warning(f"A. Couldn't handle partial input {input_val}")
        else:
            lg.warning(f"B. Couldn't handle partial input {input_val}")

    #not used
    def handle_input_hex(self, input_val):
        if len(input_val) == 21:
            self.new_input.emit(input_val)
            # implement buffer if needed

    def _connect_serial(self):
        try:
            lg.info("Trying to open Serial...")
            self._com = serial.Serial(self.opt["serial"]["com"], self.opt["serial"]["baud"])
            self._com.timeout = 1
            self.com_available = True
            lg.info("Opened Serial Connection")
        except SerialException:
            lg.warning("Failed to open Serial Connection")
            self.com_available = False
        finally:
            self.update_status.emit(self.com_available)




