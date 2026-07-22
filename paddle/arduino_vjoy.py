import serial
import serial.tools.list_ports
import json
import time
import threading
import sys
import os
import ctypes
import mmap
import pyvjoy
import tkinter as tk
from tkinter import ttk, messagebox
import customtkinter as ctk
import socket
import asyncio
import websockets
import http.server
import socketserver
import functools

# ================= การตั้งค่าเริ่มต้น =================
VJOY_DEVICE_ID = 1
VJOY_MAX = 32767
CONFIG_FILE = "config.json"

# ===== AC Content Manager Color Palette =====
BG_DEEP     = "#0d0d0d"   # พื้นหลังหลัก
BG_PANEL    = "#141414"   # กรอบ Panel
BG_CARD     = "#1a1a1a"   # Card/Input
BG_HOVER    = "#222222"   # Hover state
ACCENT      = "#E4002B"   # แดง AC
ACCENT_DIM  = "#8B0018"   # แดงหรี่
TEXT_MAIN   = "#FFFFFF"   # ข้อความหลัก
TEXT_SUB    = "#888888"   # ข้อความรอง
TEXT_DIM    = "#444444"   # ข้อความจาง
BORDER      = "#2a2a2a"   # เส้นขอบ
GREEN_OK    = "#2ECC71"
ORANGE_WARN = "#E67E22"

# ===== AC Shared Memory Struct =====
class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', ctypes.c_int32),
        ('gas', ctypes.c_float),
        ('brake', ctypes.c_float),
        ('fuel', ctypes.c_float),
        ('gear', ctypes.c_int32),
        ('rpms', ctypes.c_int32),
        ('steerAngle', ctypes.c_float),
        ('speedKmh', ctypes.c_float),
        ('velocity', ctypes.c_float * 3),
        ('accG', ctypes.c_float * 3),
        ('wheelSlip', ctypes.c_float * 4),
        ('wheelLoad', ctypes.c_float * 4),
        ('wheelsPressure', ctypes.c_float * 4),
        ('wheelAngularSpeed', ctypes.c_float * 4),
        ('tyreWear', ctypes.c_float * 4),
        ('tyreDirtyLevel', ctypes.c_float * 4),
        ('tyreCoreTemperature', ctypes.c_float * 4),
        ('camberRAD', ctypes.c_float * 4),
        ('suspensionTravel', ctypes.c_float * 4),
        ('drs', ctypes.c_float),
        ('tc', ctypes.c_float),
        ('heading', ctypes.c_float),
        ('pitch', ctypes.c_float),
        ('roll', ctypes.c_float),
        ('cgHeight', ctypes.c_float),
        ('carDamage', ctypes.c_float * 5),
        ('numberOfTyresOut', ctypes.c_int32),
        ('pitLimiterOn', ctypes.c_int32),
        ('abs', ctypes.c_float),
        ('kersCharge', ctypes.c_float),
        ('kersInput', ctypes.c_float),
        ('autoShifterOn', ctypes.c_int32),
        ('rideHeight', ctypes.c_float * 2),
        ('turboBoost', ctypes.c_float),
        ('ballast', ctypes.c_float),
        ('airDensity', ctypes.c_float),
        ('airTemp', ctypes.c_float),
        ('roadTemp', ctypes.c_float),
        ('localAngularVel', ctypes.c_float * 3),
        ('finalFF', ctypes.c_float),
    ]

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

def map_value(value, in_min, in_max, out_min, out_max):
    if in_max == in_min:
        return 0
    value = max(in_min, min(in_max, value))
    return int((value - in_min) * (out_max - out_min) / (in_max - in_min) + out_min)


# ===== Custom AC-style Canvas Progress Bar =====
class ACBar(tk.Canvas):
    """Thin, clean progress bar สไตล์ AC Content Manager"""
    def __init__(self, parent, bar_width=340, bar_height=4, color=ACCENT, center=False, **kwargs):
        super().__init__(parent, width=int(bar_width), height=int(bar_height),
                         bg=BG_PANEL, highlightthickness=0, **kwargs)
        self._bw = int(bar_width)
        self._bh = int(bar_height)
        self._color = color
        self._center = center
        self._val = 0.0
        self.update_idletasks()
        # Track background
        self.create_rectangle(0, 0, self._bw, self._bh, fill=BG_HOVER, outline="", tags="bg")
        # Fill bar
        self.create_rectangle(0, 0, 0, self._bh, fill=color, outline="", tags="fill")
        self._draw(0.0)

    def _draw(self, val):
        self._val = max(0.0, min(1.0, val))
        if self._center:
            mid = self._bw / 2
            fill_w = self._val * self._bw
            if fill_w >= mid:
                x0, x1 = mid, fill_w
            else:
                x0, x1 = fill_w, mid
        else:
            x0, x1 = 0, self._val * self._bw
        self.coords("fill", x0, 0, x1, self._bh)

    def set(self, val):
        self._draw(val)


# ===== Helper Widgets =====
def separator(parent, pady=0):
    """เส้นแบ่งบางๆ สไตล์ AC"""
    f = tk.Frame(parent, bg=BORDER, height=1)
    f.pack(fill="x", padx=0, pady=pady)
    return f

def section_label(parent, text):
    """หัวข้อ Section สีแดง + อักษรพิมพ์ใหญ่"""
    tk.Label(parent, text=text.upper(),
             font=("Segoe UI", 8, "bold"),
             fg=ACCENT, bg=BG_PANEL,
             anchor="w").pack(fill="x", padx=16, pady=(14, 6))

