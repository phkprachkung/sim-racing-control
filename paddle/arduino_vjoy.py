"""
SIM RACING CONTROL — Pro Rewrite
Architecture:
  - SerialReader  : Thread-safe serial I/O with queue-based UI callbacks
  - MobileServer  : Clean asyncio HTTP + WebSocket server in its own thread
  - App           : Tkinter UI, polls a queue at 60fps for thread-safe updates
"""

import serial
import serial.tools.list_ports
import json
import time
import threading
import queue
import sys
import os
import ctypes
import mmap
import asyncio
import websockets
import http.server
import socketserver
import functools
import socket

import pyvjoy
import tkinter as tk
from tkinter import ttk, messagebox
import customtkinter as ctk

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
MOBILE_DIR  = os.path.join(BASE_DIR, "mobile_app")

# ─── vJoy ─────────────────────────────────────────────────────────────────────
VJOY_DEVICE_ID = 1
VJOY_MAX       = 32767

# ─── Color Palette (AC Content Manager) ──────────────────────────────────────
BG_DEEP     = "#0d0d0d"
BG_PANEL    = "#141414"
BG_CARD     = "#1a1a1a"
BG_HOVER    = "#222222"
ACCENT      = "#E4002B"
ACCENT_DIM  = "#8B0018"
TEXT_MAIN   = "#FFFFFF"
TEXT_SUB    = "#888888"
TEXT_DIM    = "#444444"
BORDER      = "#2a2a2a"
GREEN_OK    = "#2ECC71"
ORANGE_WARN = "#E67E22"


# ─────────────────────────────────────────────────────────────────────────────
#  AC Shared Memory Struct
# ─────────────────────────────────────────────────────────────────────────────
class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId',             ctypes.c_int32),
        ('gas',                  ctypes.c_float),
        ('brake',                ctypes.c_float),
        ('fuel',                 ctypes.c_float),
        ('gear',                 ctypes.c_int32),
        ('rpms',                 ctypes.c_int32),
        ('steerAngle',           ctypes.c_float),
        ('speedKmh',             ctypes.c_float),
        ('velocity',             ctypes.c_float * 3),
        ('accG',                 ctypes.c_float * 3),
        ('wheelSlip',            ctypes.c_float * 4),
        ('wheelLoad',            ctypes.c_float * 4),
        ('wheelsPressure',       ctypes.c_float * 4),
        ('wheelAngularSpeed',    ctypes.c_float * 4),
        ('tyreWear',             ctypes.c_float * 4),
        ('tyreDirtyLevel',       ctypes.c_float * 4),
        ('tyreCoreTemperature',  ctypes.c_float * 4),
        ('camberRAD',            ctypes.c_float * 4),
        ('suspensionTravel',     ctypes.c_float * 4),
        ('drs',                  ctypes.c_float),
        ('tc',                   ctypes.c_float),
        ('heading',              ctypes.c_float),
        ('pitch',                ctypes.c_float),
        ('roll',                 ctypes.c_float),
        ('cgHeight',             ctypes.c_float),
        ('carDamage',            ctypes.c_float * 5),
        ('numberOfTyresOut',     ctypes.c_int32),
        ('pitLimiterOn',         ctypes.c_int32),
        ('abs',                  ctypes.c_float),
        ('kersCharge',           ctypes.c_float),
        ('kersInput',            ctypes.c_float),
        ('autoShifterOn',        ctypes.c_int32),
        ('rideHeight',           ctypes.c_float * 2),
        ('turboBoost',           ctypes.c_float),
        ('ballast',              ctypes.c_float),
        ('airDensity',           ctypes.c_float),
        ('airTemp',              ctypes.c_float),
        ('roadTemp',             ctypes.c_float),
        ('localAngularVel',      ctypes.c_float * 3),
        ('finalFF',              ctypes.c_float),
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────
def map_value(value, in_min, in_max, out_min, out_max):
    if in_max == in_min:
        return (out_min + out_max) // 2
    value = max(in_min, min(in_max, value))
    return int((value - in_min) * (out_max - out_min) / (in_max - in_min) + out_min)


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Thread-safe Serial Reader
# ─────────────────────────────────────────────────────────────────────────────
class SerialReader:
    """
    Runs a serial read loop in a daemon thread.
    Delivers parsed lines to a callback (called from the thread).
    """
    def __init__(self, port, baudrate, on_line, on_error):
        self._port     = port
        self._baud     = int(baudrate)
        self._on_line  = on_line
        self._on_error = on_error
        self._ser      = None
        self._running  = False
        self._thread   = None

    def start(self):
        self._ser = serial.Serial(self._port, self._baud, timeout=0.1)
        self._ser.reset_input_buffer()
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass

    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    def write(self, data: bytes):
        try:
            if self._ser and self._ser.is_open:
                self._ser.write(data)
        except Exception:
            pass

    def _loop(self):
        while self._running:
            try:
                if self._ser.in_waiting > 0:
                    # Drain the buffer — keep only the LATEST line
                    line = ""
                    while self._ser.in_waiting > 0:
                        raw = self._ser.readline()
                        try:
                            line = raw.decode('utf-8', errors='ignore').strip()
                        except Exception:
                            pass
                    if line:
                        self._on_line(line)
                else:
                    time.sleep(0.003)
            except Exception as e:
                if self._running:
                    self._on_error(str(e))
                break


