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
                if not self.com_available:
                    self._connect_serial()
                    self.usleep(int(1 * 10e5)) 
                    # wait one second before trying to reconnect to serial port
                    continue

                input_val = self._com.read(11)  # 1 entry at a time
                if input_val:
                    lg.debug(f"Serial input: [{input_val.hex(" ")}] length: {len(input_val)}")
                    if self.opt["comm"]["hex_string"]:
                        self.handle_input_hex(input_val)
                    else:
                        self.handle_input_bytes(input_val)

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

    expect_invalid = 0
    lost_bytes = bytearray()
    # ^ bytes following a missed start charcter that are thus unparsable.
    # Accumulated for logging.
    def handle_input_bytes(self, input_val):
        # Rolling buffer; ideally handling one whole 11-Byte line at a time, 
        # but buffer partial and process as long as available before returning.
        # expect missing characters - bitflips are handled on link-layer (I hope)
        self.buffer += input_val
        idx = 0
        while idx < len(self.buffer) - 10:
            if self.buffer[idx] >= 0xF8:
                if self.lost_bytes:
                    lg.warning(f"received unparsable data: [{self.lost_bytes.hex(" ")}]")
                self.lost_bytes = bytearray()
                if self.buffer[idx+10] == ord('\n'):
                    # assume string starting with valid address and ending
                    # on \n is a proper package, no further check possible.
                    self.new_input.emit(self.buffer[idx:idx+11])
                    self.expect_invalid = 0
                    idx += 11
                    continue
                else:
                    lg.warning(f"received invalid package: [{self.buffer[idx:idx+11].hex(" ")}]")
                    self.expect_invalid = idx+10  # silence already logged bytes
                    idx += 1
                    continue
            else:
                if idx > self.expect_invalid:
                    self.lost_bytes += self.buffer[idx].to_bytes(1)
                idx += 1
        self.buffer = self.buffer[idx:]
        self.expect_invalid -= idx
        lg.debug(f"truncated buffer to {self.buffer} (lost counter = {self.lost_bytes})")

        # safeguard against memory leak (should not happen during legitimate connection)
        if len(self.buffer) > 1024:
            lg.error(f"received only garbage data for 100+ lines straight, discarding data - radio connection dead?")
            lg.debug(f"discarded garbage data: [{self.buffer[:-11].hex(" ")}]")
            self.buffer = self.buffer[-11:]
        if len(self.lost_bytes) > 1024:
            lg.error(f"received only garbage data for 100+ lines straight, discarding data - radio connection dead?")
            lg.debug(f"discarded garbage data: [{self.lost_bytes.hex(" ")}]")
            self.lost_bytes = bytearray()

    #not used
    def handle_input_hex(self, input_val):
        if len(input_val) == 21:
            self.new_input.emit(input_val)
            # implement buffer if needed

    def _connect_serial(self):
        try:
            lg.info(f"Trying to open Serial {self.opt["serial"]["com"]} at {self.opt["serial"]["baud"]} ...")
            self._com = serial.Serial(self.opt["serial"]["com"], self.opt["serial"]["baud"])
            self._com.timeout = 1
            self.com_available = True
            lg.info("Opened Serial Connection")
        except SerialException:
            lg.warning("Failed to open Serial Connection")
            self.com_available = False
        finally:
            self.update_status.emit(self.com_available)




