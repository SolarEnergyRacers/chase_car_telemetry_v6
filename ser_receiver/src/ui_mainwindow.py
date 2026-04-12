from PyQt5.QtCore import QCoreApplication, QMetaObject, QRect, Qt
from PyQt5.QtGui import QFont, QBrush, QColor
from PyQt5.QtWidgets import *

# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'mainWindowoCbUjD.ui'
##
## Created by: Qt User Interface Compiler version 5.15.2
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################


class Ui_mainWindow(object):
    def setupUi(self, mainWindow):
        if not mainWindow.objectName():
            mainWindow.setObjectName(u"mainWindow")
        mainWindow.resize(945, 819)
        self.centralwidget = QWidget(mainWindow)
        self.centralwidget.setObjectName(u"centralwidget")
        self.verticalLayout = QVBoxLayout(self.centralwidget)
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.horizontalLayout = QHBoxLayout()
        self.horizontalLayout.setObjectName(u"horizontalLayout")
        self.lblCommAvailable = QLabel(self.centralwidget)
        self.lblCommAvailable.setObjectName(u"lblCommAvailable")

        self.horizontalLayout.addWidget(self.lblCommAvailable)

        self.horizontalLayout.setStretch(0, 4)

        self.verticalLayout.addLayout(self.horizontalLayout)

        self.horizontalLayout_3 = QHBoxLayout()
        self.horizontalLayout_3.setObjectName(u"horizontalLayout_3")
        self.plainTextEdit = QPlainTextEdit(self.centralwidget)
        self.plainTextEdit.setObjectName(u"plainTextEdit")
        font = QFont()
        font.setFamily(u"Consolas")
        self.plainTextEdit.setFont(font)
        self.plainTextEdit.setUndoRedoEnabled(False)
        self.plainTextEdit.setReadOnly(True)

        self.horizontalLayout_3.addWidget(self.plainTextEdit)

        self.verticalLayout_2 = QVBoxLayout()
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_3 = QVBoxLayout()
        self.verticalLayout_3.setObjectName(u"verticalLayout_3")
        self.lblLastReceived = QLabel(self.centralwidget)
        self.lblLastReceived.setObjectName(u"lblLastReceived")
        font1 = QFont()
        font1.setPointSize(12)
        self.lblLastReceived.setFont(font1)

        self.verticalLayout_3.addWidget(self.lblLastReceived)

        self.lblLastSent = QLabel(self.centralwidget)
        self.lblLastSent.setObjectName(u"lblLastSent")
        self.lblLastSent.setFont(font1)

        self.verticalLayout_3.addWidget(self.lblLastSent)

        self.lblLastConfirm = QLabel(self.centralwidget)
        self.lblLastConfirm.setObjectName(u"lblLastConfirm")
        self.lblLastConfirm.setFont(font1)

        self.verticalLayout_3.addWidget(self.lblLastConfirm)


        self.verticalLayout_2.addLayout(self.verticalLayout_3)

        self.horizontalLayout_5 = QHBoxLayout()
        self.horizontalLayout_5.setObjectName(u"horizontalLayout_5")
        self.label = QLabel(self.centralwidget)
        self.label.setObjectName(u"label")
        font2 = QFont()
        font2.setPointSize(16)
        self.label.setFont(font2)

        self.horizontalLayout_5.addWidget(self.label)

        self.lcdUBatt = QLCDNumber(self.centralwidget)
        self.lcdUBatt.setObjectName(u"lcdUBatt")
        font3 = QFont()
        font3.setFamily(u"MS Shell Dlg 2")
        self.lcdUBatt.setFont(font3)
        self.lcdUBatt.setAutoFillBackground(False)
        self.lcdUBatt.setStyleSheet(u"")
        self.lcdUBatt.setSmallDecimalPoint(False)
        self.lcdUBatt.setProperty("intValue", 0)

        self.horizontalLayout_5.addWidget(self.lcdUBatt)


        self.verticalLayout_2.addLayout(self.horizontalLayout_5)

        self.horizontalLayout_8 = QHBoxLayout()
        self.horizontalLayout_8.setObjectName(u"horizontalLayout_8")
        self.label_3 = QLabel(self.centralwidget)
        self.label_3.setObjectName(u"label_3")
        self.label_3.setFont(font2)

        self.horizontalLayout_8.addWidget(self.label_3)

        self.lcdMinVoltage = QLCDNumber(self.centralwidget)
        self.lcdMinVoltage.setObjectName(u"lcdMinVoltage")
        self.lcdMinVoltage.setStyleSheet(u"")

        self.horizontalLayout_8.addWidget(self.lcdMinVoltage)


        self.verticalLayout_2.addLayout(self.horizontalLayout_8)

        self.horizontalLayout_9 = QHBoxLayout()
        self.horizontalLayout_9.setObjectName(u"horizontalLayout_9")
        self.label_4 = QLabel(self.centralwidget)
        self.label_4.setObjectName(u"label_4")
        self.label_4.setFont(font2)

        self.horizontalLayout_9.addWidget(self.label_4)

        self.lcdMaxVoltage = QLCDNumber(self.centralwidget)
        self.lcdMaxVoltage.setObjectName(u"lcdMaxVoltage")
        self.lcdMaxVoltage.setStyleSheet(u"")

        self.horizontalLayout_9.addWidget(self.lcdMaxVoltage)


        self.verticalLayout_2.addLayout(self.horizontalLayout_9)

        self.horizontalLayout_6 = QHBoxLayout()
        self.horizontalLayout_6.setObjectName(u"horizontalLayout_6")
        self.label_6 = QLabel(self.centralwidget)
        self.label_6.setObjectName(u"label_6")
        self.label_6.setFont(font2)

        self.horizontalLayout_6.addWidget(self.label_6)

        self.lcdIPV = QLCDNumber(self.centralwidget)
        self.lcdIPV.setObjectName(u"lcdIPV")
        self.lcdIPV.setStyleSheet(u"")

        self.horizontalLayout_6.addWidget(self.lcdIPV)


        self.verticalLayout_2.addLayout(self.horizontalLayout_6)

        self.horizontalLayout_7 = QHBoxLayout()
        self.horizontalLayout_7.setObjectName(u"horizontalLayout_7")
        self.label_2 = QLabel(self.centralwidget)
        self.label_2.setObjectName(u"label_2")
        self.label_2.setFont(font2)

        self.horizontalLayout_7.addWidget(self.label_2)

        self.lcdSpeed = QLCDNumber(self.centralwidget)
        self.lcdSpeed.setObjectName(u"lcdSpeed")
        self.lcdSpeed.setStyleSheet(u"")

        self.horizontalLayout_7.addWidget(self.lcdSpeed)


        self.verticalLayout_2.addLayout(self.horizontalLayout_7)

        self.label_7 = QLabel(self.centralwidget)
        self.label_7.setObjectName(u"label_7")
        font4 = QFont()
        font4.setPointSize(16)
        font4.setBold(True)
        font4.setItalic(False)
        font4.setWeight(75)
        self.label_7.setFont(font4)

        self.verticalLayout_2.addWidget(self.label_7)

        self.horizontalLayout_4 = QHBoxLayout()
        self.horizontalLayout_4.setObjectName(u"horizontalLayout_4")
        self.lstErrors = QListWidget(self.centralwidget)
        QListWidgetItem(self.lstErrors)
        QListWidgetItem(self.lstErrors)
        brush = QBrush(QColor(241, 40, 0, 255))
        brush.setStyle(Qt.NoBrush)
        __qlistwidgetitem = QListWidgetItem(self.lstErrors)
        __qlistwidgetitem.setForeground(brush);
        self.lstErrors.setObjectName(u"lstErrors")

        self.horizontalLayout_4.addWidget(self.lstErrors)


        self.verticalLayout_2.addLayout(self.horizontalLayout_4)

        self.verticalLayout_2.setStretch(0, 1)
        self.verticalLayout_2.setStretch(1, 1)
        self.verticalLayout_2.setStretch(2, 1)
        self.verticalLayout_2.setStretch(3, 1)
        self.verticalLayout_2.setStretch(4, 1)
        self.verticalLayout_2.setStretch(5, 1)
        self.verticalLayout_2.setStretch(6, 1)
        self.verticalLayout_2.setStretch(7, 7)

        self.horizontalLayout_3.addLayout(self.verticalLayout_2)

        self.horizontalLayout_3.setStretch(0, 1)
        self.horizontalLayout_3.setStretch(1, 1)

        self.verticalLayout.addLayout(self.horizontalLayout_3)

        self.horizontalLayout_2 = QHBoxLayout()
        self.horizontalLayout_2.setObjectName(u"horizontalLayout_2")
        self.leditInput = QLineEdit(self.centralwidget)
        self.leditInput.setObjectName(u"leditInput")

        self.horizontalLayout_2.addWidget(self.leditInput)

        self.cbAddNL = QCheckBox(self.centralwidget)
        self.cbAddNL.setObjectName(u"cbAddNL")
        self.cbAddNL.setChecked(True)

        self.horizontalLayout_2.addWidget(self.cbAddNL)

        self.btnSend = QPushButton(self.centralwidget)
        self.btnSend.setObjectName(u"btnSend")

        self.horizontalLayout_2.addWidget(self.btnSend)

        self.line = QFrame(self.centralwidget)
        self.line.setObjectName(u"line")
        self.line.setStyleSheet(u"background-color: #505050;")
        self.line.setLineWidth(3)
        self.line.setFrameShape(QFrame.VLine)
        self.line.setFrameShadow(QFrame.Sunken)

        self.horizontalLayout_2.addWidget(self.line)

        self.btnMsgBox = QPushButton(self.centralwidget)
        self.btnMsgBox.setObjectName(u"btnMsgBox")

        self.horizontalLayout_2.addWidget(self.btnMsgBox)

        self.btnMsgDriverchange = QPushButton(self.centralwidget)
        self.btnMsgDriverchange.setObjectName(u"btnMsgDriverchange")

        self.horizontalLayout_2.addWidget(self.btnMsgDriverchange)

        self.btnMsgCharge = QPushButton(self.centralwidget)
        self.btnMsgCharge.setObjectName(u"btnMsgCharge")

        self.horizontalLayout_2.addWidget(self.btnMsgCharge)

        self.horizontalLayout_2.setStretch(0, 10)
        self.horizontalLayout_2.setStretch(1, 1)
        self.horizontalLayout_2.setStretch(2, 1)

        self.verticalLayout.addLayout(self.horizontalLayout_2)

        mainWindow.setCentralWidget(self.centralwidget)
        self.menubar = QMenuBar(mainWindow)
        self.menubar.setObjectName(u"menubar")
        self.menubar.setGeometry(QRect(0, 0, 945, 21))
        mainWindow.setMenuBar(self.menubar)
        self.statusbar = QStatusBar(mainWindow)
        self.statusbar.setObjectName(u"statusbar")
        mainWindow.setStatusBar(self.statusbar)

        self.retranslateUi(mainWindow)

        QMetaObject.connectSlotsByName(mainWindow)
    # setupUi

    def retranslateUi(self, mainWindow):
        mainWindow.setWindowTitle(QCoreApplication.translate("mainWindow", u"SER Telemetry", None))
        self.lblCommAvailable.setText(QCoreApplication.translate("mainWindow", u"Comm Available", None))
        self.plainTextEdit.setPlainText(QCoreApplication.translate("mainWindow", u"19:05.43  < 0xFE 0xA1 0x32 0x21\n"
"19:06.21  > r:g", None))
        self.lblLastReceived.setText(QCoreApplication.translate("mainWindow", u"last Frame received:", None))
        self.lblLastSent.setText(QCoreApplication.translate("mainWindow", u"last Message sent:", None))
        self.lblLastConfirm.setText(QCoreApplication.translate("mainWindow", u"last Confirm received:", None))
        self.label.setText(QCoreApplication.translate("mainWindow", u"Battery Voltage (V)", None))
        self.label_3.setText(QCoreApplication.translate("mainWindow", u"min. Cell Voltage (V)", None))
        self.label_4.setText(QCoreApplication.translate("mainWindow", u"max. Cell Voltage (V)", None))
        self.label_6.setText(QCoreApplication.translate("mainWindow", u"PV Current (A)", None))
        self.label_2.setText(QCoreApplication.translate("mainWindow", u"Speed (km/h)", None))
        self.label_7.setText(QCoreApplication.translate("mainWindow", u"Battery Errors:", None))

        __sortingEnabled = self.lstErrors.isSortingEnabled()
        self.lstErrors.setSortingEnabled(False)
        ___qlistwidgetitem = self.lstErrors.item(0)
        ___qlistwidgetitem.setText(QCoreApplication.translate("mainWindow", u"Error 1", None));
        ___qlistwidgetitem1 = self.lstErrors.item(1)
        ___qlistwidgetitem1.setText(QCoreApplication.translate("mainWindow", u"Error 2", None));
        ___qlistwidgetitem2 = self.lstErrors.item(2)
        ___qlistwidgetitem2.setText(QCoreApplication.translate("mainWindow", u"Error 3", None));
        self.lstErrors.setSortingEnabled(__sortingEnabled)

        self.cbAddNL.setText(QCoreApplication.translate("mainWindow", u"add \\n", None))
        self.btnSend.setText(QCoreApplication.translate("mainWindow", u"Send", None))
        self.btnMsgBox.setText(QCoreApplication.translate("mainWindow", u"Box", None))
        self.btnMsgDriverchange.setText(QCoreApplication.translate("mainWindow", u"Driver Change", None))
        self.btnMsgCharge.setText(QCoreApplication.translate("mainWindow", u"Charge", None))
    # retranslateUi