# ─────────────────────────────────────────────────────────────────────────────
#  Mobile Server (HTTP + WebSocket) — fully self-contained
# ─────────────────────────────────────────────────────────────────────────────
class MobileServer:
    """
    Hosts the mobile web controller over HTTP (port 8000)
    and receives real-time telemetry over WebSocket (port 8765).
    Runs entirely in its own background thread with its own asyncio event loop.
    Thread-safe: communicates back via on_data / on_status callbacks.
    """
    HTTP_PORT = 8000
    WS_PORT   = 8765

    def __init__(self, mobile_dir, on_data, on_status):
        self._mobile_dir = mobile_dir
        self._on_data    = on_data      # callback(steer, gear_up, gear_down)
        self._on_status  = on_status    # callback(status_str, color_str)
        self._stop_event = threading.Event()
        self._loop       = None
        self._httpd      = None
        self._thread     = None
        self.running     = False

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True

    def stop(self):
        self._stop_event.set()
        self.running = False
        # Stop HTTP server
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            pass
        # Stop asyncio loop
        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    def _run(self):
        # --- HTTP ---
        mobile_dir = self._mobile_dir

        class SilentHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=mobile_dir, **kwargs)

            def log_message(self, format, *args):
                pass  # Suppress HTTP log spam

        socketserver.TCPServer.allow_reuse_address = True
        try:
            self._httpd = socketserver.TCPServer(("0.0.0.0", self.HTTP_PORT), SilentHandler)
            http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            http_thread.start()
        except Exception as e:
            self._on_status(f"HTTP ERROR: {e}", ACCENT)
            return

        # --- WebSocket (asyncio) ---
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _ws_main():
            async with websockets.serve(self._ws_handler, "0.0.0.0", self.WS_PORT):
                # Keep running until stop_event is set
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.1)

        try:
            self._loop.run_until_complete(_ws_main())
        except Exception:
            pass
        finally:
            self._loop.close()

    async def _ws_handler(self, websocket, path=None):
        self._on_status("PHONE CONNECTED", GREEN_OK)
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    self._on_data(
                        data.get('steer', 0),
                        data.get('gearUp', 0),
                        data.get('gearDown', 0)
                    )
                except Exception:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._on_status("RUNNING — WAITING", ORANGE_WARN)


# ─────────────────────────────────────────────────────────────────────────────
#  Custom Widgets
# ─────────────────────────────────────────────────────────────────────────────
class ACBar(tk.Canvas):
    """Slim progress bar, optionally center-anchored."""
    def __init__(self, parent, bar_width=260, bar_height=5,
                 color=ACCENT, center=False, **kwargs):
        super().__init__(parent, width=bar_width, height=bar_height,
                         bg=BG_PANEL, highlightthickness=0, **kwargs)
        self._bw     = bar_width
        self._bh     = bar_height
        self._color  = color
        self._center = center
        self.create_rectangle(0, 0, bar_width, bar_height,
                              fill=BG_HOVER, outline="", tags="bg")
        self.create_rectangle(0, 0, 0, bar_height,
                              fill=color, outline="", tags="fill")
        self.set(0.5 if center else 0.0)

    def set(self, val):
        val = max(0.0, min(1.0, val))
        if self._center:
            mid = self._bw / 2
            x = val * self._bw
            x0, x1 = (mid, x) if x >= mid else (x, mid)
        else:
            x0, x1 = 0, val * self._bw
        self.coords("fill", x0, 0, x1, self._bh)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Application
