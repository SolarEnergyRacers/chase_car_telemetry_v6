from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import pyqtSlot

import logging as lg
from datetime import datetime

from datapoint import DataPoint
from ui_mainwindow import Ui_mainWindow

class MainWindow(QtWidgets.QMainWindow, Ui_mainWindow):
    send = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent=parent)
        self.setupUi(self)

        log_file_name = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S') + "_ser_comm.csv"
        self.log_file = open(log_file_name, 'w+')

        self.btnSend.clicked.connect(self.on_click_send)
        self.leditInput.returnPressed.connect(self.on_click_send)

        self.btnMsgBox.clicked.connect(self.on_click_msg_box)
        self.btnMsgCharge.clicked.connect(self.on_click_msg_charge)
        self.btnMsgDriverchange.clicked.connect(self.on_click_msg_driverchange)
        #self.btnSave.clicked.connect(self.on_click_save)
        #self.btnSave.setVisible(False)

        self.plainTextEdit.setPlainText("")

    def closeEvent(self, a0: QtGui.QCloseEvent) -> None:
        self.log_file.close()

    def on_click_send(self):
        txt = self.leditInput.text()
        if self.cbAddNL.isChecked():
            txt += chr(13)

        curr_time = datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]
        self.plainTextEdit.appendPlainText(f'{curr_time} >> {txt}')
        self.leditInput.setText('')

        #if txt[0] == ':' or txt[0] == '!':
            #self.lblLastSent.display("last Message sent: " +  datetime.utcnow().strftime('%H:%M:%S.%f')[:-3])

        self.send.emit(txt)

    def on_click_msg_box(self):
        txt = "!BOX"
        curr_time = datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]
        self.plainTextEdit.appendPlainText(f'{curr_time} >> {txt}')
        self.send.emit(txt)

    def on_click_msg_driverchange(self):
        txt = "!Driver Change"
        curr_time = datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]
        self.plainTextEdit.appendPlainText(f'{curr_time} >> {txt}')
        self.send.emit(txt)

    def on_click_msg_charge(self):
        txt = "!Charge"
        curr_time = datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]
        self.plainTextEdit.appendPlainText(f'{curr_time} >> {txt}')
        self.send.emit(txt)

    def on_click_save(self):
        print("u clicked save!! but it didn't do nuthin")
        #todo
        pass

    def handle_new_input(self, data):
        lg.debug(f'gui received: {data.hex(" ")}')

        curr_time = datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]
        addr = int.from_bytes(data[0:2], 'big') & 0x7FF

        self.plainTextEdit.appendPlainText(f'{curr_time} << {data.hex(" ")} addr: {hex(addr)}')

        try:
            self.log_file.write(f'{curr_time};{data.hex(" ")};{hex(addr)}\n')
        except:
            lg.error("Couldn't write to logfile!")

    def on_update_com(self, available):
        if available:
            self.lblCommAvailable.setText("COM available: TRUE")
        else:
            self.lblCommAvailable.setText("COM available: FALSE")

    def update_batt_info(self, data: DataPoint):
        self.lcdUBatt.display(data.fields["value"])

    def update_speed(self, data: DataPoint):
        self.lcdSpeed.display(data.fields["value"])

    def update_cell_info(self, data: list[DataPoint]):
        for dp in data:
            if dp.measurement == "min_voltage":
                self.lcdMinVoltage.display(f"{dp.fields["value"]:7.3f}")
            elif dp.measurement == "max_voltage":
                self.lcdMaxVoltage.display(f"{dp.fields["value"]:7.3f}")

    def update_errors(self, data: list[DataPoint]):
        self.lstErrors.clear()

        for dp in data:
            self.lstErrors.addItem(str(dp.fields["value"]) + ": " + dp.measurement)

    def update_pv_info(self, data: DataPoint):
        self.lcdIPV.display(data.fields["value"])

    def update_confirm(self, data: DataPoint):
        if data.fields["value"]:
            self.lblLastConfirm.setText("last received confirm: " + datetime.utcnow().strftime('%H:%M:%S.%f')[:-3])
