import math
import struct
import time
import logging as lg
from datapoint import DataPoint

fake_time = 0
def get_timestamp(opt: dict):
    global fake_time
    if opt["CAN"]["debug_timestamp"]["enable"]:
        return fake_time
    return int(time.time()*1000)

# Use as abstract class
class DataInput:
    def __init__(self):
        self.timestamp = 0
        pass

    def asDatapoints(self):
        raise NotImplementedError("Please Implement this method")

class CANFrame(DataInput):
    def __init__(self, opt: dict):
        DataInput.__init__(self)
        self.opt = opt
        self.timestamp = 0
        self.addr = 0
        self.data = 0

    def __init__(self, opt: dict, serial_bytes):
        DataInput.__init__(self)
        self.opt = opt
        self.timestamp = get_timestamp(opt)
        self.serial_bytes = serial_bytes

        if opt["comm"]["hex_string"]:
            serial_str = serial_bytes.decode("ascii")
            self.addr = int(serial_str[0:4], 16) & 0x7FF
            self.data = int(serial_str[4:20], 16)
        else:
            self.addr = int.from_bytes(serial_bytes[0:2], 'big') & 0x7FF
            self.data = int.from_bytes(serial_bytes[2:10], 'little')

    def get_data_i(self, length: int, signed: bool, index: int, little_endian: bool = True):
        raw_data = self.serial_bytes[2:10]
        num_bytes = length // 8
        offset = num_bytes * index

        if little_endian:
            res = int.from_bytes(raw_data[0+offset:num_bytes+offset], byteorder='little', signed=signed)
        else:
            res = int.from_bytes(raw_data[0+offset:num_bytes+offset], byteorder='big', signed=signed)

        return res

    def get_data_b(self, index: int):
        return (self.data & (0x1 << (index))) >> (index) == 1

    def get_data_f(self, index: int, little_endian: bool = False):
        raw = self.data >> (index * 32) & 0x00000000FFFFFFFF

        s = struct.pack('>L', raw)

        if little_endian:
            return struct.unpack('<f', s)[0]
        else:
            return struct.unpack('>f', s)[0]

    def isBMSFrame(self):
        return int(self.opt["CAN"]["BMS"]["base_addr"], 16) == (self.addr & 0xF00)

    def isMPPTFrame(self):
        return int(self.opt["CAN"]["MPPT"]["mppt1_id"], 16) == self.addr & 0xFF0

    def isACFrame(self):
        return int(self.opt["CAN"]["AC"]["base_addr"], 16) == (self.addr & 0xFF0)

    def isDCFrame(self):
        return int(self.opt["CAN"]["DC"]["base_addr"], 16) == (self.addr & 0xFF0)

    def isMCFrame(self):
        return int(self.opt["CAN"]["MC"]["base_addr"], 16) == (self.addr & 0xF00)

    def isDebugTimestamp(self):
        return (
            self.opt["CAN"]["debug_timestamp"]["enable"] 
            and int(self.opt["CAN"]["debug_timestamp"]["base_addr"], 16) == (self.addr & 0xFFF)
        )

    def asDatapoints(self):
        datapoints = []

        if self.isDebugTimestamp():
            global fake_time
            fake_time = self.get_data_i(64, True, 0, False)

        elif self.isBMSFrame():
            bms_baseaddr = int(self.opt["CAN"]["BMS"]["base_addr"], 16)
            if self.addr == bms_baseaddr:  # BMU Heartbeat/Serialnumber
                datapoints.append(DataPoint(
                    "bms_heartbeat",
                    {"bmu_id": self.get_data_i(32, False, 1)},
                    self.timestamp,
                    {"value": True}))

            elif self.addr & 0xFF <= 0x0C:  # CMU Status, cell data
                # CMU0 = 0x601, 0x602, 0x603 CMU1 = 0x604, 0x605 etc..
                cmu_num = math.floor(((self.addr & 0xFF) - 1) / 3)

                if self.addr & 0xFF in [0x01, 0x04, 0x07, 0x0A]:  # CMU Serial Number & Temperatures
                    heartbeat = DataPoint(
                        "cmu_heartbeat",
                        {"cmu_id": self.get_data_i(32, False, 0),
                        "cmu_num": cmu_num},
                        self.timestamp, {"value": True})

                    pt = DataPoint(
                        "pcb_temp",
                        {"cmu_num": cmu_num},
                        self.timestamp,
                        {"value": self.get_data_i(16, True, 2) / 10})

                    ct = DataPoint(
                        "cell_temp",
                        {"cmu_num": cmu_num},
                        self.timestamp,
                        {"value": self.get_data_i(16, True, 3) / 10})

                    datapoints.extend([heartbeat, pt, ct])

                elif self.addr & 0xFF in [0x02, 0x05, 0x08, 0x0B, 0x03, 0x06, 0x09, 0x0C]:# Voltages 1 & 2

                    index_offset = 0
                    if (self.addr & 0xFF) % 3 == 0:
                        index_offset = 4

                    for i in range(4):
                        cell_volt = DataPoint(
                            "cell_voltage",
                            {"cmu_num": cmu_num,
                               "cell_num": i + index_offset,
                               "cell_index": cmu_num * 8 + i + index_offset},
                            self.timestamp,
                            {"value": self.get_data_i(16, True, i) / 1000})

                        datapoints.append(cell_volt) # Cell
            elif self.addr & 0xFF == 0xF4:  # Pack SOC
                pass
                #datapoints.append(DataPoint("soc_ah", {}, self.timestamp, {"value": self.get_data_f(1)}))
                #datapoints.append(DataPoint("soc_perc", {}, self.timestamp, {"value": self.get_data_f(0)}))
            elif self.addr & 0xFF == 0xF5:  # Balance SOC (not transmitted)
                pass
                #datapoints.append(DataPoint("balance_ah", {}, self.timestamp, {"value": self.get_data_f(1)}))
                #datapoints.append(DataPoint("balance_perc", {}, self.timestamp, {"value": self.get_data_f(2)}))
            elif self.addr & 0xFF == 0xF6:  # Charger Control Info (not transmitted)
                pass
            elif self.addr & 0xFF == 0xF7:  # Precharge Status
                # contactors
                datapoints.append(DataPoint("err_cont_1_driver", {}, self.timestamp, {"value": self.get_data_b(0)}))
                datapoints.append(DataPoint("err_cont_2_driver", {}, self.timestamp, {"value": self.get_data_b(1)}))
                datapoints.append(DataPoint("output_cont_1_driver", {}, self.timestamp, {"value": self.get_data_b(3)}))
                datapoints.append(DataPoint("output_cont_2_driver", {}, self.timestamp, {"value": self.get_data_b(4)}))
                datapoints.append(DataPoint("err_cont_12v_supp", {}, self.timestamp, {"value": not self.get_data_b(5)}))
                datapoints.append(DataPoint("err_cont_3_driver", {}, self.timestamp, {"value": self.get_data_b(6)}))
                datapoints.append(DataPoint("output_cont_3_driver", {}, self.timestamp, {"value": self.get_data_b(7)}))

                # precharge state
                precharge_state = ""
                match self.get_data_i(8, False, 1):
                    case 0:
                        precharge_state = "error"
                    case 1:
                        precharge_state = "idle"
                    case 2:
                        precharge_state = "measure"
                    case 3:
                        precharge_state = "precharge"
                    case 4:
                        precharge_state = "run"
                    case 5:
                        precharge_state = "enable"

                datapoints.append(DataPoint("prec_state", {}, self.timestamp, {"value": precharge_state}))

                # contactor supply voltage
                datapoints.append(
                    DataPoint("cont_voltage", {}, self.timestamp, {"value": self.get_data_i(16, False, 1) / 1000}))

                datapoints.append(DataPoint("prec_timer_elaps", {}, self.timestamp, {"value": self.get_data_b(6 * 8)}))
                datapoints.append(DataPoint("prec_timer", {}, self.timestamp, {"value": self.get_data_i(8, False, 7)}))
            elif self.addr & 0xFF == 0xF8:  # min/max cell voltage
                min_cell_volt = DataPoint(
                    "min_voltage",
                    {"cmu_num": self.get_data_i(8, False, 4),
                     "cell_num": self.get_data_i(8, False, 5)},
                    self.timestamp,
                    {"value": self.get_data_i(16, False, 0) / 1000})

                datapoints.append(min_cell_volt)

                max_cell_volt = DataPoint(
                    "max_voltage",
                    {"cmu_num": self.get_data_i(8, False, 6),
                     "cell_num": self.get_data_i(8, False, 7)},
                    self.timestamp,
                    {"value": self.get_data_i(16, False, 1) / 1000})

                datapoints.append(max_cell_volt)
            elif self.addr & 0xFF == 0xF9:  # min/max cell temp
                min_cell_temp = DataPoint(
                    "min_temp",
                    {"cmu_num": self.get_data_i(8, False, 4)},
                    self.timestamp,
                    {"value": self.get_data_i(16, False, 0) / 10})

                datapoints.append(min_cell_temp)

                max_cell_temp = DataPoint(
                    "max_temp",
                    {"cmu_num": self.get_data_i(8, False, 6)},
                    self.timestamp,
                    {"value": self.get_data_i(16, False, 1) / 10})

                datapoints.append(max_cell_temp)
            elif self.addr & 0xFF == 0xFA:  # Pack Voltage & Current
                datapoints.append(DataPoint("batt_volt", {}, self.timestamp, {"value": self.get_data_i(32, False, 0)/1000}))
                datapoints.append(DataPoint("batt_curr", {}, self.timestamp, {"value": self.get_data_i(32, True, 1)/1000}))
            elif self.addr & 0xFF == 0xFB:  # Pack Status
                pass
            elif self.addr & 0xFF == 0xFC:  # Fan & 12V Status
                pass
            elif self.addr & 0xFF == 0xFD:  # Ext. Pack Status
                datapoints.append(DataPoint("cell_over_voltage", {}, self.timestamp, {"value": self.get_data_b(0)}))
                datapoints.append(DataPoint("cell_under_voltage", {}, self.timestamp, {"value": self.get_data_b(1)}))
                datapoints.append(DataPoint("cell_over_temp", {}, self.timestamp, {"value": self.get_data_b(2)}))
                datapoints.append(DataPoint("measurement_untrusted", {}, self.timestamp, {"value": self.get_data_b(3)}))

                datapoints.append(DataPoint("cmu_comm_timeout", {}, self.timestamp, {"value": self.get_data_b(4)}))
                datapoints.append(DataPoint("vehicle_comm_timeout", {}, self.timestamp, {"value": self.get_data_b(5)}))
                datapoints.append(DataPoint("bmu_in_setup_mode", {}, self.timestamp, {"value": self.get_data_b(6)}))
                datapoints.append(DataPoint("cmu_can_power", {}, self.timestamp, {"value": self.get_data_b(7)}))

                datapoints.append(DataPoint("pack_isolation_fail", {}, self.timestamp, {"value": self.get_data_b(8)}))
                datapoints.append(DataPoint("soc_invalid", {}, self.timestamp, {"value": self.get_data_b(9)}))
                datapoints.append(DataPoint("can_power_low", {}, self.timestamp, {"value": self.get_data_b(10)}))
                datapoints.append(DataPoint("contactor_stuck", {}, self.timestamp, {"value": self.get_data_b(11)}))

                datapoints.append(DataPoint("unexpected_cell", {}, self.timestamp, {"value": self.get_data_b(12)}))

                datapoints.append(DataPoint("bmu_hw_version", {}, self.timestamp, {"value": self.get_data_i(8, False, 4)}))
                datapoints.append(DataPoint("bmu_model_id", {}, self.timestamp, {"value": self.get_data_i(8, False, 5)}))
        elif self.isMPPTFrame(): 

            mppt_id = "Err:" + str(self.addr)
            if self.addr & 0xFF0 == int(self.opt["CAN"]["MPPT"]["mppt1_id"], 16):
                mppt_id = "1"
            elif self.addr & 0xFF0 == int(self.opt["CAN"]["MPPT"]["mppt2_id"], 16):
                mppt_id = "2"
            elif self.addr & 0xFF0 == int(self.opt["CAN"]["MPPT"]["mppt3_id"], 16):
                mppt_id = "3"

            if self.addr & 0xF == 0x0:  # MPPT input
                datapoints.append( DataPoint(
                    "mppt_in_voltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(0, False)}))

                datapoints.append( DataPoint(
                    "mppt_in_current",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(1, False)}))

            elif self.addr & 0xF == 0x1:  # MPPT output
                datapoints.append( DataPoint(
                    "mppt_out_voltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(0, False)}))

                datapoints.append( DataPoint(
                    "mppt_out_current",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(1, False)}))

            elif self.addr & 0xF == 0x2:  # Temps
                datapoints.append(DataPoint(
                    "mppt_mosfet_temp",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(0, False)}))

                datapoints.append(DataPoint(
                    "mppt_controller_temp",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(1, False)}))

            elif self.addr & 0xF == 0x3:  # Aux Power voltages
                datapoints.append(DataPoint(
                    "aux_12V_voltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(1, True)}))

                datapoints.append(DataPoint(
                    "aux_3V_voltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(0, True)}))

            elif self.addr & 0xF == 0x4:  # Limits
                datapoints.append(DataPoint(
                    "mppt_max_out_voltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(1, True)}))

                datapoints.append(DataPoint(
                    "mppt_max_in_current",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(0, True)}))

            elif self.addr & 0xF == 0x5:  # Status
                datapoints.append(DataPoint(
                    "mppt_can_rx_err",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_i(8, False, 0)}))

                datapoints.append(DataPoint(
                    "mppt_can_tx_err",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                     {"value": self.get_data_i(8, False, 1)}))

                datapoints.append(DataPoint(
                    "mppt_can_overflow",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_i(8, False, 2)}))

                # Error Flags
                datapoints.append(DataPoint(
                    "mppt_low_array_power",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(24)}))

                datapoints.append(DataPoint(
                    "mppt_mosfet_overheat",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(25)}))

                datapoints.append(DataPoint(
                    "mppt_batt_low",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(26)}))

                datapoints.append(DataPoint(
                    "mppt_batt_full",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(27)}))

                datapoints.append(DataPoint(
                    "mppt_12V_undervoltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(28)}))

                # b(29) is reserved
                datapoints.append(DataPoint(
                    "mppt_hw_over_curr",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(30)}))

                datapoints.append(DataPoint(
                    "mppt_hw_over_volt",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(31)}))

                # Limit Flags
                datapoints.append(DataPoint(
                    "mppt_inp_curr_min",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(32)}))

                datapoints.append(DataPoint(
                    "mppt_inp_curr_max",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(33)}))

                datapoints.append(DataPoint(
                    "mppt_out_volt_max",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(34)}))

                datapoints.append(DataPoint(
                    "mppt_lim_mosfet_temp",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(35)}))

                datapoints.append(DataPoint(
                    "mppt_dutycycle_min",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(36)}))

                datapoints.append(DataPoint(
                    "mppt_dutycycle_max",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(37)}))

                datapoints.append(DataPoint(
                    "mppt_local",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(38)}))

                datapoints.append(DataPoint(
                    "mppt_global",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(39)}))

                # Mode
                datapoints.append(DataPoint(
                    "mppt_is_on",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_b(40)}))

            elif self.addr & 0xF == 0x6:  # Power Connector
                datapoints.append(DataPoint(
                    "mppt_conn_out_voltage",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(1, True)}))

                datapoints.append(DataPoint(
                    "mppt_conn_temp",
                    {"mppt_id": mppt_id},
                    self.timestamp,
                    {"value": self.get_data_f(0, True)}))
        elif self.isDCFrame():
            if self.addr & 0xF == 0x0:
                datapoints.append(DataPoint("dc_lifesign", {}, self.timestamp, {"value":self.get_data_i(16, False, 0)}))
                datapoints.append(DataPoint("dc_potentiometer", {}, self.timestamp, {"value":self.get_data_i(16, False, 1)}))
                datapoints.append(DataPoint("dc_acceleration", {}, self.timestamp, {"value":self.get_data_i(16, False, 2)}))
                datapoints.append(DataPoint("dc_deceleration", {}, self.timestamp, {"value":self.get_data_i(16, False, 3)}))

            elif self.addr & 0xF == 0x1:
                datapoints.append(DataPoint("targetspeed", {}, self.timestamp, {"value":self.get_data_i(16, False, 0)}))
                datapoints.append(DataPoint("targetpower", {}, self.timestamp, {"value":self.get_data_i(16, False, 1)/1000.0}))
                datapoints.append(DataPoint("accel_display", {}, self.timestamp, {"value":self.get_data_i(8, True, 4)}))
                datapoints.append(DataPoint("speed", {}, self.timestamp, {"value":self.get_data_i(8, False, 6)}))

                drive_direction = 'fwd' if self.get_data_b(56) else 'rwd'
                datapoints.append(DataPoint("drive_direction", {}, self.timestamp, {"value":drive_direction}))
                datapoints.append(DataPoint("brake_pedal", {}, self.timestamp, {"value":self.get_data_b(57)}))
                datapoints.append(DataPoint("motor_on", {}, self.timestamp, {"value": self.get_data_b(58)}))
                datapoints.append(DataPoint("const_mode_on", {}, self.timestamp, {"value": self.get_data_b(59)}))
                datapoints.append(DataPoint("driver_confirm", {}, self.timestamp, {"value": self.get_data_b(60)}))
        elif self.isACFrame():
            datapoints.append(DataPoint("ac_life_sign", {}, self.timestamp, {"value": self.get_data_i(16, False, 0)}))
            datapoints.append(DataPoint("Kp", {}, self.timestamp, {"value": self.get_data_i(8, False, 2)}))
            datapoints.append(DataPoint("Ki", {}, self.timestamp, {"value": self.get_data_i(8, False, 3)}))
            datapoints.append(DataPoint("Kd", {}, self.timestamp, {"value": self.get_data_i(8, False, 4)}))

            mode = 'speed' if self.get_data_b(33) else 'power'
            datapoints.append(DataPoint("ac_mode", {}, self.timestamp, {"value": mode}))
        elif self.isMCFrame():
            if self.addr & 0xFF == 0x09: # ERPM, Current, Duty Cycle
                datapoints.append(DataPoint("mc_erpm", {}, self.timestamp, {"value": self.get_data_i(32, True, 0, False)}))
                datapoints.append(DataPoint("mc_current", {}, self.timestamp, {"value": self.get_data_i(16, True, 2, False)/10}))
                datapoints.append(DataPoint("mc_duty_cycle", {}, self.timestamp, {"value": self.get_data_i(16, True, 3, False)/1000}))

            elif self.addr & 0xFF == 0x0e: # Ah Used, Ah Charged
                datapoints.append(DataPoint("mc_Ah_used", {}, self.timestamp, {"value": self.get_data_i(32, True, 0, False)/10000}))
                datapoints.append(DataPoint("mc_Ah_charged", {}, self.timestamp, {"value": self.get_data_i(32, True, 1, False)/10000}))

            elif self.addr & 0xFF == 0x0f: # Wh Used, Wh Charged
                datapoints.append(DataPoint("mc_Wh_used", {}, self.timestamp, {"value": self.get_data_i(32, True, 0, False)/10000}))
                datapoints.append(DataPoint("mc_Wh_charged", {}, self.timestamp, {"value": self.get_data_i(32, True, 1, False)/10000}))

            elif self.addr & 0xFF == 0x10:  # Temp Fet, Temp Motor, Current In, PID position
                datapoints.append(DataPoint("mc_fet_temp", {}, self.timestamp, {"value": self.get_data_i(16, True, 0, False)/10}))
                datapoints.append(DataPoint("mc_motor_temp", {}, self.timestamp, {"value": self.get_data_i(16, True, 1, False)/10}))
                datapoints.append(DataPoint("mc_curr_in", {}, self.timestamp, {"value": self.get_data_i(16, True, 2, False)/10}))
                datapoints.append(DataPoint("mc_pid_pos", {}, self.timestamp, {"value": self.get_data_i(16, True, 3, False)/50}))

            elif self.addr & 0xFF == 0x1b:  # Tachometer, Voltage In
                datapoints.append(DataPoint("mc_tacho", {"unit": "EREV"}, self.timestamp, {"value": self.get_data_i(32, True, 0, False) / 6}))
                datapoints.append(DataPoint("mc_volt_in", {}, self.timestamp, {"value": self.get_data_i(16, True, 2, False) / 10}))
        else:  # Prob. transmission error or wrong addresses configured
            lg.warning("Couldn't assign CAN Frame from: " + hex(self.addr))

        lg.debug(f"asDatapoints() -> {datapoints}")


        return datapoints
