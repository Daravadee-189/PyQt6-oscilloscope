import os
import time
import queue
import threading

import pyvisa
from PIL import ImageGrab

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QLineEdit, QGroupBox, QCheckBox, QComboBox, 
    QTextEdit, QFrame, QMessageBox, QDialog, QListWidget, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QPolygonF, QPixmap

DEFAULT_V_DIVS = 8
DEFAULT_H_DIVS = 10

class PrototypeScopeController(QWidget):
    def __init__(self):
        super().__init__()
        
        # Window Setting
        self.setWindowTitle("Rigol UI Controller")
        self.resize(1150, 850)
        self.setStyleSheet("background-color: #1e222b; color: #abb2bf;")

        # ตัวแปรสถานะ 
        self.rm = None
        self.scope = None
        self.is_connected = False
        self.show_signals = False

        # Thread & Queue
        self.visa_lock = threading.Lock()  
        self.log_lock = threading.Lock()   
        self.command_log = []
        self._log_widget_cursor = 0

        self.data_queue = queue.Queue()
        self.fetching = False

        self.num_v_divs = DEFAULT_V_DIVS
        self.num_h_divs = DEFAULT_H_DIVS
        self.current_hscale = 0.001
        self.current_hoffset = 0.0
        self.last_waveform_data = {}

        # UI Layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Top Bar
        top_bar = QFrame()
        top_bar.setStyleSheet("background-color: #282c34; border-radius: 5px;")
        top_layout = QHBoxLayout(top_bar)

        lbl_device = QLabel("DEVICE :")
        lbl_device.setStyleSheet("font-weight: bold;")
        
        self.lbl_device_name = QLabel("Not connected")
        self.lbl_device_name.setStyleSheet("color: #98c379; font-family: Courier; font-weight: bold;")
        self.lbl_device_name.setMinimumWidth(250)

        self.btn_connect = QPushButton("CONNECT DEVICE")
        self.btn_connect.setStyleSheet("background-color: #61afef; color: #1e222b; font-weight: bold; padding: 5px;")
        self.btn_connect.clicked.connect(self.toggle_connection)

        lbl_scpi = QLabel("SCPI COMMAND:")
        lbl_scpi.setStyleSheet("color: #e5c07b; font-weight: bold;")
        
        self.ent_scpi = QLineEdit()
        self.ent_scpi.setFixedWidth(200)
        self.ent_scpi.setStyleSheet("background-color: #1e222b; color: white; border: 1px solid #3e4451;")
        self.ent_scpi.returnPressed.connect(self.send_custom_scpi)
        
        btn_send_scpi = QPushButton("SEND")
        btn_send_scpi.setStyleSheet("background-color: #c678dd; color: white; font-weight: bold; padding: 5px;")
        btn_send_scpi.clicked.connect(self.send_custom_scpi)

        top_layout.addWidget(lbl_device)
        top_layout.addWidget(self.lbl_device_name)
        top_layout.addWidget(self.btn_connect)
        top_layout.addStretch()
        top_layout.addWidget(lbl_scpi)
        top_layout.addWidget(self.ent_scpi)
        top_layout.addWidget(btn_send_scpi)
        
        main_layout.addWidget(top_bar)

        # Center (3 parts)
        center_area_layout = QHBoxLayout()
        
        # Left Panel (CH1 - CH4)
        left_panel = QVBoxLayout()
        self.ch_widgets = {}
        
        ch_configs = [
            {"name": "CH1", "num": 1, "color": "#e5c07b", "def_vdiv": "2 V/div", "def_offset": "4 V"},
            {"name": "CH2", "num": 2, "color": "#56b6c2", "def_vdiv": "2 V/div", "def_offset": "0 V"},
            {"name": "CH3", "num": 3, "color": "#c678dd", "def_vdiv": "2 V/div", "def_offset": "-4 V"},
            {"name": "CH4", "num": 4, "color": "#61afef", "def_vdiv": "2 V/div", "def_offset": "-8 V"},
        ]

        for ch in ch_configs:
            group_box = QGroupBox(ch["name"])
            group_box.setStyleSheet(f"QGroupBox {{ color: {ch['color']}; font-weight: bold; border: 1px solid #3e4451; margin-top: 10px; }} QGroupBox::title {{ subcontrol-origin: margin; left: 10px; }}")
            group_layout = QVBoxLayout()

            chk_display = QCheckBox("Display")
            chk_display.setChecked(True)
            chk_display.toggled.connect(lambda checked, name=ch["name"]: self.send_channel_display(name))

            cb_vdiv = QComboBox()
            cb_vdiv.addItems(["500 mV/div", "1 V/div", "2 V/div", "5 V/div", "10 V/div"])
            cb_vdiv.setCurrentText(ch["def_vdiv"])
            cb_vdiv.currentTextChanged.connect(lambda text, name=ch["name"]: self.send_channel_scale(name))

            ent_offset = QLineEdit()
            ent_offset.setText(ch["def_offset"])
            ent_offset.editingFinished.connect(lambda name=ch["name"]: self.send_channel_offset(name))

            cb_coupling = QComboBox()
            cb_coupling.addItems(["DC", "AC", "GND"])
            cb_coupling.currentTextChanged.connect(lambda text, name=ch["name"]: self.send_channel_coupling(name))

            group_layout.addWidget(chk_display)
            group_layout.addWidget(QLabel("V/div:"))
            group_layout.addWidget(cb_vdiv)
            group_layout.addWidget(QLabel("Offset:"))
            group_layout.addWidget(ent_offset)
            group_layout.addWidget(QLabel("Coupling:"))
            group_layout.addWidget(cb_coupling)
            
            group_box.setLayout(group_layout)
            left_panel.addWidget(group_box)
            
            self.ch_widgets[ch["name"]] = {
                "num": ch["num"], "color": ch["color"],
                "display": chk_display, "vdiv": cb_vdiv, 
                "offset": ent_offset, "coupling": cb_coupling
            }
            
        left_panel.addStretch()
        center_area_layout.addLayout(left_panel, stretch=0)

        # Center Panel (Canvas + Log)
        middle_panel = QVBoxLayout()
        
        # Use QLabel for show QPixmap
        self.canvas = QLabel()
        self.canvas.setStyleSheet("background-color: black;")
        self.canvas.setMinimumSize(400, 300)
        
        self.log_text = QTextEdit()
        self.log_text.setStyleSheet("background-color: #14161a; color: #98c379; font-family: Courier; font-size: 10px;")
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(130)

        middle_panel.addWidget(self.canvas, stretch=1)
        middle_panel.addWidget(QLabel("SCPI COMMAND LOG"))
        middle_panel.addWidget(self.log_text)
        
        center_area_layout.addLayout(middle_panel, stretch=1)

        # Right Panel (Actions, Horizontal, Trigger)
        right_panel = QVBoxLayout()
        
        # MAIN ACTIONS
        action_group = QGroupBox("MAIN ACTIONS")
        action_layout = QVBoxLayout()
        self.btn_run = QPushButton("▶ RUN")
        self.btn_run.setStyleSheet("background-color: #98c379; color: #1e222b; font-weight: bold; padding: 5px;")
        self.btn_run.clicked.connect(self.run_scope)
        self.btn_stop = QPushButton("■ STOP")
        self.btn_stop.setStyleSheet("background-color: #e06c75; color: white; font-weight: bold; padding: 5px;")
        self.btn_stop.clicked.connect(self.stop_scope)
        
        btn_capture = QPushButton("CAPTURE SCREEN")
        btn_capture.setStyleSheet("background-color: #61afef; color: #1e222b; font-weight: bold; padding: 5px;")
        btn_capture.clicked.connect(self.save_png)
        
        btn_push = QPushButton("SEND SETTINGS TO SCOPE")
        btn_push.setStyleSheet("background-color: #e5c07b; color: #1e222b; font-weight: bold; padding: 5px;")
        btn_push.clicked.connect(self.push_all_settings_to_scope)

        action_layout.addWidget(self.btn_run)
        action_layout.addWidget(self.btn_stop)
        action_layout.addWidget(btn_capture)
        action_layout.addWidget(btn_push)
        action_group.setLayout(action_layout)
        right_panel.addWidget(action_group)

        # Horizontal
        horiz_group = QGroupBox("Horizontal")
        horiz_layout = QVBoxLayout()
        self.cb_tdiv = QComboBox()
        self.cb_tdiv.addItems(["100 us/div", "500 us/div", "1 ms/div", "2 ms/div"])
        self.cb_tdiv.setCurrentText("1 ms/div")
        self.cb_tdiv.currentTextChanged.connect(self.send_horizontal_scale)
        
        self.ent_hoffset = QLineEdit("0 s")
        self.ent_hoffset.editingFinished.connect(self.send_horizontal_offset)
        
        horiz_layout.addWidget(QLabel("T/div:"))
        horiz_layout.addWidget(self.cb_tdiv)
        horiz_layout.addWidget(QLabel("Offset:"))
        horiz_layout.addWidget(self.ent_hoffset)
        horiz_group.setLayout(horiz_layout)
        right_panel.addWidget(horiz_group)

        # Trigger
        trig_group = QGroupBox("Trigger")
        trig_layout = QVBoxLayout()
        
        self.cb_src = QComboBox()
        self.cb_src.addItems(["CHAN1", "CHAN2", "CHAN3", "CHAN4"])
        self.cb_src.currentTextChanged.connect(self.send_trigger_settings)
        
        self.cb_slope = QComboBox()
        self.cb_slope.addItems(["POS", "NEG"])
        self.cb_slope.currentTextChanged.connect(self.send_trigger_settings)
        
        self.ent_level = QLineEdit("0 V")
        self.ent_level.editingFinished.connect(self.send_trigger_settings)
        
        self.cb_sweep = QComboBox()
        self.cb_sweep.addItems(["AUTO", "NORMAL", "SINGLE"])
        self.cb_sweep.currentTextChanged.connect(self.send_trigger_settings)
        
        trig_layout.addWidget(QLabel("Source:"))
        trig_layout.addWidget(self.cb_src)
        trig_layout.addWidget(QLabel("Slope:"))
        trig_layout.addWidget(self.cb_slope)
        trig_layout.addWidget(QLabel("Level:"))
        trig_layout.addWidget(self.ent_level)
        trig_layout.addWidget(QLabel("Sweep:"))
        trig_layout.addWidget(self.cb_sweep)
        trig_group.setLayout(trig_layout)
        right_panel.addWidget(trig_group)
        
        right_panel.addStretch()
        center_area_layout.addLayout(right_panel, stretch=0)

        main_layout.addLayout(center_area_layout)

        # Status Bar
        status_bar = QFrame()
        status_bar.setStyleSheet("background-color: #14161a;")
        status_layout = QHBoxLayout(status_bar)
        
        self.lbl_status_text = QLabel("SYSTEM STATUS : Offline")
        self.lbl_status_text.setStyleSheet("color: #e06c75; font-weight: bold;")
        
        self.lbl_fps = QLabel("SENSORS STATUS : STANDBY")
        self.lbl_fps.setStyleSheet("color: #98c379; font-weight: bold;")
        
        status_layout.addWidget(self.lbl_status_text)
        status_layout.addStretch()
        status_layout.addWidget(self.lbl_fps)
        
        main_layout.addWidget(status_bar)

        # QTimer Setting
        self.timer_log = QTimer(self)
        self.timer_log.timeout.connect(self._refresh_log_widget)
        self.timer_log.start(400)

        self.timer_update = QTimer(self)
        self.timer_update.timeout.connect(self.live_update_loop)
        self.timer_update.start(30)


    # Number Parsers & Formatters
    def _parse_volts_per_div(self, text):
        if "mV" in text:
            return float(text.replace("mV/div", "").strip()) / 1000.0
        return float(text.replace("V/div", "").strip())

    def _parse_volts(self, text):
        return float(text.replace("V", "").replace("v", "").strip())

    def _parse_seconds_per_div(self, text):
        if "us" in text:
            return float(text.replace("us/div", "").strip()) / 1_000_000.0
        return float(text.replace("ms/div", "").strip()) / 1000.0

    def _parse_seconds(self, text):
        return float(text.replace("s", "").replace("S", "").strip())

    def _format_volts_per_div(self, v):
        return f"{v * 1000:g} mV/div" if abs(v) < 1 else f"{v:g} V/div"

    def _format_seconds_per_div(self, s):
        return f"{s * 1_000_000:g} us/div" if abs(s) < 1e-3 else f"{s * 1000:g} ms/div"

    # Logging & Custom Commands
    def _log(self, text, tag="SYS"):
        line = f"[{time.strftime('%H:%M:%S')}] {tag:9s} {text}"
        with self.log_lock:
            self.command_log.append(line)
            if len(self.command_log) > 3000:
                self.command_log = self.command_log[-3000:]

    def _scpi_write(self, cmd):
        if not self.is_connected or not self.scope:
            return
        try:
            with self.visa_lock:
                self.scope.write(cmd)
            self._log(cmd, "TX")
        except Exception as e:
            self._log(f"WRITE FAILED: {cmd} ({e})", "SYS")

    def _scpi_query(self, cmd):
        if not self.scope:
            raise RuntimeError("not connected")
        with self.visa_lock:
            resp = self.scope.query(cmd)
        self._log(f"{cmd} -> {resp.strip()}", "TX/RX")
        return resp

    def send_custom_scpi(self):
        cmd = self.ent_scpi.text().strip()
        if not cmd:
            return
        
        if not self.is_connected or not self.scope:
            self._log("Cannot send command: Instrument not connected", "SYS")
            QMessageBox.warning(self, "Not Connected", "Please connect to the device first.")
            return

        try:
            if "?" in cmd:
                self._scpi_query(cmd)
            else:
                self._scpi_write(cmd)
        except Exception as e:
            self._log(f"CUSTOM CMD ERROR: {e}", "SYS")
        
        self.ent_scpi.setText("") 

    def _refresh_log_widget(self):
        with self.log_lock:
            total = len(self.command_log)
            new_lines = self.command_log[self._log_widget_cursor:] if self._log_widget_cursor < total else []
            self._log_widget_cursor = total
            
        if new_lines:
            for line in new_lines:
                self.log_text.append(line)
            
            doc = self.log_text.document()
            if doc.blockCount() > 300:
                cursor = self.log_text.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                for _ in range(doc.blockCount() - 300):
                    cursor.select(cursor.SelectionType.BlockUnderCursor)
                    cursor.removeSelectedText()
                    cursor.deleteChar()
            
            self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    # SCPI: Channel / Horizontal / Trigger
    def send_channel_display(self, ch_name):
        ch_num = self.ch_widgets[ch_name]["num"]
        state = 1 if self.ch_widgets[ch_name]["display"].isChecked() else 0
        self._scpi_write(f":CHANnel{ch_num}:DISPlay {state}")
        self.draw_oscilloscope_screen()

    def send_channel_scale(self, ch_name):
        ch_num = self.ch_widgets[ch_name]["num"]
        val = self._parse_volts_per_div(self.ch_widgets[ch_name]["vdiv"].currentText())
        self._scpi_write(f":CHANnel{ch_num}:SCALe {val}")

    def send_channel_offset(self, ch_name):
        ch_num = self.ch_widgets[ch_name]["num"]
        val = self._parse_volts(self.ch_widgets[ch_name]["offset"].text())
        self._scpi_write(f":CHANnel{ch_num}:OFFSet {val}")

    def send_channel_coupling(self, ch_name):
        ch_num = self.ch_widgets[ch_name]["num"]
        val = self.ch_widgets[ch_name]["coupling"].currentText()
        self._scpi_write(f":CHANnel{ch_num}:COUPling {val}")

    def send_horizontal_scale(self):
        val = self._parse_seconds_per_div(self.cb_tdiv.currentText())
        self._scpi_write(f":TIMebase:MAIN:SCALe {val}")

    def send_horizontal_offset(self):
        val = self._parse_seconds(self.ent_hoffset.text())
        self._scpi_write(f":TIMebase:MAIN:OFFSet {val}")

    def send_trigger_settings(self):
        src = self.cb_src.currentText()
        slope = "POSitive" if self.cb_slope.currentText() == "POS" else "NEGative"
        level = self._parse_volts(self.ent_level.text())
        sweep = self.cb_sweep.currentText()

        self._scpi_write(f":TRIGger:EDGe:SOURce {src}")
        self._scpi_write(f":TRIGger:EDGe:SLOPe {slope}")
        self._scpi_write(f":TRIGger:EDGe:LEVel {level}")
        self._scpi_write(f":TRIGger:SWEep {sweep}")

    def push_all_settings_to_scope(self):
        for name in self.ch_widgets:
            self.send_channel_display(name)
            self.send_channel_scale(name)
            self.send_channel_offset(name)
            self.send_channel_coupling(name)
        self.send_horizontal_scale()
        self.send_horizontal_offset()
        self.send_trigger_settings()

    def pull_settings_from_scope(self):
        if not self.is_connected or not self.scope:
            return
        for name, info in self.ch_widgets.items():
            ch = info["num"]
            try:
                info["display"].setChecked(self._scpi_query(f":CHANnel{ch}:DISPlay?").strip() in ("1", "ON"))
            except Exception as e:
                self._log(f"WARN: could not read CH{ch} display state: {e}", "SYS")
            try:
                info["vdiv"].setCurrentText(self._format_volts_per_div(float(self._scpi_query(f":CHANnel{ch}:SCALe?"))))
            except Exception as e:
                self._log(f"WARN: could not read CH{ch} scale: {e}", "SYS")
            try:
                info["offset"].setText(f"{float(self._scpi_query(f':CHANnel{ch}:OFFSet?')):g} V")
            except Exception as e:
                self._log(f"WARN: could not read CH{ch} offset: {e}", "SYS")
            try:
                info["coupling"].setCurrentText(self._scpi_query(f":CHANnel{ch}:COUPling?").strip())
            except Exception as e:
                self._log(f"WARN: could not read CH{ch} coupling: {e}", "SYS")

        try:
            self.current_hscale = float(self._scpi_query(":TIMebase:MAIN:SCALe?"))
            self.cb_tdiv.setCurrentText(self._format_seconds_per_div(self.current_hscale))
        except Exception as e:
            self._log(f"WARN: could not read timebase scale: {e}", "SYS")
        try:
            self.current_hoffset = float(self._scpi_query(":TIMebase:MAIN:OFFSet?"))
            self.ent_hoffset.setText(f"{self.current_hoffset:g} s")
        except Exception as e:
            self._log(f"WARN: could not read timebase offset: {e}", "SYS")

        try:
            self.cb_src.setCurrentText(self._scpi_query(":TRIGger:EDGe:SOURce?").strip())
        except Exception as e:
            self._log(f"WARN: could not read trigger source: {e}", "SYS")
        try:
            slope = self._scpi_query(":TRIGger:EDGe:SLOPe?").strip().upper()
            self.cb_slope.setCurrentText({"POSITIVE": "POS", "NEGATIVE": "NEG"}.get(slope, "POS"))
        except Exception as e:
            self._log(f"WARN: could not read trigger slope: {e}", "SYS")
        try:
            self.ent_level.setText(f"{float(self._scpi_query(':TRIGger:EDGe:LEVel?')):g} V")
        except Exception as e:
            self._log(f"WARN: could not read trigger level: {e}", "SYS")
        try:
            self.cb_sweep.setCurrentText(self._scpi_query(":TRIGger:SWEep?").strip().upper())
        except Exception as e:
            self._log(f"WARN: could not read trigger sweep mode: {e}", "SYS")

    # Connection Management
    def _prompt_resource_choice(self, candidates):
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Instrument")
        dlg.setStyleSheet("background-color: #282c34; color: #abb2bf;")
        
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Multiple USB instruments found:"))
        
        list_widget = QListWidget()
        list_widget.addItems(candidates)
        list_widget.setCurrentRow(0)
        layout.addWidget(list_widget)
        
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)
        
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return list_widget.currentItem().text()
        return None

    def toggle_connection(self):
        if not self.is_connected:
            try:
                self.rm = pyvisa.ResourceManager("@py")
                available_resources = self.rm.list_resources()
                candidates = [r for r in available_resources if "USB" in r and "INSTR" in r]
                
                if not candidates:
                    raise Exception("No USB instrument found.")
                
                visa_address = candidates[0] if len(candidates) == 1 else self._prompt_resource_choice(candidates)
                if not visa_address:
                    return

                self.scope = self.rm.open_resource(visa_address)
                self.scope.timeout = 3000
                try:
                    self.scope.chunk_size = 1024 * 1024
                except Exception:
                    pass

                idn = self.scope.query("*IDN?").strip()
                self._log(f"*IDN? -> {idn}", "TX/RX")
                idn_parts = [p.strip() for p in idn.split(",")]
                manufacturer = idn_parts[0] if len(idn_parts) > 0 else "Rigol"
                model = idn_parts[1] if len(idn_parts) > 1 else "Unknown"
                serial = idn_parts[2] if len(idn_parts) > 2 else "Unknown"

                self.is_connected = True
                self.show_signals = True
                self.btn_connect.setText("DISCONNECT")
                self.btn_connect.setStyleSheet("background-color: #e06c75; color: white; font-weight: bold; padding: 5px;")
                self.lbl_device_name.setText(f"{manufacturer} {model} (S/N: {serial})")
                self.lbl_status_text.setStyleSheet("color: #98c379; font-weight: bold;")
                self.lbl_status_text.setText("SYSTEM STATUS : Online")

                self._scpi_write(":WAVeform:MODE NORMal")
                self._scpi_write(":WAVeform:FORMat BYTE")

                self.pull_settings_from_scope()

            except Exception as e:
                QMessageBox.critical(self, "Connection Error", str(e))
                self.is_connected = False
                self.show_signals = False
                self.lbl_device_name.setText("Not connected")
                self.draw_oscilloscope_screen()
        else:
            self.is_connected = False
            with self.visa_lock:
                if self.scope:
                    try:
                        self.scope.close()
                    except Exception:
                        pass
                    self.scope = None
            self.show_signals = False
            self.btn_connect.setText("CONNECT DEVICE")
            self.btn_connect.setStyleSheet("background-color: #61afef; color: #1e222b; font-weight: bold; padding: 5px;")
            self.lbl_device_name.setText("Not connected")
            self.lbl_status_text.setStyleSheet("color: #e06c75; font-weight: bold;")
            self.lbl_status_text.setText("SYSTEM STATUS : Offline")
            self.draw_oscilloscope_screen()

    def run_scope(self):
        self._scpi_write(":RUN")
        self.lbl_fps.setText("SENSORS STATUS : RUNNING")

    def stop_scope(self):
        self._scpi_write(":STOP")
        self.lbl_fps.setText("SENSORS STATUS : STOPPED")

    def save_png(self, filepath="scope_screen.png"):
        if not self.is_connected or not self.scope:
            QMessageBox.warning(self, "Not Connected", "Please connect to an oscilloscope first.")
            return

        try:
            self._log("Requesting screenshot from instrument...", "SYS")
            with self.visa_lock:
                original_timeout = self.scope.timeout
                self.scope.timeout = 7000  
                try:
                    model = self.lbl_device_name.text().upper()
                    if "DHO" in model:
                        self.scope.write(":DISPlay:SNAP?")
                    else:
                        self.scope.write(":DISP:DATA? ON,0,PNG")

                    header = self.scope.read_bytes(2)
                    if header[0:1] != b"#":
                        raise RuntimeError(f"Unexpected header byte: {header!r}. Expected '#'.")
                    num_digits = int(header[1:2])
                    if num_digits == 0:
                        raise RuntimeError("Received indefinite-length block - not supported.")
                    payload_length = int(self.scope.read_bytes(num_digits).decode())

                    self._log(f"Downloading {payload_length} bytes of image data...", "SYS")
                    png_data = self.scope.read_bytes(payload_length)
                finally:
                    self.scope.timeout = original_timeout

            if not png_data.startswith(b"\x89PNG"):
                raise RuntimeError("Returned data is not a valid PNG image.")

            with open(filepath, "wb") as f:
                f.write(png_data)

            self._log(f"Screenshot saved to {filepath} ({len(png_data)} bytes)", "SYS")
            QMessageBox.information(self, "Success", f"Instrument screen captured to:\n{os.path.abspath(filepath)}")

        except Exception as e:
            self._log(f"Screenshot capture failed: {e}", "SYS")
            QMessageBox.critical(self, "Capture Error", str(e))

    # Waveform Streaming 
    def fetch_waveform(self, channel):
        with self.visa_lock:
            self.scope.write(f":WAVeform:SOURce CHANnel{channel}")
            preamble = self.scope.query(":WAVeform:PREamble?").strip().split(",")
            raw = self.scope.query_binary_values(":WAVeform:DATA?", datatype="B", container=list)
        
        x_increment, x_origin = float(preamble[4]), float(preamble[5])
        y_increment, y_origin, y_reference = float(preamble[7]), float(preamble[8]), float(preamble[9])

        volts = [(b - y_reference - y_origin) * y_increment for b in raw]
        times = [x_origin + i * x_increment for i in range(len(raw))]
        return times, volts

    def _build_snapshot(self):
        active = [
            {"name": name, "num": info["num"], "color": info["color"]}
            for name, info in self.ch_widgets.items() if info["display"].isChecked()
        ]
        return {"active_channels": active}

    def _bg_fetch(self, snapshot):
        result = {"channels": {}, "errors": []}
        try:
            result["hscale"] = float(self._scpi_query(":TIMebase:MAIN:SCALe?"))
            result["hoffset"] = float(self._scpi_query(":TIMebase:MAIN:OFFSet?"))
        except Exception as e:
            result["errors"].append(f"timebase read: {e}")

        for ch in snapshot["active_channels"]:
            if not self.is_connected:
                break
            try:
                times, volts = self.fetch_waveform(ch["num"])
                result["channels"][ch["name"]] = (times, volts, ch["color"])
            except Exception as e:
                result["errors"].append(f"{ch['name']}: {e}")

        if self.is_connected:
            self.data_queue.put(result)

    def live_update_loop(self):
        if not self.is_connected:
            self.fetching = False
            return

        try:
            while True:  
                data = self.data_queue.get_nowait()
                if "hscale" in data:
                    self.current_hscale = data["hscale"]
                if "hoffset" in data:
                    self.current_hoffset = data["hoffset"]
                self.last_waveform_data.update(data["channels"])
                self.draw_oscilloscope_screen()
                self.fetching = False
        except queue.Empty:
            pass

        if not self.fetching and self.show_signals:
            self.fetching = True
            snap = self._build_snapshot()
            threading.Thread(target=self._bg_fetch, args=(snap,), daemon=True).start()

    def draw_oscilloscope_screen(self):
        w = self.canvas.width()
        h = self.canvas.height()
        
        # To prevent drawing, When it is too small or not show
        if w <= 1 or h <= 1:
            return

        # Create Pixmap (For waveform background)
        pixmap = QPixmap(w, h)
        pixmap.fill(QColor("black"))
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw Grid
        grid_pen = QPen(QColor("#2c313c"))
        grid_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(grid_pen)
        
        for i in range(1, self.num_h_divs):
            x = int((i / self.num_h_divs) * w)
            painter.drawLine(x, 0, x, h)
            
        for i in range(1, self.num_v_divs):
            y = int((i / self.num_v_divs) * h)
            painter.drawLine(0, y, w, y)
            
        center_pen = QPen(QColor("#5c6370"), 1)
        painter.setPen(center_pen)
        painter.drawLine(int(w / 2), 0, int(w / 2), h)
        painter.drawLine(0, int(h / 2), w, int(h / 2))

        # Recheck Connection
        if not self.is_connected:
            painter.setPen(QColor("#5c6370"))
            font = painter.font()
            font.setPointSize(14)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "SCOPE DISCONNECTED\nPress 'CONNECT DEVICE' to scan.")
            painter.end()
            self.canvas.setPixmap(pixmap)
            return

        # Draw graph for each channels
        for name, info in self.ch_widgets.items():
            if not info["display"].isChecked() or name not in self.last_waveform_data:
                continue
                
            times, volts, color_hex = self.last_waveform_data[name]
            if not times or not volts:
                continue

            try:
                vdiv = self._parse_volts_per_div(info["vdiv"].currentText())
                offset = self._parse_volts(info["offset"].text())
            except ValueError:
                continue 

            if vdiv == 0: 
                continue

            t_min, t_max = times[0], times[-1]
            t_range = t_max - t_min if (t_max > t_min) else 1
            
            polygon = QPolygonF()
            for t, v in zip(times, volts):
                x = ((t - t_min) / t_range) * w
                y = (h / 2) - ((v - offset) / (vdiv * self.num_v_divs)) * h
                polygon.append(QPointF(x, y))

            if polygon.count() >= 2:
                trace_pen = QPen(QColor(color_hex), 1.5)
                painter.setPen(trace_pen)
                painter.drawPolyline(polygon)
                
        painter.end()

        # Bring Pixmap to show on QLabel
        self.canvas.setPixmap(pixmap)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    window = PrototypeScopeController()
    window.show()
    sys.exit(app.exec())