def row_label(parent, text, value_var=None):
    """แถวข้อมูล label + value"""
    f = tk.Frame(parent, bg=BG_PANEL)
    f.pack(fill="x", padx=16, pady=2)
    tk.Label(f, text=text, font=("Segoe UI", 9),
             fg=TEXT_SUB, bg=BG_PANEL, anchor="w", width=18).pack(side="left")
    if value_var:
        lbl = tk.Label(f, textvariable=value_var,
                       font=("Consolas", 9, "bold"),
                       fg=TEXT_MAIN, bg=BG_PANEL, anchor="e")
        lbl.pack(side="right")
    return f


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("SIM RACING CONTROL")
        self.geometry("760x680")
        self.resizable(False, False)
        self.configure(bg=BG_DEEP)

        # Serial
        self.ser_pedals = None
        self.ser_wheel = None
        self.is_pedals_connected = False
        self.is_wheel_connected = False

        # Telemetry
        self.shm = None
        self.ac_connected = False

        # Threads / vJoy
        self.pedals_thread = None
        self.wheel_thread = None
        self.ffb_thread = None
        self.vjoy_dev = None
        self.save_timer = None
        self.txt_ped_log = None
        self.txt_whl_log = None

        # Live value vars
        self.var_steer  = tk.StringVar(value="0.0°")
        self.var_thr    = tk.StringVar(value="0%")
        self.var_brk    = tk.StringVar(value="0%")
        self.var_ffb    = tk.StringVar(value="±0%")
        self.var_ped_st = tk.StringVar(value="DISCONNECTED")
        self.var_whl_st = tk.StringVar(value="DISCONNECTED")
        self.var_vjoy   = tk.StringVar(value="—")
        self.var_ac_st  = tk.StringVar(value="WAITING")

        # Mobile Wheel Server
        self.mobile_http_thread = None
        self.mobile_ws_thread = None
        self.is_mobile_server_running = False
        self.var_mobile_st = tk.StringVar(value="STOPPED")
        self.var_mobile_ip = tk.StringVar(value="—")
        self.mobile_loop = None

        # Config
        self.config = {
            "port_pedals": "",
            "port_wheel": "",
            "baudrate_pedals": "115200",
            "baudrate_wheel": "115200",
            "throttle_min": 0,
            "brake_min": 0,
            "steer_min": -180,
            "steer_max": 180,
            "ffb_gain": 100,
            "invert_steer": False,
            "invert_ffb": False
        }
        self.load_config()

        # Config vars (sliders/switches)
        self.t_min_var    = tk.DoubleVar(value=self.config.get("throttle_min", 0))
        self.b_min_var    = tk.DoubleVar(value=self.config.get("brake_min", 0))
        self.steer_angle_var = tk.DoubleVar(value=self.config.get("steer_angle", 900))
        self.steer_center_var = tk.DoubleVar(value=self.config.get("steer_center", 0))
        self.ffb_gain_var = tk.DoubleVar(value=self.config.get("ffb_gain", 100))
        self.invert_steer_var = tk.BooleanVar(value=self.config.get("invert_steer", False))
        self.invert_ffb_var = tk.BooleanVar(value=self.config.get("invert_ffb", False))

        self.port_ped_var  = tk.StringVar(value=self.config.get("port_pedals", ""))
        self.port_whl_var  = tk.StringVar(value=self.config.get("port_wheel", ""))
        self.baud_ped_var  = tk.StringVar(value=self.config.get("baudrate_pedals", "115200"))
        self.baud_whl_var  = tk.StringVar(value=self.config.get("baudrate_wheel", "115200"))

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.init_vjoy()
        self.refresh_ports()
        
        # Auto-connect saved COM ports on startup
        self.after(1000, self.auto_connect)

    # =================================================================
    #  UI BUILD
    # =================================================================
    def build_ui(self):
        # ---------- Title Bar ----------
        title_bar = tk.Frame(self, bg=BG_DEEP)
        title_bar.pack(fill="x", padx=0, pady=0)

        tk.Label(title_bar, text="SIM RACING",
                 font=("Segoe UI", 18, "bold"),
                 fg=TEXT_MAIN, bg=BG_DEEP).pack(side="left", padx=20, pady=14)
        tk.Label(title_bar, text="CONTROL PANEL",
                 font=("Segoe UI", 18),
                 fg=ACCENT, bg=BG_DEEP).pack(side="left", pady=14)

        # Version / status pill
        tk.Label(title_bar, textvariable=self.var_vjoy,
                 font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_DEEP).pack(side="right", padx=20)

        # Red separator under title
        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # ---------- Tab Bar ----------
        self._tab_frame = tk.Frame(self, bg=BG_DEEP)
        self._tab_frame.pack(fill="x")
        self._active_tab = tk.StringVar(value="dashboard")
        self._tab_btns = {}
        self._pages = {}
        for name, label in [("dashboard", "DASHBOARD"), ("settings", "SETTINGS"), ("logs", "LOGS")]:
            btn = tk.Label(self._tab_frame, text=label,
                           font=("Segoe UI", 9, "bold"),
                           fg=TEXT_SUB, bg=BG_DEEP,
                           padx=22, pady=10, cursor="hand2")
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e, n=name: self._switch_tab(n))
            self._tab_btns[name] = btn

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # ---------- Page container ----------
        container = tk.Frame(self, bg=BG_DEEP)
        container.pack(fill="both", expand=True)

        self._pages["dashboard"] = self._build_dashboard(container)
        self._pages["settings"]  = self._build_settings(container)
        self._pages["logs"]      = self._build_logs(container)

        self._switch_tab("dashboard")

    def _switch_tab(self, name):
        self._active_tab.set(name)
        for n, page in self._pages.items():
            if n == name:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for n, btn in self._tab_btns.items():
            if n == name:
                btn.configure(fg=TEXT_MAIN, bg=BG_DEEP)
                # Underline active: draw a red bottom border using a thin frame trick
                btn.configure(relief="flat")
            else:
                btn.configure(fg=TEXT_SUB, bg=BG_DEEP)

    # ---- DASHBOARD PAGE ----
    def _build_dashboard(self, parent):
        page = tk.Frame(parent, bg=BG_DEEP)

        left = tk.Frame(page, bg=BG_DEEP)
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=12)

        right = tk.Frame(page, bg=BG_DEEP)
        right.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)

        # ---- Board 1 : Pedals ----
        p1 = self._make_panel(left, "Board 1  ·  Pedals")
        self._build_connection_row(p1,
            port_var=self.port_ped_var,
            baud_var=self.baud_ped_var,
            status_var=self.var_ped_st,
            btn_cmd=self.toggle_pedals,
            btn_attr="btn_connect_ped",
            lbl_attr="lbl_ped_status_color")
        p1.pack(fill="x", pady=(0, 8))

        # ---- Board 2 : Wheel & FFB ----
        p2 = self._make_panel(left, "Board 2  ·  Wheel & FFB")
        self._build_connection_row(p2,
            port_var=self.port_whl_var,
            baud_var=self.baud_whl_var,
            status_var=self.var_whl_st,
            btn_cmd=self.toggle_wheel,
            btn_attr="btn_connect_whl",
            lbl_attr="lbl_whl_status_color")
        p2.pack(fill="x", pady=(0, 8))

        # ---- AC Status ----
        p_ac = self._make_panel(left, "Assetto Corsa")
        ac_row = tk.Frame(p_ac, bg=BG_PANEL)
        ac_row.pack(fill="x", padx=16, pady=(4, 12))
        tk.Label(ac_row, text="Shared Memory",
                 font=("Segoe UI", 9), fg=TEXT_SUB, bg=BG_PANEL).pack(side="left")
        self.lbl_ac_dot = tk.Label(ac_row, text="●",
                                   font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG_PANEL)
        self.lbl_ac_dot.pack(side="right", padx=(0, 2))
        self.lbl_ac_status = tk.Label(ac_row, textvariable=self.var_ac_st,
                                      font=("Consolas", 9, "bold"),
                                      fg=TEXT_DIM, bg=BG_PANEL)
        self.lbl_ac_status.pack(side="right", padx=(0, 8))
        p_ac.pack(fill="x", pady=(0, 8))

        # ---- Mobile Backup Wheel ----
        p_mob = self._make_panel(left, "Board 3  ·  Mobile Wheel (Backup)")
        mob_row = tk.Frame(p_mob, bg=BG_PANEL)
        mob_row.pack(fill="x", padx=16, pady=(4, 4))
        
        tk.Label(mob_row, text="IP", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_PANEL, width=7, anchor="w").pack(side="left")
        tk.Label(mob_row, textvariable=self.var_mobile_ip,
                 font=("Consolas", 9), fg=TEXT_MAIN, bg=BG_PANEL).pack(side="left", padx=(0, 10))

        self.btn_connect_mob = tk.Label(mob_row, text="START APP",
                       font=("Segoe UI", 8, "bold"),
                       fg=TEXT_MAIN, bg=ACCENT_DIM,
                       padx=12, pady=4, cursor="hand2")
        self.btn_connect_mob.pack(side="right")
        self.btn_connect_mob.bind("<Button-1>", lambda e: self.toggle_mobile_server())
        self.btn_connect_mob.bind("<Enter>", lambda e: self.btn_connect_mob.configure(bg=ACCENT))
        self.btn_connect_mob.bind("<Leave>", lambda e: self._btn_leave(self.btn_connect_mob))

        st_row_m = tk.Frame(p_mob, bg=BG_PANEL)
        st_row_m.pack(fill="x", padx=16, pady=(0, 10))
        self.lbl_mob_status_color = tk.Label(st_row_m, text="●", font=("Segoe UI", 8),
                       fg=TEXT_DIM, bg=BG_PANEL)
        self.lbl_mob_status_color.pack(side="left")
        tk.Label(st_row_m, textvariable=self.var_mobile_st,
                 font=("Consolas", 8), fg=TEXT_DIM, bg=BG_PANEL).pack(side="left", padx=(4, 0))
        
        p_mob.pack(fill="x")

        # ---- Live Monitor ----
        pm = self._make_panel(right, "Live Monitor")
        separator(pm, pady=0)
        self.prog_steer   = self._monitor_row(pm, "STEERING",  center=True, color=ACCENT)
        self.prog_throttle = self._monitor_row(pm, "THROTTLE", color=GREEN_OK, var=self.var_thr)
        self.prog_brake    = self._monitor_row(pm, "BRAKE",    color=ACCENT,  var=self.var_brk)
        self.prog_ffb      = self._monitor_row(pm, "FFB",      center=True, color=ORANGE_WARN, var=self.var_ffb)

        # Steer value label sits next to bar — special row
        # (var_steer is handled inside _monitor_row via var param)
        self.prog_steer._label_var = self.var_steer  # keep ref
        pm.pack(fill="both", expand=True)

        return page

    def _make_panel(self, parent, title):
        """Card panel สไตล์ AC"""
        # ใช้ highlightthickness เพื่อทำขอบ (Border) โดยไม่ต้องสร้าง Frame ซ้อนกัน
        f = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER)
        
        # Title row
        title_row = tk.Frame(f, bg=BG_PANEL)
        title_row.pack(fill="x", padx=16, pady=(10, 6))
        tk.Label(title_row, text=title.upper(),
                 font=("Segoe UI", 8, "bold"),
                 fg=TEXT_SUB, bg=BG_PANEL).pack(side="left")
        return f

    def _build_connection_row(self, parent, port_var, baud_var,
                               status_var, btn_cmd, btn_attr, lbl_attr):
        # COM row
        com_row = tk.Frame(parent, bg=BG_PANEL)
        com_row.pack(fill="x", padx=16, pady=(0, 4))

        tk.Label(com_row, text="PORT", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_PANEL, width=7, anchor="w").pack(side="left")

        # Port combo
        port_combo = ttk.Combobox(com_row, textvariable=port_var,
                                   width=9, font=("Consolas", 9))
        port_combo.pack(side="left", padx=(0, 6))
        self._style_combo(port_combo)

        # Baud combo
        baud_combo = ttk.Combobox(com_row, textvariable=baud_var,
                                   values=["9600", "115200", "250000", "500000"],
                                   width=8, font=("Consolas", 9))
        baud_combo.pack(side="left", padx=(0, 10))
        self._style_combo(baud_combo)

        # Connect button
        btn = tk.Label(com_row, text="CONNECT",
                       font=("Segoe UI", 8, "bold"),
                       fg=TEXT_MAIN, bg=ACCENT_DIM,
                       padx=12, pady=4, cursor="hand2")
        btn.pack(side="right")
        btn.bind("<Button-1>", lambda e: btn_cmd())
        btn.bind("<Enter>", lambda e: btn.configure(bg=ACCENT))
        btn.bind("<Leave>", lambda e: self._btn_leave(btn))
        setattr(self, btn_attr, btn)
        setattr(self, "combo_" + btn_attr.replace("btn_connect_", "") + "_port", port_combo)

        # Refresh btn
        ref = tk.Label(com_row, text="↻",
                       font=("Segoe UI", 11),
                       fg=TEXT_DIM, bg=BG_PANEL,
                       cursor="hand2", padx=4)
        ref.pack(side="right", padx=(0, 4))
        ref.bind("<Button-1>", lambda e: self.refresh_ports())
        ref.bind("<Enter>", lambda e: ref.configure(fg=TEXT_MAIN))
        ref.bind("<Leave>", lambda e: ref.configure(fg=TEXT_DIM))

        # Status row
        st_row = tk.Frame(parent, bg=BG_PANEL)
        st_row.pack(fill="x", padx=16, pady=(0, 10))
        dot = tk.Label(st_row, text="●", font=("Segoe UI", 8),
                       fg=TEXT_DIM, bg=BG_PANEL)
        dot.pack(side="left")
        setattr(self, lbl_attr, dot)
        tk.Label(st_row, textvariable=status_var,
                 font=("Consolas", 8), fg=TEXT_DIM, bg=BG_PANEL).pack(side="left", padx=(4, 0))

    def _style_combo(self, combo):
        """Apply dark style to ttk Combobox"""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=BG_CARD,
                        background=BG_CARD,
                        foreground=TEXT_MAIN,
                        bordercolor=BORDER,
                        arrowcolor=TEXT_SUB,
                        selectbackground=BG_HOVER,
                        selectforeground=TEXT_MAIN,
                        insertcolor=TEXT_MAIN)
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG_CARD)],
                  selectbackground=[("readonly", BG_CARD)],
                  selectforeground=[("readonly", TEXT_MAIN)])
        combo.configure(style="TCombobox")

    def _btn_leave(self, btn):
        """ปุ่ม hover out — check ว่า active หรือเปล่า"""
        current = btn.cget("text")
        if "DISCONNECT" in current or "DISCONNECT" == current:
            btn.configure(bg=ACCENT)
        else:
            btn.configure(bg=ACCENT_DIM)

    def _monitor_row(self, parent, label, color=ACCENT, center=False, var=None):
        """แถว Monitor สไตล์ AC — label + bar + value"""
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill="x", padx=16, pady=6)

        lbl_container = tk.Frame(f, bg=BG_PANEL, width=70, height=20)
        lbl_container.pack_propagate(False)
        lbl_container.pack(side="left")

        tk.Label(lbl_container, text=label,
                 font=("Segoe UI", 8, "bold"),
                 fg=TEXT_SUB, bg=BG_PANEL, anchor="w").pack(side="left", fill="both", expand=True)

        bar = ACBar(f, bar_width=260, bar_height=5, color=color, center=center)
        bar.pack(side="left", padx=(6, 10))
        bar.set(0.5 if center else 0.0)

        val_var = var if var else (self.var_steer if label == "STEERING" else tk.StringVar(value="—"))
        tk.Label(f, textvariable=val_var,
                 font=("Consolas", 9, "bold"),
                 fg=TEXT_MAIN, bg=BG_PANEL, width=9, anchor="e").pack(side="right")
        return bar

    # ---- SETTINGS PAGE ----
    def _build_settings(self, parent):
        page = tk.Frame(parent, bg=BG_DEEP)

        # Scrollable frame simulation (two columns)
        left = tk.Frame(page, bg=BG_DEEP)
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=12)

        right = tk.Frame(page, bg=BG_DEEP)
        right.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)

        # ---- Pedals ----
        pp = self._make_panel(left, "Pedals Calibration")
        self._setting_slider(pp, "Throttle Deadzone (Min)", self.t_min_var, 0, 49)
        self._setting_slider(pp, "Brake Deadzone (Min)",    self.b_min_var, 0, 49)
        pp.pack(fill="x", pady=(0, 8))

        # ---- Steering ----
        sp = self._make_panel(left, "Steering Calibration")
        self._setting_slider(sp, "Max Rotation (degrees)", self.steer_angle_var, 180, 1440)
        self._setting_slider(sp, "Center Offset (degrees)", self.steer_center_var, -90, 90)
        
        # Add Invert Steering Switch
        sw_steer = ctk.CTkSwitch(sp, text="INVERT STEERING", variable=self.invert_steer_var,
                                 progress_color=ACCENT, fg_color=BG_HOVER, button_color=TEXT_MAIN,
                                 button_hover_color=ACCENT, command=self.save_config_delayed)
        sw_steer.pack(padx=16, pady=8, anchor="w")
        sp.pack(fill="x")

        # ---- FFB ----
        fp = self._make_panel(right, "Force Feedback & Hardware")
        self._setting_slider(fp, "FFB Gain", self.ffb_gain_var, 0, 100, suffix="%")
        
        # Add Invert FFB Switch
        sw_ffb = ctk.CTkSwitch(fp, text="INVERT FORCE FEEDBACK", variable=self.invert_ffb_var,
                               progress_color=ACCENT, fg_color=BG_HOVER, button_color=TEXT_MAIN,
                               button_hover_color=ACCENT, command=self.save_config_delayed)
        sw_ffb.pack(padx=16, pady=(8, 12), anchor="w")

        # Add Test Motor Button
        btn_test = ctk.CTkButton(fp, text="TEST MOTOR SWEEP", fg_color=ACCENT_DIM, hover_color=ACCENT,
                                 text_color=TEXT_MAIN, font=("Segoe UI", 9, "bold"),
                                 command=self.test_motor)
        btn_test.pack(fill="x", padx=16, pady=(4, 4))

        # Add Re-Center Button
        btn_align = ctk.CTkButton(fp, text="RE-CENTER WHEEL", fg_color="#1a3a1a", hover_color="#2a6a2a",
                                  text_color=TEXT_MAIN, font=("Segoe UI", 9, "bold"),
                                  command=self.send_align_command)
        btn_align.pack(fill="x", padx=16, pady=(0, 16))
        fp.pack(fill="x")

        return page

    def test_motor(self):
        if self.ser_wheel and self.ser_wheel.is_open:
            try:
                self.ser_wheel.write(b"TEST\n")
            except Exception as e:
                print(f"Test Motor Error: {e}")

    # ---- LOGS PAGE ----
    def _build_logs(self, parent):
        page = tk.Frame(parent, bg=BG_DEEP)

        # Split into two panels (left/right columns)
        left = self._make_panel(page, "Board 1  ·  Pedals Logs")
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=12)

        right = self._make_panel(page, "Board 2  ·  Wheel Logs")
        right.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)

        # Pedals log text box
        self.txt_ped_log = tk.Text(left, bg=BG_CARD, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
                                   highlightthickness=1, highlightbackground=BORDER, relief="flat",
                                   font=("Consolas", 9), height=20, width=30)
        self.txt_ped_log.pack(fill="both", expand=True, padx=16, pady=(10, 16))
        
        # Wheel log text box
        self.txt_whl_log = tk.Text(right, bg=BG_CARD, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
                                   highlightthickness=1, highlightbackground=BORDER, relief="flat",
                                   font=("Consolas", 9), height=20, width=30)
        self.txt_whl_log.pack(fill="both", expand=True, padx=16, pady=(10, 16))

        return page

    def add_log(self, text_widget, line):
        if text_widget:
            self.after(0, self._insert_log_main_thread, text_widget, line)

    def _insert_log_main_thread(self, text_widget, line):
        try:
            text_widget.insert("end", line + "\n")
            # Keep log size limited to last 100 lines to avoid memory leak
            num_lines = int(text_widget.index('end-1c').split('.')[0])
            if num_lines > 100:
                text_widget.delete("1.0", "2.0")
            text_widget.see("end")
        except Exception:
            pass

    def _setting_slider(self, parent, label, var, from_, to, suffix=""):
        """Slider row สไตล์ AC — label บน, slider + value ล่าง"""
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill="x", padx=16, pady=(4, 8))

        header = tk.Frame(f, bg=BG_PANEL)
        header.pack(fill="x")
        tk.Label(header, text=label,
                 font=("Segoe UI", 9), fg=TEXT_SUB, bg=BG_PANEL,
                 anchor="w").pack(side="left")

        val_lbl = tk.Label(header, font=("Consolas", 9, "bold"),
                           fg=ACCENT, bg=BG_PANEL)
        val_lbl.pack(side="right")

        def update_val(*a):
            v = var.get()
            val_lbl.configure(text=f"{v:.0f}{suffix}")
            self.save_config_delayed()

        var.trace_add("write", update_val)
        update_val()

        slider = ctk.CTkSlider(f, variable=var, from_=from_, to=to,
                               fg_color=BG_HOVER,
                               progress_color=ACCENT,
                               button_color=TEXT_MAIN,
                               button_hover_color=ACCENT,
                               height=14)
        slider.pack(fill="x", pady=(8, 4))

    # =================================================================
    #  PORTS
    # =================================================================
    def refresh_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        if not ports:
            ports = ["No Ports Found"]

        # Update both combos
        try:
            self.combo_ped_port.configure(values=ports)
            self.combo_whl_port.configure(values=ports)
            if self.port_ped_var.get() not in ports and ports[0] != "No Ports Found":
                self.combo_ped_port.set(ports[0])
            if self.port_whl_var.get() not in ports and ports[0] != "No Ports Found" and len(ports) > 1:
                self.combo_whl_port.set(ports[1])
        except Exception:
            pass

    # =================================================================
    #  vJOY
    # =================================================================
    def init_vjoy(self):
        try:
            self.vjoy_dev = pyvjoy.VJoyDevice(VJOY_DEVICE_ID)
            self.var_vjoy.set("vJoy  ✓  Device 1")
        except Exception as e:
            self.var_vjoy.set(f"vJoy  ✗  Monitor Only")
            self.var_ped_st.set("vJoy NOT ENABLED")
            self.after(500, self.prompt_auto_vjoy)

    def prompt_auto_vjoy(self):
        vjoy_config_path = "C:\\Program Files\\vJoy\\x64\\vJoyConfig.exe"
        if not os.path.exists(vjoy_config_path):
            messagebox.showerror("vJoy Error", 
                "ไม่พบโปรแกรม vJoy ในเครื่อง กรุณาติดตั้งโปรแกรม vJoy ก่อนใช้งานครับ\n"
                "(vJoy is not installed. Please install vJoy.)")
            return

        ans = messagebox.askyesno("vJoy Not Enabled", 
            "ตรวจพบว่าไดรเวอร์ vJoy ยังไม่ได้เปิดใช้งาน หรือตั้งค่าไม่ถูกต้อง\n\n"
            "ต้องการให้โปรแกรมเปิดใช้งานและตั้งค่า vJoy (Device 1) อัตโนมัติหรือไม่?\n"
            "(ระบบจะขอสิทธิ์ Admin เพื่อเปิดโปรแกรมตั้งค่า)")
        
        if ans:
            try:
                # 1. Enable vJoy (runs as admin)
                ctypes.windll.shell32.ShellExecuteW(None, "runas", vjoy_config_path, "enable on", None, 1)
                time.sleep(2.5)
                # 2. Add & Configure Device 1 (X, Y, Z axes, 8 buttons)
                ctypes.windll.shell32.ShellExecuteW(None, "runas", vjoy_config_path, "1 -f -a x y z -b 8", None, 1)
                
                messagebox.showinfo("vJoy Configured", 
                    "ส่งคำสั่งตั้งค่า vJoy เรียบร้อยแล้วครับ!\n"
                    "กรุณาปิดโปรแกรมและเปิดใหม่อีกครั้งเพื่อเริ่มใช้งานครับ")
            except Exception as err:
                messagebox.showerror("vJoy Error", f"ไม่สามารถตั้งค่า vJoy ได้: {err}")

    def auto_connect(self):
        # Auto connect pedals if port exists in config and is currently available
        port_ped = self.port_ped_var.get()
        if port_ped and port_ped != "No Ports Found" and port_ped != "":
            self.connect_pedals()
        
        # Auto connect wheel if port exists in config and is currently available
        port_whl = self.port_whl_var.get()
        if port_whl and port_whl != "No Ports Found" and port_whl != "" and port_whl != port_ped:
            self.connect_wheel()

    # =================================================================
    #  STATUS HELPERS
    # =================================================================
    def _set_status(self, dot_attr, var, text, color):
        var.set(text)
        try:
            getattr(self, dot_attr).configure(fg=color)
        except Exception:
            pass

    # =================================================================
    #  PEDALS BOARD 1
    # =================================================================
    def toggle_pedals(self):
        if self.is_pedals_connected:
            self.disconnect_pedals()
        else:
            self.connect_pedals()

    def connect_pedals(self):
        port = self.port_ped_var.get()
        baudrate = self.baud_ped_var.get()
        if port == "No Ports Found" or not port:
            return
        try:
            self.ser_pedals = serial.Serial(port, int(baudrate), timeout=0.1)
            self.ser_pedals.reset_input_buffer()
            self.is_pedals_connected = True

            self.btn_connect_ped.configure(text="DISCONNECT", bg=ACCENT)
            self._set_status("lbl_ped_status_color", self.var_ped_st,
                             f"CONNECTED  ·  {port}", GREEN_OK)

            self.pedals_thread = threading.Thread(target=self.pedals_loop, daemon=True)
            self.pedals_thread.start()
            self.save_config_delayed()
        except Exception as e:
            self._set_status("lbl_ped_status_color", self.var_ped_st,
                             f"ERROR: {e}", ACCENT)

    def disconnect_pedals(self):
        self.is_pedals_connected = False
        if self.ser_pedals and self.ser_pedals.is_open:
            self.ser_pedals.close()
        self.btn_connect_ped.configure(text="CONNECT", bg=ACCENT_DIM)
        self._set_status("lbl_ped_status_color", self.var_ped_st, "DISCONNECTED", TEXT_DIM)

    def pedals_loop(self):
        while self.is_pedals_connected and self.ser_pedals.is_open:
            try:
                if self.ser_pedals.in_waiting > 0:
                    line = ""
                    while self.ser_pedals.in_waiting > 0:
                        try:
                            line = self.ser_pedals.readline().decode('utf-8', errors='ignore').strip()
                        except Exception:
                            pass
                    if line:
                        self.process_pedals_data(line)
                time.sleep(0.005)
            except Exception as e:
                self.after(0, self.handle_pedals_error, str(e))
                break

    def handle_pedals_error(self, err):
        self.disconnect_pedals()
        self._set_status("lbl_ped_status_color", self.var_ped_st,
                         f"LOST: {err}", ACCENT)

    def process_pedals_data(self, line):
        self.add_log(self.txt_ped_log, line)
        try:
            data = json.loads(line)
            raw_t = data.get('Throttle', 0)
            raw_b = data.get('Brake', 0)

            t_pct = map_value(raw_t, self.config.get("throttle_min", 0), 100, 0, 100)
            b_pct = map_value(raw_b, self.config.get("brake_min", 0), 100, 0, 100)

            if self.vjoy_dev:
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_X, map_value(t_pct, 0, 100, 0, VJOY_MAX))
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_Y, map_value(b_pct, 0, 100, 0, VJOY_MAX))

            self.after(0, self.update_pedals_ui, t_pct, b_pct)
        except Exception:
            pass

    def update_pedals_ui(self, t_pct, b_pct):
        self.prog_throttle.set(t_pct / 100.0)
        self.prog_brake.set(b_pct / 100.0)
        self.var_thr.set(f"{t_pct}%")
        self.var_brk.set(f"{b_pct}%")

    # =================================================================
    #  WHEEL BOARD 2
    # =================================================================
    def toggle_wheel(self):
        if self.is_wheel_connected:
            self.disconnect_wheel()
        else:
            self.connect_wheel()

    def connect_wheel(self):
        port = self.port_whl_var.get()
        baudrate = self.baud_whl_var.get()
        if port == "No Ports Found" or not port:
            return
        try:
            self.ser_wheel = serial.Serial(port, int(baudrate), timeout=0.1)
            self.ser_wheel.reset_input_buffer()
            self.is_wheel_connected = True

            self.btn_connect_whl.configure(text="DISCONNECT", bg=ACCENT)
            self._set_status("lbl_whl_status_color", self.var_whl_st,
                             f"CONNECTED  ·  {port}", GREEN_OK)

            self.wheel_thread = threading.Thread(target=self.wheel_loop, daemon=True)
            self.wheel_thread.start()
            self.ffb_thread = threading.Thread(target=self.ac_ffb_loop, daemon=True)
            self.ffb_thread.start()
            
            # ส่งคำสั่ง ALIGN หลังจากเชื่อมต่อ 2 วินาที เพื่อให้บอร์ดบู้ตเสร็จและกลับศูนย์กลางแบบนุ่มนวล
            self.after(2000, self.send_align_command)
            
            self.save_config_delayed()
        except Exception as e:
            self._set_status("lbl_whl_status_color", self.var_whl_st,
                             f"ERROR: {e}", ACCENT)

    def send_align_command(self):
        if self.ser_wheel and self.ser_wheel.is_open:
            try:
                self.ser_wheel.write(b"ALIGN\n")
            except Exception:
                pass

    def disconnect_wheel(self):
        self.is_wheel_connected = False
        if self.ser_wheel and self.ser_wheel.is_open:
            self.ser_wheel.close()
        self.btn_connect_whl.configure(text="CONNECT", bg=ACCENT_DIM)
        self._set_status("lbl_whl_status_color", self.var_whl_st, "DISCONNECTED", TEXT_DIM)

        self.prog_steer.set(0.5)
        self.var_steer.set("0.0°")
        self.prog_ffb.set(0.5)
        self.var_ffb.set("±0%")

    def wheel_loop(self):
        while self.is_wheel_connected and self.ser_wheel.is_open:
            try:
                if self.ser_wheel.in_waiting > 0:
                    line = ""
                    while self.ser_wheel.in_waiting > 0:
                        try:
                            line = self.ser_wheel.readline().decode('utf-8', errors='ignore').strip()
                        except Exception:
                            pass
                    if line:
                        self.process_wheel_data(line)
                time.sleep(0.005)
            except Exception as e:
                self.after(0, self.handle_wheel_error, str(e))
                break

    def handle_wheel_error(self, err):
        self.disconnect_wheel()
        self._set_status("lbl_whl_status_color", self.var_whl_st,
                         f"LOST: {err}", ACCENT)

    def process_wheel_data(self, line):
        self.add_log(self.txt_whl_log, line)
        try:
            data = json.loads(line)
            raw_steer = data.get('Steer', 0)
            gear_up   = data.get('GearUp', 0)
            gear_down = data.get('GearDown', 0)

            steer_center = self.config.get("steer_center", 0)
            raw_steer -= steer_center

            # Apply Invert Steering
            if self.invert_steer_var.get():
                raw_steer = -raw_steer

            steer_angle = self.config.get("steer_angle", 900)
            s_min = -steer_angle / 2.0
            s_max = steer_angle / 2.0
            vjoy_steer = map_value(raw_steer, s_min, s_max, 0, VJOY_MAX)

            if self.vjoy_dev:
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_Z, vjoy_steer)
                self.vjoy_dev.set_button(1, 1 if gear_up else 0)
                self.vjoy_dev.set_button(2, 1 if gear_down else 0)

            self.after(0, self.update_wheel_ui, raw_steer, s_min, s_max)
        except Exception as e:
            print(f"Wheel JSON Error: {e} | Raw line: {line}")

    def update_wheel_ui(self, steer_angle, s_min, s_max):
        norm_val = (steer_angle - s_min) / (s_max - s_min) if s_max != s_min else 0.5
        norm_val = max(0.0, min(1.0, norm_val))
        self.prog_steer.set(norm_val)
        self.var_steer.set(f"{steer_angle:.1f}°")

    # =================================================================
    #  MOBILE APP SERVER
    # =================================================================
    def toggle_mobile_server(self):
        if self.is_mobile_server_running:
            self.stop_mobile_server()
        else:
            self.start_mobile_server()

    def start_mobile_server(self):
        try:
            self.is_mobile_server_running = True
            local_ip = get_local_ip()
            self.var_mobile_ip.set(f"http://{local_ip}:8000")
            
            # Start HTTP Server
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory="mobile_app")
            # Allow reusing address to prevent "Address already in use" errors on restart
            socketserver.TCPServer.allow_reuse_address = True
            self.httpd = socketserver.TCPServer(("", 8000), handler)
            self.mobile_http_thread = threading.Thread(target=self._run_http_server, daemon=True)
            self.mobile_http_thread.start()

            # Start WebSocket Server
            self.mobile_ws_thread = threading.Thread(target=self._run_ws_server, daemon=True)
            self.mobile_ws_thread.start()

            self.btn_connect_mob.configure(text="STOP APP", bg=ACCENT)
            self._set_status("lbl_mob_status_color", self.var_mobile_st, "RUNNING (WAITING)", ORANGE_WARN)
        except Exception as e:
            self._set_status("lbl_mob_status_color", self.var_mobile_st, f"ERROR: {e}", ACCENT)
            self.is_mobile_server_running = False
    
    def _run_http_server(self):
        try:
            self.httpd.serve_forever()
        except Exception:
            pass
            
    def _run_ws_server(self):
        self.mobile_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.mobile_loop)
        # Using handle_ws wrapper for backwards compatibility with websockets versions
        start_server = websockets.serve(self._ws_handler, "0.0.0.0", 8765)
        self.mobile_loop.run_until_complete(start_server)
        self.mobile_loop.run_forever()
        
    async def _ws_handler(self, websocket, path=None):
        self.after(0, lambda: self._set_status("lbl_mob_status_color", self.var_mobile_st, "CONNECTED TO PHONE", GREEN_OK))
        try:
            async for message in websocket:
                self.process_mobile_data(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.after(0, lambda: self._set_status("lbl_mob_status_color", self.var_mobile_st, "RUNNING (WAITING)", ORANGE_WARN))

    def stop_mobile_server(self):
        self.is_mobile_server_running = False
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except: pass
        try:
            if self.mobile_loop:
                self.mobile_loop.call_soon_threadsafe(self.mobile_loop.stop)
        except: pass
        self.btn_connect_mob.configure(text="START APP", bg=ACCENT_DIM)
        self._set_status("lbl_mob_status_color", self.var_mobile_st, "STOPPED", TEXT_DIM)
        self.var_mobile_ip.set("—")
        
    def process_mobile_data(self, message):
        try:
            data = json.loads(message)
            raw_steer = data.get('steer', 0)
            gear_up = data.get('gearUp', 0)
            gear_down = data.get('gearDown', 0)

            steer_center = self.config.get("steer_center", 0)
            raw_steer -= steer_center

            if self.invert_steer_var.get():
                raw_steer = -raw_steer
            
            # Mobile tilt max is roughly 90 degrees
            s_min = -90.0
            s_max = 90.0
            
            vjoy_steer = map_value(raw_steer, s_min, s_max, 0, VJOY_MAX)

            if self.vjoy_dev:
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_Z, vjoy_steer)
                self.vjoy_dev.set_button(1, 1 if gear_up else 0)
                self.vjoy_dev.set_button(2, 1 if gear_down else 0)

            self.after(0, self.update_wheel_ui, raw_steer, s_min, s_max)
        except Exception as e:
            print(f"Mobile JSON Error: {e} | Raw line: {message}")

    # =================================================================
    #  AC FFB
    # =================================================================
    def ac_ffb_loop(self):
        shm_physics = None
        while self.is_wheel_connected:
            if not self.ac_connected:
                try:
                    shm_physics = mmap.mmap(0, ctypes.sizeof(SPageFilePhysics), "acpmf_physics")
                    self.ac_connected = True
                    self.after(0, lambda: (
                        self.var_ac_st.set("CONNECTED"),
                        self.lbl_ac_dot.configure(fg=GREEN_OK),
                        self.lbl_ac_status.configure(fg=GREEN_OK)
                    ))
                except Exception:
                    self.ac_connected = False
                    self.after(0, lambda: (
                        self.var_ac_st.set("WAITING"),
                        self.lbl_ac_dot.configure(fg=TEXT_DIM),
                        self.lbl_ac_status.configure(fg=TEXT_DIM)
                    ))
                    time.sleep(2.0)
                    continue

            try:
                shm_physics.seek(0)
                physics = SPageFilePhysics.from_buffer(shm_physics)
                ffb_val = physics.finalFF
                gain = self.config.get("ffb_gain", 100) / 100.0
                gained_ffb = ffb_val * gain

                # Apply Invert FFB
                if self.invert_ffb_var.get():
                    gained_ffb = -gained_ffb

                ffb_output = int(max(-1.0, min(1.0, gained_ffb)) * 127)

                if self.ser_wheel and self.ser_wheel.is_open:
                    # ✅ BUG FIX: ส่งโดยไม่มีช่องว่างหลัง : เพื่อให้ Arduino parse ได้ถูกต้อง
                    self.ser_wheel.write(f"FFB:{ffb_output}\n".encode())

                self.after(0, self.update_ffb_ui, gained_ffb)
            except Exception:
                self.ac_connected = False
                self.after(0, lambda: (
                    self.var_ac_st.set("DISCONNECTED"),
                    self.lbl_ac_dot.configure(fg=ACCENT),
                    self.lbl_ac_status.configure(fg=ACCENT)
                ))
                time.sleep(1.0)

            time.sleep(0.01)

        if shm_physics:
            try:
                shm_physics.close()
            except BufferError:
                pass

    def update_ffb_ui(self, gained_ffb):
        pct = gained_ffb * 100.0
        self.var_ffb.set(f"{pct:+.0f}%")
        self.prog_ffb.set((gained_ffb + 1.0) / 2.0)

    # =================================================================
    #  CONFIG
    # =================================================================
    def sync_config_cache(self):
        self.config["throttle_min"] = self.t_min_var.get()
        self.config["brake_min"]    = self.b_min_var.get()
        self.config["steer_angle"]  = self.steer_angle_var.get()
        self.config["steer_center"] = self.steer_center_var.get()
        self.config["ffb_gain"]     = self.ffb_gain_var.get()
        self.config["invert_steer"] = self.invert_steer_var.get()
        self.config["invert_ffb"]   = self.invert_ffb_var.get()

    def save_config_delayed(self, *args):
        self.sync_config_cache()
        if self.save_timer:
            self.after_cancel(self.save_timer)
        self.save_timer = self.after(1000, self.save_config)

    def save_config(self):
        self.sync_config_cache()
        self.config.update({
            "port_pedals":    self.port_ped_var.get(),
            "port_wheel":     self.port_whl_var.get(),
            "baudrate_pedals": self.baud_ped_var.get(),
            "baudrate_wheel":  self.baud_whl_var.get(),
        })
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception:
            pass

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    self.config.update(json.load(f))
            except Exception:
                pass

    def on_closing(self):
        self.is_pedals_connected = False
        self.is_wheel_connected = False
        if self.ser_pedals and self.ser_pedals.is_open:
            self.ser_pedals.close()
        if self.ser_wheel and self.ser_wheel.is_open:
            self.ser_wheel.close()
        if self.is_mobile_server_running:
            self.stop_mobile_server()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