# ─────────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    # ── UI update queue (thread → main thread) ────────────────────────────
    _POLL_MS = 16  # ~60 fps UI refresh

    def __init__(self):
        super().__init__()

        self.title("SIM RACING CONTROL")
        self.geometry("780x700")
        self.resizable(False, False)
        self.configure(bg=BG_DEEP)

        # Thread-safe UI update queue
        self._ui_queue: queue.Queue = queue.Queue()

        # Hardware state
        self._pedals: SerialReader | None = None
        self._wheel:  SerialReader | None = None
        self.is_pedals_connected = False
        self.is_wheel_connected  = False
        self.vjoy_dev            = None
        self.ac_connected        = False
        self._ffb_thread         = None
        self._mobile: MobileServer | None = None
        self.save_timer          = None

        # Logging widgets (set later in build_ui)
        self.txt_ped_log = None
        self.txt_whl_log = None

        # StringVars for live display
        self.var_steer    = tk.StringVar(value="0.0°")
        self.var_thr      = tk.StringVar(value="0%")
        self.var_brk      = tk.StringVar(value="0%")
        self.var_ffb      = tk.StringVar(value="±0%")
        self.var_ped_st   = tk.StringVar(value="DISCONNECTED")
        self.var_whl_st   = tk.StringVar(value="DISCONNECTED")
        self.var_vjoy     = tk.StringVar(value="—")
        self.var_ac_st    = tk.StringVar(value="WAITING")
        self.var_mob_st   = tk.StringVar(value="STOPPED")
        self.var_mob_ip   = tk.StringVar(value="—")

        # Load config & create tkinter vars
        self.config = {
            "port_pedals":      "",
            "port_wheel":       "",
            "baudrate_pedals":  "115200",
            "baudrate_wheel":   "115200",
            "throttle_min":     0,
            "brake_min":        0,
            "steer_angle":      900,
            "steer_center":     0,
            "ffb_gain":         100,
            "invert_steer":     False,
            "invert_ffb":       False,
        }
        self._load_config()

        self.t_min_var       = tk.DoubleVar(value=self.config["throttle_min"])
        self.b_min_var       = tk.DoubleVar(value=self.config["brake_min"])
        self.steer_angle_var = tk.DoubleVar(value=self.config["steer_angle"])
        self.steer_center_var= tk.DoubleVar(value=self.config["steer_center"])
        self.ffb_gain_var    = tk.DoubleVar(value=self.config["ffb_gain"])
        self.invert_steer_var= tk.BooleanVar(value=self.config["invert_steer"])
        self.invert_ffb_var  = tk.BooleanVar(value=self.config["invert_ffb"])
        self.port_ped_var    = tk.StringVar(value=self.config["port_pedals"])
        self.port_whl_var    = tk.StringVar(value=self.config["port_wheel"])
        self.baud_ped_var    = tk.StringVar(value=self.config["baudrate_pedals"])
        self.baud_whl_var    = tk.StringVar(value=self.config["baudrate_wheel"])

        # Build UI
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start polling the UI queue
        self._poll_ui_queue()

        # Init hardware
        self._init_vjoy()
        self._refresh_ports()
        self.after(800, self._auto_connect)

    # ═════════════════════════════════════════════════════════════════════════
    #  THREAD-SAFE UI UPDATE QUEUE
    # ═════════════════════════════════════════════════════════════════════════
    def _post(self, fn, *args):
        """Post a callable to be executed on the main thread. Safe from any thread."""
        self._ui_queue.put_nowait((fn, args))

    def _poll_ui_queue(self):
        """Drain the queue on the main thread at ~60fps."""
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(self._POLL_MS, self._poll_ui_queue)

    # ═════════════════════════════════════════════════════════════════════════
    #  UI BUILD
    # ═════════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        # Title bar
        tb = tk.Frame(self, bg=BG_DEEP)
        tb.pack(fill="x")
        tk.Label(tb, text="SIM RACING", font=("Segoe UI", 18, "bold"),
                 fg=TEXT_MAIN, bg=BG_DEEP).pack(side="left", padx=20, pady=12)
        tk.Label(tb, text="CONTROL PANEL", font=("Segoe UI", 18),
                 fg=ACCENT, bg=BG_DEEP).pack(side="left", pady=12)
        tk.Label(tb, textvariable=self.var_vjoy, font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_DEEP).pack(side="right", padx=20)

        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # Tabs
        self._tab_frame = tk.Frame(self, bg=BG_DEEP)
        self._tab_frame.pack(fill="x")
        self._tab_btns = {}
        self._pages    = {}

        for key, label in [("dashboard", "DASHBOARD"), ("settings", "SETTINGS"), ("logs", "LOGS")]:
            b = tk.Label(self._tab_frame, text=label,
                         font=("Segoe UI", 9, "bold"),
                         fg=TEXT_SUB, bg=BG_DEEP,
                         padx=22, pady=10, cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self._switch_tab(k))
            self._tab_btns[key] = b

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        container = tk.Frame(self, bg=BG_DEEP)
        container.pack(fill="both", expand=True)

        self._pages["dashboard"] = self._build_dashboard(container)
        self._pages["settings"]  = self._build_settings(container)
        self._pages["logs"]      = self._build_logs(container)
        self._switch_tab("dashboard")

    def _switch_tab(self, name):
        for k, page in self._pages.items():
            page.pack(fill="both", expand=True) if k == name else page.pack_forget()
        for k, btn in self._tab_btns.items():
            btn.configure(fg=TEXT_MAIN if k == name else TEXT_SUB)

    # ── Dashboard ─────────────────────────────────────────────────────────
    def _build_dashboard(self, parent):
        page = tk.Frame(parent, bg=BG_DEEP)

        left  = tk.Frame(page, bg=BG_DEEP)
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=12)
        right = tk.Frame(page, bg=BG_DEEP)
        right.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)

        # Board 1 — Pedals
        p1 = self._make_panel(left, "Board 1  ·  Pedals")
        self._conn_row(p1, self.port_ped_var, self.baud_ped_var,
                       self.var_ped_st, self.toggle_pedals,
                       "btn_connect_ped", "dot_ped")
        p1.pack(fill="x", pady=(0, 8))

        # Board 2 — Wheel & FFB
        p2 = self._make_panel(left, "Board 2  ·  Wheel & FFB")
        self._conn_row(p2, self.port_whl_var, self.baud_whl_var,
                       self.var_whl_st, self.toggle_wheel,
                       "btn_connect_whl", "dot_whl")
        p2.pack(fill="x", pady=(0, 8))

        # Assetto Corsa
        p_ac = self._make_panel(left, "Assetto Corsa")
        ac_row = tk.Frame(p_ac, bg=BG_PANEL)
        ac_row.pack(fill="x", padx=16, pady=(4, 12))
        tk.Label(ac_row, text="Shared Memory", font=("Segoe UI", 9),
                 fg=TEXT_SUB, bg=BG_PANEL).pack(side="left")
        self.dot_ac = tk.Label(ac_row, text="●", font=("Segoe UI", 10),
                               fg=TEXT_DIM, bg=BG_PANEL)
        self.dot_ac.pack(side="right", padx=(0, 2))
        self.lbl_ac_status = tk.Label(ac_row, textvariable=self.var_ac_st,
                                      font=("Consolas", 9, "bold"),
                                      fg=TEXT_DIM, bg=BG_PANEL)
        self.lbl_ac_status.pack(side="right", padx=(0, 8))
        p_ac.pack(fill="x", pady=(0, 8))

        # Board 3 — Mobile Wheel
        p_mob = self._make_panel(left, "Board 3  ·  Mobile Wheel (Backup)")
        mob_row = tk.Frame(p_mob, bg=BG_PANEL)
        mob_row.pack(fill="x", padx=16, pady=(4, 4))
        tk.Label(mob_row, text="URL", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_PANEL, width=5, anchor="w").pack(side="left")
        tk.Label(mob_row, textvariable=self.var_mob_ip,
                 font=("Consolas", 9), fg=TEXT_MAIN, bg=BG_PANEL).pack(side="left", padx=(0, 8))
        self.btn_connect_mob = tk.Label(mob_row, text="START APP",
                                        font=("Segoe UI", 8, "bold"),
                                        fg=TEXT_MAIN, bg=ACCENT_DIM,
                                        padx=12, pady=4, cursor="hand2")
        self.btn_connect_mob.pack(side="right")
        self.btn_connect_mob.bind("<Button-1>", lambda e: self._toggle_mobile())
        self.btn_connect_mob.bind("<Enter>", lambda e: self.btn_connect_mob.configure(bg=ACCENT))
        self.btn_connect_mob.bind("<Leave>", lambda e: self._btn_leave(self.btn_connect_mob))

        st_m = tk.Frame(p_mob, bg=BG_PANEL)
        st_m.pack(fill="x", padx=16, pady=(0, 10))
        self.dot_mob = tk.Label(st_m, text="●", font=("Segoe UI", 8),
                                fg=TEXT_DIM, bg=BG_PANEL)
        self.dot_mob.pack(side="left")
        tk.Label(st_m, textvariable=self.var_mob_st,
                 font=("Consolas", 8), fg=TEXT_DIM, bg=BG_PANEL).pack(side="left", padx=(4, 0))
        p_mob.pack(fill="x")

        # Live Monitor
        pm = self._make_panel(right, "Live Monitor")
        tk.Frame(pm, bg=BORDER, height=1).pack(fill="x", pady=0)
        self.bar_steer    = self._mon_row(pm, "STEERING",  center=True,  color=ACCENT,       var=self.var_steer)
        self.bar_throttle = self._mon_row(pm, "THROTTLE",  center=False, color=GREEN_OK,     var=self.var_thr)
        self.bar_brake    = self._mon_row(pm, "BRAKE",     center=False, color=ACCENT,       var=self.var_brk)
        self.bar_ffb      = self._mon_row(pm, "FFB",       center=True,  color=ORANGE_WARN,  var=self.var_ffb)
        pm.pack(fill="both", expand=True)

        return page

    def _make_panel(self, parent, title):
        f = tk.Frame(parent, bg=BG_PANEL,
                     highlightthickness=1, highlightbackground=BORDER)
        tr = tk.Frame(f, bg=BG_PANEL)
        tr.pack(fill="x", padx=16, pady=(10, 6))
        tk.Label(tr, text=title.upper(), font=("Segoe UI", 8, "bold"),
                 fg=TEXT_SUB, bg=BG_PANEL).pack(side="left")
        return f

    def _conn_row(self, parent, port_var, baud_var, status_var,
                  cmd, btn_attr, dot_attr):
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", padx=16, pady=(0, 4))

        tk.Label(row, text="PORT", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_PANEL, width=6, anchor="w").pack(side="left")

        port_cb = ttk.Combobox(row, textvariable=port_var,
                               width=9, font=("Consolas", 9))
        port_cb.pack(side="left", padx=(0, 4))
        self._style_combo(port_cb)

        baud_cb = ttk.Combobox(row, textvariable=baud_var,
                               values=["9600", "115200", "250000", "500000"],
                               width=8, font=("Consolas", 9))
        baud_cb.pack(side="left", padx=(0, 8))
        self._style_combo(baud_cb)

        ref = tk.Label(row, text="↻", font=("Segoe UI", 11),
                       fg=TEXT_DIM, bg=BG_PANEL, cursor="hand2", padx=2)
        ref.pack(side="right", padx=(0, 4))
        ref.bind("<Button-1>", lambda e: self._refresh_ports())
        ref.bind("<Enter>",    lambda e: ref.configure(fg=TEXT_MAIN))
        ref.bind("<Leave>",    lambda e: ref.configure(fg=TEXT_DIM))

        btn = tk.Label(row, text="CONNECT",
                       font=("Segoe UI", 8, "bold"),
                       fg=TEXT_MAIN, bg=ACCENT_DIM,
                       padx=12, pady=4, cursor="hand2")
        btn.pack(side="right")
        btn.bind("<Button-1>", lambda e: cmd())
        btn.bind("<Enter>",    lambda e: btn.configure(bg=ACCENT))
        btn.bind("<Leave>",    lambda e: self._btn_leave(btn))
        setattr(self, btn_attr, btn)

        # Store port combo reference for refresh
        pfx = btn_attr.replace("btn_connect_", "")
        setattr(self, f"combo_{pfx}_port", port_cb)

        # Status dot row
        sr = tk.Frame(parent, bg=BG_PANEL)
        sr.pack(fill="x", padx=16, pady=(0, 10))
        dot = tk.Label(sr, text="●", font=("Segoe UI", 8),
                       fg=TEXT_DIM, bg=BG_PANEL)
        dot.pack(side="left")
        setattr(self, dot_attr, dot)
        tk.Label(sr, textvariable=status_var,
                 font=("Consolas", 8), fg=TEXT_DIM, bg=BG_PANEL).pack(side="left", padx=(4, 0))

    def _mon_row(self, parent, label, center=False, color=ACCENT, var=None):
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill="x", padx=16, pady=6)
        lc = tk.Frame(f, bg=BG_PANEL, width=70, height=20)
        lc.pack_propagate(False)
        lc.pack(side="left")
        tk.Label(lc, text=label, font=("Segoe UI", 8, "bold"),
                 fg=TEXT_SUB, bg=BG_PANEL, anchor="w").pack(fill="both", expand=True)
        bar = ACBar(f, bar_width=260, bar_height=5, color=color, center=center)
        bar.pack(side="left", padx=(6, 10))
        bar.set(0.5 if center else 0.0)
        if var:
            tk.Label(f, textvariable=var, font=("Consolas", 9, "bold"),
                     fg=TEXT_MAIN, bg=BG_PANEL, width=9, anchor="e").pack(side="right")
        return bar

    def _style_combo(self, combo):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=BG_CARD, background=BG_CARD,
                        foreground=TEXT_MAIN, bordercolor=BORDER,
                        arrowcolor=TEXT_SUB, selectbackground=BG_HOVER,
                        selectforeground=TEXT_MAIN)
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG_CARD)],
                  selectbackground=[("readonly", BG_CARD)],
                  selectforeground=[("readonly", TEXT_MAIN)])
        combo.configure(style="TCombobox")

    def _btn_leave(self, btn):
        btn.configure(bg=ACCENT if "DISCONNECT" in btn.cget("text") or
                                   "STOP" in btn.cget("text")
                      else ACCENT_DIM)

    # ── Settings ──────────────────────────────────────────────────────────
    def _build_settings(self, parent):
        page = tk.Frame(parent, bg=BG_DEEP)
        left  = tk.Frame(page, bg=BG_DEEP)
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=12)
        right = tk.Frame(page, bg=BG_DEEP)
        right.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)

        # Pedals
        pp = self._make_panel(left, "Pedals Calibration")
        self._slider(pp, "Throttle Deadzone (Min)", self.t_min_var, 0, 49)
        self._slider(pp, "Brake Deadzone (Min)",    self.b_min_var, 0, 49)
        pp.pack(fill="x", pady=(0, 8))

        # Steering
        sp = self._make_panel(left, "Steering Calibration")
        self._slider(sp, "Max Rotation (°)", self.steer_angle_var, 180, 1440)
        self._slider(sp, "Center Offset (°)", self.steer_center_var, -90, 90)
        ctk.CTkSwitch(sp, text="INVERT STEERING", variable=self.invert_steer_var,
                      progress_color=ACCENT, fg_color=BG_HOVER,
                      button_color=TEXT_MAIN, button_hover_color=ACCENT,
                      command=self._save_config_delayed
                      ).pack(padx=16, pady=8, anchor="w")
        sp.pack(fill="x")

        # FFB
        fp = self._make_panel(right, "Force Feedback & Hardware")
        self._slider(fp, "FFB Gain", self.ffb_gain_var, 0, 100, suffix="%")
        ctk.CTkSwitch(fp, text="INVERT FORCE FEEDBACK", variable=self.invert_ffb_var,
                      progress_color=ACCENT, fg_color=BG_HOVER,
                      button_color=TEXT_MAIN, button_hover_color=ACCENT,
                      command=self._save_config_delayed
                      ).pack(padx=16, pady=(8, 12), anchor="w")
        ctk.CTkButton(fp, text="TEST MOTOR SWEEP",
                      fg_color=ACCENT_DIM, hover_color=ACCENT,
                      text_color=TEXT_MAIN, font=("Segoe UI", 9, "bold"),
                      command=self._test_motor
                      ).pack(fill="x", padx=16, pady=(4, 4))
        ctk.CTkButton(fp, text="RE-CENTER WHEEL",
                      fg_color="#1a3a1a", hover_color="#2a6a2a",
                      text_color=TEXT_MAIN, font=("Segoe UI", 9, "bold"),
                      command=self._send_align
                      ).pack(fill="x", padx=16, pady=(0, 16))
        fp.pack(fill="x")

        return page

    def _slider(self, parent, label, var, from_, to, suffix=""):
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill="x", padx=16, pady=(4, 8))
        h = tk.Frame(f, bg=BG_PANEL)
        h.pack(fill="x")
        tk.Label(h, text=label, font=("Segoe UI", 9),
                 fg=TEXT_SUB, bg=BG_PANEL, anchor="w").pack(side="left")
        val_lbl = tk.Label(h, font=("Consolas", 9, "bold"),
                           fg=ACCENT, bg=BG_PANEL)
        val_lbl.pack(side="right")

        def _upd(*_):
            val_lbl.configure(text=f"{var.get():.0f}{suffix}")
            self._save_config_delayed()

        var.trace_add("write", _upd)
        _upd()

        ctk.CTkSlider(f, variable=var, from_=from_, to=to,
                      fg_color=BG_HOVER, progress_color=ACCENT,
                      button_color=TEXT_MAIN, button_hover_color=ACCENT,
                      height=14).pack(fill="x", pady=(8, 4))

    # ── Logs ──────────────────────────────────────────────────────────────
    def _build_logs(self, parent):
        page = tk.Frame(parent, bg=BG_DEEP)

        lp = self._make_panel(page, "Board 1  ·  Pedals Log")
        lp.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=12)
        self.txt_ped_log = tk.Text(lp, bg=BG_CARD, fg=TEXT_MAIN,
                                   insertbackground=TEXT_MAIN,
                                   highlightthickness=1, highlightbackground=BORDER,
                                   relief="flat", font=("Consolas", 9),
                                   height=20, width=30)
        self.txt_ped_log.pack(fill="both", expand=True, padx=16, pady=(10, 16))

        rp = self._make_panel(page, "Board 2  ·  Wheel Log")
        rp.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)
        self.txt_whl_log = tk.Text(rp, bg=BG_CARD, fg=TEXT_MAIN,
                                   insertbackground=TEXT_MAIN,
                                   highlightthickness=1, highlightbackground=BORDER,
                                   relief="flat", font=("Consolas", 9),
                                   height=20, width=30)
        self.txt_whl_log.pack(fill="both", expand=True, padx=16, pady=(10, 16))

        return page

    def _append_log(self, widget, line):
        if not widget:
            return
        widget.insert("end", line + "\n")
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > 100:
            widget.delete("1.0", "2.0")
        widget.see("end")

    # ═════════════════════════════════════════════════════════════════════════
    #  PORTS
    # ═════════════════════════════════════════════════════════════════════════
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if not ports:
            ports = ["No Ports Found"]
        try:
            self.combo_ped_port.configure(values=ports)
            self.combo_whl_port.configure(values=ports)
            if self.port_ped_var.get() not in ports and ports[0] != "No Ports Found":
                self.combo_ped_port.set(ports[0])
            if (self.port_whl_var.get() not in ports
                    and ports[0] != "No Ports Found" and len(ports) > 1):
                self.combo_whl_port.set(ports[1])
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════════════
    #  vJOY
    # ═════════════════════════════════════════════════════════════════════════
    def _init_vjoy(self):
        try:
            self.vjoy_dev = pyvjoy.VJoyDevice(VJOY_DEVICE_ID)
            self.var_vjoy.set("vJoy  ✓  Device 1")
        except Exception:
            self.var_vjoy.set("vJoy  ✗  Monitor Only")
            self.after(500, self._prompt_vjoy)

    def _prompt_vjoy(self):
        path = "C:\\Program Files\\vJoy\\x64\\vJoyConfig.exe"
        if not os.path.exists(path):
            messagebox.showerror("vJoy Error",
                "vJoy is not installed. Please install it first.")
            return
        if messagebox.askyesno("vJoy Not Enabled",
                "vJoy driver not enabled or misconfigured.\n\n"
                "Auto-configure vJoy Device 1 (requires Admin)?"):
            try:
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", path, "enable on", None, 1)
                time.sleep(2.5)
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", path, "1 -f -a x y z -b 8", None, 1)
                messagebox.showinfo("vJoy Configured",
                    "vJoy configured! Please restart the app.")
            except Exception as e:
                messagebox.showerror("vJoy Error", str(e))

    def _auto_connect(self):
        ped = self.port_ped_var.get()
        if ped and ped not in ("", "No Ports Found"):
            self.connect_pedals()
        whl = self.port_whl_var.get()
        if whl and whl not in ("", "No Ports Found") and whl != ped:
            self.connect_wheel()

    # ═════════════════════════════════════════════════════════════════════════
    #  STATUS HELPERS  (always call from main thread)
    # ═════════════════════════════════════════════════════════════════════════
    def _set_dot(self, dot_attr, color):
        try:
            getattr(self, dot_attr).configure(fg=color)
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════════════
    #  PEDALS
    # ═════════════════════════════════════════════════════════════════════════
    def toggle_pedals(self):
        if self.is_pedals_connected:
            self._disconnect_pedals()
        else:
            self.connect_pedals()

    def connect_pedals(self):
        port = self.port_ped_var.get()
        if not port or port == "No Ports Found":
            return
        try:
            self._pedals = SerialReader(
                port, self.baud_ped_var.get(),
                on_line  = self._on_pedal_line,
                on_error = self._on_pedal_error
            )
            self._pedals.start()
            self.is_pedals_connected = True
            self.btn_connect_ped.configure(text="DISCONNECT", bg=ACCENT)
            self.var_ped_st.set(f"CONNECTED  ·  {port}")
            self._set_dot("dot_ped", GREEN_OK)
            self._save_config_delayed()
        except Exception as e:
            self.var_ped_st.set(f"ERROR: {e}")
            self._set_dot("dot_ped", ACCENT)

    def _disconnect_pedals(self):
        self.is_pedals_connected = False
        if self._pedals:
            self._pedals.stop()
            self._pedals = None
        self.btn_connect_ped.configure(text="CONNECT", bg=ACCENT_DIM)
        self.var_ped_st.set("DISCONNECTED")
        self._set_dot("dot_ped", TEXT_DIM)

    def _on_pedal_line(self, line):
        # Called from SerialReader thread — post to main thread
        self._post(self._process_pedal_line, line)

    def _on_pedal_error(self, err):
        self._post(self._handle_pedal_error, err)

    def _handle_pedal_error(self, err):
        self._disconnect_pedals()
        self.var_ped_st.set(f"LOST: {err}")
        self._set_dot("dot_ped", ACCENT)

    def _process_pedal_line(self, line):
        self._append_log(self.txt_ped_log, line)
        try:
            data  = json.loads(line)
            raw_t = data.get("Throttle", 0)
            raw_b = data.get("Brake",    0)
            t_pct = map_value(raw_t, self.config.get("throttle_min", 0), 100, 0, 100)
            b_pct = map_value(raw_b, self.config.get("brake_min",    0), 100, 0, 100)
            if self.vjoy_dev:
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_X, map_value(t_pct, 0, 100, 0, VJOY_MAX))
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_Y, map_value(b_pct, 0, 100, 0, VJOY_MAX))
            self.bar_throttle.set(t_pct / 100.0)
            self.bar_brake.set(b_pct / 100.0)
            self.var_thr.set(f"{t_pct}%")
            self.var_brk.set(f"{b_pct}%")
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════════════
    #  WHEEL
    # ═════════════════════════════════════════════════════════════════════════
    def toggle_wheel(self):
        if self.is_wheel_connected:
            self._disconnect_wheel()
        else:
            self.connect_wheel()

    def connect_wheel(self):
        port = self.port_whl_var.get()
        if not port or port == "No Ports Found":
            return
        try:
            self._wheel = SerialReader(
                port, self.baud_whl_var.get(),
                on_line  = self._on_wheel_line,
                on_error = self._on_wheel_error
            )
            self._wheel.start()
            self.is_wheel_connected = True
            self.btn_connect_whl.configure(text="DISCONNECT", bg=ACCENT)
            self.var_whl_st.set(f"CONNECTED  ·  {port}")
            self._set_dot("dot_whl", GREEN_OK)

            # Start FFB thread
            self._ffb_thread = threading.Thread(target=self._ffb_loop, daemon=True)
            self._ffb_thread.start()

            # Auto-align after board boot
            self.after(2000, self._send_align)
            self._save_config_delayed()
        except Exception as e:
            self.var_whl_st.set(f"ERROR: {e}")
            self._set_dot("dot_whl", ACCENT)

    def _disconnect_wheel(self):
        self.is_wheel_connected = False
        self.ac_connected = False
        if self._wheel:
            self._wheel.stop()
            self._wheel = None
        self.btn_connect_whl.configure(text="CONNECT", bg=ACCENT_DIM)
        self.var_whl_st.set("DISCONNECTED")
        self._set_dot("dot_whl", TEXT_DIM)
        self.bar_steer.set(0.5)
        self.bar_ffb.set(0.5)
        self.var_steer.set("0.0°")
        self.var_ffb.set("±0%")

    def _on_wheel_line(self, line):
        self._post(self._process_wheel_line, line)

    def _on_wheel_error(self, err):
        self._post(self._handle_wheel_error, err)

    def _handle_wheel_error(self, err):
        self._disconnect_wheel()
        self.var_whl_st.set(f"LOST: {err}")
        self._set_dot("dot_whl", ACCENT)

    def _process_wheel_line(self, line):
        self._append_log(self.txt_whl_log, line)
        try:
            data      = json.loads(line)
            raw_steer = data.get("Steer",    0)
            gear_up   = data.get("GearUp",   0)
            gear_down = data.get("GearDown", 0)

            raw_steer -= self.config.get("steer_center", 0)
            if self.invert_steer_var.get():
                raw_steer = -raw_steer

            half  = self.config.get("steer_angle", 900) / 2.0
            vjoy  = map_value(raw_steer, -half, half, 0, VJOY_MAX)
            if self.vjoy_dev:
                self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_Z, vjoy)
                self.vjoy_dev.set_button(1, 1 if gear_up   else 0)
                self.vjoy_dev.set_button(2, 1 if gear_down else 0)

            norm = (raw_steer + half) / (half * 2) if half else 0.5
            self.bar_steer.set(max(0.0, min(1.0, norm)))
            self.var_steer.set(f"{raw_steer:.1f}°")
        except Exception:
            pass

    def _send_align(self):
        if self._wheel:
            self._wheel.write(b"ALIGN\n")

    def _test_motor(self):
        if self._wheel:
            self._wheel.write(b"TEST\n")

    # ═════════════════════════════════════════════════════════════════════════
    #  AC FFB LOOP  (runs in background thread)
    # ═════════════════════════════════════════════════════════════════════════
    def _ffb_loop(self):
        shm = None
        while self.is_wheel_connected:
            if not self.ac_connected:
                try:
                    shm = mmap.mmap(0, ctypes.sizeof(SPageFilePhysics), "acpmf_physics")
                    self.ac_connected = True
                    self._post(self._set_ac_status, "CONNECTED", GREEN_OK)
                except Exception:
                    self._post(self._set_ac_status, "WAITING", TEXT_DIM)
                    time.sleep(2.0)
                    continue

            try:
                shm.seek(0)
                phys     = SPageFilePhysics.from_buffer(shm)
                ffb      = phys.finalFF * (self.config.get("ffb_gain", 100) / 100.0)
                if self.invert_ffb_var.get():
                    ffb = -ffb
                out = int(max(-1.0, min(1.0, ffb)) * 127)
                if self._wheel:
                    self._wheel.write(f"FFB:{out}\n".encode())
                self._post(self._update_ffb_ui, ffb)
            except Exception:
                self.ac_connected = False
                self._post(self._set_ac_status, "DISCONNECTED", ACCENT)
                time.sleep(1.0)
            time.sleep(0.01)

        if shm:
            try:
                shm.close()
            except BufferError:
                pass

    def _set_ac_status(self, text, color):
        self.var_ac_st.set(text)
        self.dot_ac.configure(fg=color)
        self.lbl_ac_status.configure(fg=color)

    def _update_ffb_ui(self, ffb):
        self.var_ffb.set(f"{ffb * 100:+.0f}%")
        self.bar_ffb.set((ffb + 1.0) / 2.0)

    # ═════════════════════════════════════════════════════════════════════════
    #  MOBILE SERVER
    # ═════════════════════════════════════════════════════════════════════════
    def _toggle_mobile(self):
        if self._mobile and self._mobile.running:
            self._stop_mobile()
        else:
            self._start_mobile()

    def _start_mobile(self):
        ip = get_local_ip()
        self.var_mob_ip.set(f"http://{ip}:{MobileServer.HTTP_PORT}")
        self._mobile = MobileServer(
            mobile_dir = MOBILE_DIR,
            on_data    = self._on_mobile_data,
            on_status  = self._on_mobile_status
        )
        self._mobile.start()
        self.btn_connect_mob.configure(text="STOP APP", bg=ACCENT)
        self.var_mob_st.set("RUNNING — WAITING")
        self.dot_mob.configure(fg=ORANGE_WARN)

    def _stop_mobile(self):
        if self._mobile:
            self._mobile.stop()
            self._mobile = None
        self.btn_connect_mob.configure(text="START APP", bg=ACCENT_DIM)
        self.var_mob_st.set("STOPPED")
        self.dot_mob.configure(fg=TEXT_DIM)
        self.var_mob_ip.set("—")

    def _on_mobile_data(self, steer, gear_up, gear_down):
        """Called from MobileServer thread — post to main thread."""
        self._post(self._process_mobile, steer, gear_up, gear_down)

    def _on_mobile_status(self, text, color):
        """Called from MobileServer thread — post to main thread."""
        self._post(self._update_mobile_status, text, color)

    def _update_mobile_status(self, text, color):
        self.var_mob_st.set(text)
        self.dot_mob.configure(fg=color)

    def _process_mobile(self, raw_steer, gear_up, gear_down):
        raw_steer -= self.config.get("steer_center", 0)
        if self.invert_steer_var.get():
            raw_steer = -raw_steer
        # Mobile max tilt ~90 degrees
        S = 90.0
        vjoy = map_value(raw_steer, -S, S, 0, VJOY_MAX)
        if self.vjoy_dev:
            self.vjoy_dev.set_axis(pyvjoy.HID_USAGE_Z, vjoy)
            self.vjoy_dev.set_button(1, 1 if gear_up   else 0)
            self.vjoy_dev.set_button(2, 1 if gear_down else 0)
        norm = (raw_steer + S) / (S * 2)
        self.bar_steer.set(max(0.0, min(1.0, norm)))
        self.var_steer.set(f"{raw_steer:.1f}°")

    # ═════════════════════════════════════════════════════════════════════════
    #  CONFIG
    # ═════════════════════════════════════════════════════════════════════════
    def _sync_config(self):
        self.config.update({
            "throttle_min":    self.t_min_var.get(),
            "brake_min":       self.b_min_var.get(),
            "steer_angle":     self.steer_angle_var.get(),
            "steer_center":    self.steer_center_var.get(),
            "ffb_gain":        self.ffb_gain_var.get(),
            "invert_steer":    self.invert_steer_var.get(),
            "invert_ffb":      self.invert_ffb_var.get(),
            "port_pedals":     self.port_ped_var.get(),
            "port_wheel":      self.port_whl_var.get(),
            "baudrate_pedals": self.baud_ped_var.get(),
            "baudrate_wheel":  self.baud_whl_var.get(),
        })

    def _save_config_delayed(self, *_):
        self._sync_config()
        if self.save_timer:
            self.after_cancel(self.save_timer)
        self.save_timer = self.after(1000, self._save_config)

    def _save_config(self):
        self._sync_config()
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception:
            pass

    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    self.config.update(json.load(f))
            except Exception:
                pass

    # ═════════════════════════════════════════════════════════════════════════
    #  CLEANUP
    # ═════════════════════════════════════════════════════════════════════════
    def _on_close(self):
        self.is_pedals_connected = False
        self.is_wheel_connected  = False
        if self._pedals:
            self._pedals.stop()
        if self._wheel:
            self._wheel.stop()
        if self._mobile and self._mobile.running:
            self._mobile.stop()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
