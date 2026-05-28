"""
Phone Controller GUI — live mirror + system monitor.

Layout:
  [screenshot panel] | [controls] | [stats + processes]

Workers (daemon threads, stopped by self._stop event on window close):
  - screen worker: when live mode on, polls SHOT at the configured interval
  - stats worker:  polls STATS every 1.5s, parses, updates labels
  - procs worker:  polls PROCS every 2.5s, updates process text
"""
import collections
import io
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from tkinter import ttk, scrolledtext, messagebox, filedialog
from PIL import Image, ImageTk

try:
    import av  # PyAV — H.264 decoder
    HAVE_AV = True
except ImportError:
    HAVE_AV = False

HOST = "127.0.0.1"
PORT = 8889
PREVIEW_W = 320
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))


def resolve_adb():
    """Locate adb.exe. Order: bundled vendor/ → SDK default → PATH.

    Bundled copy ships with the MSI so a fresh install works without an
    Android SDK. SDK and PATH fallbacks let developers override with a newer
    platform-tools build by removing the bundled file or shadowing it on PATH.
    """
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates = [
        os.path.join(HERE, "vendor", "platform-tools", exe),
        os.path.expandvars(rf"%LOCALAPPDATA%\Android\Sdk\platform-tools\{exe}"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    found = shutil.which("adb")
    if found:
        return found
    return None


ADB = resolve_adb()

# Active device serial — set after enumeration. Lets adb() target a specific
# device when multiple are attached (otherwise adb errors with "more than one").
_active_serial = None

# Perfetto trace config — covers what spec 5.3 wants: sched, cpu_freq, cpu_idle,
# memory, binder, thermal, plus atrace gfx/view/wm for app frame markers.
# Duration overridden by --duration flag at run-time.
PERFETTO_CONFIG = """
buffers: {
  size_kb: 65536
  fill_policy: DISCARD
}
data_sources: {
  config {
    name: "linux.process_stats"
    process_stats_config {
      scan_all_processes_on_start: true
      proc_stats_poll_ms: 1000
    }
  }
}
data_sources: {
  config {
    name: "linux.sys_stats"
    sys_stats_config {
      cpufreq_period_ms: 1000
      meminfo_period_ms: 1000
      stat_period_ms: 1000
      stat_counters: STAT_CPU_TIMES
      stat_counters: STAT_FORK_COUNT
    }
  }
}
data_sources: {
  config {
    name: "android.power"
    android_power_config {
      battery_poll_ms: 1000
      battery_counters: BATTERY_COUNTER_CHARGE
      battery_counters: BATTERY_COUNTER_CAPACITY_PERCENT
      battery_counters: BATTERY_COUNTER_CURRENT
      collect_power_rails: true
    }
  }
}
data_sources: {
  config {
    name: "linux.ftrace"
    ftrace_config {
      ftrace_events: "sched/sched_switch"
      ftrace_events: "sched/sched_wakeup"
      ftrace_events: "sched/sched_wakeup_new"
      ftrace_events: "sched/sched_waking"
      ftrace_events: "power/cpu_frequency"
      ftrace_events: "power/cpu_idle"
      ftrace_events: "power/suspend_resume"
      ftrace_events: "binder/binder_transaction"
      ftrace_events: "binder/binder_transaction_received"
      ftrace_events: "thermal/thermal_temperature"
      atrace_categories: "view"
      atrace_categories: "gfx"
      atrace_categories: "wm"
      atrace_categories: "am"
      atrace_categories: "input"
      atrace_categories: "binder_driver"
      atrace_apps: "*"
    }
  }
}
"""


def adb_cmd(*args):
    """Build an adb argv list with -s injected when an active device is set."""
    cmd = [ADB]
    if _active_serial:
        cmd += ["-s", _active_serial]
    return cmd + list(args)


def adb(*args, capture=True, timeout=10):
    return subprocess.run(
        adb_cmd(*args),
        capture_output=capture,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def adb_list_devices():
    """Return list of (serial, label) for online devices. label is 'model (serial)'."""
    try:
        # start-server is implicit on the next call, but explicit start avoids
        # a spurious "daemon started" line in `devices -l` output.
        subprocess.run([ADB, "start-server"], capture_output=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        r = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        # Extract model:XXX field if present
        model = serial
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token[6:]
                break
        out.append((serial, f"{model} ({serial})"))
    return out


def normalize_lf(path):
    with open(path, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    with open(path, "wb") as f:
        f.write(data)


def send_cmd(cmd, return_bytes=False, timeout=8.0):
    """One-shot TCP request to the daemon. Returns str or bytes."""
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((HOST, PORT))
        s.sendall((cmd + "\n").encode("utf-8"))
        buf = bytearray()
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
        s.close()
        return bytes(buf) if return_bytes else buf.decode("utf-8", "replace").strip()
    except Exception as e:
        return None if return_bytes else f"ERR {e}"


class PhoneController:
    def __init__(self, root):
        self.root = root
        root.title("Phone Controller — SM-M446K (live)")
        root.geometry("1240x820")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.device_w = 1080
        self.device_h = 2408
        self.scale = self.device_w / PREVIEW_W
        self.drag_start = None
        self.last_img_ref = None

        # workers
        self._stop = threading.Event()
        self._live = threading.Event()  # screen live mode toggle
        self._live_interval = 0.4  # PNG-mode interval seconds
        self._video_proc = None     # legacy (unused)
        self._video_sock = None     # STREAM socket (H.264 mode)

        # Macro recorder
        self._recording = False
        self._record_t0 = None
        self._record_steps = []  # list[(rel_seconds, cmd_str)]

        # state for CPU% delta
        self._last_cpu = None  # (timestamp, [u,n,s,i,iow,irq,sirq])
        # state for GPU busy% delta (Adreno cumulative path)
        self._last_gpu_busy = None  # (busy, total)
        # display refresh rate — detected after connect, used as jank baseline
        self.refresh_hz = 60.0

        # Rolling time-series buffer for the bottom chart panel.
        # 600 samples × 1.5s = 15 minutes of history.
        self.stats_history = collections.deque(maxlen=600)
        # Foreground-app transition markers: list[(ts, pkg)] within history window.
        self.fg_events = collections.deque(maxlen=200)
        self._last_fg_pkg = None
        # Thermal throttle entry/exit markers — populated when cpu_temp crosses
        # THERMAL_THRESH. list[(ts, "in"|"out")]
        self.thermal_events = collections.deque(maxlen=200)
        self._thermal_state = False
        self.THERMAL_THRESH = 65.0  # °C
        # Jank markers — list[(ts, frame_dur_ms)]. Populated by the jank timeline
        # sampler while a jank window is open.
        self.jank_events = collections.deque(maxlen=300)

        # ===== session state (artifacts collection) =====
        self.session_dir = None
        self.session_started = None       # epoch seconds
        self.session_stop_evt = threading.Event()
        self.session_files = {}           # name -> open file handle
        self._logcat_proc = None
        self._llm_logcat_proc = None
        self._perfetto_running = threading.Event()
        self._simpleperf_running = threading.Event()

        self._build_ui()
        self._start_workers()
        threading.Thread(target=self._auto_connect, daemon=True).start()

    # ============================================================ UI
    def _build_ui(self):
        # Toolbar
        bar = tk.Frame(self.root)
        bar.pack(fill="x", padx=4, pady=4)
        tk.Button(bar, text="Setup / Start daemon", command=self._setup_thr).pack(side="left")
        tk.Button(bar, text="Ping", command=lambda: self._async("PING")).pack(side="left", padx=4)
        tk.Button(bar, text="Refresh shot", command=self.refresh_shot).pack(side="left")
        self.live_var = tk.IntVar()
        tk.Checkbutton(bar, text="Live mirror", variable=self.live_var,
                       command=self._toggle_live).pack(side="left", padx=8)
        tk.Label(bar, text="Mode:").pack(side="left")
        default_mode = "H.264" if HAVE_AV else "PNG"
        self.mode_var = tk.StringVar(value=default_mode)
        modes = ["H.264", "PNG"] if HAVE_AV else ["PNG"]
        ttk.Combobox(bar, textvariable=self.mode_var, values=modes,
                     state="readonly", width=6).pack(side="left", padx=2)
        tk.Label(bar, text="PNG ms:").pack(side="left")
        self.interval_var = tk.IntVar(value=400)
        tk.Spinbox(bar, from_=150, to=3000, increment=50, width=5,
                   textvariable=self.interval_var,
                   command=self._update_interval).pack(side="left")
        self.kbd_var = tk.IntVar()
        tk.Checkbutton(bar, text="PC keyboard → phone", variable=self.kbd_var,
                       command=self._toggle_kbd).pack(side="left", padx=8)
        tk.Button(bar, text="Stability", command=self._open_stability).pack(side="left", padx=2)
        tk.Button(bar, text="Cleanup tmp",
                  command=lambda: self._cleanup_tmp(all_files=False)).pack(side="left", padx=2)
        self.session_btn = tk.Button(bar, text="● Start session",
                                     fg="green", command=self._toggle_session)
        self.session_btn.pack(side="left", padx=4)
        tk.Button(bar, text="Perfetto 30s",
                  command=lambda: self._capture_perfetto(30)).pack(side="left", padx=2)
        tk.Button(bar, text="Compare…",
                  command=self._open_compare).pack(side="left", padx=2)
        tk.Button(bar, text="3D view",
                  command=self._open_3d_view).pack(side="left", padx=2)
        tk.Button(bar, text="Logcat",
                  command=self._open_logcat).pack(side="left", padx=2)
        tk.Button(bar, text="Battery",
                  command=self._open_battery).pack(side="left", padx=2)
        tk.Button(bar, text="LLM live",
                  command=self._open_llm_live).pack(side="left", padx=2)
        tk.Button(bar, text="Scenario…",
                  command=self._open_scenario).pack(side="left", padx=2)
        tk.Button(bar, text="Flame…",
                  command=self._open_flame).pack(side="left", padx=2)
        tk.Button(bar, text="Wire…",
                  command=self._open_wire).pack(side="left", padx=2)
        self.session_lbl = tk.Label(bar, text="", fg="gray", font=("Consolas", 9))
        self.session_lbl.pack(side="left", padx=4)
        self.fps_label = tk.Label(bar, text="0.0 fps", fg="gray", width=10, anchor="w")
        self.fps_label.pack(side="left", padx=8)
        self.status = tk.Label(bar, text="●  not connected", fg="gray")
        self.status.pack(side="right")
        # Device picker (populated by _auto_connect)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(bar, textvariable=self.device_var,
                                         values=[], state="readonly", width=28)
        self.device_combo.pack(side="right", padx=4)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_select)
        tk.Button(bar, text="↻", width=2, command=self._refresh_devices,
                  fg="gray").pack(side="right")
        # serial -> label mapping for combobox
        self._device_map = {}

        # Main 3 columns
        # Bottom time-series chart panel (packed BEFORE main so it claims bottom
        # strip before main expands to fill).
        self._build_chart_panel()
        main = tk.Frame(self.root)
        main.pack(side="top", fill="both", expand=True, padx=4, pady=4)

        # ---- column 1: screenshot
        left = tk.Frame(main)
        left.pack(side="left", fill="y")
        self.canvas = tk.Canvas(left, width=PREVIEW_W,
                                height=int(PREVIEW_W * self.device_h / self.device_w),
                                bg="#111", highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.create_text(PREVIEW_W // 2, 60, fill="#888",
                                text="Setup → Live mirror",
                                font=("Segoe UI", 11))

        # ---- column 2: controls
        mid = tk.Frame(main)
        mid.pack(side="left", fill="both", expand=True, padx=8)

        keys = tk.LabelFrame(mid, text="Keys")
        keys.pack(fill="x")
        for txt, code in [("Home", "HOME"), ("Back", "BACK"), ("Recent", "APP_SWITCH"),
                          ("Power", "POWER"), ("Vol+", "VOLUME_UP"), ("Vol-", "VOLUME_DOWN"),
                          ("Menu", "MENU"), ("Enter", "ENTER")]:
            tk.Button(keys, text=txt, width=6,
                      command=lambda c=code: self._async(f"KEY {c}")).pack(side="left", padx=1, pady=2)

        apps = tk.LabelFrame(mid, text="Apps")
        apps.pack(fill="x", pady=6)
        for name, pkg in [("Instagram", "com.instagram.android"),
                          ("Threads", "com.instagram.barcelona"),
                          ("Browser", "com.sec.android.app.sbrowser"),
                          ("Settings", "com.android.settings")]:
            tk.Button(apps, text=name, width=10,
                      command=lambda p=pkg: self._async(f"APP {p}")).pack(side="left", padx=1)

        macro = tk.LabelFrame(mid, text="Macro recorder (detached — survives unplug)")
        macro.pack(fill="x", pady=6)
        btns = tk.Frame(macro); btns.pack(fill="x")
        self.rec_btn = tk.Button(btns, text="● Rec", fg="red",
                                 command=self._toggle_record)
        self.rec_btn.pack(side="left", padx=1)
        tk.Button(btns, text="Clear", command=self._clear_record).pack(side="left", padx=1)
        tk.Button(btns, text="Save .sh…", command=self._save_macro).pack(side="left", padx=1)
        tk.Button(btns, text="Push & Run", command=self._push_run_macro,
                  fg="white", bg="#2a8").pack(side="left", padx=4)
        tk.Label(btns, text=" Loop:").pack(side="left")
        self.loop_var = tk.IntVar(value=1)
        tk.Spinbox(btns, from_=1, to=99999, width=5,
                   textvariable=self.loop_var).pack(side="left")
        tk.Button(btns, text="Stop all", command=self._stop_macros,
                  fg="white", bg="#c33").pack(side="left", padx=4)
        self.rec_status = tk.Label(btns, text="0 steps", fg="gray")
        self.rec_status.pack(side="right", padx=4)
        self.macro_list = tk.Listbox(macro, height=5, font=("Consolas", 9))
        self.macro_list.pack(fill="x")

        tx = tk.LabelFrame(mid, text="Text input")
        tx.pack(fill="x", pady=6)
        self.text_entry = tk.Entry(tx)
        self.text_entry.pack(side="left", fill="x", expand=True, padx=2, pady=2)
        self.text_entry.bind("<Return>", lambda e: self._send_text())
        tk.Button(tx, text="Send", command=self._send_text).pack(side="right", padx=2)

        raw = tk.LabelFrame(mid, text="Raw command (e.g. SH ls /sdcard)")
        raw.pack(fill="x", pady=6)
        self.raw_entry = tk.Entry(raw)
        self.raw_entry.pack(side="left", fill="x", expand=True, padx=2, pady=2)
        self.raw_entry.bind("<Return>", lambda e: self._send_raw())
        tk.Button(raw, text="Send", command=self._send_raw).pack(side="right", padx=2)

        logf = tk.LabelFrame(mid, text="Log")
        logf.pack(fill="both", expand=True, pady=6)
        self.log = scrolledtext.ScrolledText(logf, height=10, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        # ---- column 3: stats + processes
        right = tk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        metrics = tk.LabelFrame(right, text="System")
        metrics.pack(fill="x")
        self.cpu_var = self._metric_row(metrics, 0, "CPU", "%")
        self.mem_var = self._metric_row(metrics, 1, "MEM", "%")
        self.temp_var = self._metric_row(metrics, 2, "CPU TEMP", "°C")
        self.bat_var = self._metric_row(metrics, 3, "BAT", "")
        self.gpu_var = self._metric_row(metrics, 4, "GPU", "")
        self.pwr_var = self._metric_row(
            metrics, 5, "POWER", "mW",
            hint=(
                "Power (mW) is read from kernel sysfs nodes:\n"
                "  /sys/class/power_supply/.../current_now × voltage_now\n"
                "  (or Qualcomm /sys/class/.../power_now if exported)\n\n"
                "These nodes require root or a userdebug build on most\n"
                "stock Android devices. On a regular shell user, the read\n"
                "is blocked by SELinux/DAC and POWER shows 'n/a'.\n\n"
                "To enable: use a userdebug/rooted device, or read totals\n"
                "from 'dumpsys batterystats' instead (lower resolution)."
            ),
        )
        # Track whether at least one PWR_* line has ever arrived. After a few
        # polls with no data we flip POWER to 'n/a' and log a one-time hint.
        self._pwr_seen = False
        self._pwr_polls = 0
        metrics.columnconfigure(2, weight=1)

        # CPU progress bars per core
        cores = tk.LabelFrame(right, text="CPU cores (freq MHz)")
        cores.pack(fill="x", pady=6)
        self.core_labels = []
        for i in range(8):
            lab = tk.Label(cores, text=f"C{i}: —", font=("Consolas", 9),
                           width=12, anchor="w")
            lab.grid(row=i // 4, column=i % 4, padx=2, pady=1, sticky="w")
            self.core_labels.append(lab)

        therm = tk.LabelFrame(right, text="Thermal")
        therm.pack(fill="x", pady=6)
        self.therm_text = tk.Label(therm, text="—", font=("Consolas", 9),
                                   justify="left", anchor="w")
        self.therm_text.pack(fill="x", padx=4, pady=2)

        procs = tk.LabelFrame(right, text="Apps (foreground at top — double-click=logcat, right-click=dump)")
        procs.pack(fill="both", expand=True, pady=6)
        cols = ("pid", "cpu", "mem", "res", "pkg")
        self.procs_tv = ttk.Treeview(procs, columns=cols, show="headings",
                                     height=18, selectmode="browse")
        widths = {"pid": 60, "cpu": 55, "mem": 55, "res": 60, "pkg": 250}
        anchors = {"pid": "e", "cpu": "e", "mem": "e", "res": "e", "pkg": "w"}
        headers = {"pid": "PID", "cpu": "CPU%", "mem": "MEM%", "res": "RES", "pkg": "Package / Cmd"}
        for c in cols:
            self.procs_tv.heading(c, text=headers[c])
            self.procs_tv.column(c, width=widths[c], anchor=anchors[c], stretch=(c == "pkg"))
        self.procs_tv.tag_configure("fg", background="#dff7d8", font=("Segoe UI", 9, "bold"))
        sb = ttk.Scrollbar(procs, orient="vertical", command=self.procs_tv.yview)
        self.procs_tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.procs_tv.pack(fill="both", expand=True)
        self.procs_tv.bind("<Double-Button-1>", self._on_proc_double)
        self.procs_tv.bind("<Button-3>", self._on_proc_rmouse)
        # Context menu (built lazily; populated per right-click)
        self._proc_menu = None

    # ============================================================ bottom chart panel
    # Metric series definitions for the time-series chart:
    #   (key, label, color, scale_fn(value)→0..1, format_current(value)→str)
    CHART_SERIES = [
        ("cpu_pct",   "CPU",   "#ff5252",
         lambda v: max(0.0, min(1.0, v / 100.0)),
         lambda v: f"{v:.1f}%"),
        ("mem_pct",   "MEM",   "#4ad991",
         lambda v: max(0.0, min(1.0, v / 100.0)),
         lambda v: f"{v:.1f}%"),
        ("gpu_busy",  "GPU",   "#5b8bff",
         lambda v: max(0.0, min(1.0, v / 100.0)),
         lambda v: f"{v:.0f}%"),
        ("cpu_temp",  "TEMP",  "#ff9f43",
         lambda v: max(0.0, min(1.0, (v - 25.0) / 60.0)),  # 25–85°C → 0–1
         lambda v: f"{v:.1f}°C"),
        ("gpu_freq_mhz", "GPU MHz", "#b48cff",
         lambda v: max(0.0, min(1.0, v / 1000.0)),         # 0–1 GHz → 0–1
         lambda v: f"{v:.0f}MHz"),
        ("power_mw",  "POWER", "#ffd34a",
         lambda v: max(0.0, min(1.0, v / 5000.0)),         # 0–5W → 0–1
         lambda v: f"{v:.0f}mW"),
    ]

    CHART_WINDOWS = [("1m", 60), ("5m", 300), ("15m", 900)]

    def _build_chart_panel(self):
        chart_root = tk.Frame(self.root, height=210, bg="#0e0e0e")
        chart_root.pack(side="bottom", fill="x")
        chart_root.pack_propagate(False)

        bar = tk.Frame(chart_root, bg="#0e0e0e")
        bar.pack(fill="x", padx=4, pady=2)
        tk.Label(bar, text="Time series:", bg="#0e0e0e", fg="#ccc",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.chart_visibility = {}
        for key, label, color, _, _ in self.CHART_SERIES:
            v = tk.BooleanVar(value=True)
            self.chart_visibility[key] = v
            cb = tk.Checkbutton(bar, text=label, variable=v,
                                bg="#0e0e0e", fg=color, selectcolor="#222",
                                activebackground="#0e0e0e",
                                activeforeground=color,
                                font=("Consolas", 9, "bold"),
                                command=self._redraw_chart)
            cb.pack(side="left", padx=2)
        # Spacer
        tk.Label(bar, text="  Window:", bg="#0e0e0e", fg="#888").pack(side="left", padx=(12, 2))
        self.chart_window_var = tk.StringVar(value="5m")
        for label, _sec in self.CHART_WINDOWS:
            tk.Radiobutton(bar, text=label, value=label,
                           variable=self.chart_window_var,
                           bg="#0e0e0e", fg="#aaa", selectcolor="#222",
                           activebackground="#0e0e0e", activeforeground="#fff",
                           command=self._redraw_chart).pack(side="left", padx=1)
        # FG event toggle
        self.chart_show_events = tk.BooleanVar(value=True)
        self.chart_show_jank = tk.BooleanVar(value=True)
        self.chart_show_thermal = tk.BooleanVar(value=True)
        for var, lbl in [(self.chart_show_jank, "jank"),
                         (self.chart_show_thermal, "thermal")]:
            tk.Checkbutton(bar, text=lbl, variable=var,
                           bg="#0e0e0e", fg="#aaa",
                           selectcolor="#0e0e0e", activebackground="#0e0e0e",
                           command=self._redraw_chart).pack(side="left", padx=(8, 2))
        tk.Checkbutton(bar, text="FG events", variable=self.chart_show_events,
                       bg="#0e0e0e", fg="#aaa", selectcolor="#222",
                       activebackground="#0e0e0e", activeforeground="#fff",
                       command=self._redraw_chart).pack(side="left", padx=(12, 2))
        # Right-side legend showing current values
        self.chart_legend_lbl = tk.Label(bar, text="", bg="#0e0e0e",
                                         fg="#ccc", font=("Consolas", 9),
                                         anchor="e", justify="right")
        self.chart_legend_lbl.pack(side="right", padx=4)

        self.chart_canvas = tk.Canvas(chart_root, bg="#0e0e0e",
                                      highlightthickness=0)
        self.chart_canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.chart_canvas.bind("<Configure>", lambda _e: self._redraw_chart())
        # Hover tooltip
        self.chart_canvas.bind("<Motion>", self._chart_hover)
        self._chart_hover_text = None

    def _chart_hover(self, e):
        history = list(self.stats_history)
        if not history:
            return
        c = self.chart_canvas
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 50:
            return
        sec = next((s for lbl, s in self.CHART_WINDOWS
                    if lbl == self.chart_window_var.get()), 300)
        t_now = history[-1]["ts"]
        t_min = t_now - sec
        margin_left, margin_right = 36, 6
        plot_w = w - margin_left - margin_right
        if plot_w <= 0 or not (margin_left <= e.x <= w - margin_right):
            return
        t_at = t_min + (e.x - margin_left) / plot_w * sec
        # Find closest sample
        closest = min(history, key=lambda s: abs(s["ts"] - t_at))
        c.delete("hover")
        # Vertical guide line
        c.create_line(e.x, 4, e.x, h - 16, fill="#555", dash=(2, 2), tags="hover")
        # Value tooltip text
        bits = [time.strftime("%H:%M:%S", time.localtime(closest["ts"]))]
        for key, label, color, _scale, fmt in self.CHART_SERIES:
            if not self.chart_visibility[key].get():
                continue
            v = closest.get(key)
            if v is None:
                continue
            bits.append(f"{label} {fmt(v)}")
        self.chart_legend_lbl.config(text="  ".join(bits))

    def _redraw_chart(self):
        c = self.chart_canvas
        c.delete("all")
        history = list(self.stats_history)
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 50:
            return
        if not history:
            c.create_text(w // 2, h // 2, fill="#444",
                          text="waiting for samples…", font=("Segoe UI", 10))
            return
        sec = next((s for lbl, s in self.CHART_WINDOWS
                    if lbl == self.chart_window_var.get()), 300)
        t_now = history[-1]["ts"]
        t_min = t_now - sec
        margin_left, margin_right = 36, 6
        margin_top, margin_bottom = 6, 18
        plot_x0 = margin_left
        plot_x1 = w - margin_right
        plot_y0 = margin_top
        plot_y1 = h - margin_bottom
        plot_w = plot_x1 - plot_x0
        plot_h = plot_y1 - plot_y0
        # Grid
        for frac, lbl in [(0.0, "0"), (0.25, "25"), (0.5, "50"),
                          (0.75, "75"), (1.0, "100")]:
            y = plot_y1 - frac * plot_h
            c.create_line(plot_x0, y, plot_x1, y, fill="#1c1c1c")
            c.create_text(plot_x0 - 3, y, anchor="e", fill="#555",
                          font=("Consolas", 7), text=lbl)
        # Time axis labels
        for ratio in [0.0, 0.5, 1.0]:
            x = plot_x0 + ratio * plot_w
            c.create_line(x, plot_y1, x, plot_y1 + 3, fill="#444")
            label_t = t_now - sec * (1 - ratio)
            lbl = time.strftime("%H:%M:%S", time.localtime(label_t))
            c.create_text(x, plot_y1 + 5, anchor="n", fill="#666",
                          font=("Consolas", 7), text=lbl)

        # FG event vertical lines
        if self.chart_show_events.get():
            for ts, pkg in list(self.fg_events):
                if ts < t_min or ts > t_now:
                    continue
                x = plot_x0 + (ts - t_min) / sec * plot_w
                c.create_line(x, plot_y0, x, plot_y1, fill="#3a3a5a",
                              dash=(1, 2))
                c.create_text(x, plot_y0 + 2, anchor="n", fill="#7878a0",
                              font=("Consolas", 7), text=pkg[-22:])

        # Thermal throttle bands — pair each "in" with the next "out" and
        # shade that horizontal span behind the data.
        if self.chart_show_thermal.get():
            ev = [(ts, k) for ts, k in list(self.thermal_events)
                  if t_min <= ts <= t_now]
            i = 0
            while i < len(ev):
                ts, k = ev[i]
                if k == "in":
                    end_ts = t_now
                    if i + 1 < len(ev) and ev[i + 1][1] == "out":
                        end_ts = ev[i + 1][0]
                        i += 1
                    x1 = plot_x0 + (ts - t_min) / sec * plot_w
                    x2 = plot_x0 + (end_ts - t_min) / sec * plot_w
                    c.create_rectangle(x1, plot_y0, x2, plot_y1,
                                       fill="#3a1414", outline="",
                                       stipple="gray12")
                    c.create_text(x1 + 2, plot_y0 + 12, anchor="nw",
                                  fill="#ff8a8a", font=("Consolas", 7),
                                  text=f"≥{self.THERMAL_THRESH:.0f}°C")
                i += 1

        # Jank tick marks at the bottom of the plot.
        if self.chart_show_jank.get():
            for ts, dur in list(self.jank_events):
                if ts < t_min or ts > t_now:
                    continue
                x = plot_x0 + (ts - t_min) / sec * plot_w
                color = "#ff5252" if dur < 700 else "#8b1414"
                c.create_line(x, plot_y1 - 6, x, plot_y1, fill=color, width=2)

        # Draw each series
        legend_bits = []
        for key, label, color, scale, fmt in self.CHART_SERIES:
            if not self.chart_visibility[key].get():
                continue
            pts = []
            cur_val = None
            for s in history:
                if s["ts"] < t_min:
                    continue
                v = s.get(key)
                if v is None:
                    continue
                cur_val = v
                x = plot_x0 + (s["ts"] - t_min) / sec * plot_w
                y = plot_y1 - scale(v) * plot_h
                pts.extend([x, y])
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=2, smooth=False)
            if cur_val is not None:
                legend_bits.append(f"{label} {fmt(cur_val)}")
        if legend_bits:
            self.chart_legend_lbl.config(text="   ".join(legend_bits))

    def _metric_row(self, parent, row, label, unit, hint=None):
        tk.Label(parent, text=label, width=10, anchor="w").grid(row=row, column=0, sticky="w", padx=4, pady=1)
        v = tk.StringVar(value="—")
        tk.Label(parent, textvariable=v, font=("Segoe UI", 11, "bold"),
                 width=10, anchor="e").grid(row=row, column=1, sticky="e", padx=2)
        bar = ttk.Progressbar(parent, length=180, maximum=100)
        bar.grid(row=row, column=2, sticky="ew", padx=4)
        if hint:
            q = tk.Label(parent, text="ⓘ", fg="gray", cursor="question_arrow")
            q.grid(row=row, column=3, padx=2)
            q.bind("<Button-1>", lambda e, t=label, h=hint: messagebox.showinfo(t, h))
        v._bar = bar
        v._unit = unit
        return v

    # ============================================================ logging
    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _set_status(self, txt, color="green"):
        self.status.config(text=f"●  {txt}", fg=color)

    # ============================================================ command dispatch
    _RECORDABLE = {"TAP", "SWIPE", "KEY", "TEXT", "IME_TEXT", "APP", "STOP"}

    def _async(self, cmd):
        if self._recording:
            op = cmd.split(None, 1)[0]
            if op in self._RECORDABLE:
                self._record_step(cmd)
        def worker():
            r = send_cmd(cmd)
            self.root.after(0, lambda: self._log(f"> {cmd}  →  {str(r)[:120]}"))
        threading.Thread(target=worker, daemon=True).start()

    # ============================================================ macro recorder
    def _toggle_record(self):
        self._recording = not self._recording
        if self._recording:
            if self._record_t0 is None:
                self._record_t0 = time.time()
            self.rec_btn.config(text="■ Stop", bg="red", fg="white",
                                activebackground="#c00")
            self.rec_status.config(
                text=f"{len(self._record_steps)} steps · REC", fg="red")
        else:
            self.rec_btn.config(text="● Rec", bg="SystemButtonFace", fg="red",
                                activebackground="SystemButtonFace")
            self.rec_status.config(
                text=f"{len(self._record_steps)} steps", fg="gray")

    def _record_step(self, cmd):
        t = time.time() - self._record_t0
        self._record_steps.append((t, cmd))
        self.macro_list.insert("end", f"{t:6.2f}s  {cmd[:80]}")
        self.macro_list.see("end")
        self.rec_status.config(
            text=f"{len(self._record_steps)} steps · REC", fg="red")

    def _clear_record(self):
        self._record_steps = []
        self._record_t0 = None
        self.macro_list.delete(0, "end")
        if self._recording:
            self._record_t0 = time.time()
            self.rec_status.config(text="0 steps · REC", fg="red")
        else:
            self.rec_status.config(text="0 steps", fg="gray")

    @staticmethod
    def _shell_single_quote(s):
        # Wrap arg in single quotes, escape embedded '.
        return "'" + s.replace("'", "'\\''") + "'"

    def _cmd_to_shell(self, cmd):
        parts = cmd.split(None, 1)
        op = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        if op == "TAP":
            return f"input tap {args}"
        if op == "SWIPE":
            return f"input swipe {args}"
        if op == "KEY":
            return f"input keyevent {args}"
        if op == "TEXT":
            # `input text` wants spaces as %s; quote for shell safety
            t = args.replace(" ", "%s")
            return f"input text {self._shell_single_quote(t)}"
        if op == "IME_TEXT":
            return (f"am broadcast -a ADB_INPUT_TEXT --es msg "
                    f"{self._shell_single_quote(args)} >/dev/null 2>&1")
        if op == "APP":
            return (f"monkey -p {args} -c android.intent.category.LAUNCHER 1 "
                    f">/dev/null 2>&1")
        if op == "STOP":
            return f"am force-stop {args}"
        return f"# unrecognized: {cmd}"

    def _build_script(self, name="manual", loops=1):
        """Detached-ready script:
          - writes its own PID to /data/local/tmp/macro_<name>.pid for Stop button
          - traps EXIT to remove the pid file
          - wraps body in a loop if loops > 1 (use 0 for infinite, --max 0)
        """
        pid_file = f"/data/local/tmp/macro_{name}.pid"
        log_file = f"/data/local/tmp/macro_{name}.log"
        lines = [
            "#!/system/bin/sh",
            f"# Macro recorded by Phone Controller",
            f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  steps={len(self._record_steps)}  loops={loops}",
            f"echo $$ > {pid_file}",
            f"trap 'rm -f {pid_file}' EXIT",
            f"echo \"[$(date)] macro start (loops={loops})\" >> {log_file}",
            "",
        ]
        body = []
        prev_t = 0.0
        for t, cmd in self._record_steps:
            dt = t - prev_t
            if dt > 0.05:
                body.append(f"  sleep {dt:.3f}")
            body.append(f"  {self._cmd_to_shell(cmd)}")
            prev_t = t

        if loops == 0:
            lines.append("while :; do")
            lines.extend(body)
            lines.append("done")
        elif loops == 1:
            lines.extend(line.lstrip() for line in body)
        else:
            lines.append(f"i=0; while [ $i -lt {loops} ]; do")
            lines.extend(body)
            lines.append("  i=$((i+1))")
            lines.append("done")
        lines.append(f"echo \"[$(date)] macro done\" >> {log_file}")
        return "\n".join(lines) + "\n"

    def _save_macro(self):
        if not self._record_steps:
            self._log("macro empty — nothing to save")
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".sh",
            filetypes=[("Shell script", "*.sh"), ("All files", "*.*")],
            initialfile=f"macro_{time.strftime('%Y%m%d_%H%M%S')}.sh",
            initialdir=HERE,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self._build_script())
            self._log(f"saved: {path}")
        except Exception as e:
            self._log(f"save err: {e}")

    def _push_run_macro(self):
        if not self._record_steps:
            self._log("macro empty — nothing to push")
            return
        threading.Thread(target=self._do_push_run, daemon=True).start()

    def _do_push_run(self):
        try:
            loops = max(0, int(self.loop_var.get()))
        except Exception:
            loops = 1
        ts = time.strftime("%Y%m%d_%H%M%S")
        local = os.path.join(HERE, f"macro_{ts}.sh")
        remote = f"/data/local/tmp/macro_{ts}.sh"
        log_remote = f"/data/local/tmp/macro_{ts}.log"
        pid_remote = f"/data/local/tmp/macro_{ts}.pid"
        try:
            with open(local, "w", encoding="utf-8", newline="\n") as f:
                f.write(self._build_script(name=ts, loops=loops))
            adb("push", local, remote)
            adb("shell", f"chmod 755 {remote}")
            # setsid + redirected stdio + & → survives adb disconnect.
            # The shell command returns immediately; macro keeps running on device.
            adb("shell",
                f"setsid sh {remote} </dev/null >/dev/null 2>&1 &",
                timeout=10)
            # Give it a moment to write the pid file
            time.sleep(0.4)
            r = adb("shell", f"cat {pid_remote} 2>/dev/null")
            pid = (r.stdout or "").strip()
            self.root.after(0, lambda: self._log(
                f"macro launched detached  pid={pid or '?'}  loops={loops if loops else '∞'}\n"
                f"  script: {remote}\n"
                f"  log:    {log_remote}\n"
                f"  pidfile:{pid_remote}\n"
                f"  케이블 분리해도 계속 돌아갑니다. 멈추려면 'Stop all'."))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"macro launch err: {e}"))

    def _stop_macros(self):
        threading.Thread(target=self._do_stop_macros, daemon=True).start()

    def _do_stop_macros(self):
        # Kill every macro PID we can find, then nuke pid files.
        # SH is our handler command for arbitrary shell.
        sh = (
            "n=0;"
            "for f in /data/local/tmp/macro_*.pid; do "
            "  [ -f \"$f\" ] || continue;"
            "  p=$(cat \"$f\" 2>/dev/null);"
            "  if [ -n \"$p\" ]; then kill -9 \"$p\" 2>/dev/null && n=$((n+1)); fi;"
            "  rm -f \"$f\";"
            "done;"
            # Also catch any orphaned macro processes by script name
            "for p in $(ps -ef 2>/dev/null | awk '/sh \\/data\\/local\\/tmp\\/macro_/ && !/awk/ {print $2}'); do "
            "  kill -9 $p 2>/dev/null && n=$((n+1));"
            "done;"
            "echo killed=$n"
        )
        r = send_cmd(f"SH {sh}", timeout=8)
        self.root.after(0, lambda: self._log(f"stop all → {r}"))

    def _send_text(self):
        t = self.text_entry.get()
        if not t:
            return
        # Route via ADBKeyboard if any non-ASCII char present.
        cmd = "IME_TEXT" if any(ord(c) > 127 for c in t) else "TEXT"
        self._async(f"{cmd} {t}")
        self.text_entry.delete(0, "end")

    def _send_raw(self):
        c = self.raw_entry.get().strip()
        if c:
            self._async(c)
            self.raw_entry.delete(0, "end")

    # ============================================================ screen
    def refresh_shot(self):
        threading.Thread(target=self._do_shot, daemon=True).start()

    def _do_shot(self):
        data = send_cmd("SHOT", return_bytes=True, timeout=8)
        if not data:
            self.root.after(0, lambda: self._log("SHOT: no data"))
            return
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as e:
            self.root.after(0, lambda: self._log(f"SHOT decode err: {e} ({len(data)}B)"))
            return
        w, h = img.size
        ph = int(PREVIEW_W * h / w)
        preview = img.resize((PREVIEW_W, ph), Image.BILINEAR)
        tkimg = ImageTk.PhotoImage(preview)

        def apply():
            self.device_w, self.device_h = w, h
            self.scale = w / PREVIEW_W
            self.canvas.config(width=PREVIEW_W, height=ph)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=tkimg)
            self.last_img_ref = tkimg
            self._set_status(f"connected {w}x{h}", "green")
        self.root.after(0, apply)

    def _toggle_live(self):
        if self.live_var.get():
            self._live.set()
        else:
            self._live.clear()
            self._stop_video_proc()

    def _stop_video_proc(self):
        p = self._video_proc
        self._video_proc = None
        if p:
            try: p.kill()
            except Exception: pass
        s = self._video_sock
        self._video_sock = None
        if s:
            try: s.close()
            except Exception: pass

    def _update_interval(self):
        try:
            self._live_interval = max(0.10, self.interval_var.get() / 1000.0)
        except Exception:
            pass

    # ============================================================ canvas events
    def _on_press(self, e):
        self.drag_start = (e.x, e.y, time.time())

    def _on_release(self, e):
        if not self.drag_start:
            return
        x0, y0, t0 = self.drag_start
        x1, y1 = e.x, e.y
        dt = max(80, int((time.time() - t0) * 1000))
        s = self.scale
        dx, dy = x1 - x0, y1 - y0
        if (dx * dx + dy * dy) ** 0.5 < 6:
            self._async(f"TAP {int(x0*s)} {int(y0*s)}")
        else:
            self._async(f"SWIPE {int(x0*s)} {int(y0*s)} {int(x1*s)} {int(y1*s)} {dt}")
        self.drag_start = None

    # ============================================================ workers
    def _start_workers(self):
        threading.Thread(target=self._screen_loop, daemon=True).start()
        threading.Thread(target=self._stats_loop, daemon=True).start()
        threading.Thread(target=self._procs_loop, daemon=True).start()

    def _screen_loop(self):
        """Dispatches between H.264 and PNG modes based on current selection."""
        while not self._stop.is_set():
            if not self._live.is_set():
                self.root.after(0, lambda: self.fps_label.config(text="0.0 fps", fg="gray"))
                time.sleep(0.2)
                continue
            mode = self.mode_var.get()
            if mode == "H.264" and HAVE_AV:
                self._h264_session()
            else:
                self._png_session()

    def _png_session(self):
        last = time.time()
        frames = 0
        while not self._stop.is_set() and self._live.is_set() and self.mode_var.get() == "PNG":
            t0 = time.time()
            self._do_shot()
            frames += 1
            if time.time() - last >= 1.0:
                fps = frames / (time.time() - last)
                self.root.after(0, lambda f=fps: self.fps_label.config(
                    text=f"{f:.1f} fps (PNG)", fg=("green" if f > 1 else "orange")))
                frames = 0
                last = time.time()
            elapsed = time.time() - t0
            time.sleep(max(0.02, self._live_interval - elapsed))

    def _h264_session(self):
        """Stream H.264 via daemon socket (NOT adb exec-out — that path throttles to ~20KB/s).
        screenrecord on phone has 180s limit; this returns when exhausted, outer loop restarts.
        """
        rec_w = self.device_w // 2 - (self.device_w // 2) % 8
        rec_h = self.device_h // 2 - (self.device_h // 2) % 8
        sock = None
        try:
            sock = socket.socket()
            sock.settimeout(10)
            sock.connect((HOST, PORT))
            sock.sendall(f"STREAM {rec_w}x{rec_h} 6M 175\n".encode())
            sock.settimeout(5)
        except Exception as e:
            self.root.after(0, lambda: self._log(f"STREAM connect err: {e}"))
            if sock:
                try: sock.close()
                except Exception: pass
            time.sleep(1)
            return

        self._video_sock = sock
        codec = av.CodecContext.create("h264", "r")
        last = time.time()
        frames = 0
        try:
            while (not self._stop.is_set() and self._live.is_set()
                   and self.mode_var.get() == "H.264"):
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    # No data for 5s → screen idle. Keep waiting.
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    packets = codec.parse(chunk)
                except av.AVError:
                    continue
                for pkt in packets:
                    try:
                        for frame in codec.decode(pkt):
                            self._show_av_frame(frame)
                            frames += 1
                            if time.time() - last >= 1.0:
                                fps = frames / (time.time() - last)
                                self.root.after(0, lambda f=fps: self.fps_label.config(
                                    text=f"{f:.1f} fps (H.264)",
                                    fg=("green" if f > 10 else "orange")))
                                frames = 0
                                last = time.time()
                    except av.AVError:
                        pass
        finally:
            try: sock.close()
            except Exception: pass
            self._video_sock = None

    def _show_av_frame(self, frame):
        # Convert YUV → RGB → PIL → ImageTk on a Tk-safe call.
        try:
            arr = frame.to_ndarray(format="rgb24")
        except Exception:
            return
        h, w = arr.shape[:2]
        ph = int(PREVIEW_W * h / w)
        img = Image.fromarray(arr).resize((PREVIEW_W, ph), Image.BILINEAR)
        tkimg = ImageTk.PhotoImage(img)

        def apply():
            self.canvas.config(width=PREVIEW_W, height=ph)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=tkimg)
            self.last_img_ref = tkimg
        self.root.after(0, apply)

    def _stats_loop(self):
        consec_fail = 0
        while not self._stop.is_set():
            raw = send_cmd("STATS", timeout=5)
            if raw and not raw.startswith("ERR"):
                self._apply_stats(raw)
                consec_fail = 0
            else:
                consec_fail += 1
                if consec_fail == 3:
                    self._kick_reconnect("stats")
            time.sleep(1.5 if consec_fail < 3 else min(8.0, 1.5 + consec_fail))

    def _procs_loop(self):
        consec_fail = 0
        while not self._stop.is_set():
            raw = send_cmd("APPS", timeout=5)
            if raw and not raw.startswith("ERR"):
                self.root.after(0, lambda r=raw: self._set_apps(r))
                consec_fail = 0
            else:
                consec_fail += 1
            time.sleep(2.5 if consec_fail < 3 else min(10.0, 2.5 + consec_fail))

    REG_METRICS = ("cpu_pct", "mem_pct", "gpu_busy", "cpu_temp", "power_mw")

    def _regression_check(self, sample):
        """Compare current sample to baseline ± Nσ; log once per metric / 60s."""
        if not self.session_started:
            return
        elapsed = sample["ts"] - self.session_started
        if elapsed < self.REG_BASELINE_SEC:
            for k in self.REG_METRICS:
                v = sample.get(k)
                if v is not None:
                    self._reg_baseline[k].append(float(v))
            return
        if not self._reg_stats:
            # Finalize baseline at first post-baseline sample.
            for k, vs in self._reg_baseline.items():
                if len(vs) < 4:
                    continue
                mean = sum(vs) / len(vs)
                var = sum((x - mean) ** 2 for x in vs) / len(vs)
                std = max(0.5, var ** 0.5)
                self._reg_stats[k] = (mean, std)
            if not self._reg_stats:
                return
            self._log(
                "regression baseline locked: "
                + ", ".join(f"{k}={m:.1f}±{s:.1f}"
                            for k, (m, s) in self._reg_stats.items())
            )
        now = sample["ts"]
        for k, (mean, std) in self._reg_stats.items():
            v = sample.get(k)
            if v is None:
                continue
            z = (v - mean) / std
            if abs(z) < self.REG_SIGMA:
                continue
            last = self._reg_last_alert.get(k, 0)
            if now - last < 60:
                continue
            self._reg_last_alert[k] = now
            direction = "↑" if z > 0 else "↓"
            self._log(
                f"⚠ regression: {k}={v:.1f} {direction} "
                f"(baseline {mean:.1f}±{std:.1f}, z={z:+.1f})"
            )

    # Single in-flight reconnect attempt — guarded so multiple workers
    # firing at once don't pile up adb forward/setup calls.
    def _kick_reconnect(self, src):
        if getattr(self, "_reconnecting", False):
            return
        self._reconnecting = True

        def worker():
            try:
                self._log(f"[{src}] daemon unreachable — auto reconnect…")
                self._set_status("reconnecting…", "orange")
                # Re-enumerate devices, re-forward, re-ping
                try:
                    self._auto_connect()
                except Exception as e:
                    self._log(f"auto_connect failed: {e}")
            finally:
                self._reconnecting = False

        threading.Thread(target=worker, daemon=True).start()

    def _set_apps(self, raw):
        fg_pkg = ""
        apps = []  # list of (pid, cpu, mem, res, pkg)
        for line in raw.splitlines():
            parts = line.split(None, 5)
            if not parts:
                continue
            if parts[0] == "FG" and len(parts) >= 2:
                fg_pkg = parts[1]
            elif parts[0] == "APP" and len(parts) >= 6:
                _, pid, cpu, mem, res, pkg = parts
                apps.append((pid, cpu, mem, res, pkg))

        # Reorder: foreground app(s) first, then others by original (CPU) order
        fg_rows, other_rows = [], []
        for r in apps:
            (r in fg_rows) if False else None
            if r[4].split()[0].split(":")[0] == fg_pkg or r[4].startswith(fg_pkg):
                fg_rows.append(r)
            else:
                other_rows.append(r)
        ordered = fg_rows + other_rows

        # Preserve selection by PID
        sel_pid = None
        sel = self.procs_tv.selection()
        if sel:
            sel_pid = self.procs_tv.item(sel[0])["values"][0]

        self.procs_tv.delete(*self.procs_tv.get_children())
        for r in ordered:
            pid, cpu, mem, res, pkg = r
            tag = ("fg",) if r in fg_rows else ()
            iid = self.procs_tv.insert("", "end", values=(pid, cpu, mem, res, pkg), tags=tag)
            if sel_pid is not None and str(pid) == str(sel_pid):
                self.procs_tv.selection_set(iid)
        # Update window title with FG package
        if fg_pkg:
            self.root.title(f"Phone Controller — fg: {fg_pkg}")
            if fg_pkg != self._last_fg_pkg:
                self.fg_events.append((time.time(), fg_pkg))
                self._last_fg_pkg = fg_pkg
        # JSONL emit
        if self.session_dir:
            self._jsonl_write("process", {
                "ts": time.time(),
                "fg_pkg": fg_pkg,
                "apps": [{"pid": p, "cpu": c, "mem": m, "res": rs, "pkg": pk}
                         for p, c, m, rs, pk in ordered],
            })

    # ============================================================ logcat / dump per-pid
    def _selected_proc(self):
        sel = self.procs_tv.selection()
        if not sel:
            return None
        vals = self.procs_tv.item(sel[0])["values"]
        if len(vals) < 5:
            return None
        return {"pid": str(vals[0]), "cpu": vals[1], "mem": vals[2],
                "res": vals[3], "pkg": vals[4]}

    def _on_proc_double(self, _e):
        p = self._selected_proc()
        if p:
            self._open_logcat(p["pid"], p["pkg"])

    def _on_proc_rmouse(self, e):
        row = self.procs_tv.identify_row(e.y)
        if row:
            self.procs_tv.selection_set(row)
        p = self._selected_proc()
        if not p:
            return
        # Strip extra args from cmd field, keep first token (likely package)
        pkg = p["pkg"].split()[0].split(":")[0]
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"PID {p['pid']} · {pkg}", state="disabled")
        menu.add_separator()
        menu.add_command(label="logcat (pid)",
                         command=lambda: self._open_logcat(p["pid"], p["pkg"]))
        menu.add_command(label="threads (per-TID CPU/wait)",
                         command=lambda: self._open_threads(p["pid"], pkg))
        menu.add_command(label="jank timeline (gfxinfo framestats)",
                         command=lambda: self._open_jank(pkg))
        menu.add_command(label="memory detail (PSS/RSS/Heap/Graphics)",
                         command=lambda: self._open_memory(pkg))
        menu.add_command(label="I/O + scheduling (cpuset/governor)",
                         command=lambda: self._open_io_sched(p["pid"], pkg))
        menu.add_command(label="Network I/O (rx/tx)",
                         command=lambda: self._open_netio(p["pid"], pkg))
        menu.add_command(label="simpleperf 30s record + report",
                         command=lambda: self._capture_simpleperf(p["pid"], pkg, 30))
        menu.add_command(label="appops get",
                         command=lambda: self._open_dump(f"appops {pkg}",
                                                          ["shell", "appops", "get", pkg]))
        menu.add_separator()
        menu.add_command(label="dumpsys meminfo <pid>",
                         command=lambda: self._open_dump(f"meminfo {p['pid']}",
                                                          ["shell", "dumpsys", "meminfo", p["pid"]]))
        menu.add_command(label="dumpsys gfxinfo <pkg>",
                         command=lambda: self._open_dump(f"gfxinfo {pkg}",
                                                          ["shell", "dumpsys", "gfxinfo", pkg]))
        menu.add_command(label="dumpsys activity <pkg>",
                         command=lambda: self._open_dump(f"activity {pkg}",
                                                          ["shell", "dumpsys", "activity", pkg]))
        menu.add_command(label="dumpsys package <pkg>",
                         command=lambda: self._open_dump(f"package {pkg}",
                                                          ["shell", "dumpsys", "package", pkg]))
        menu.add_separator()
        menu.add_command(label="Trigger ANR stack (kill -3, see /data/anr/)",
                         command=lambda: self._async(f"SH kill -3 {p['pid']}"))
        menu.add_command(label="Force-stop package",
                         command=lambda: self._async(f"STOP {pkg}"))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _open_dump(self, title, adb_args):
        win = tk.Toplevel(self.root)
        win.title(f"dump — {title}")
        win.geometry("980x700")
        bar = tk.Frame(win); bar.pack(fill="x")
        info = tk.Label(bar, text="loading…", fg="orange"); info.pack(side="left")
        tk.Button(bar, text="Reload",
                  command=lambda: threading.Thread(target=load, daemon=True).start()
                  ).pack(side="right")
        txt = scrolledtext.ScrolledText(win, font=("Consolas", 9), wrap="none")
        txt.pack(fill="both", expand=True)

        def load():
            try:
                r = subprocess.run(
                    adb_cmd(*adb_args), capture_output=True, text=True, timeout=30,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
                out = (r.stdout or "") + (("\n--- stderr ---\n" + r.stderr) if r.stderr else "")
            except Exception as e:
                out = f"ERR: {e}"
            self.root.after(0, lambda: [
                txt.delete("1.0", "end"),
                txt.insert("1.0", out),
                info.config(text=f"{len(out):,} chars", fg="green"),
            ])
        threading.Thread(target=load, daemon=True).start()

    # ============================================================ memory detail
    def _open_memory(self, pkg):
        win = tk.Toplevel(self.root)
        win.title(f"memory — {pkg}")
        win.geometry("760x580")
        bar = tk.Frame(win); bar.pack(fill="x")
        auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Auto (3s)", variable=auto_var).pack(side="left")
        tk.Button(bar, text="Refresh now",
                  command=lambda: threading.Thread(target=poll, daemon=True).start()
                  ).pack(side="left", padx=4)
        info = tk.Label(bar, text="sampling…", fg="orange"); info.pack(side="left", padx=8)
        pid_lbl = tk.Label(bar, text="", font=("Consolas", 9), fg="gray")
        pid_lbl.pack(side="right", padx=8)

        # Tree of key/value pairs (always-present rows so the layout stays stable).
        cols = ("metric", "value", "delta")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=14)
        for c, w, a in [("metric", 200, "w"), ("value", 140, "e"), ("delta", 110, "e")]:
            tv.heading(c, text={"metric": "Metric", "value": "Current",
                                "delta": "Δ since open"}[c])
            tv.column(c, width=w, anchor=a, stretch=(c == "metric"))
        tv.tag_configure("hi", font=("Segoe UI", 9, "bold"))
        tv.tag_configure("up", foreground="#c33")
        tv.tag_configure("down", foreground="#393")
        tv.pack(fill="x", padx=4, pady=4)

        # Sparkline of PSS over time
        plot_lbl = tk.LabelFrame(win, text="TOTAL PSS over time (KB)")
        plot_lbl.pack(fill="both", expand=True, padx=4, pady=4)
        canvas = tk.Canvas(plot_lbl, bg="#111", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        history = []  # list[(timestamp, total_pss_kb)]
        baseline = {}  # metric -> first observed value (for delta column)
        stop = threading.Event()

        # Metric labels we extract from "App Summary"
        APP_SUMMARY_KEYS = [
            "Java Heap", "Native Heap", "Code", "Stack", "Graphics",
            "Private Other", "System", "Unknown",
            "TOTAL PSS", "TOTAL RSS", "TOTAL SWAP PSS",
        ]
        STATUS_KEYS = ["VmPeak", "VmSize", "VmRSS", "VmData", "VmStk", "VmExe", "VmLib"]
        SMAPS_KEYS = ["Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem",
                      "Shared_Clean", "Shared_Dirty", "Private_Clean", "Private_Dirty",
                      "Swap", "SwapPss"]

        def parse(raw):
            """Return {metric_name: kb_int}."""
            out = {}
            section = None
            in_app_summary = False
            for line in raw.splitlines():
                if line.startswith("==="):
                    section = line.strip("= ").strip()
                    in_app_summary = False
                    continue
                if section == "MEMINFO":
                    if "App Summary" in line:
                        in_app_summary = True
                        continue
                    if in_app_summary:
                        # Two formats:
                        #   "         Java Heap:    12345"
                        #   "              TOTAL PSS:  43000      TOTAL RSS:  88888  TOTAL SWAP PSS: 0"
                        # Try TOTAL line first (multiple key/value pairs)
                        if "TOTAL" in line:
                            # Walk pairs
                            import re
                            for m in re.finditer(r"(TOTAL [A-Z ]+?):\s*(\d+)", line):
                                out[m.group(1).strip()] = int(m.group(2))
                            continue
                        # Single key:value
                        m = line.strip().split(":")
                        if len(m) == 2:
                            key = m[0].strip()
                            val = m[1].strip().split()
                            if val and val[0].isdigit():
                                out[key] = int(val[0])
                elif section == "PID":
                    s = line.strip()
                    if s.isdigit():
                        out["__pid"] = int(s)
                elif section == "SMAPS_ROLLUP" or section == "STATUS":
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].endswith(":"):
                        key = parts[0].rstrip(":")
                        if parts[1].isdigit() and (key in SMAPS_KEYS or key in STATUS_KEYS):
                            out[key] = int(parts[1])
                elif section == "OOM":
                    s = line.strip()
                    if s.lstrip("-").isdigit():
                        # First number is oom_score, second is oom_score_adj
                        if "__oom_score" not in out:
                            out["__oom_score"] = int(s)
                        else:
                            out["__oom_score_adj"] = int(s)
            return out

        def poll():
            try:
                raw = send_cmd(f"MEMDETAIL {pkg}", timeout=10)
            except Exception as e:
                self.root.after(0, lambda: info.config(text=f"err: {e}", fg="red"))
                return
            if not raw or raw.startswith("ERR"):
                self.root.after(0, lambda: info.config(
                    text=f"err: {str(raw)[:80]}", fg="red"))
                return
            data = parse(raw)
            if not data:
                self.root.after(0, lambda: info.config(
                    text="no meminfo (process not running?)", fg="orange"))
                return
            history.append((time.time(), data.get("TOTAL PSS", 0)))
            if len(history) > 120:
                history.pop(0)
            self.root.after(0, lambda d=data: render(d))

        def render(data):
            if not baseline:
                baseline.update(data)
            tv.delete(*tv.get_children())
            # App Summary
            for k in APP_SUMMARY_KEYS:
                if k in data:
                    cur = data[k]
                    base = baseline.get(k, cur)
                    delta = cur - base
                    fmt = f"{cur:,} KB"
                    dfmt = f"{delta:+,} KB" if delta else ""
                    tag = "hi" if k.startswith("TOTAL") else ""
                    dtag = "up" if delta > 0 else ("down" if delta < 0 else "")
                    tv.insert("", "end", values=(k, fmt, dfmt),
                              tags=(tag, dtag) if dtag else (tag,))
            # smaps_rollup highlights
            for k in ("Rss", "Pss", "SwapPss"):
                if k in data:
                    cur = data[k]
                    base = baseline.get(k, cur)
                    delta = cur - base
                    fmt = f"{cur:,} KB"
                    dfmt = f"{delta:+,} KB" if delta else ""
                    dtag = "up" if delta > 0 else ("down" if delta < 0 else "")
                    tv.insert("", "end", values=(f"smaps {k}", fmt, dfmt),
                              tags=(dtag,) if dtag else ())
            # Status excerpts
            for k in ("VmPeak", "VmRSS", "VmData"):
                if k in data:
                    tv.insert("", "end", values=(f"status {k}", f"{data[k]:,} KB", ""))
            # PID + OOM
            pid_str = data.get("__pid", "—")
            oom = data.get("__oom_score", "—")
            oom_adj = data.get("__oom_score_adj", "—")
            pid_lbl.config(text=f"pid={pid_str}  oom={oom}/{oom_adj}")
            info.config(text=f"{pkg}", fg="green")
            redraw_sparkline()

        def redraw_sparkline():
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w < 10 or h < 10 or not history:
                return
            values = [v for _, v in history]
            vmin = min(values)
            vmax = max(values)
            if vmax == vmin:
                vmax = vmin + 1
            # Reference: starting baseline
            base = baseline.get("TOTAL PSS", values[0])
            y_base = h - 5 - (base - vmin) * (h - 10) / (vmax - vmin)
            canvas.create_line(5, y_base, w - 5, y_base, fill="#444", dash=(2, 2))
            canvas.create_text(w - 8, y_base - 8, anchor="e", fill="#777",
                               text=f"baseline {base:,}", font=("Consolas", 8))
            # Line graph
            n = len(values)
            if n < 2:
                return
            step = (w - 10) / (n - 1)
            pts = []
            for i, v in enumerate(values):
                x = 5 + i * step
                y = h - 5 - (v - vmin) * (h - 10) / (vmax - vmin)
                pts.extend([x, y])
            canvas.create_line(*pts, fill="#6cf", width=2)
            canvas.create_text(8, 10, anchor="w", fill="#6cf",
                               text=f"max {vmax:,}  min {vmin:,}",
                               font=("Consolas", 9))

        def loop():
            while not stop.is_set():
                poll()
                # Auto-respect toggle: skip work loop when disabled
                for _ in range(30):  # 30 × 100ms = 3s
                    if stop.is_set() or not auto_var.get():
                        break
                    time.sleep(0.1)
                if not auto_var.get():
                    time.sleep(0.5)

        canvas.bind("<Configure>", lambda _e: redraw_sparkline())
        threading.Thread(target=loop, daemon=True).start()

        def close():
            stop.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ I/O + scheduling
    def _open_io_sched(self, pid, pkg):
        win = tk.Toplevel(self.root)
        win.title(f"I/O + sched — pid {pid} · {pkg}")
        win.geometry("780x600")
        info = tk.Label(win, text="sampling…", fg="orange"); info.pack(anchor="w", padx=4)

        io_frame = tk.LabelFrame(win, text="/proc/<pid>/io  (Δ/s since open)")
        io_frame.pack(fill="x", padx=4, pady=2)
        io_text = tk.Label(io_frame, text="—", font=("Consolas", 9),
                           justify="left", anchor="w")
        io_text.pack(fill="x", padx=4, pady=2)

        sched_frame = tk.LabelFrame(win, text="scheduling — cpuset / cgroup / governor / sched")
        sched_frame.pack(fill="both", expand=True, padx=4, pady=2)
        sched_text = scrolledtext.ScrolledText(sched_frame, font=("Consolas", 9), height=24)
        sched_text.pack(fill="both", expand=True)

        stop = threading.Event()
        first_io = {"data": None, "t": None}

        def parse_io(block):
            d = {}
            for line in block.splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()
                    if val and val[0].lstrip("-").isdigit():
                        d[key] = int(val[0])
            return d

        def fmt_bytes(n):
            for unit in ("B", "KB", "MB", "GB"):
                if abs(n) < 1024:
                    return f"{n:.1f}{unit}"
                n /= 1024
            return f"{n:.1f}TB"

        def loop_io():
            while not stop.is_set():
                raw = send_cmd(f"IO_PID {pid}", timeout=5)
                if not raw or raw.startswith("ERR"):
                    self.root.after(0, lambda r=raw: info.config(
                        text=f"err: {str(r)[:60]}", fg="red"))
                    time.sleep(2)
                    continue
                ts = None
                body = []
                for line in raw.splitlines():
                    if line.startswith("TIME "):
                        try: ts = int(line.split(None, 1)[1])
                        except: pass
                    elif line.startswith("==="):
                        continue
                    else:
                        body.append(line)
                d = parse_io("\n".join(body))
                if not d:
                    self.root.after(0, lambda: info.config(text="empty /proc/io", fg="orange"))
                    time.sleep(2)
                    continue
                # First sample = baseline
                if first_io["data"] is None:
                    first_io["data"] = dict(d)
                    first_io["t"] = ts
                    self.root.after(0, lambda dd=d: io_text.config(
                        text=fmt_io_block(dd, None, 0)))
                else:
                    dt = max(1, (ts - first_io["t"]) / 1e9) if ts else 1
                    self.root.after(0, lambda dd=d, ddt=dt: io_text.config(
                        text=fmt_io_block(dd, first_io["data"], ddt)))
                self.root.after(0, lambda: info.config(
                    text=f"pid {pid} · {pkg}", fg="green"))
                time.sleep(2.0)

        def fmt_io_block(cur, base, dt_s):
            lines = []
            order = ["rchar", "wchar", "syscr", "syscw",
                     "read_bytes", "write_bytes", "cancelled_write_bytes"]
            for k in order:
                if k not in cur:
                    continue
                v = cur[k]
                if base:
                    delta = v - base.get(k, v)
                    rate = delta / max(0.001, dt_s)
                    if k.endswith("bytes") or k.endswith("char"):
                        rate_s = fmt_bytes(rate) + "/s"
                        delta_s = fmt_bytes(delta)
                    else:
                        rate_s = f"{rate:.1f}/s"
                        delta_s = f"{delta}"
                    lines.append(f"{k:24s} {v:>15,d}    Δ {delta_s:>10}    {rate_s}")
                else:
                    fmtv = fmt_bytes(v) if k.endswith("bytes") or k.endswith("char") else f"{v:,}"
                    lines.append(f"{k:24s} {v:>15,d}    ({fmtv})")
            return "\n".join(lines)

        def fetch_sched_once():
            raw = send_cmd(f"SCHED_PID {pid}", timeout=5)
            if not raw or raw.startswith("ERR"):
                self.root.after(0, lambda r=raw: sched_text.insert("end",
                    f"sched err: {r}\n"))
                return
            self.root.after(0, lambda: [
                sched_text.delete("1.0", "end"),
                sched_text.insert("end", raw),
            ])

        def loop_sched():
            while not stop.is_set():
                fetch_sched_once()
                for _ in range(50):  # 5s
                    if stop.is_set():
                        return
                    time.sleep(0.1)

        threading.Thread(target=loop_io, daemon=True).start()
        threading.Thread(target=loop_sched, daemon=True).start()

        def close():
            stop.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ stability viewer
    def _open_stability(self):
        win = tk.Toplevel(self.root)
        win.title("Stability — crash / ANR / tombstone")
        win.geometry("1020x680")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True)

        def make_tab(label, cmd):
            frame = tk.Frame(nb)
            nb.add(frame, text=label)
            bar = tk.Frame(frame); bar.pack(fill="x")
            tk.Button(bar, text="Refresh",
                      command=lambda: threading.Thread(target=load, daemon=True).start()
                      ).pack(side="left")
            info = tk.Label(bar, text="loading…", fg="orange"); info.pack(side="left", padx=8)
            txt = scrolledtext.ScrolledText(frame, font=("Consolas", 9), wrap="none")
            txt.pack(fill="both", expand=True)

            def load():
                raw = send_cmd(cmd, timeout=20)
                self.root.after(0, lambda r=raw: [
                    txt.delete("1.0", "end"),
                    txt.insert("end", r or "(no data)"),
                    info.config(text=f"{len(r or ''):,} chars", fg="green"),
                ])
            threading.Thread(target=load, daemon=True).start()

        make_tab("Crash buffer (logcat -b crash)", "CRASH_RECENT")
        make_tab("ANR (/data/anr + dropbox)", "ANR_LS")
        make_tab("Tombstones (/data/tombstones)", "TOMBSTONE_LS")
        make_tab("Binder", "BINDER_DUMP")
        make_tab("Activity processes", "ACTIVITY_PROCS")

    # ============================================================ tmp cleanup
    def _cleanup_tmp(self, all_files=False):
        def go():
            arg = "all" if all_files else ""
            r = send_cmd(f"CLEANUP_TMP {arg}", timeout=10)
            self.root.after(0, lambda: self._log(f"cleanup_tmp{' (all)' if all_files else ''} → {r}"))
        threading.Thread(target=go, daemon=True).start()

    # ============================================================ session / artifacts
    def _toggle_session(self):
        if self.session_dir is None:
            self._start_session()
        else:
            self._stop_session()

    def _start_session(self):
        ts = time.strftime("%Y%m%d_%H%M%S")
        artifacts_root = os.path.join(HERE, "artifacts")
        session_dir = os.path.join(artifacts_root, ts)
        try:
            os.makedirs(session_dir, exist_ok=True)
            os.makedirs(os.path.join(session_dir, "screenshots"), exist_ok=True)
        except OSError as e:
            messagebox.showerror("session", f"can't create artifacts dir: {e}")
            return
        self.session_dir = session_dir
        self.session_started = time.time()
        self.session_stop_evt.clear()
        # Regression-detector state — collect baseline samples during the first
        # 30s, then alert when subsequent samples exceed mean ± 3σ. Throttled
        # per metric to one alert / 60s so a sustained anomaly doesn't spam.
        self._reg_baseline = collections.defaultdict(list)
        self._reg_stats = {}
        self._reg_last_alert = {}
        self.REG_BASELINE_SEC = 30
        self.REG_SIGMA = 3.0
        # Open append-mode JSONL writers
        try:
            self.session_files = {
                "realtime": open(os.path.join(session_dir, "realtime_metrics.jsonl"),
                                 "a", encoding="utf-8", buffering=1),
                "process": open(os.path.join(session_dir, "process_metrics.jsonl"),
                                "a", encoding="utf-8", buffering=1),
                "llm": open(os.path.join(session_dir, "llm_runtime_metrics.jsonl"),
                            "a", encoding="utf-8", buffering=1),
            }
        except OSError as e:
            messagebox.showerror("session", f"can't open jsonl: {e}")
            self.session_dir = None
            return
        # device_info.json (one-shot snapshot)
        threading.Thread(target=self._collect_device_info, daemon=True).start()
        # logcat capture (full log) + LLM_METRIC filtered tag
        self._start_logcat_capture()
        self._start_llm_metric_capture()
        self.session_btn.config(text="■ Stop session", fg="red")
        self._log(f"session started → {session_dir}")
        # Live label update worker
        threading.Thread(target=self._session_label_loop, daemon=True).start()

    def _stop_session(self):
        if self.session_dir is None:
            return
        self.session_stop_evt.set()
        # Stop logcat subprocesses
        for proc_attr in ("_logcat_proc", "_llm_logcat_proc"):
            p = getattr(self, proc_attr)
            if p:
                try: p.kill()
                except Exception: pass
                setattr(self, proc_attr, None)
        # Close JSONL files
        for f in self.session_files.values():
            try: f.close()
            except Exception: pass
        sdir = self.session_dir
        self.session_files = {}
        self.session_dir = None
        self.session_started = None
        self.session_btn.config(text="● Start session", fg="green")
        self.session_lbl.config(text="")
        # Generate report
        try:
            self._generate_report(sdir)
            self._log(f"session stopped. artifacts → {sdir}")
        except Exception as e:
            self._log(f"report gen err: {e}")

    def _session_label_loop(self):
        while not self.session_stop_evt.is_set() and self.session_started:
            dur = int(time.time() - self.session_started)
            self.root.after(0, lambda d=dur: self.session_lbl.config(
                text=f"REC {d//60:02d}:{d%60:02d}", fg="red"))
            time.sleep(1.0)

    def _jsonl_write(self, name, obj):
        f = self.session_files.get(name)
        if not f:
            return
        try:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def _collect_device_info(self):
        raw = send_cmd("DEVICE_INFO", timeout=10)
        info = {"collected_at": time.time(), "raw": raw}
        if raw:
            props = {}
            section = None
            for line in raw.splitlines():
                if line.startswith("==="):
                    section = line.strip("= ").strip()
                    continue
                if section == "PROPS" and "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
                elif section == "MEMTOTAL":
                    s = line.strip()
                    if s.isdigit():
                        info["mem_total_kb"] = int(s)
                elif section == "CPU_COUNT":
                    s = line.strip()
                    if s.isdigit():
                        info["cpu_count"] = int(s)
                elif section == "KERNEL":
                    info.setdefault("kernel", line.strip())
            info["props"] = props
            info["refresh_hz"] = self.refresh_hz
        path = os.path.join(self.session_dir, "device_info.json")
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(info, fp, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _start_logcat_capture(self):
        path = os.path.join(self.session_dir, "logcat.txt")
        try:
            f = open(path, "ab", buffering=0)
        except OSError as e:
            self._log(f"logcat capture err: {e}")
            return
        proc = subprocess.Popen(
            adb_cmd("logcat", "-v", "time", "-T", "1"),
            stdout=f, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        self._logcat_proc = proc

    def _start_llm_metric_capture(self):
        """Filter logcat for tag 'LLM_METRIC' and parse each line as JSON."""
        proc = subprocess.Popen(
            adb_cmd("logcat", "-s", "LLM_METRIC:I", "-v", "raw", "-T", "1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            bufsize=1,
        )
        self._llm_logcat_proc = proc

        def reader():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    if self.session_stop_evt.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith("---"):
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        # Not JSON — store raw as message
                        obj = {"raw": line, "ts": time.time()}
                    obj.setdefault("ts", time.time())
                    self._jsonl_write("llm", obj)
            except Exception:
                pass
        threading.Thread(target=reader, daemon=True).start()

    # ============================================================ perfetto
    def _capture_perfetto(self, duration_s=30):
        if self._perfetto_running.is_set():
            self._log("perfetto already running")
            return
        threading.Thread(target=self._do_perfetto, args=(duration_s,), daemon=True).start()

    def _do_perfetto(self, duration_s):
        self._perfetto_running.set()
        try:
            self.root.after(0, lambda: self._set_status(
                f"perfetto recording ({duration_s}s)...", "orange"))
            cfg = f'duration_ms: {duration_s * 1000}\n' + PERFETTO_CONFIG
            remote = f"/data/misc/perfetto-traces/trace_{int(time.time())}.pb"
            # Push config inline via -c - --txt
            proc = subprocess.Popen(
                adb_cmd("shell", "perfetto", "-c", "-", "--txt", "-o", remote),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            out, _ = proc.communicate(cfg.encode("utf-8"), timeout=duration_s + 60)
            if proc.returncode != 0:
                msg = (out or b"").decode("utf-8", "replace")[:400]
                self.root.after(0, lambda: [
                    self._log(f"perfetto err rc={proc.returncode}:\n{msg}"),
                    self._set_status("perfetto failed", "red"),
                ])
                return
            # Pull
            local_dir = self.session_dir or os.path.join(HERE, "artifacts", "_perfetto")
            os.makedirs(local_dir, exist_ok=True)
            local = os.path.join(local_dir, "perfetto_trace.pb")
            r = adb("pull", remote, local, timeout=120)
            adb("shell", f"rm -f {remote}", timeout=10)
            self.root.after(0, lambda: [
                self._log(f"perfetto saved → {local}\n  open at https://ui.perfetto.dev"),
                self._set_status("connected", "green"),
            ])
        except subprocess.TimeoutExpired:
            self.root.after(0, lambda: self._log("perfetto timed out"))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"perfetto err: {e}"))
        finally:
            self._perfetto_running.clear()

    # ============================================================ simpleperf
    def _capture_simpleperf(self, pid, pkg, duration_s=30):
        if self._simpleperf_running.is_set():
            self._log("simpleperf already running")
            return
        threading.Thread(target=self._do_simpleperf,
                         args=(pid, pkg, duration_s), daemon=True).start()

    def _do_simpleperf(self, pid, pkg, duration_s):
        self._simpleperf_running.set()
        try:
            self.root.after(0, lambda: self._set_status(
                f"simpleperf recording pid {pid} ({duration_s}s)...", "orange"))
            remote_data = f"/data/local/tmp/perf_{pid}_{int(time.time())}.data"
            # Record (-g call graphs, --duration)
            r = adb("shell", "simpleperf", "record", "-g",
                    "--duration", str(duration_s), "-p", str(pid),
                    "-o", remote_data, timeout=duration_s + 60)
            if r.returncode != 0:
                msg = (r.stdout or "") + "\n" + (r.stderr or "")
                self.root.after(0, lambda: [
                    self._log(f"simpleperf record err:\n{msg[:400]}"),
                    self._set_status("simpleperf failed", "red"),
                ])
                return
            # Run report on-device
            report = adb("shell", "simpleperf", "report", "-i", remote_data, timeout=60)
            local_dir = self.session_dir or os.path.join(HERE, "artifacts", "_simpleperf")
            os.makedirs(local_dir, exist_ok=True)
            local_data = os.path.join(local_dir, f"perf_{pid}.data")
            local_report = os.path.join(local_dir, f"simpleperf_report_{pid}.txt")
            adb("pull", remote_data, local_data, timeout=120)
            with open(local_report, "w", encoding="utf-8") as fp:
                fp.write(f"# simpleperf report — pid {pid} ({pkg}) duration {duration_s}s\n\n")
                fp.write(report.stdout or "")
            adb("shell", f"rm -f {remote_data}", timeout=10)
            self.root.after(0, lambda: [
                self._log(f"simpleperf saved → {local_report}"),
                self._set_status("connected", "green"),
            ])
        except subprocess.TimeoutExpired:
            self.root.after(0, lambda: self._log("simpleperf timed out"))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"simpleperf err: {e}"))
        finally:
            self._simpleperf_running.clear()

    # ============================================================ report generation
    def _generate_report(self, session_dir):
        """Apply bottleneck rules (spec §9) over collected JSONL → report.md."""
        rt_path = os.path.join(session_dir, "realtime_metrics.jsonl")
        proc_path = os.path.join(session_dir, "process_metrics.jsonl")
        llm_path = os.path.join(session_dir, "llm_runtime_metrics.jsonl")
        di_path = os.path.join(session_dir, "device_info.json")
        report_path = os.path.join(session_dir, "report.md")

        def load_jsonl(p):
            if not os.path.exists(p):
                return []
            out = []
            with open(p, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return out

        rt = load_jsonl(rt_path)
        procs = load_jsonl(proc_path)
        llm = load_jsonl(llm_path)
        device = {}
        if os.path.exists(di_path):
            try:
                with open(di_path, "r", encoding="utf-8") as fp:
                    device = json.load(fp)
            except json.JSONDecodeError:
                pass

        # Aggregates over realtime metrics
        def vals(key):
            return [r[key] for r in rt if isinstance(r.get(key), (int, float))]

        cpu_vals = vals("cpu_pct")
        mem_vals = vals("mem_pct")
        temp_vals = vals("cpu_temp")
        gpu_busy_vals = vals("gpu_busy")
        gpu_freq_vals = vals("gpu_freq_mhz")
        pwr_vals = vals("power_mw")

        def peak(xs): return max(xs) if xs else None
        def avg(xs): return (sum(xs) / len(xs)) if xs else None

        # Findings — apply rules from spec §9
        findings = []
        if temp_vals and len(rt) > 10:
            mid = len(rt) // 2
            t1 = avg([rt[i].get("cpu_temp") for i in range(mid) if isinstance(rt[i].get("cpu_temp"), (int, float))])
            t2 = avg([rt[i].get("cpu_temp") for i in range(mid, len(rt)) if isinstance(rt[i].get("cpu_temp"), (int, float))])
            if t1 and t2 and t2 - t1 > 5:
                findings.append(f"**thermal 상승**: 전반기 평균 {t1:.1f}°C → 후반기 {t2:.1f}°C (+{t2-t1:.1f}°C). "
                                "thermal throttling 의심 — decode tokens/sec와 후반부 GPU/CPU freq 하락 동시 확인 필요.")
        if gpu_busy_vals and peak(gpu_busy_vals) is not None and peak(gpu_busy_vals) < 20 \
                and llm and any("tokens_per_sec" in m and m.get("tokens_per_sec", 0) < 5 for m in llm):
            findings.append("**GPU 미사용 의심**: GPU busy peak <20%이고 tokens/sec 낮음. "
                            "GPU delegate 요청 후 CPU fallback 가능성 — backend_actual 확인 필요.")
        if cpu_vals and peak(cpu_vals) is not None and peak(cpu_vals) > 80:
            findings.append(f"**CPU 포화**: peak {peak(cpu_vals):.1f}%. CPU 병목 — simpleperf top function 확인 권장.")
        # LLM-specific findings
        if llm:
            ttft_vals = [m.get("ttft_ms") for m in llm if isinstance(m.get("ttft_ms"), (int, float))]
            tps_vals = [m.get("tokens_per_sec") for m in llm if isinstance(m.get("tokens_per_sec"), (int, float))]
            prefill_vals = [m.get("prefill_ms") for m in llm if isinstance(m.get("prefill_ms"), (int, float))]
            if ttft_vals and max(ttft_vals) > 2000:
                findings.append(f"**TTFT 높음**: max {max(ttft_vals):.0f}ms — prefill 또는 cold-load 병목 의심.")
            if tps_vals and min(tps_vals) < 3:
                findings.append(f"**decode 느림**: min tokens/sec {min(tps_vals):.1f}. backend/quantization 재검토.")

        # Render markdown
        props = device.get("props", {}) if isinstance(device, dict) else {}
        manuf = props.get("ro.product.manufacturer", "?")
        model = props.get("ro.product.model", "?")
        soc = props.get("ro.soc.model") or props.get("ro.board.platform", "?")
        andver = props.get("ro.build.version.release", "?")
        ram_mb = (device.get("mem_total_kb", 0) // 1024) if device else 0
        gpu_model = props.get("ro.hardware.chipname") or ""
        last_llm = llm[-1] if llm else {}
        dur_s = int(rt[-1]["ts"] - rt[0]["ts"]) if len(rt) >= 2 else 0

        with open(report_path, "w", encoding="utf-8") as fp:
            fp.write("# Android LLM Runtime Analysis Report\n\n")
            fp.write(f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S')} · session duration {dur_s}s · "
                     f"{len(rt)} realtime samples, {len(procs)} process samples, {len(llm)} LLM events_\n\n")

            fp.write("## 1. Device Summary\n")
            fp.write(f"- Manufacturer: {manuf}\n")
            fp.write(f"- Model: {model}\n")
            fp.write(f"- SoC: {soc}\n")
            fp.write(f"- Android Version: {andver}\n")
            fp.write(f"- RAM: {ram_mb} MB\n")
            fp.write(f"- GPU: {gpu_model}\n")
            fp.write(f"- Refresh rate: {device.get('refresh_hz', '?')} Hz\n\n")

            fp.write("## 2. Model Summary\n")
            fp.write(f"- Model: {last_llm.get('model_name', '—')}\n")
            fp.write(f"- Quantization: {last_llm.get('quantization', '—')}\n")
            fp.write(f"- Runtime: {last_llm.get('runtime', '—')}\n")
            fp.write(f"- Requested Backend: {last_llm.get('backend_requested', '—')}\n")
            fp.write(f"- Actual Backend: {last_llm.get('backend_actual', '—')}\n\n")

            fp.write("## 3. Performance Summary\n")
            def fmt(v, suffix=""):
                return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else "—"
            fp.write(f"- Load Time: {fmt(last_llm.get('load_time_ms'), ' ms')}\n")
            fp.write(f"- TTFT: {fmt(last_llm.get('ttft_ms'), ' ms')}\n")
            fp.write(f"- Prefill Time: {fmt(last_llm.get('prefill_ms'), ' ms')}\n")
            fp.write(f"- Decode Tokens/sec: {fmt(last_llm.get('tokens_per_sec'))}\n")
            fp.write(f"- ms/token: {fmt(last_llm.get('ms_per_token'))}\n")
            fp.write(f"- Peak CPU: {fmt(peak(cpu_vals), '%')}\n")
            fp.write(f"- Peak MEM: {fmt(peak(mem_vals), '%')}\n")
            fp.write(f"- Peak GPU Busy: {fmt(peak(gpu_busy_vals), '%')}\n")
            fp.write(f"- Peak Temperature: {fmt(peak(temp_vals), '°C')}\n")
            fp.write(f"- Peak Power: {fmt(peak(pwr_vals), ' mW')}\n\n")

            fp.write("## 4. Bottleneck Findings\n")
            if findings:
                for i, f_ in enumerate(findings, 1):
                    fp.write(f"- Finding {i}: {f_}\n")
            else:
                fp.write("- (no findings — insufficient data or no clear bottleneck pattern)\n")
            fp.write("\n")

            fp.write("## 5. Evidence\n")
            fp.write(f"- realtime_metrics.jsonl ({len(rt)} samples)\n")
            fp.write(f"- process_metrics.jsonl ({len(procs)} samples)\n")
            fp.write(f"- llm_runtime_metrics.jsonl ({len(llm)} events)\n")
            if os.path.exists(os.path.join(session_dir, "perfetto_trace.pb")):
                fp.write("- perfetto_trace.pb — open at https://ui.perfetto.dev\n")
            spr = [n for n in os.listdir(session_dir) if n.startswith("simpleperf_report_")]
            for n in spr:
                fp.write(f"- {n}\n")
            fp.write("\n")

            fp.write("## 6. Recommendations\n")
            if not llm:
                fp.write("- **LLM 메트릭이 수집되지 않았습니다.** 앱에서 `Log.i(\"LLM_METRIC\", json)` 호출 필요. "
                         "JSON 스키마는 `LLM_METRIC_SCHEMA.md` 참고.\n")
            if not findings:
                fp.write("- 더 긴 세션 또는 LLM 실제 추론 중 캡처 권장.\n")
            else:
                fp.write("- 위 Findings 기반 모델/runtime 최적화 후 동일 세션 재측정 비교.\n")

    # ============================================================ session compare
    # Realtime metrics shown in the compare table.
    # (jsonl_key, display name, lower_is_better, format_fn)
    _COMPARE_METRICS = [
        ("cpu_pct",      "CPU %",       True,  lambda v: f"{v:.1f}"),
        ("mem_pct",      "MEM %",       True,  lambda v: f"{v:.1f}"),
        ("gpu_busy",     "GPU %",       True,  lambda v: f"{v:.1f}"),
        ("cpu_temp",     "Temp °C",     True,  lambda v: f"{v:.1f}"),
        ("gpu_freq_mhz", "GPU MHz",     False, lambda v: f"{v:.0f}"),
        ("power_mw",     "Power mW",    True,  lambda v: f"{v:.0f}"),
    ]
    # LLM metrics — taken from llm_runtime_metrics.jsonl event="done" rows.
    _COMPARE_LLM = [
        ("ttft_ms",        "TTFT ms",   True,  lambda v: f"{v:.0f}"),
        ("tokens_per_sec", "tps",       False, lambda v: f"{v:.1f}"),
    ]

    @staticmethod
    def _load_metrics(session_dir):
        """Return dict[metric_key] -> list[float] from realtime + llm JSONL."""
        out = collections.defaultdict(list)
        for fname in ("realtime_metrics.jsonl", "llm_runtime_metrics.jsonl"):
            path = os.path.join(session_dir, fname)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for k, v in row.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            out[k].append(float(v))
        return out

    @staticmethod
    def _stat(vals):
        """Return (mean, p95) or (None, None) for empty input."""
        if not vals:
            return None, None
        n = len(vals)
        mean = sum(vals) / n
        s = sorted(vals)
        p95 = s[min(n - 1, int(0.95 * n))]
        return mean, p95

    def _open_compare(self):
        artifacts_root = os.path.join(HERE, "artifacts")
        initial = artifacts_root if os.path.isdir(artifacts_root) else HERE
        a = filedialog.askdirectory(title="Baseline session (A)", initialdir=initial)
        if not a:
            return
        b = filedialog.askdirectory(title="Compare against (B)", initialdir=initial)
        if not b:
            return

        try:
            metrics_a = self._load_metrics(a)
            metrics_b = self._load_metrics(b)
        except Exception as e:
            messagebox.showerror("compare", f"load failed: {e}")
            return

        if not metrics_a or not metrics_b:
            messagebox.showwarning(
                "compare",
                "One or both sessions have no realtime_metrics.jsonl / "
                "llm_runtime_metrics.jsonl data.",
            )
            return

        win = tk.Toplevel(self.root)
        win.title(f"Compare — {os.path.basename(a)}  vs  {os.path.basename(b)}")
        win.geometry("1000x640")

        hdr = tk.Frame(win); hdr.pack(fill="x", padx=8, pady=6)
        tk.Label(hdr, text=f"A: {a}", font=("Consolas", 9), fg="#888"
                 ).pack(anchor="w")
        tk.Label(hdr, text=f"B: {b}", font=("Consolas", 9), fg="#888"
                 ).pack(anchor="w")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        # ----- Tab 1: stats table -----
        tab_tbl = tk.Frame(nb); nb.add(tab_tbl, text="Stats")
        tk.Label(tab_tbl,
                 text="Δ% colored green when change improves the metric "
                      "(↑ for tps/GPU MHz, ↓ for everything else).",
                 font=("Segoe UI", 9, "italic"), fg="#666"
                 ).pack(anchor="w", pady=(4, 0))

        cols = ("metric", "a_mean", "a_p95", "b_mean", "b_p95", "delta_mean", "n")
        tv = ttk.Treeview(tab_tbl, columns=cols, show="headings", height=18)
        widths = {"metric": 140, "a_mean": 100, "a_p95": 100,
                  "b_mean": 100, "b_p95": 100, "delta_mean": 110, "n": 90}
        headers = {"metric": "Metric", "a_mean": "A mean", "a_p95": "A p95",
                   "b_mean": "B mean", "b_p95": "B p95",
                   "delta_mean": "Δ mean", "n": "samples A/B"}
        for c in cols:
            tv.heading(c, text=headers[c])
            anchor = "w" if c == "metric" else "e"
            tv.column(c, width=widths[c], anchor=anchor, stretch=(c == "metric"))
        tv.tag_configure("better", foreground="#4ad991")
        tv.tag_configure("worse", foreground="#ff5252")
        tv.tag_configure("noisy", foreground="#888")
        tv.pack(fill="both", expand=True, padx=4, pady=6)

        def fill(rows):
            for key, name, lower_is_better, fmt in rows:
                va = metrics_a.get(key, [])
                vb = metrics_b.get(key, [])
                mean_a, p95_a = self._stat(va)
                mean_b, p95_b = self._stat(vb)
                if mean_a is None and mean_b is None:
                    continue
                if mean_a is None or mean_b is None or mean_a == 0:
                    delta_str = "—"
                    tag = "noisy"
                else:
                    delta_pct = (mean_b - mean_a) / abs(mean_a) * 100.0
                    delta_str = f"{delta_pct:+.1f}%"
                    if abs(delta_pct) < 5:
                        tag = "noisy"
                    else:
                        improved = (delta_pct < 0) if lower_is_better else (delta_pct > 0)
                        tag = "better" if improved else "worse"
                tv.insert("", "end",
                          values=(name,
                                  fmt(mean_a) if mean_a is not None else "—",
                                  fmt(p95_a) if p95_a is not None else "—",
                                  fmt(mean_b) if mean_b is not None else "—",
                                  fmt(p95_b) if p95_b is not None else "—",
                                  delta_str,
                                  f"{len(va)}/{len(vb)}"),
                          tags=(tag,))

        fill(self._COMPARE_METRICS)
        tv.insert("", "end", values=("— LLM —", "", "", "", "", "", ""), tags=("noisy",))
        fill(self._COMPARE_LLM)

        # ----- Tab 2: overlay chart -----
        tab_ov = tk.Frame(nb); nb.add(tab_ov, text="Overlay chart")
        ov_bar = tk.Frame(tab_ov); ov_bar.pack(fill="x", padx=4, pady=4)
        tk.Label(ov_bar, text="Metric:").pack(side="left")
        metric_keys = [k for k, _, _, _ in self._COMPARE_METRICS] + \
                      [k for k, _, _, _ in self._COMPARE_LLM]
        metric_var = tk.StringVar(value=metric_keys[0])
        cb = ttk.Combobox(ov_bar, textvariable=metric_var, values=metric_keys,
                          state="readonly", width=16)
        cb.pack(side="left", padx=4)
        tk.Label(ov_bar, text="(A=red, B=green — normalized 0..max of either series)",
                 fg="#888", font=("Consolas", 9)).pack(side="left", padx=8)

        ov_canvas = tk.Canvas(tab_ov, bg="#0e0e0e", highlightthickness=0)
        ov_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        def draw_overlay():
            key = metric_var.get()
            va = metrics_a.get(key, [])
            vb = metrics_b.get(key, [])
            c = ov_canvas
            c.delete("all")
            w = c.winfo_width(); h = c.winfo_height()
            if w < 10 or h < 10:
                return
            if not va and not vb:
                c.create_text(w // 2, h // 2, fill="#666",
                              text=f"no data for '{key}' in either session",
                              font=("Segoe UI", 11))
                return
            ml, mr, mt, mb = 50, 12, 12, 24
            pw = w - ml - mr; ph = h - mt - mb
            ymax = max([0.0] + [v for v in va] + [v for v in vb])
            if ymax <= 0:
                ymax = 1.0
            for pct in (25, 50, 75):
                y = mt + ph - ph * pct / 100
                c.create_line(ml, y, w - mr, y, fill="#1c1c1c")
                lbl = f"{ymax * pct / 100:.1f}"
                c.create_text(ml - 4, y, anchor="e", fill="#555",
                              font=("Consolas", 8), text=lbl)
            c.create_text(ml, mt - 2, anchor="sw", fill="#888",
                          font=("Consolas", 9), text=f"{key}  (max {ymax:.2f})")

            def plot(vs, color):
                if not vs:
                    return
                n = len(vs)
                step = pw / max(1, n - 1) if n > 1 else 0
                pts = []
                for i, v in enumerate(vs):
                    x = ml + i * step
                    y = mt + ph - (v / ymax) * ph
                    pts.append((x, y))
                if len(pts) >= 2:
                    flat = [cc for p in pts for cc in p]
                    c.create_line(*flat, fill=color, width=2)
                elif len(pts) == 1:
                    c.create_oval(pts[0][0] - 3, pts[0][1] - 3,
                                  pts[0][0] + 3, pts[0][1] + 3,
                                  fill=color, outline=color)

            plot(va, "#ff5252")
            plot(vb, "#4ad991")
            # Legend at top-right
            c.create_text(w - mr, mt, anchor="ne", fill="#ff5252",
                          font=("Consolas", 9, "bold"),
                          text=f"A  n={len(va)}")
            c.create_text(w - mr, mt + 14, anchor="ne", fill="#4ad991",
                          font=("Consolas", 9, "bold"),
                          text=f"B  n={len(vb)}")

        cb.bind("<<ComboboxSelected>>", lambda _e: draw_overlay())
        ov_canvas.bind("<Configure>", lambda _e: draw_overlay())
        win.after(80, draw_overlay)

        btns = tk.Frame(win); btns.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(btns, text="Close", command=win.destroy).pack(side="right")

    # ============================================================ 3D view tree (Layout Inspector-style)
    @staticmethod
    def _parse_uia_xml(xml_text):
        """Flatten uiautomator XML into nodes with parent/children + extra attrs."""
        nodes = []

        def walk(elem, depth, parent_idx):
            my_idx = -1
            bounds = elem.get("bounds")
            if bounds:
                m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if x2 > x1 and y2 > y1:
                        my_idx = len(nodes)
                        nodes.append({
                            "idx": my_idx,
                            "parent": parent_idx,
                            "children": [],
                            "depth": depth,
                            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                            "class": elem.get("class", ""),
                            "text": elem.get("text", ""),
                            "resource-id": elem.get("resource-id", ""),
                            "package": elem.get("package", ""),
                            "content-desc": elem.get("content-desc", ""),
                            "clickable": elem.get("clickable", ""),
                            "focused": elem.get("focused", ""),
                            "enabled": elem.get("enabled", ""),
                            "scrollable": elem.get("scrollable", ""),
                            "checkable": elem.get("checkable", ""),
                            "checked": elem.get("checked", ""),
                            "long-clickable": elem.get("long-clickable", ""),
                            "password": elem.get("password", ""),
                            "selected": elem.get("selected", ""),
                            "visible": True,
                        })
                        if parent_idx >= 0:
                            nodes[parent_idx]["children"].append(my_idx)
            next_parent = my_idx if my_idx >= 0 else parent_idx
            for child in list(elem):
                walk(child, depth + 1, next_parent)

        walk(ET.fromstring(xml_text), 0, -1)
        return nodes

    def _open_3d_view(self):
        """Capture uiautomator dump → flatten → render as a rotatable 3D stack."""
        win = tk.Toplevel(self.root)
        win.title("3D view tree — capturing…")
        win.geometry("960x720")
        info = tk.Label(win, text="Capturing uiautomator dump…", fg="orange",
                        font=("Consolas", 10))
        info.pack(fill="x", padx=8, pady=6)

        def worker():
            ts = time.strftime("%Y%m%d_%H%M%S")
            if self.session_dir:
                out_dir = os.path.join(self.session_dir, "uia")
            else:
                out_dir = os.path.join(HERE, "artifacts", "_uia")
            remote = "/sdcard/_uia_3d.xml"
            local = None
            try:
                os.makedirs(out_dir, exist_ok=True)
                adb("shell", "uiautomator", "dump", remote, timeout=15)
                local = os.path.join(out_dir, f"uia_{ts}_3d.xml")
                adb("pull", remote, local, timeout=15)
                adb("shell", "rm", "-f", remote, timeout=5)
                with open(local, encoding="utf-8") as fp:
                    xml_text = fp.read()
                nodes = self._parse_uia_xml(xml_text)
            except Exception as e:
                self.root.after(0, lambda er=e: (
                    info.config(text=f"capture failed: {er}", fg="red")))
                return
            if not nodes:
                self.root.after(0, lambda: info.config(
                    text="no nodes parsed from dump", fg="red"))
                return
            self.root.after(0, lambda: self._render_3d(win, info, nodes, local))

        threading.Thread(target=worker, daemon=True).start()

    def _render_3d(self, win, info, nodes, source_path):
        """Build the 3D canvas + tree/properties side panel."""
        win.title(f"3D view tree — {len(nodes)} nodes")
        info.destroy()

        max_x = max(n["x2"] for n in nodes)
        max_y = max(n["y2"] for n in nodes)
        max_depth = max(n["depth"] for n in nodes) or 1
        DEPTH_SPACING = 40.0
        model_cx = max_x / 2.0
        model_cy = max_y / 2.0
        palette = [
            "#4ec9ff", "#b48cff", "#4ad991", "#ffd34a",
            "#ff9f43", "#ff6bd6", "#5b8bff", "#ff5252",
        ]

        view = {"yaw": -0.5, "pitch": 0.55, "scale": 0.5,
                "tx": 0.0, "ty": 0.0,
                "drag_last": None, "press_pos": None,
                "selected": None, "hover": None}

        # ----- Layout: paned [canvas | side panel] -----
        paned = ttk.PanedWindow(win, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = tk.Frame(paned, bg="#0e0e0e")
        right = tk.Frame(paned, bg="#181818", width=360)
        right.pack_propagate(False)
        paned.add(left, weight=3)
        paned.add(right, weight=1)

        # Top toolbar
        bar = tk.Frame(left, bg="#0e0e0e"); bar.pack(fill="x", padx=6, pady=4)
        tk.Label(bar, bg="#0e0e0e", fg="#888",
                 text="drag: rotate · wheel: zoom · click: select · dbl-click: reset",
                 font=("Consolas", 9)).pack(side="left", padx=4)
        hover_lbl = tk.Label(bar, bg="#0e0e0e", fg="#4ec9ff",
                             font=("Consolas", 9), anchor="e")
        hover_lbl.pack(side="right", padx=4)
        tk.Button(bar, text="Snap", command=lambda: show_screenshot()
                  ).pack(side="right", padx=4)

        canvas = tk.Canvas(left, bg="#0e0e0e", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        status = tk.Label(left, bg="#0e0e0e", fg="#666",
                          text=f"source: {source_path}    "
                               f"screen: {max_x}×{max_y}  depth: {max_depth}",
                          font=("Consolas", 9), anchor="w")
        status.pack(fill="x", padx=8, pady=(0, 6))

        # ----- Right side panel -----
        tk.Label(right, bg="#181818", fg="#4ec9ff",
                 text="View hierarchy",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 2))

        # Search bar (class / resource-id / text / content-desc partial match)
        search_frame = tk.Frame(right, bg="#181818")
        search_frame.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(search_frame, bg="#181818", fg="#888",
                 text="Find:", font=("Consolas", 9)).pack(side="left")
        search_var = tk.StringVar()
        search_entry = tk.Entry(search_frame, textvariable=search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=4)
        search_state = {"results": [], "i": 0}

        def do_search(advance=False):
            q = search_var.get().strip().lower()
            if not q:
                return
            matches = []
            for n in nodes:
                hay = " ".join(str(n[k] or "") for k in
                               ("class", "resource-id", "text", "content-desc")).lower()
                if q in hay:
                    matches.append(n["idx"])
            if not matches:
                hover_lbl.config(text=f"no match for '{q}'")
                return
            if advance and matches == search_state["results"]:
                search_state["i"] = (search_state["i"] + 1) % len(matches)
            else:
                search_state["results"] = matches
                search_state["i"] = 0
            select_node(matches[search_state["i"]])
            hover_lbl.config(text=f"match {search_state['i']+1}/{len(matches)}")
        search_entry.bind("<Return>", lambda _e: do_search(advance=True))
        tk.Button(search_frame, text="Next",
                  command=lambda: do_search(advance=True)).pack(side="left")

        tree_frame = tk.Frame(right, bg="#181818")
        tree_frame.pack(fill="both", expand=True, padx=4)
        tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse", height=14)
        tree_sb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        tree.tag_configure("hidden", foreground="#555")

        # Build tree
        for n in nodes:
            cls = n["class"].rsplit(".", 1)[-1] or "?"
            rid = n["resource-id"].rsplit("/", 1)[-1]
            text = (n["text"] or "").strip()
            label = f"{cls}"
            if rid:
                label += f" #{rid}"
            if text:
                label += f"  \"{text[:24]}\""
            parent_iid = "" if n["parent"] < 0 else str(n["parent"])
            tree.insert(parent_iid, "end", iid=str(n["idx"]), text=label, open=True)

        # Action buttons
        btns = tk.Frame(right, bg="#181818")
        btns.pack(fill="x", padx=8, pady=4)
        visible_var = tk.BooleanVar(value=True)
        vis_chk = tk.Checkbutton(btns, text="Visible", variable=visible_var,
                                 bg="#181818", fg="#ddd", selectcolor="#181818",
                                 activebackground="#181818",
                                 command=lambda: toggle_visible(visible_var.get()))
        vis_chk.pack(side="left")
        tk.Button(btns, text="Hide",
                  command=lambda: set_visible_selected(False)).pack(side="left", padx=2)
        tk.Button(btns, text="Solo",
                  command=lambda: solo_selected()).pack(side="left", padx=2)
        tk.Button(btns, text="Show all",
                  command=lambda: show_all()).pack(side="left", padx=2)

        # Navigation row
        nav = tk.Frame(right, bg="#181818")
        nav.pack(fill="x", padx=8, pady=2)
        tk.Label(nav, bg="#181818", fg="#888", text="Nav:",
                 font=("Consolas", 9)).pack(side="left")

        def nav_to(direction):
            sel = view["selected"]
            if sel is None:
                return
            n = nodes[sel]
            if direction == "parent" and n["parent"] >= 0:
                select_node(n["parent"])
            elif direction == "child" and n["children"]:
                select_node(n["children"][0])
            elif direction in ("prev", "next") and n["parent"] >= 0:
                sibs = nodes[n["parent"]]["children"]
                try:
                    i = sibs.index(sel)
                    if direction == "prev" and i > 0:
                        select_node(sibs[i - 1])
                    elif direction == "next" and i < len(sibs) - 1:
                        select_node(sibs[i + 1])
                except ValueError:
                    pass

        tk.Button(nav, text="↑ parent", command=lambda: nav_to("parent")).pack(side="left", padx=2)
        tk.Button(nav, text="↓ child",  command=lambda: nav_to("child")).pack(side="left", padx=2)
        tk.Button(nav, text="← prev",   command=lambda: nav_to("prev")).pack(side="left", padx=2)
        tk.Button(nav, text="next →",   command=lambda: nav_to("next")).pack(side="left", padx=2)

        # Measure mode — when toggled, the next two selections become A/B and
        # the diff (center distance + bounding-box overlap) is shown.
        measure_var = tk.BooleanVar(value=False)
        measure_state = {"first": None}
        meas_row = tk.Frame(right, bg="#181818")
        meas_row.pack(fill="x", padx=8, pady=2)
        tk.Checkbutton(meas_row, text="Measure (pick A then B)",
                       variable=measure_var,
                       bg="#181818", fg="#ddd", selectcolor="#181818",
                       activebackground="#181818",
                       command=lambda: on_measure_toggle()).pack(side="left")
        measure_lbl = tk.Label(right, bg="#181818", fg="#888",
                               text="", font=("Consolas", 9),
                               justify="left", anchor="w")
        measure_lbl.pack(fill="x", padx=8, pady=(0, 4))

        def on_measure_toggle():
            measure_state["first"] = None
            if measure_var.get():
                measure_lbl.config(text="Measure: click first node (A)…", fg="#ffd34a")
            else:
                measure_lbl.config(text="", fg="#888")

        # Properties area
        tk.Label(right, bg="#181818", fg="#4ec9ff",
                 text="Properties",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 2))
        props_txt = scrolledtext.ScrolledText(right, height=14, font=("Consolas", 9),
                                              bg="#1a1a1a", fg="#ddd", insertbackground="#ddd")
        props_txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        props_txt.configure(state="disabled")

        # ----- Projection / hit-test -----
        def project(x, y, z):
            x -= model_cx
            y -= model_cy
            cy_, sy_ = math.cos(view["yaw"]), math.sin(view["yaw"])
            pc, ps = math.cos(view["pitch"]), math.sin(view["pitch"])
            x1 = x * cy_ - z * sy_
            z1 = x * sy_ + z * cy_
            y1 = y * pc - z1 * ps
            z2 = y * ps + z1 * pc
            sx = x1 * view["scale"]
            sy = y1 * view["scale"]
            return sx, sy, z2

        def hit_test(ex, ey):
            w = canvas.winfo_width(); h = canvas.winfo_height()
            cx0 = w / 2; cy0 = h / 2
            best = None; best_z = -1e18
            for n in nodes:
                if not n["visible"]:
                    continue
                z = -n["depth"] * DEPTH_SPACING
                xs = []; ys = []; z_sum = 0
                for (x, y, zz) in [
                    (n["x1"], n["y1"], z), (n["x2"], n["y1"], z),
                    (n["x2"], n["y2"], z), (n["x1"], n["y2"], z),
                ]:
                    px, py, pz = project(x, y, zz)
                    xs.append(px + cx0); ys.append(py + cy0)
                    z_sum += pz
                if min(xs) <= ex <= max(xs) and min(ys) <= ey <= max(ys):
                    if z_sum > best_z:
                        best_z = z_sum
                        best = n["idx"]
            return best

        def redraw():
            canvas.delete("all")
            w = canvas.winfo_width(); h = canvas.winfo_height()
            if w < 10 or h < 10:
                return
            cx = w / 2; cy = h / 2
            sel = view["selected"]
            rendered = []
            for n in nodes:
                if not n["visible"]:
                    continue
                z = -n["depth"] * DEPTH_SPACING
                proj = []; z_sum = 0.0
                for (x, y, zz) in [
                    (n["x1"], n["y1"], z), (n["x2"], n["y1"], z),
                    (n["x2"], n["y2"], z), (n["x1"], n["y2"], z),
                ]:
                    px, py, pz = project(x, y, zz)
                    proj.append((px + cx, py + cy))
                    z_sum += pz
                rendered.append((z_sum / 4.0, n, proj))
            rendered.sort(key=lambda r: r[0])
            for _, n, pts in rendered:
                color = palette[n["depth"] % len(palette)]
                flat = [c for p in pts for c in p]
                if n["idx"] == sel:
                    canvas.create_polygon(*flat, fill=color, stipple="gray25",
                                          outline="#ffffff", width=3)
                else:
                    canvas.create_polygon(*flat, fill=color, stipple="gray12",
                                          outline=color, width=1)
            # Axis gizmo
            ax_x, ax_y = w - 60, 60
            for axis, (dx, dy, dz), c in [
                ("x", (60, 0, 0), "#ff5252"),
                ("y", (0, 60, 0), "#4ad991"),
                ("z", (0, 0, -60), "#5b8bff"),
            ]:
                px, py, _ = project(model_cx + dx, model_cy + dy, dz)
                canvas.create_line(ax_x, ax_y,
                                   ax_x + px * 0.6, ax_y + py * 0.6,
                                   fill=c, width=2, arrow="last")
                canvas.create_text(ax_x + px * 0.7, ax_y + py * 0.7,
                                   text=axis, fill=c, font=("Consolas", 9, "bold"))

        # ----- Selection plumbing -----
        def fill_props(n):
            props_txt.configure(state="normal")
            props_txt.delete("1.0", "end")
            if n is None:
                props_txt.insert("1.0", "(no selection)\n\nClick a node in the canvas "
                                 "or pick one from the tree above.")
            else:
                lines = [
                    f"idx           {n['idx']}",
                    f"depth         {n['depth']}",
                    f"parent        {n['parent']}",
                    f"children      {len(n['children'])}",
                    f"class         {n['class']}",
                    f"resource-id   {n['resource-id']}",
                    f"package       {n['package']}",
                    f"text          {n['text']}",
                    f"content-desc  {n['content-desc']}",
                    f"bounds        [{n['x1']},{n['y1']}][{n['x2']},{n['y2']}]"
                    f"  ({n['x2']-n['x1']}×{n['y2']-n['y1']})",
                    f"clickable     {n['clickable']}",
                    f"long-clk      {n['long-clickable']}",
                    f"enabled       {n['enabled']}",
                    f"focused       {n['focused']}",
                    f"scrollable    {n['scrollable']}",
                    f"checkable     {n['checkable']}",
                    f"checked       {n['checked']}",
                    f"selected      {n['selected']}",
                    f"password      {n['password']}",
                    f"visible       {n['visible']}",
                ]
                props_txt.insert("1.0", "\n".join(lines))
            props_txt.configure(state="disabled")

        def select_node(idx, from_tree=False):
            # Measure mode: capture A on first pick, then show diff on each subsequent pick.
            if measure_var.get() and idx is not None:
                first = measure_state["first"]
                if first is None:
                    measure_state["first"] = idx
                    measure_lbl.config(
                        text=f"Measure: A = node {idx}.  Click second node (B)…",
                        fg="#ffd34a")
                elif idx != first:
                    a = nodes[first]; b = nodes[idx]
                    aw, ah = a["x2"] - a["x1"], a["y2"] - a["y1"]
                    bw, bh = b["x2"] - b["x1"], b["y2"] - b["y1"]
                    cax = (a["x1"] + a["x2"]) / 2; cay = (a["y1"] + a["y2"]) / 2
                    cbx = (b["x1"] + b["x2"]) / 2; cby = (b["y1"] + b["y2"]) / 2
                    dx, dy = cbx - cax, cby - cay
                    dist = (dx * dx + dy * dy) ** 0.5
                    ox = max(0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
                    oy = max(0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
                    measure_lbl.config(
                        text=(f"A {first} ({aw}×{ah})  B {idx} ({bw}×{bh})\n"
                              f"  center Δ=({dx:+.0f},{dy:+.0f})  dist={dist:.0f}px\n"
                              f"  bbox overlap={ox}×{oy} = {ox*oy}px²"),
                        fg="#4ec9ff")
                    measure_state["first"] = idx  # chain: next click sets new B

            view["selected"] = idx
            if idx is None:
                fill_props(None)
                visible_var.set(True)
                redraw()
                return
            n = nodes[idx]
            fill_props(n)
            visible_var.set(bool(n["visible"]))
            if not from_tree:
                try:
                    tree.selection_set(str(idx))
                    tree.see(str(idx))
                except tk.TclError:
                    pass
            redraw()

        # Snap current phone screen — opens a thumbnail next to the 3D view so
        # the user can correlate the rendered hierarchy with what's on screen.
        def show_screenshot():
            def worker():
                png = send_cmd("SHOT", return_bytes=True, timeout=10)
                if not png or (isinstance(png, bytes) and png.startswith(b"ERR")):
                    self.root.after(0, lambda: messagebox.showerror(
                        "screenshot", "no PNG returned (daemon down?)"))
                    return
                try:
                    img = Image.open(io.BytesIO(png))
                except Exception as e:
                    self.root.after(0, lambda er=e: messagebox.showerror(
                        "screenshot", f"decode: {er}"))
                    return

                def show():
                    sw = tk.Toplevel(win)
                    sw.title("Phone screenshot (snap)")
                    img.thumbnail((520, 1080))
                    tkimg = ImageTk.PhotoImage(img)
                    lab = tk.Label(sw, image=tkimg, bg="#000")
                    lab.image = tkimg
                    lab.pack()
                self.root.after(0, show)
            threading.Thread(target=worker, daemon=True).start()

        def on_tree_select(_e):
            sel = tree.selection()
            if sel:
                try:
                    select_node(int(sel[0]), from_tree=True)
                except ValueError:
                    pass

        tree.bind("<<TreeviewSelect>>", on_tree_select)

        def refresh_tree_styles():
            for n in nodes:
                tree.item(str(n["idx"]),
                          tags=("hidden",) if not n["visible"] else ())

        def toggle_visible(want):
            sel = view["selected"]
            if sel is None:
                return
            nodes[sel]["visible"] = bool(want)
            refresh_tree_styles()
            redraw()

        def set_visible_selected(want):
            sel = view["selected"]
            if sel is None:
                return
            nodes[sel]["visible"] = want
            visible_var.set(want)
            refresh_tree_styles()
            redraw()

        def solo_selected():
            sel = view["selected"]
            if sel is None:
                return
            keep = set()
            cur = sel
            while cur >= 0:
                keep.add(cur)
                cur = nodes[cur]["parent"]
            # also keep descendants
            stack = [sel]
            while stack:
                cur = stack.pop()
                for ch in nodes[cur]["children"]:
                    keep.add(ch)
                    stack.append(ch)
            for n in nodes:
                n["visible"] = (n["idx"] in keep)
            visible_var.set(True)
            refresh_tree_styles()
            redraw()

        def show_all():
            for n in nodes:
                n["visible"] = True
            if view["selected"] is not None:
                visible_var.set(True)
            refresh_tree_styles()
            redraw()

        # ----- Mouse handlers -----
        def on_press(e):
            view["drag_last"] = (e.x, e.y)
            view["press_pos"] = (e.x, e.y)

        def on_drag(e):
            last = view["drag_last"]
            if last is None:
                return
            dx, dy = e.x - last[0], e.y - last[1]
            if view["press_pos"]:
                pdx = e.x - view["press_pos"][0]
                pdy = e.y - view["press_pos"][1]
                if pdx * pdx + pdy * pdy > 16:
                    view["press_pos"] = None  # commits to drag, not click
            view["yaw"] += dx * 0.01
            view["pitch"] += dy * 0.01
            view["pitch"] = max(-1.5, min(1.5, view["pitch"]))
            view["drag_last"] = (e.x, e.y)
            redraw()

        def on_release(e):
            view["drag_last"] = None
            if view["press_pos"] is not None:
                # treat as click
                hit = hit_test(e.x, e.y)
                select_node(hit)
            view["press_pos"] = None

        def on_wheel(e):
            factor = 1.1 if e.delta > 0 else (1 / 1.1)
            view["scale"] *= factor
            view["scale"] = max(0.05, min(5.0, view["scale"]))
            redraw()

        def on_reset(_e):
            view.update({"yaw": -0.5, "pitch": 0.55,
                         "tx": 0.0, "ty": 0.0})
            # rescale to fit
            autofit()

        def on_motion(e):
            idx = hit_test(e.x, e.y)
            if idx is None:
                hover_lbl.config(text="")
                return
            n = nodes[idx]
            cls = n["class"].rsplit(".", 1)[-1] or "?"
            rid = n["resource-id"].rsplit("/", 1)[-1]
            t = (n["text"] or "")[:30]
            txt = f"d{n['depth']} {cls}"
            if rid:
                txt += f" #{rid}"
            if t:
                txt += f' "{t}"'
            hover_lbl.config(text=txt)

        canvas.bind("<Configure>", lambda e: redraw())
        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<Double-Button-1>", on_reset)
        canvas.bind("<Motion>", on_motion)

        def autofit():
            w = canvas.winfo_width() or 900
            target = w * 0.55
            if max_x > 0:
                view["scale"] = target / max_x
            redraw()

        fill_props(None)
        canvas.after(50, autofit)

    # ============================================================ per-UID network I/O
    @staticmethod
    def _resolve_uid_for_pid(pid):
        """Read /proc/<pid>/status → Uid: line. Returns int or None."""
        raw = send_cmd(f"SH cat /proc/{pid}/status 2>/dev/null", timeout=4)
        if not raw or raw.startswith("ERR"):
            return None
        for line in raw.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1])
                    except ValueError:
                        return None
        return None

    @staticmethod
    def _parse_qtaguid(raw, my_uid):
        """Parse /proc/net/xt_qtaguid/stats — older Android per-uid counters.
        Returns {iface: (rx_bytes, tx_bytes)} summed over cnt_set 0/1."""
        out = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 9 or parts[0] == "idx":
                continue
            try:
                iface = parts[1]
                tag = parts[2]
                uid = int(parts[3])
                rx = int(parts[5])
                tx = int(parts[7])
            except (ValueError, IndexError):
                continue
            if uid != my_uid:
                continue
            # tag=0x0 only — matches TrafficStats.getUidRxBytes semantics
            # (untagged traffic). Both cnt_set values (0=bg, 1=fg) get summed.
            if tag != "0x0":
                continue
            r, t = out.get(iface, (0, 0))
            out[iface] = (r + rx, t + tx)
        return out

    # Android transport ids → human label (see android.net.NetworkCapabilities)
    _TRANSPORT_LABEL = {
        0: "cellular", 1: "wifi", 2: "bluetooth", 3: "ethernet",
        4: "vpn", 5: "wifi-aware", 6: "lowpan", 7: "test", 8: "usb",
        9: "thread", 10: "satellite",
    }

    @staticmethod
    def _parse_dumpsys_netstats(raw, my_uid):
        """Parse `dumpsys netstats detail`. Two row shapes appear:

        (a) Single-line dev/xt rows with rxBytes=/txBytes=:
            [N] iface=wlan0 uid=-1 set=DEFAULT tag=0x0 ... rxBytes=N txBytes=N
        (b) Multi-line uid history blocks:
            ident=[{...transports={1}}] uid=10237 set=FOREGROUND tag=0x0
                st=1778716800 rb=685289 rp=1829 tb=1684424 tp=2100 op=0
                st=...
        For (b) we map transports={N} → a transport label since the rows
        don't carry an interface name.

        Returns {interface_label: (cumulative_rx, cumulative_tx)}.
        """
        out = {}
        cur_iface = None
        cur_uid = None
        cur_tag = "0x0"
        cur_match = False

        def label_from_transports(s):
            m = re.search(r"transports=\{([^}]*)\}", s)
            if not m:
                return None
            nums = []
            for tok in m.group(1).split(","):
                tok = tok.strip()
                if tok.isdigit():
                    nums.append(int(tok))
            if not nums:
                return None
            # Prefer the "primary" transport — wifi/cellular/eth over vpn.
            for pref in (1, 0, 3, 4, 8):
                if pref in nums:
                    return PhoneController._TRANSPORT_LABEL.get(pref, f"t{pref}")
            return PhoneController._TRANSPORT_LABEL.get(nums[0], f"t{nums[0]}")

        for raw_line in raw.splitlines():
            s = raw_line.strip()
            # Block (b) header: starts with ident=
            if s.startswith("ident="):
                ifa = label_from_transports(s) or "?"
                um = re.search(r"\buid=(-?\d+)", s)
                tm = re.search(r"\btag=(0x[\dA-Fa-f]+)", s)
                cur_iface = ifa
                cur_uid = int(um.group(1)) if um else None
                cur_tag = tm.group(1) if tm else "0x0"
                cur_match = (cur_uid == my_uid and cur_tag.lower() == "0x0")
                continue
            # Block (a) single-line dev row
            if "iface=" in s and ("rxBytes=" in s or "txBytes=" in s):
                ifm = re.search(r"iface=([\w.:-]+)", s)
                um = re.search(r"\buid=(-?\d+)", s)
                tm = re.search(r"\btag=(0x[\dA-Fa-f]+)", s)
                if ifm and um:
                    line_iface = ifm.group(1)
                    line_uid = int(um.group(1))
                    line_tag = tm.group(1) if tm else "0x0"
                    if line_uid == my_uid and line_tag.lower() == "0x0":
                        rxm = re.search(r"rxBytes=(\d+)", s)
                        txm = re.search(r"txBytes=(\d+)", s)
                        rxv = int(rxm.group(1)) if rxm else 0
                        txv = int(txm.group(1)) if txm else 0
                        r, t = out.get(line_iface, (0, 0))
                        out[line_iface] = (r + rxv, t + txv)
                continue
            # Block (b) data rows: accumulate while we're inside our uid context
            if cur_match and (" rb=" in (" " + s) or " tb=" in (" " + s)):
                rbm = re.search(r"\brb=(\d+)", s)
                tbm = re.search(r"\btb=(\d+)", s)
                rxv = int(rbm.group(1)) if rbm else 0
                txv = int(tbm.group(1)) if tbm else 0
                key = cur_iface or "?"
                r, t = out.get(key, (0, 0))
                out[key] = (r + rxv, t + txv)
        return out

    def _open_netio(self, pid, pkg):
        """Per-UID rx/tx via xt_qtaguid (preferred) or dumpsys netstats (fallback)."""
        win = tk.Toplevel(self.root)
        win.title(f"Network I/O — pid {pid} · {pkg}  (resolving uid…)")
        win.geometry("860x600")

        bar = tk.Frame(win); bar.pack(fill="x", padx=6, pady=4)
        paused = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause", variable=paused).pack(side="left")
        iface_var = tk.StringVar(value="(all)")
        tk.Label(bar, text="Iface:").pack(side="left", padx=(8, 2))
        iface_cb = ttk.Combobox(bar, textvariable=iface_var,
                                values=["(all)"], width=18, state="readonly")
        iface_cb.pack(side="left")
        info = tk.Label(bar, text="resolving uid…", fg="orange",
                        font=("Consolas", 9))
        info.pack(side="left", padx=8)
        src_lbl = tk.Label(bar, text="", fg="#888", font=("Consolas", 9))
        src_lbl.pack(side="right", padx=4)

        tk.Label(win, text="rx KB/s (cyan) · tx KB/s (orange) — per-UID counters",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(8, 0))
        chart = tk.Canvas(win, height=260, bg="#0e0e0e", highlightthickness=0)
        chart.pack(fill="both", expand=True, padx=8)

        cur = tk.Label(win, text="—", font=("Consolas", 10),
                       fg="#ddd", anchor="w", justify="left")
        cur.pack(fill="x", padx=8, pady=4)

        history = collections.deque(maxlen=300)
        per_iface_history = collections.defaultdict(
            lambda: collections.deque(maxlen=300))
        ifaces_seen = set()
        prev = {"snap": None, "ts": None}
        stop = threading.Event()
        state = {"uid": None, "source": None, "interval": 2.0}

        def fetch():
            uid = state["uid"]
            if uid is None:
                return None
            # Try qtaguid first (1s polling works, accurate per-uid)
            raw = send_cmd("SH cat /proc/net/xt_qtaguid/stats 2>/dev/null",
                           timeout=4)
            if raw and not raw.startswith("ERR") and "idx" in raw[:120]:
                state["source"] = "xt_qtaguid"
                state["interval"] = 1.0
                return self._parse_qtaguid(raw, uid)
            # Fallback: dumpsys netstats detail (cumulative; poll less often)
            raw2 = send_cmd("SH dumpsys netstats detail 2>/dev/null", timeout=10)
            if raw2 and not raw2.startswith("ERR") and len(raw2) > 200:
                state["source"] = "dumpsys netstats"
                state["interval"] = 5.0
                return self._parse_dumpsys_netstats(raw2, uid)
            state["source"] = None
            return None

        def redraw():
            chart.delete("all")
            w = chart.winfo_width(); h = chart.winfo_height()
            if w < 10 or h < 10 or not history:
                return
            ml, mr, mt, mb = 50, 12, 10, 22
            pw = w - ml - mr; ph = h - mt - mb
            sel = iface_var.get()
            if sel == "(all)":
                series = list(history)
            else:
                series = list(per_iface_history.get(sel, []))
            if not series:
                return
            ymax = max([1.0] + [max(rx, tx) for _, rx, tx in series])
            for pct in (25, 50, 75):
                y = mt + ph - ph * pct / 100
                chart.create_line(ml, y, w - mr, y, fill="#1c1c1c")
                chart.create_text(ml - 4, y, anchor="e", fill="#555",
                                  font=("Consolas", 8),
                                  text=f"{ymax * pct / 100:.0f}")
            n = len(series)
            step = pw / max(1, n - 1) if n > 1 else 0
            rx_pts = []; tx_pts = []
            for i, (_t, rx, tx) in enumerate(series):
                x = ml + i * step
                rx_pts.append((x, mt + ph - (rx / ymax) * ph))
                tx_pts.append((x, mt + ph - (tx / ymax) * ph))
            if len(rx_pts) >= 2:
                chart.create_line(*[c for p in rx_pts for c in p],
                                  fill="#4ec9ff", width=2)
                chart.create_line(*[c for p in tx_pts for c in p],
                                  fill="#ff9f43", width=2)
            chart.create_text(ml + 4, mt + 2, anchor="nw", fill="#888",
                              font=("Consolas", 8),
                              text=f"max ≈ {ymax:.0f} KB/s")

        def loop():
            # Resolve uid first (one-shot)
            uid = self._resolve_uid_for_pid(pid)
            if uid is None:
                self.root.after(0, lambda: info.config(
                    text="failed to resolve UID from /proc/<pid>/status",
                    fg="red"))
                return
            state["uid"] = uid
            self.root.after(0, lambda: win.title(
                f"Network I/O — uid {uid}  pid {pid} · {pkg}"))

            while not stop.is_set():
                if paused.get():
                    time.sleep(0.5); continue
                snap = fetch()
                ts = time.time()
                if snap is None:
                    self.root.after(0, lambda: info.config(
                        text="no per-UID source available (qtaguid removed, "
                             "dumpsys parse failed)", fg="red"))
                    time.sleep(3); continue
                if not snap:
                    # uid had no entries yet (no traffic)
                    self.root.after(0, lambda s=state["source"]: (
                        info.config(text=f"src={s} · no traffic yet for uid",
                                    fg="#888"),
                        src_lbl.config(text=f"src: {s}")))
                else:
                    if prev["snap"] is not None and prev["ts"] is not None:
                        dt = ts - prev["ts"]
                        if dt > 0:
                            total_rx = total_tx = 0
                            per = {}
                            for name, (rx_b, tx_b) in snap.items():
                                p_rx, p_tx = prev["snap"].get(name, (rx_b, tx_b))
                                d_rx = max(0, rx_b - p_rx) / dt / 1024.0
                                d_tx = max(0, tx_b - p_tx) / dt / 1024.0
                                per[name] = (d_rx, d_tx)
                                total_rx += d_rx
                                total_tx += d_tx
                            history.append((ts, total_rx, total_tx))
                            for name, (rx, tx) in per.items():
                                per_iface_history[name].append((ts, rx, tx))
                                if name not in ifaces_seen:
                                    ifaces_seen.add(name)
                                    new_vals = ["(all)"] + sorted(ifaces_seen)
                                    self.root.after(
                                        0, lambda v=new_vals:
                                        iface_cb.configure(values=v))
                            sel = iface_var.get()
                            if sel == "(all)":
                                rx_now, tx_now = total_rx, total_tx
                            else:
                                rx_now, tx_now = per.get(sel, (0, 0))
                            msg = (f"rx={rx_now:.2f} KB/s  "
                                   f"tx={tx_now:.2f} KB/s   "
                                   f"ifaces={len(snap)}")
                            self.root.after(0, lambda m=msg,
                                                   s=state["source"],
                                                   iv=state["interval"]:
                                            (cur.config(text=m),
                                             info.config(
                                                 text=f"sampling every {iv:.0f}s",
                                                 fg="#888"),
                                             src_lbl.config(text=f"src: {s}"),
                                             redraw()))
                    prev["snap"] = snap
                    prev["ts"] = ts
                time.sleep(state["interval"])

        chart.bind("<Configure>", lambda _e: redraw())
        iface_cb.bind("<<ComboboxSelected>>", lambda _e: redraw())
        threading.Thread(target=loop, daemon=True).start()

        def close():
            stop.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ logcat live
    _LOGCAT_RE = re.compile(
        r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+([VDIWEF])\s+([^:]+):\s*(.*)$"
    )
    _LEVEL_ORDER = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5}

    def _open_logcat(self, pid=None, pkg=None):
        win = tk.Toplevel(self.root)
        if pid:
            win.title(f"Logcat — pid {pid} · {pkg or ''}")
        else:
            win.title("Logcat — live")
        win.geometry("1100x700")

        bar = tk.Frame(win); bar.pack(fill="x", padx=6, pady=4)
        paused = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause", variable=paused).pack(side="left")
        tk.Label(bar, text="Level≥").pack(side="left", padx=(8, 2))
        level_var = tk.StringVar(value="V")
        ttk.Combobox(bar, textvariable=level_var, values=["V", "D", "I", "W", "E", "F"],
                     width=3, state="readonly").pack(side="left")
        tk.Label(bar, text="Tag:").pack(side="left", padx=(8, 2))
        tag_var = tk.StringVar()
        tk.Entry(bar, textvariable=tag_var, width=20).pack(side="left")
        tk.Label(bar, text="Regex:").pack(side="left", padx=(8, 2))
        rx_var = tk.StringVar()
        tk.Entry(bar, textvariable=rx_var, width=24).pack(side="left")
        auto_scroll = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Follow tail", variable=auto_scroll).pack(side="left", padx=8)

        txt = scrolledtext.ScrolledText(win, font=("Consolas", 9),
                                        bg="#0e0e0e", fg="#ddd",
                                        insertbackground="#ddd")
        txt.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        txt.config(state="disabled")
        txt.tag_configure("V", foreground="#666")
        txt.tag_configure("D", foreground="#5b8bff")
        txt.tag_configure("I", foreground="#4ad991")
        txt.tag_configure("W", foreground="#ffd34a")
        txt.tag_configure("E", foreground="#ff5252")
        txt.tag_configure("F", foreground="#ff6bd6")

        info = tk.Label(bar, text="", fg="#888"); info.pack(side="right", padx=4)

        def clear():
            txt.config(state="normal")
            txt.delete("1.0", "end")
            txt.config(state="disabled")
        tk.Button(bar, text="Clear", command=clear).pack(side="left", padx=2)

        def save():
            path = filedialog.asksaveasfilename(
                defaultextension=".log",
                filetypes=[("log", "*.log"), ("text", "*.txt")])
            if path:
                with open(path, "w", encoding="utf-8") as fp:
                    fp.write(txt.get("1.0", "end"))
        tk.Button(bar, text="Save…", command=save).pack(side="left", padx=2)

        queue = collections.deque(maxlen=5000)
        stop = threading.Event()
        proc_ref = {"p": None}

        def reader():
            cmd = adb_cmd("logcat", "-v", "threadtime")
            if pid:
                cmd += [f"--pid={pid}"]
            try:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as e:
                self.root.after(0, lambda er=e: info.config(text=f"err: {er}", fg="red"))
                return
            proc_ref["p"] = p
            try:
                for line in p.stdout:
                    if stop.is_set():
                        break
                    queue.append(line.rstrip())
            finally:
                try:
                    p.terminate()
                except Exception:
                    pass

        threading.Thread(target=reader, daemon=True).start()

        def pump():
            if stop.is_set():
                return
            if paused.get():
                win.after(300, pump); return
            threshold = self._LEVEL_ORDER.get(level_var.get(), 0)
            tag_filter = tag_var.get().strip().lower()
            rx_pat = rx_var.get().strip()
            rx_compiled = None
            if rx_pat:
                try:
                    rx_compiled = re.compile(rx_pat, re.IGNORECASE)
                except re.error:
                    rx_compiled = None
            n_app = 0
            txt.config(state="normal")
            drain = min(800, len(queue))
            for _ in range(drain):
                try:
                    line = queue.popleft()
                except IndexError:
                    break
                m = self._LOGCAT_RE.match(line)
                if m:
                    level, tag, _msg = m.groups()
                    if self._LEVEL_ORDER.get(level, 0) < threshold:
                        continue
                    if tag_filter and tag_filter not in tag.lower():
                        continue
                    if rx_compiled and not rx_compiled.search(line):
                        continue
                    txt.insert("end", line + "\n", level)
                else:
                    if rx_compiled and not rx_compiled.search(line):
                        continue
                    txt.insert("end", line + "\n")
                n_app += 1
            # Cap text widget length
            try:
                total = int(txt.index("end-1c").split(".")[0])
                if total > 3000:
                    txt.delete("1.0", f"{total - 3000}.0")
            except ValueError:
                pass
            if auto_scroll.get() and n_app:
                txt.see("end")
            txt.config(state="disabled")
            info.config(text=f"buf={len(queue)}  +{n_app}")
            win.after(200, pump)

        win.after(200, pump)

        def close():
            stop.set()
            p = proc_ref["p"]
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ stubs filled later
    def _open_battery(self):
        win = tk.Toplevel(self.root)
        win.title("Battery breakdown")
        win.geometry("960x680")

        bar = tk.Frame(win); bar.pack(fill="x", padx=6, pady=4)
        info = tk.Label(bar, text="loading dumpsys batterystats…",
                        fg="#888", font=("Consolas", 9))
        info.pack(side="left", padx=4)
        tk.Button(bar, text="Refresh",
                  command=lambda: refresh()).pack(side="right", padx=2)

        nb = ttk.Notebook(win); nb.pack(fill="both", expand=True, padx=6, pady=4)

        # ----- Tab: Estimated power use (per-uid) -----
        tab_pwr = tk.Frame(nb); nb.add(tab_pwr, text="Estimated power (mAh)")
        cols = ("uid", "pkg", "mah", "cpu", "wakelock", "wifi", "screen")
        tv = ttk.Treeview(tab_pwr, columns=cols, show="headings", height=18)
        widths = {"uid": 70, "pkg": 280, "mah": 80,
                  "cpu": 80, "wakelock": 90, "wifi": 80, "screen": 80}
        for c in cols:
            tv.heading(c, text=c.upper())
            tv.column(c, width=widths[c],
                      anchor=("w" if c in ("uid", "pkg") else "e"),
                      stretch=(c == "pkg"))
        sb = ttk.Scrollbar(tab_pwr, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(fill="both", expand=True)

        # ----- Tab: Wakelocks -----
        tab_wl = tk.Frame(nb); nb.add(tab_wl, text="Wakelocks")
        wl_cols = ("name", "ms", "count", "pkg")
        wl_tv = ttk.Treeview(tab_wl, columns=wl_cols, show="headings", height=18)
        for c, wcol in zip(wl_cols, (260, 100, 80, 260)):
            wl_tv.heading(c, text=c.upper())
            wl_tv.column(c, width=wcol, anchor=("w" if c in ("name", "pkg") else "e"))
        wl_sb = ttk.Scrollbar(tab_wl, orient="vertical", command=wl_tv.yview)
        wl_tv.configure(yscrollcommand=wl_sb.set)
        wl_sb.pack(side="right", fill="y")
        wl_tv.pack(fill="both", expand=True)

        # ----- Tab: Raw -----
        tab_raw = tk.Frame(nb); nb.add(tab_raw, text="Raw")
        raw_txt = scrolledtext.ScrolledText(tab_raw, font=("Consolas", 9),
                                            bg="#1a1a1a", fg="#ddd")
        raw_txt.pack(fill="both", expand=True)

        def parse(raw):
            """batterystats csv-ish format. We mine three classes of rows:
              ',estpwr,<mah_total>,<uid>,...' — per-uid estimated power summary
              ',wl,<pkg>,<name>,<full_ms>,...'
              and free-form per-uid component lines from --charged."""
            est = {}  # uid -> {mah, cpu, wakelock, wifi, screen, pkg}
            wakelocks = []  # (name, ms, count, pkg)
            uid_pkg = {}
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    continue
                # Lines that map uid -> package name(s)
                # format example: "9,0,i,uid,10123,com.example"
                if len(parts) >= 6 and parts[3] == "uid":
                    try:
                        u = int(parts[4]); pk = parts[5]
                    except ValueError:
                        continue
                    uid_pkg.setdefault(u, []).append(pk)
                    continue
                # Per-uid summary (sample formats vary across Android versions;
                # we look for a row whose 4th field is a numeric uid and find
                # mAh / mAh-cpu / mAh-wakelock / etc.)
                # ",pwi,<type>,<mah>" style is also present per uid.
                if "pwi" in parts:
                    try:
                        i = parts.index("pwi")
                        kind = parts[i + 1]
                        mah = float(parts[i + 2])
                    except (ValueError, IndexError):
                        continue
                    # which uid this line belongs to: parts[3] typically
                    try:
                        u = int(parts[3])
                    except (ValueError, IndexError):
                        continue
                    rec = est.setdefault(u, {"mah": 0.0, "cpu": 0.0,
                                             "wakelock": 0.0, "wifi": 0.0,
                                             "screen": 0.0})
                    if kind in ("cpu", "wake", "wifi", "screen"):
                        key = "wakelock" if kind == "wake" else kind
                        rec[key] += mah
                    rec["mah"] += mah
                # ",wl,<name>,<count_full>,<full_ms>,..." format varies; capture loosely.
                if len(parts) >= 5 and parts[2] == "wl":
                    name = parts[3]
                    try:
                        full_ms = int(parts[4])
                    except ValueError:
                        full_ms = 0
                    count = 0
                    try:
                        count = int(parts[5]) if len(parts) > 5 else 0
                    except ValueError:
                        pass
                    try:
                        u = int(parts[1])
                    except ValueError:
                        u = -1
                    pk = ",".join(uid_pkg.get(u, [])) if u >= 0 else ""
                    wakelocks.append((name, full_ms, count, pk))
            # Attach package names to est rows
            for u, rec in est.items():
                rec["pkg"] = ",".join(uid_pkg.get(u, [])) or str(u)
                rec["uid"] = u
            return est, wakelocks

        def refresh():
            info.config(text="loading…", fg="#888")
            tv.delete(*tv.get_children())
            wl_tv.delete(*wl_tv.get_children())
            raw_txt.delete("1.0", "end")

            def worker():
                try:
                    r = adb("shell", "dumpsys", "batterystats", "--checkin",
                            timeout=20)
                    raw = (r.stdout or "") + (r.stderr or "")
                except Exception as e:
                    self.root.after(0, lambda er=e: info.config(
                        text=f"err: {er}", fg="red"))
                    return
                est, wls = parse(raw)
                self.root.after(0, lambda: render(est, wls, raw))

            threading.Thread(target=worker, daemon=True).start()

        def render(est, wls, raw):
            raw_txt.insert("1.0", raw[:200000])
            rows = sorted(est.values(), key=lambda r: -r["mah"])[:60]
            for r in rows:
                tv.insert("", "end", values=(
                    r["uid"], r["pkg"], f"{r['mah']:.2f}",
                    f"{r.get('cpu', 0):.2f}",
                    f"{r.get('wakelock', 0):.2f}",
                    f"{r.get('wifi', 0):.2f}",
                    f"{r.get('screen', 0):.2f}",
                ))
            wls.sort(key=lambda w: -w[1])
            for name, ms, count, pkg in wls[:200]:
                wl_tv.insert("", "end", values=(name, ms, count, pkg))
            info.config(text=f"power rows={len(rows)}  wakelocks={len(wls)}",
                        fg="#888")

        refresh()

    def _open_llm_live(self):
        win = tk.Toplevel(self.root)
        win.title("LLM live — TTFT · tps · decode_ms")
        win.geometry("1100x680")

        bar = tk.Frame(win); bar.pack(fill="x", padx=6, pady=4)
        tk.Label(bar, text="Logcat tag: LLM_METRIC   (app must emit JSON via "
                          "Log.i(\"LLM_METRIC\", json))", fg="#888",
                 font=("Consolas", 9)).pack(side="left", padx=4)
        info = tk.Label(bar, text="events=0", fg="#888", font=("Consolas", 9))
        info.pack(side="right", padx=4)
        tk.Label(bar, text="thermal °C ≥").pack(side="right", padx=(8, 2))
        therm_var = tk.DoubleVar(value=60.0)
        tk.Spinbox(bar, from_=30, to=100, increment=1, width=4,
                   textvariable=therm_var).pack(side="right")

        # Two stacked canvases — values per event + summary numbers below.
        chart = tk.Canvas(win, height=380, bg="#0e0e0e", highlightthickness=0)
        chart.pack(fill="both", expand=True, padx=8, pady=4)

        summary = tk.Label(win, text="—", bg="#181818", fg="#ddd",
                           font=("Consolas", 10), justify="left", anchor="w")
        summary.pack(fill="x", padx=8, pady=(0, 4))

        log_lbl = tk.Label(win, text="Recent events", fg="#4ec9ff",
                           font=("Segoe UI", 9, "bold"))
        log_lbl.pack(anchor="w", padx=8)
        log_txt = scrolledtext.ScrolledText(win, height=8, font=("Consolas", 9),
                                            bg="#1a1a1a", fg="#ddd")
        log_txt.pack(fill="x", padx=8, pady=(0, 8))
        log_txt.config(state="disabled")

        events = []  # list of dicts {ts, ttft_ms, tps, decode_ms, backend, model}
        backend_changes = []  # list of (event_idx, backend_actual)
        stop = threading.Event()
        proc_ref = {"p": None}

        def reader():
            # `-T 1` starts at the tail (skip historical buffer).
            cmd = adb_cmd("logcat", "-T", "1", "-s", "LLM_METRIC:I")
            try:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as e:
                self.root.after(0, lambda er=e: info.config(text=f"err: {er}", fg="red"))
                return
            proc_ref["p"] = p
            try:
                for line in p.stdout:
                    if stop.is_set():
                        break
                    # threadtime line: "<date> <time> <pid> <tid> I LLM_METRIC: {json…}"
                    if "LLM_METRIC" not in line:
                        continue
                    i = line.find("{")
                    if i < 0:
                        continue
                    try:
                        obj = json.loads(line[i:].strip())
                    except json.JSONDecodeError:
                        continue
                    if obj.get("event") not in ("done", "decode", "ttft", None):
                        # accept anything; just collect what we can
                        pass
                    rec = {
                        "ts": time.time(),
                        "ttft_ms": float(obj["ttft_ms"]) if "ttft_ms" in obj else None,
                        "tps":     float(obj["tokens_per_sec"]) if "tokens_per_sec" in obj else None,
                        "decode_ms": float(obj["decode_ms"]) if "decode_ms" in obj else None,
                        "backend": obj.get("backend_actual") or obj.get("backend_requested"),
                        "model":   obj.get("model_name", ""),
                        "raw":     obj,
                    }
                    events.append(rec)
                    if rec["backend"] and (
                            not backend_changes
                            or backend_changes[-1][1] != rec["backend"]):
                        backend_changes.append((len(events) - 1, rec["backend"]))
                    self.root.after(0, lambda r=rec: append_log(r))
            finally:
                try:
                    p.terminate()
                except Exception:
                    pass

        def append_log(rec):
            log_txt.config(state="normal")
            ttft_s   = "-" if rec["ttft_ms"]   is None else f"{rec['ttft_ms']:.0f}ms"
            tps_s    = "-" if rec["tps"]       is None else f"{rec['tps']:.1f}"
            decode_s = "-" if rec["decode_ms"] is None else f"{rec['decode_ms']:.0f}ms"
            model_s  = rec["model"] or "?"
            backend_s = rec["backend"] or "?"
            line = (f"{time.strftime('%H:%M:%S')}  "
                    f"model={model_s:16s}  "
                    f"ttft={ttft_s:>9s}  "
                    f"tps={tps_s:>6s}  "
                    f"decode={decode_s:>9s}  "
                    f"backend={backend_s}\n")
            log_txt.insert("end", line)
            try:
                total = int(log_txt.index("end-1c").split(".")[0])
                if total > 500:
                    log_txt.delete("1.0", f"{total - 500}.0")
            except ValueError:
                pass
            log_txt.see("end")
            log_txt.config(state="disabled")

        def redraw():
            chart.delete("all")
            w = chart.winfo_width(); h = chart.winfo_height()
            if w < 10 or h < 10:
                return
            if not events:
                chart.create_text(w // 2, h // 2, fill="#666",
                                  text="waiting for LLM_METRIC events…",
                                  font=("Segoe UI", 11))
                return
            n = len(events)
            margin_l = 50; margin_r = 12; margin_t = 14; margin_b = 24
            pw = w - margin_l - margin_r
            ph = h - margin_t - margin_b
            x_step = pw / max(1, n - 1) if n > 1 else 0

            def draw_series(getter, color, scale_max, label):
                vals = [getter(e) for e in events]
                pts = []
                for i, v in enumerate(vals):
                    if v is None:
                        continue
                    x = margin_l + i * x_step
                    y = margin_t + ph - min(1.0, v / scale_max) * ph
                    pts.append((x, y))
                if len(pts) >= 2:
                    flat = [c for p in pts for c in p]
                    chart.create_line(*flat, fill=color, width=2)
                elif len(pts) == 1:
                    chart.create_oval(pts[0][0] - 3, pts[0][1] - 3,
                                      pts[0][0] + 3, pts[0][1] + 3,
                                      fill=color, outline=color)
                # legend
                chart.create_text(margin_l + 4, margin_t + 10,
                                  text=label, anchor="nw", fill=color,
                                  font=("Consolas", 9))

            # Three series each on its own band (split vertically).
            # TTFT: 0..3000ms top third
            # tps:  0..60 middle third
            # decode_ms: 0..200 bottom third
            band_h = ph / 3
            for band_idx, (getter, color, smax, label, unit) in enumerate([
                (lambda e: e["ttft_ms"], "#ff9f43", 3000.0, "TTFT (ms, 0–3000)", "ms"),
                (lambda e: e["tps"],     "#4ad991",   60.0, "tokens/sec (0–60)", "tps"),
                (lambda e: e["decode_ms"], "#5b8bff", 200.0, "decode_ms (0–200)", "ms"),
            ]):
                band_top = margin_t + band_idx * band_h
                # band background + label
                chart.create_rectangle(margin_l, band_top, w - margin_r,
                                       band_top + band_h - 2,
                                       fill="#141414", outline="#222")
                # grid lines at 25/50/75%
                for pct in (25, 50, 75):
                    y = band_top + band_h - band_h * pct / 100
                    chart.create_line(margin_l, y, w - margin_r, y, fill="#222")
                # local series
                vals = [getter(e) for e in events]
                pts = []
                for i, v in enumerate(vals):
                    if v is None:
                        continue
                    x = margin_l + i * x_step
                    y = band_top + band_h - min(1.0, v / smax) * band_h
                    pts.append((x, y))
                if len(pts) >= 2:
                    flat = [c for p in pts for c in p]
                    chart.create_line(*flat, fill=color, width=2)
                elif len(pts) == 1:
                    chart.create_oval(pts[0][0] - 3, pts[0][1] - 3,
                                      pts[0][0] + 3, pts[0][1] + 3,
                                      fill=color, outline=color)
                chart.create_text(margin_l + 6, band_top + 4,
                                  text=label, anchor="nw", fill=color,
                                  font=("Consolas", 9, "bold"))

            # Backend change markers (vertical dashed line + label)
            for idx, b in backend_changes:
                x = margin_l + idx * x_step
                chart.create_line(x, margin_t, x, margin_t + ph,
                                  fill="#b48cff", dash=(3, 3))
                chart.create_text(x + 2, margin_t + 2, anchor="nw",
                                  fill="#b48cff",
                                  text=f"→ {b}", font=("Consolas", 8))

            # Update summary
            tps_vals = [e["tps"] for e in events if e["tps"] is not None]
            ttft_vals = [e["ttft_ms"] for e in events if e["ttft_ms"] is not None]
            # Thermal-aware tps — uses the main app's stats_history if available
            t_thresh = therm_var.get()
            hot_tps = []
            normal_tps = []
            if hasattr(self, "stats_history") and self.stats_history:
                # Map each event ts to nearest stats sample temperature.
                stats = sorted(self.stats_history, key=lambda s: s["ts"])
                stats_ts = [s["ts"] for s in stats]
                for e in events:
                    if e["tps"] is None:
                        continue
                    # binary search would be nicer; linear is fine here
                    temp = None
                    for s in stats:
                        if s["ts"] > e["ts"]:
                            break
                        if s.get("cpu_temp") is not None:
                            temp = s["cpu_temp"]
                    if temp is None:
                        continue
                    if temp >= t_thresh:
                        hot_tps.append(e["tps"])
                    else:
                        normal_tps.append(e["tps"])

            def avg(xs):
                return sum(xs) / len(xs) if xs else None

            ttft_avg_s = "-" if not ttft_vals else f"{avg(ttft_vals):.0f}ms"
            tps_avg_s  = "-" if not tps_vals  else f"{avg(tps_vals):.1f}"
            hot_avg_s  = "-" if not hot_tps   else f"{avg(hot_tps):.1f}"
            cool_avg_s = "-" if not normal_tps else f"{avg(normal_tps):.1f}"
            summary.config(text=(
                f"events={len(events)}   "
                f"TTFT avg={ttft_avg_s:>8s}   "
                f"tps avg={tps_avg_s:>5s}   "
                f"|   thermal-aware tps (≥{t_thresh:.0f}°C): "
                f"hot={hot_avg_s} (n={len(hot_tps)})   "
                f"cool={cool_avg_s} (n={len(normal_tps)})   "
                f"|   backend changes: {len(backend_changes)}"
            ))
            info.config(text=f"events={len(events)}")

        def tick():
            if stop.is_set():
                return
            redraw()
            win.after(800, tick)

        chart.bind("<Configure>", lambda _e: redraw())
        threading.Thread(target=reader, daemon=True).start()
        win.after(400, tick)

        def close():
            stop.set()
            p = proc_ref["p"]
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    def _open_scenario(self):
        win = tk.Toplevel(self.root)
        win.title("Scenario runner")
        win.geometry("780x600")

        cfg = tk.Frame(win); cfg.pack(fill="x", padx=10, pady=8)

        # Macro file
        tk.Label(cfg, text="Macro .sh:").grid(row=0, column=0, sticky="w")
        macro_var = tk.StringVar()
        tk.Entry(cfg, textvariable=macro_var, width=60).grid(row=0, column=1, sticky="ew", padx=4)

        def pick_macro():
            p = filedialog.askopenfilename(
                title="Pick macro script",
                initialdir=HERE,
                filetypes=[("shell script", "*.sh"), ("all", "*.*")])
            if p:
                macro_var.set(p)
        tk.Button(cfg, text="Browse…", command=pick_macro).grid(row=0, column=2, padx=2)

        # Iter / gap
        tk.Label(cfg, text="Iterations:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        n_var = tk.IntVar(value=3)
        tk.Spinbox(cfg, from_=1, to=999, width=6, textvariable=n_var
                   ).grid(row=1, column=1, sticky="w", pady=(6, 0))
        tk.Label(cfg, text="Iter gap (s):").grid(row=1, column=1, pady=(6, 0))
        gap_var = tk.IntVar(value=5)
        tk.Spinbox(cfg, from_=0, to=600, width=6, textvariable=gap_var
                   ).grid(row=1, column=1, sticky="e", padx=(0, 80), pady=(6, 0))

        # Capture toggles
        perf_var = tk.BooleanVar(value=False)
        tk.Checkbutton(cfg, text="Perfetto each iter",
                       variable=perf_var).grid(row=2, column=0, sticky="w", pady=(8, 0))
        perf_dur_var = tk.IntVar(value=30)
        tk.Spinbox(cfg, from_=5, to=120, width=4, textvariable=perf_dur_var
                   ).grid(row=2, column=1, sticky="w", pady=(8, 0))
        tk.Label(cfg, text="s").grid(row=2, column=1, sticky="w", padx=(50, 0), pady=(8, 0))

        sp_var = tk.BooleanVar(value=False)
        tk.Checkbutton(cfg, text="simpleperf each iter",
                       variable=sp_var).grid(row=3, column=0, sticky="w")
        tk.Label(cfg, text="PID:").grid(row=3, column=1, sticky="w")
        sp_pid_var = tk.StringVar()
        tk.Entry(cfg, textvariable=sp_pid_var, width=8).grid(row=3, column=1, sticky="w", padx=(40, 0))
        tk.Label(cfg, text="pkg:").grid(row=3, column=1, sticky="w", padx=(120, 0))
        sp_pkg_var = tk.StringVar()
        tk.Entry(cfg, textvariable=sp_pkg_var, width=24).grid(row=3, column=1, sticky="w", padx=(160, 0))

        # Buttons
        btn_row = tk.Frame(win); btn_row.pack(fill="x", padx=10, pady=4)
        start_btn = tk.Button(btn_row, text="Start", fg="green")
        start_btn.pack(side="left", padx=2)
        stop_btn = tk.Button(btn_row, text="Stop", fg="red", state="disabled")
        stop_btn.pack(side="left", padx=2)
        progress_lbl = tk.Label(btn_row, text="idle", fg="#888", font=("Consolas", 9))
        progress_lbl.pack(side="left", padx=10)

        # Log
        log = scrolledtext.ScrolledText(win, height=22, font=("Consolas", 9),
                                        bg="#1a1a1a", fg="#ddd")
        log.pack(fill="both", expand=True, padx=10, pady=4)

        def slog(msg):
            ts = time.strftime("%H:%M:%S")
            log.insert("end", f"{ts}  {msg}\n")
            log.see("end")

        stop_evt = threading.Event()
        worker_ref = {"t": None}

        def run_scenario():
            macro = macro_var.get().strip()
            if not macro or not os.path.isfile(macro):
                slog("ERROR: pick a valid macro .sh file")
                return
            n = max(1, int(n_var.get()))
            gap = max(0, int(gap_var.get()))
            do_perf = perf_var.get()
            perf_dur = max(5, int(perf_dur_var.get()))
            do_sp = sp_var.get()
            sp_pid = sp_pid_var.get().strip()
            sp_pkg = sp_pkg_var.get().strip()
            if do_sp and not sp_pid.isdigit():
                slog("ERROR: simpleperf needs a numeric PID")
                return

            # Auto-start a session if one isn't already running so all artifacts
            # (jsonl + traces + reports) collect under one folder.
            started_here = False
            if self.session_dir is None:
                self._start_session()
                started_here = True
                slog(f"auto-started session → {self.session_dir}")

            scenario_root = self.session_dir or HERE

            def runner():
                start_btn.config(state="disabled")
                stop_btn.config(state="normal")
                try:
                    for i in range(1, n + 1):
                        if stop_evt.is_set():
                            slog("STOPPED by user")
                            break
                        iter_dir = os.path.join(scenario_root, f"iter_{i:03d}")
                        os.makedirs(iter_dir, exist_ok=True)
                        slog(f"=== iter {i}/{n} → {iter_dir}")
                        self.root.after(0, lambda i=i:
                                        progress_lbl.config(text=f"iter {i}/{n}",
                                                            fg="orange"))

                        # Kick captures (non-blocking)
                        if do_perf:
                            slog(f"  perfetto {perf_dur}s start")
                            self._capture_perfetto(perf_dur)
                        if do_sp:
                            slog(f"  simpleperf pid={sp_pid} {perf_dur}s start")
                            self._capture_simpleperf(sp_pid, sp_pkg or "?", perf_dur)

                        # Run macro synchronously — push + sh
                        ts_iter = time.strftime("%Y%m%d_%H%M%S")
                        remote = f"/data/local/tmp/scenario_{ts_iter}_{i:03d}.sh"
                        try:
                            adb("push", macro, remote, timeout=30)
                            adb("shell", f"chmod 755 {remote}", timeout=10)
                            slog("  macro running…")
                            r = adb("shell", f"sh {remote}", timeout=600)
                            adb("shell", f"rm -f {remote}", timeout=5)
                            rc = r.returncode
                            slog(f"  macro done rc={rc}")
                        except Exception as e:
                            slog(f"  macro ERR: {e}")

                        # Wait for capture threads to finish (they pull files
                        # into self.session_dir on completion).
                        if do_perf or do_sp:
                            slog("  waiting for captures…")
                            t0 = time.time()
                            while (self._perfetto_running.is_set()
                                   or self._simpleperf_running.is_set()):
                                if stop_evt.is_set():
                                    break
                                if time.time() - t0 > perf_dur + 90:
                                    slog("  capture wait timeout")
                                    break
                                time.sleep(0.5)

                        # Move artifacts into the iter subfolder.
                        for fname in ("perfetto_trace.pb",
                                      f"simpleperf_report_{sp_pid}.txt",
                                      f"perf_{sp_pid}.data"):
                            src = os.path.join(scenario_root, fname)
                            if os.path.isfile(src):
                                try:
                                    shutil.move(src, os.path.join(iter_dir, fname))
                                    slog(f"  → {fname} → iter_{i:03d}/")
                                except Exception as e:
                                    slog(f"  move {fname} ERR: {e}")

                        if i < n and not stop_evt.is_set() and gap > 0:
                            slog(f"  sleep {gap}s before next iter")
                            for _ in range(gap * 2):
                                if stop_evt.is_set():
                                    break
                                time.sleep(0.5)

                    slog("scenario complete")
                finally:
                    if started_here and self.session_dir:
                        self._stop_session()
                        slog("session auto-stopped")
                    self.root.after(0, lambda: [
                        start_btn.config(state="normal"),
                        stop_btn.config(state="disabled"),
                        progress_lbl.config(text="done", fg="green"),
                    ])
                    stop_evt.clear()

            stop_evt.clear()
            t = threading.Thread(target=runner, daemon=True)
            worker_ref["t"] = t
            t.start()

        start_btn.config(command=run_scenario)
        stop_btn.config(command=lambda: (stop_evt.set(),
                                         progress_lbl.config(text="stopping…",
                                                             fg="red")))

        def close():
            stop_evt.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ flame graph (simpleperf report)
    @staticmethod
    def _parse_simpleperf_report(text):
        """Best-effort callgraph parser. Returns a tree dict
        {"name": str, "pct": float, "children": [...]}.

        simpleperf -g output varies by version; this targets the common form:
          NN.NN%  Command ... Symbol     ← top-level samples
              |
              |-- NN.NN% -- foo
              |    |
              |    |-- NN.NN% -- bar
              ...
        Indentation level is inferred from the column where the digit starts.
        """
        root = {"name": "[root]", "pct": 100.0, "children": []}
        in_body = False
        stack = [(0, root)]  # (indent_col, node)
        line_re = re.compile(r"^(?P<indent>[\s|`\-]*?)(?P<pct>\d+(?:\.\d+)?)%"
                             r"(?:\s*--\s*|\s+)(?P<sym>.+?)\s*$")
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            # Skip header noise until we see overhead/percent
            if not in_body:
                if "%" not in line:
                    continue
                in_body = True
            m = line_re.match(line)
            if not m:
                continue
            indent = m.group("indent")
            try:
                pct = float(m.group("pct"))
            except ValueError:
                continue
            sym = m.group("sym").strip()
            # Skip table-header-style summary rows that pack multiple cols into 'sym'
            # (e.g. "85.00% appname 1234 1234 libfoo.so main"). For top-level rows
            # we want the last token as symbol.
            if not indent.strip() and len(sym.split()) >= 4 and "  " in sym:
                # collapse multi-column row → take the trailing symbol part
                sym = sym.split()[-1]
            depth = len(indent.expandtabs(4))
            while len(stack) > 1 and stack[-1][0] >= depth:
                stack.pop()
            node = {"name": sym, "pct": pct, "children": []}
            stack[-1][1]["children"].append(node)
            stack.append((depth, node))
        return root

    def _open_flame(self):
        artifacts_root = os.path.join(HERE, "artifacts")
        initial = artifacts_root if os.path.isdir(artifacts_root) else HERE
        path = filedialog.askopenfilename(
            title="Pick simpleperf_report_*.txt",
            initialdir=initial,
            filetypes=[("simpleperf report", "*.txt"), ("all", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                text = fp.read()
        except Exception as e:
            messagebox.showerror("flame", f"read err: {e}"); return
        tree = self._parse_simpleperf_report(text)
        if not tree["children"]:
            messagebox.showwarning(
                "flame",
                "Could not parse callgraph from this file.\n"
                "Make sure simpleperf was run with -g (call graph).")
            return

        win = tk.Toplevel(self.root)
        win.title(f"Flame graph — {os.path.basename(path)}")
        win.geometry("1200x680")

        bar = tk.Frame(win); bar.pack(fill="x", padx=6, pady=4)
        tk.Label(bar, text="click: zoom-in   right-click: zoom-out   ESC: reset",
                 fg="#888", font=("Consolas", 9)).pack(side="left", padx=4)
        info = tk.Label(bar, text="", fg="#4ec9ff",
                        font=("Consolas", 9), anchor="e")
        info.pack(side="right", padx=4)

        canvas = tk.Canvas(win, bg="#0e0e0e", highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=6, pady=4)

        ROW_H = 18
        palette = [
            "#ff9f43", "#ff5252", "#ffd34a", "#4ad991",
            "#5b8bff", "#b48cff", "#4ec9ff", "#ff6bd6",
        ]
        # Pre-compute box layout once. (x0_norm, x1_norm, depth, node)
        layout_boxes = []

        def layout(node, x0, x1, depth):
            layout_boxes.append((x0, x1, depth, node))
            if not node["children"]:
                return
            total = sum(c["pct"] for c in node["children"])
            if total <= 0:
                return
            cur = x0
            w = x1 - x0
            for c in node["children"]:
                cw = w * c["pct"] / total
                layout(c, cur, cur + cw, depth + 1)
                cur += cw
        layout(tree, 0.0, 1.0, 0)

        max_depth = max((b[2] for b in layout_boxes), default=1)
        view = {"x_min": 0.0, "x_max": 1.0, "stack": []}

        def redraw():
            canvas.delete("all")
            w = canvas.winfo_width(); h = canvas.winfo_height()
            if w < 10 or h < 10:
                return
            x_min, x_max = view["x_min"], view["x_max"]
            x_range = max(1e-9, x_max - x_min)
            for x0, x1, depth, node in layout_boxes:
                if x1 < x_min or x0 > x_max:
                    continue
                sx0 = (x0 - x_min) / x_range * w
                sx1 = (x1 - x_min) / x_range * w
                if sx1 - sx0 < 1.5:
                    continue  # skip slivers
                sy0 = h - (depth + 1) * ROW_H
                sy1 = sy0 + ROW_H - 2
                if sy1 < 0:
                    continue
                color = palette[depth % len(palette)]
                canvas.create_rectangle(sx0, sy0, sx1, sy1,
                                        fill=color, outline="#0e0e0e")
                # text if box is wide enough
                if sx1 - sx0 > 60:
                    name = node["name"]
                    # trim long symbols
                    if len(name) > 60:
                        name = name[:57] + "…"
                    canvas.create_text(sx0 + 4, (sy0 + sy1) / 2,
                                       anchor="w", fill="#111",
                                       font=("Consolas", 8),
                                       text=f"{name}  {node['pct']:.1f}%")

        def hit_test(ex, ey):
            w = canvas.winfo_width(); h = canvas.winfo_height()
            x_min, x_max = view["x_min"], view["x_max"]
            x_range = max(1e-9, x_max - x_min)
            for x0, x1, depth, node in layout_boxes:
                sx0 = (x0 - x_min) / x_range * w
                sx1 = (x1 - x_min) / x_range * w
                sy0 = h - (depth + 1) * ROW_H
                sy1 = sy0 + ROW_H - 2
                if sx0 <= ex <= sx1 and sy0 <= ey <= sy1:
                    return (x0, x1, node)
            return None

        def on_motion(e):
            hit = hit_test(e.x, e.y)
            if hit is None:
                info.config(text=""); return
            _, _, node = hit
            info.config(text=f"{node['name']}  {node['pct']:.2f}%  ({len(node['children'])} children)")

        def on_click(e):
            hit = hit_test(e.x, e.y)
            if hit is None:
                return
            x0, x1, _ = hit
            view["stack"].append((view["x_min"], view["x_max"]))
            view["x_min"] = x0
            view["x_max"] = x1
            redraw()

        def on_rclick(_e):
            if view["stack"]:
                view["x_min"], view["x_max"] = view["stack"].pop()
                redraw()

        def on_esc(_e):
            view["stack"].clear()
            view["x_min"] = 0.0
            view["x_max"] = 1.0
            redraw()

        canvas.bind("<Configure>", lambda _e: redraw())
        canvas.bind("<Motion>", on_motion)
        canvas.bind("<Button-1>", on_click)
        canvas.bind("<Button-3>", on_rclick)
        win.bind("<Escape>", on_esc)
        win.after(80, redraw)

    # ============================================================ pcap viewer (PCAPdroid integration)
    # Minimal libpcap v2 parser supporting RAW IP (LINKTYPE 12/101) and
    # Ethernet (LINKTYPE 1). pcap-ng files start with 0x0a0d0d0a and are
    # rejected with a helpful message.
    _PCAP_MAGICS = {
        0xa1b2c3d4: ("<", False),  # little-endian, microsecond
        0xd4c3b2a1: (">", False),  # big-endian, microsecond
        0xa1b23c4d: ("<", True),   # little-endian, nanosecond
        0x4d3cb2a1: (">", True),   # big-endian, nanosecond
    }
    _IP_PROTO_NAME = {
        1: "ICMP", 2: "IGMP", 6: "TCP", 17: "UDP", 41: "IPv6",
        47: "GRE", 50: "ESP", 51: "AH", 58: "ICMPv6", 89: "OSPF",
        132: "SCTP",
    }

    @staticmethod
    def _parse_pcap(path, max_packets=20000):
        """Yield dicts {idx, ts, src, dst, proto, length, info, raw}.

        Best-effort. Skips malformed records and returns whatever it got."""
        out = []
        with open(path, "rb") as fp:
            hdr = fp.read(24)
            if len(hdr) < 24:
                return out, "file too small for pcap header"
            magic = struct.unpack("<I", hdr[:4])[0]
            if magic == 0x0a0d0d0a:
                return out, ("This file is pcap-ng (block-based). Save as "
                             "legacy pcap from PCAPdroid, or convert with: "
                             "editcap -F pcap in.pcapng out.pcap")
            if magic not in PhoneController._PCAP_MAGICS:
                return out, f"unknown magic 0x{magic:08x}"
            endian, _nsec = PhoneController._PCAP_MAGICS[magic]
            # version_major(2) version_minor(2) thiszone(4) sigfigs(4) snaplen(4) network(4)
            _vmaj, _vmin, _tz, _sig, _snap, linktype = struct.unpack(
                endian + "HHIIII", hdr[4:24])
            if linktype not in (1, 12, 101, 113, 228):
                # Try anyway — many captures fudge linktype values.
                pass

            i = 0
            while True:
                rec = fp.read(16)
                if len(rec) < 16:
                    break
                ts_sec, ts_usec, incl, orig = struct.unpack(endian + "IIII", rec)
                data = fp.read(incl)
                if len(data) < incl:
                    break
                # Strip link-layer to get IP header
                if linktype == 1:  # Ethernet
                    if len(data) < 14:
                        continue
                    ethertype = struct.unpack(">H", data[12:14])[0]
                    payload = data[14:]
                    if ethertype != 0x0800 and ethertype != 0x86DD:
                        # Not IPv4/IPv6 — skip
                        continue
                else:  # RAW / LINUX SLL handled loosely as raw
                    payload = data
                pkt = PhoneController._parse_ip(payload, ts_sec, ts_usec)
                if pkt is None:
                    continue
                pkt["idx"] = len(out) + 1
                pkt["raw"] = data
                out.append(pkt)
                i += 1
                if len(out) >= max_packets:
                    break
        return out, None

    @staticmethod
    def _ipv4_str(b):
        return ".".join(str(x) for x in b)

    @staticmethod
    def _ipv6_str(b):
        groups = struct.unpack("!8H", b)
        return ":".join(f"{g:x}" for g in groups)

    @staticmethod
    def _parse_ip(payload, ts_sec, ts_usec):
        if not payload:
            return None
        v = payload[0] >> 4
        if v == 4:
            if len(payload) < 20:
                return None
            ihl = (payload[0] & 0x0F) * 4
            if ihl < 20 or len(payload) < ihl:
                return None
            tot_len = struct.unpack(">H", payload[2:4])[0]
            proto = payload[9]
            src = PhoneController._ipv4_str(payload[12:16])
            dst = PhoneController._ipv4_str(payload[16:20])
            l4 = payload[ihl:]
            proto_name = PhoneController._IP_PROTO_NAME.get(proto, f"p{proto}")
            info = ""
            sp = dp = None
            if proto == 6 and len(l4) >= 14:  # TCP
                sp, dp, seq, ack = struct.unpack(">HHII", l4[:12])
                doff_flags = struct.unpack(">H", l4[12:14])[0]
                flags = doff_flags & 0x1FF
                flag_str = ""
                if flags & 0x02: flag_str += "S"
                if flags & 0x10: flag_str += "A"
                if flags & 0x01: flag_str += "F"
                if flags & 0x04: flag_str += "R"
                if flags & 0x08: flag_str += "P"
                if flags & 0x20: flag_str += "U"
                info = f"[{flag_str}] seq={seq} ack={ack}"
            elif proto == 17 and len(l4) >= 8:  # UDP
                sp, dp, ulen, _ = struct.unpack(">HHHH", l4[:8])
                info = f"len={ulen}"
                if dp == 53 or sp == 53:
                    info += "  " + PhoneController._dns_summary(l4[8:])
            return {
                "ts": ts_sec + ts_usec / 1_000_000.0,
                "src": f"{src}:{sp}" if sp is not None else src,
                "dst": f"{dst}:{dp}" if dp is not None else dst,
                "proto": proto_name,
                "length": tot_len,
                "info": info,
            }
        elif v == 6:
            if len(payload) < 40:
                return None
            payload_len = struct.unpack(">H", payload[4:6])[0]
            nxt = payload[6]
            src = PhoneController._ipv6_str(payload[8:24])
            dst = PhoneController._ipv6_str(payload[24:40])
            l4 = payload[40:]
            proto_name = PhoneController._IP_PROTO_NAME.get(nxt, f"p{nxt}")
            info = ""
            sp = dp = None
            if nxt == 6 and len(l4) >= 14:
                sp, dp = struct.unpack(">HH", l4[:4])
            elif nxt == 17 and len(l4) >= 8:
                sp, dp, ulen, _ = struct.unpack(">HHHH", l4[:8])
                info = f"len={ulen}"
            return {
                "ts": ts_sec + ts_usec / 1_000_000.0,
                "src": f"[{src}]:{sp}" if sp is not None else src,
                "dst": f"[{dst}]:{dp}" if dp is not None else dst,
                "proto": proto_name,
                "length": 40 + payload_len,
                "info": info,
            }
        return None

    @staticmethod
    def _dns_summary(buf):
        if len(buf) < 12:
            return ""
        try:
            tid, flags, qd, an, ns_, ar = struct.unpack(">HHHHHH", buf[:12])
        except struct.error:
            return ""
        kind = "Q" if (flags & 0x8000) == 0 else "R"
        # Parse first qname
        i = 12
        labels = []
        while i < len(buf):
            ln = buf[i]
            if ln == 0:
                break
            if ln & 0xC0:
                break
            i += 1
            if i + ln > len(buf):
                return ""
            labels.append(buf[i:i+ln].decode("ascii", "replace"))
            i += ln
        return f"DNS-{kind} {'.'.join(labels)}"

    def _open_wire(self):
        win = tk.Toplevel(self.root)
        win.title("Packet capture (PCAPdroid integration)")
        win.geometry("1200x720")
        wire_stop = threading.Event()

        # Workers post UI updates here; if the window is gone or torn down
        # mid-flight (TclError), silently skip rather than spam stderr.
        def safe_ui(fn):
            if wire_stop.is_set():
                return
            try:
                if not win.winfo_exists():
                    return
                fn()
            except tk.TclError:
                pass

        def post_ui(fn):
            self.root.after(0, lambda: safe_ui(fn))

        bar = tk.Frame(win); bar.pack(fill="x", padx=6, pady=4)
        path_var = tk.StringVar()
        tk.Label(bar, text="pcap:").pack(side="left")
        tk.Entry(bar, textvariable=path_var, width=60).pack(side="left", padx=4, fill="x", expand=True)

        def open_local():
            p = filedialog.askopenfilename(
                title="Open pcap",
                initialdir=HERE,
                filetypes=[("pcap", "*.pcap *.cap"),
                           ("pcap-ng", "*.pcapng"), ("all", "*.*")])
            if p:
                path_var.set(p)
                load(p)
        tk.Button(bar, text="Open local…", command=open_local).pack(side="left", padx=2)

        # One-click record/stop via PCAPdroid CaptureCtrl intent.
        PCAPDROID_PKG = "com.emanuelef.remote_capture"
        PCAPDROID_CTL = f"{PCAPDROID_PKG}/.activities.CaptureCtrl"
        recording = {"name": None, "started": None, "filter": ""}
        rec_btn = tk.Button(bar, text="● Record", fg="green")
        rec_btn.pack(side="left", padx=2)
        tk.Label(bar, text="App filter:").pack(side="left", padx=(8, 2))
        app_filter_var = tk.StringVar()
        tk.Entry(bar, textvariable=app_filter_var, width=22).pack(side="left")

        def is_pcapdroid_installed():
            r = adb("shell", "pm", "list", "packages", PCAPDROID_PKG, timeout=5)
            return PCAPDROID_PKG in (r.stdout or "")

        def offer_install():
            dlg = tk.Toplevel(win)
            dlg.title("PCAPdroid not installed")
            dlg.transient(win)
            dlg.grab_set()
            tk.Label(dlg, justify="left", padx=14, pady=10,
                     text=("PCAPdroid is required for one-click capture.\n"
                           "Free, no root. github.com/emanuele-f/PCAPdroid\n\n"
                           "Pick how to install:")).pack()
            btns = tk.Frame(dlg); btns.pack(pady=10, padx=14, fill="x")

            def do_adb_install():
                dlg.destroy()
                threading.Thread(target=_auto_install_pcapdroid,
                                 args=(post_ui, info, recording, rec_btn),
                                 daemon=True).start()

            def do_play_store():
                dlg.destroy()
                adb("shell", "am", "start",
                    "-a", "android.intent.action.VIEW",
                    "-d", f"market://details?id={PCAPDROID_PKG}",
                    timeout=5)
                info.config(text="opening Play Store on phone…", fg="orange")

            tk.Button(btns, text="Install via adb (auto, ~10 MB)",
                      command=do_adb_install).pack(fill="x", pady=2)
            tk.Button(btns, text="Open Play Store on phone",
                      command=do_play_store).pack(fill="x", pady=2)
            tk.Button(btns, text="Cancel",
                      command=dlg.destroy).pack(fill="x", pady=2)

        def _auto_install_pcapdroid(post_ui, info, recording, rec_btn):
            import urllib.request, urllib.error, tempfile
            post_ui(lambda: info.config(
                text="finding latest PCAPdroid release…", fg="orange"))
            try:
                req = urllib.request.Request(
                    "https://api.github.com/repos/emanuele-f/PCAPdroid/releases/latest",
                    headers={"Accept": "application/vnd.github+json",
                             "User-Agent": "phone-controller"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    rel = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                post_ui(lambda er=e: info.config(
                    text=f"github api err: {er}", fg="red"))
                return
            apk_url = None
            for a in rel.get("assets", []):
                name = a.get("name", "")
                if name.endswith(".apk") and "debug" not in name.lower():
                    apk_url = a.get("browser_download_url")
                    apk_name = name
                    break
            if not apk_url:
                post_ui(lambda: info.config(
                    text="no .apk asset on github releases", fg="red"))
                return

            post_ui(lambda n=apk_name: info.config(
                text=f"downloading {n}…", fg="orange"))
            tmp_apk = os.path.join(tempfile.gettempdir(), apk_name)
            try:
                with urllib.request.urlopen(apk_url, timeout=120) as r:
                    with open(tmp_apk, "wb") as fp:
                        shutil.copyfileobj(r, fp)
            except Exception as e:
                post_ui(lambda er=e: info.config(
                    text=f"download err: {er}", fg="red"))
                return

            post_ui(lambda: info.config(text="adb install…", fg="orange"))
            try:
                r = adb("install", "-r", tmp_apk, timeout=120)
            except Exception as e:
                post_ui(lambda er=e: info.config(
                    text=f"install err: {er}", fg="red"))
                return
            ok = (r.returncode == 0
                  and ("Success" in (r.stdout or "")
                       or "Success" in (r.stderr or "")))
            if ok:
                post_ui(lambda: info.config(
                    text="PCAPdroid installed. Click ● Record again "
                         "(approve VPN on phone first time).",
                    fg="green"))
            else:
                msg = ((r.stderr or "") + (r.stdout or ""))[:300]
                post_ui(lambda m=msg: info.config(
                    text=f"install failed: {m}", fg="red"))

        def update_status():
            if wire_stop.is_set() or recording["name"] is None:
                return
            if not win.winfo_exists():
                return
            try:
                dur = int(time.time() - recording["started"])
                info.config(text=f"● recording {recording['name']} · {dur}s",
                            fg="red")
                win.after(1000, update_status)
            except tk.TclError:
                pass

        PCAP_DIR = "/sdcard/Download/PCAPdroid"

        def list_pcaps():
            r = adb("shell", "ls", "-1", PCAP_DIR, timeout=5)
            if r.returncode != 0:
                return set()
            return {ln.strip() for ln in (r.stdout or "").splitlines()
                    if ln.strip().endswith(".pcap")}

        def start_capture():
            if not is_pcapdroid_installed():
                offer_install(); return
            # Snapshot existing files so we can identify *our* new capture by diff,
            # regardless of what name PCAPdroid actually uses.
            before = list_pcaps()
            ts = time.strftime("%Y%m%d_%H%M%S")
            name = f"pc_{ts}.pcap"
            args = ["shell", "am", "start",
                    "-e", "action", "start",
                    "-e", "pcap_dump_mode", "pcap_file",
                    "-e", "pcap_name", name]
            flt = app_filter_var.get().strip()
            if flt:
                args += ["-e", "app_filter", flt]
            args += ["-n", PCAPDROID_CTL]
            self._log(f"wire: start intent (name={name}, filter={flt or '-'})")
            r = adb(*args, timeout=10)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "")[:400] or "failed"
                self._log(f"wire: start failed: {msg}")
                messagebox.showerror("start", msg)
                return
            recording["name"] = name
            recording["filter"] = flt
            recording["started"] = time.time()
            recording["before"] = before
            rec_btn.config(text="■ Stop & pull", fg="red")
            info.config(
                text=f"● recording {name} · 0s   (reproduce traffic on phone)",
                fg="red")
            update_status()

        def stop_and_pull_async():
            threading.Thread(target=stop_and_pull, daemon=True).start()

        def stop_and_pull():
            name = recording["name"]
            before = recording.get("before") or set()
            if not name:
                return
            self._log("wire: stop intent")
            post_ui(lambda: info.config(text="stopping capture…", fg="orange"))
            adb("shell", "am", "start",
                "-e", "action", "stop",
                "-n", PCAPDROID_CTL, timeout=10)
            # PCAPdroid flushes asynchronously — wait until a *new* pcap file
            # appears (vs the snapshot we took before recording).
            found = None
            new_files = set()
            self._log(f"wire: searching new pcap (baseline={len(before)} files)")
            for attempt in range(10):
                if wire_stop.is_set():
                    return
                after = list_pcaps()
                new_files = after - before
                if new_files:
                    # newest by timestamped name (lex sort works for pc_<ts>)
                    pick = sorted(new_files)[-1]
                    found = f"{PCAP_DIR}/{pick}"
                    self._log(f"wire: new file detected → {pick}")
                    break
                post_ui(lambda a=attempt: info.config(
                    text=f"waiting for pcap flush… ({a+1}/10)", fg="orange"))
                time.sleep(1.0)

            if not found:
                recording["name"] = None
                self._log("wire: no new pcap appeared in 10s")
                msg = ("no new pcap appeared. Check PCAPdroid → Settings → "
                       "Dump mode = 'PCAP file' (NOT 'PCAP socket'). "
                       "Verify dump dir = /sdcard/Download/PCAPdroid/.")
                post_ui(lambda m=msg: (
                    info.config(text=m, fg="red"),
                    rec_btn.config(text="● Record", fg="green"),
                ))
                return

            # Wait until file size stabilizes — PCAPdroid keeps writing for a
            # few seconds after `stop` until the dump worker actually finishes.
            # Pulling too early gives a truncated file (e.g. just the header
            # plus one packet).
            last_size = -1
            stable = 0
            final_size = 0
            for attempt in range(30):
                if wire_stop.is_set():
                    return
                r = adb("shell", "stat", "-c", "%s", found, timeout=5)
                try:
                    cur = int((r.stdout or "0").strip())
                except ValueError:
                    cur = 0
                if cur > 24 and cur == last_size:
                    stable += 1
                    if stable >= 2:
                        final_size = cur
                        break
                else:
                    stable = 0
                last_size = cur
                post_ui(lambda c=cur, a=attempt: info.config(
                    text=f"flushing pcap… {c} bytes (stable {stable}/2)",
                    fg="orange"))
                time.sleep(1.0)
            self._log(f"wire: file stabilized at {last_size} bytes")

            local_dir = os.path.join(HERE, "artifacts", "_pcap")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, os.path.basename(found))
            post_ui(lambda p=found: info.config(
                text=f"pulling {os.path.basename(p)} ({last_size} bytes)…",
                fg="orange"))
            try:
                adb("pull", found, local_path, timeout=120)
            except Exception as e:
                recording["name"] = None
                self._log(f"wire: pull err: {e}")
                post_ui(lambda er=e: (
                    info.config(text=f"pull err: {er}", fg="red"),
                    rec_btn.config(text="● Record", fg="green"),
                ))
                return
            self._log(f"wire: pulled → {local_path}")

            recording["name"] = None
            post_ui(lambda: (
                rec_btn.config(text="● Record", fg="green"),
                path_var.set(local_path),
                load(local_path),
                info.config(
                    text=(f"loaded {os.path.basename(local_path)} — "
                          f"{len(packets['all'])} packets"),
                    fg="green"),
            ))

        def toggle_record():
            if recording["name"] is None:
                start_capture()
            else:
                stop_and_pull_async()

        rec_btn.config(command=toggle_record)

        def help_msg():
            messagebox.showinfo(
                "PCAPdroid",
                "PCAPdroid is a free Android app that captures packets via a\n"
                "local VPN — no root needed.\n\n"
                "Install:\n"
                "  Play Store / F-Droid / github.com/emanuele-f/PCAPdroid\n\n"
                "Usage:\n"
                "  1. Open PCAPdroid → Settings → Dump mode = 'PCAP file'\n"
                "  2. (optional) restrict capture to one app under Target app\n"
                "  3. Tap Start → grant VPN permission → reproduce traffic\n"
                "  4. Tap Stop → pcap is written to /sdcard/Download/PCAPdroid/\n"
                "  5. Here, click 'Pull from device'\n\n"
                "Note: TLS-encrypted payloads stay encrypted unless you set up\n"
                "decryption in PCAPdroid (mitmproxy or per-app SSL key log).")
        tk.Button(bar, text="Help", command=help_msg).pack(side="left", padx=2)

        info = tk.Label(bar, text="", fg="#888", font=("Consolas", 9))
        info.pack(side="right", padx=4)

        # Filter
        flt_bar = tk.Frame(win); flt_bar.pack(fill="x", padx=6)
        tk.Label(flt_bar, text="Filter (substring of src/dst/proto/info):"
                 ).pack(side="left")
        flt_var = tk.StringVar()
        tk.Entry(flt_bar, textvariable=flt_var, width=40
                 ).pack(side="left", padx=4)

        # Packet list
        body = tk.Frame(win); body.pack(fill="both", expand=True, padx=6, pady=4)
        cols = ("idx", "time", "src", "dst", "proto", "len", "info")
        tv = ttk.Treeview(body, columns=cols, show="headings", height=22)
        widths = {"idx": 60, "time": 90, "src": 220, "dst": 220,
                  "proto": 60, "len": 60, "info": 320}
        for c in cols:
            tv.heading(c, text=c.upper())
            tv.column(c, width=widths[c],
                      anchor=("w" if c in ("src", "dst", "info") else "e"),
                      stretch=(c == "info"))
        sb = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)

        # Hex viewer (right)
        hex_lbl = tk.Label(win, text="Selected packet bytes",
                           font=("Segoe UI", 9, "bold"))
        hex_lbl.pack(anchor="w", padx=8, pady=(8, 0))
        hex_txt = scrolledtext.ScrolledText(win, height=10, font=("Consolas", 9),
                                            bg="#1a1a1a", fg="#ddd")
        hex_txt.pack(fill="x", padx=8, pady=(0, 8))
        hex_txt.config(state="disabled")

        packets = {"all": [], "shown": []}

        def render():
            tv.delete(*tv.get_children())
            q = flt_var.get().strip().lower()
            shown = []
            t0 = packets["all"][0]["ts"] if packets["all"] else 0
            for pkt in packets["all"]:
                hay = (str(pkt["src"]) + " " + str(pkt["dst"]) + " " +
                       str(pkt["proto"]) + " " + str(pkt["info"])).lower()
                if q and q not in hay:
                    continue
                shown.append(pkt)
                tv.insert("", "end", iid=str(pkt["idx"]), values=(
                    pkt["idx"],
                    f"{pkt['ts'] - t0:.3f}",
                    pkt["src"], pkt["dst"], pkt["proto"], pkt["length"],
                    pkt["info"][:120],
                ))
            packets["shown"] = shown
            info.config(text=f"{len(shown)} / {len(packets['all'])} packets",
                        fg="#888")

        def on_select(_e):
            sel = tv.selection()
            if not sel:
                return
            try:
                idx = int(sel[0])
            except ValueError:
                return
            pkt = next((p for p in packets["all"] if p["idx"] == idx), None)
            if pkt is None:
                return
            data = pkt["raw"]
            lines = []
            for i in range(0, len(data), 16):
                chunk = data[i:i+16]
                hexs = " ".join(f"{b:02x}" for b in chunk)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                lines.append(f"{i:04x}  {hexs:<48}  {asc}")
            hex_txt.config(state="normal")
            hex_txt.delete("1.0", "end")
            hex_txt.insert("1.0", "\n".join(lines))
            hex_txt.config(state="disabled")

        tv.bind("<<TreeviewSelect>>", on_select)
        flt_var.trace_add("write", lambda *_: render())

        # Open in external wireshark.exe if present
        def open_external():
            p = path_var.get().strip()
            if not p or not os.path.isfile(p):
                messagebox.showinfo("wireshark", "Open or pull a pcap first"); return
            for cand in [
                shutil.which("wireshark"),
                r"C:\Program Files\Wireshark\Wireshark.exe",
                r"C:\Program Files (x86)\Wireshark\Wireshark.exe",
            ]:
                if cand and os.path.isfile(cand):
                    subprocess.Popen([cand, p])
                    return
            messagebox.showinfo("wireshark",
                                "wireshark.exe not found on PATH or in standard install paths.\n"
                                "Install from https://www.wireshark.org/")
        tk.Button(bar, text="Open in Wireshark",
                  command=open_external).pack(side="left", padx=2)

        def load(p):
            try:
                pkts, err = self._parse_pcap(p)
            except Exception as e:
                messagebox.showerror("pcap", f"parse err: {e}"); return
            if err:
                messagebox.showwarning("pcap", err)
            packets["all"] = pkts
            render()

        def on_close():
            wire_stop.set()
            try:
                win.destroy()
            except tk.TclError:
                pass
        win.protocol("WM_DELETE_WINDOW", on_close)

    # ============================================================ SurfaceFlinger per-layer latency
    def _open_sf_latency(self, pkg):
        win = tk.Toplevel(self.root)
        win.title(f"Layer latency (SurfaceFlinger) — {pkg}")
        win.geometry("980x600")
        top = tk.Frame(win); top.pack(fill="x")
        tk.Label(top, text="Layers (filtered by package, click to inspect):",
                 fg="gray").pack(side="left", padx=4)
        tk.Button(top, text="Refresh list",
                  command=lambda: threading.Thread(target=load_list, daemon=True).start()
                  ).pack(side="right", padx=4)

        list_frame = tk.Frame(win); list_frame.pack(fill="x", padx=4)
        lb = tk.Listbox(list_frame, height=6, font=("Consolas", 9))
        lb.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        info = tk.Label(win, text="select a layer", fg="orange")
        info.pack(anchor="w", padx=4)

        canvas = tk.Canvas(win, bg="#111", highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=4, pady=4)

        all_layers = []
        latencies = []  # list[ms] for selected layer

        def load_list():
            raw = send_cmd("SF_LIST", timeout=10)
            if not raw or raw.startswith("ERR"):
                self.root.after(0, lambda r=raw: info.config(
                    text=f"sf_list err: {r}", fg="red"))
                return
            layers = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("==="):
                    continue
                # Filter by package keyword (substring match on layer name).
                if pkg.lower() in line.lower():
                    layers.append(line)
            if not layers:
                # Fallback: show everything if no filter match
                layers = [l.strip() for l in raw.splitlines()
                          if l.strip() and not l.startswith("===")][:200]
            all_layers[:] = layers
            self.root.after(0, lambda: [
                lb.delete(0, "end"),
                *[lb.insert("end", l) for l in layers],
                info.config(text=f"{len(layers)} layer(s) — click one", fg="green"),
            ])

        def on_select(_evt=None):
            sel = lb.curselection()
            if not sel:
                return
            layer = all_layers[sel[0]]
            threading.Thread(target=lambda: fetch_lat(layer), daemon=True).start()

        def fetch_lat(layer):
            raw = send_cmd(f"SF_LATENCY {layer}", timeout=10)
            if not raw or raw.startswith("ERR"):
                self.root.after(0, lambda r=raw: info.config(
                    text=f"latency err: {r}", fg="red"))
                return
            # First line = refresh period ns; subsequent lines = "app_vsync set present"
            ms_list = []
            lines = [l for l in raw.splitlines() if l.strip() and l != "===EOF==="]
            for line in lines[1:]:
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    app_vsync = int(parts[0])
                    present = int(parts[2])
                except ValueError:
                    continue
                if app_vsync <= 0 or present <= 0:
                    continue
                lat_ms = (present - app_vsync) / 1_000_000.0
                if -200 < lat_ms < 1000:
                    ms_list.append(lat_ms)
            latencies[:] = ms_list
            self.root.after(0, lambda: [
                info.config(text=f"layer: {layer[:80]}  ({len(ms_list)} samples)", fg="green"),
                redraw(),
            ])

        def redraw():
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w < 50 or h < 50 or not latencies:
                return
            deadline = 1000.0 / max(24.0, self.refresh_hz)
            max_ms = max(deadline * 3, max(latencies) * 1.15, 30.0)
            margin = 40
            plot_h = h - margin - 10
            # Deadline lines
            for ref, color, lbl in [(deadline, "#3a3", f"{self.refresh_hz:.0f}Hz"),
                                    (deadline * 2, "#dc7", "2x")]:
                y = h - margin - (ref / max_ms) * plot_h
                canvas.create_line(margin, y, w - 5, y, fill=color, dash=(3, 3))
                canvas.create_text(margin - 2, y, anchor="e", fill=color,
                                   text=lbl, font=("Consolas", 8))
            # Bars left-to-right (oldest → newest)
            n = len(latencies)
            bw = max(2.0, (w - margin - 10) / n)
            for i, ms in enumerate(latencies):
                if ms < 0:
                    continue  # buffer wasn't ready
                x = margin + i * bw
                bar_h = min(plot_h, (ms / max_ms) * plot_h)
                y_top = h - margin - bar_h
                color = ("#3a3" if ms < deadline else
                         ("#dc7" if ms < deadline * 2 else "#e44"))
                canvas.create_rectangle(x, y_top, x + bw - 1, h - margin,
                                        fill=color, width=0)
            # Stats
            valid = [m for m in latencies if m >= 0]
            if valid:
                p95 = sorted(valid)[max(0, int(len(valid) * 0.95) - 1)]
                avg = sum(valid) / len(valid)
                canvas.create_text(10, 10, anchor="nw", fill="#bbb",
                    font=("Consolas", 9),
                    text=f"n={len(valid)}  avg={avg:.1f}ms  p95={p95:.1f}ms")

        lb.bind("<<ListboxSelect>>", on_select)
        canvas.bind("<Configure>", lambda _e: redraw())
        threading.Thread(target=load_list, daemon=True).start()

    # ============================================================ binder / activity
    def _open_binder_view(self):
        win = tk.Toplevel(self.root)
        win.title("binder / activity processes")
        win.geometry("980x700")
        nb = ttk.Notebook(win); nb.pack(fill="both", expand=True)
        for label, cmd in [("binder", "BINDER_DUMP"),
                           ("activity processes", "ACTIVITY_PROCS")]:
            frame = tk.Frame(nb); nb.add(frame, text=label)
            bar = tk.Frame(frame); bar.pack(fill="x")
            info = tk.Label(bar, text="loading…", fg="orange"); info.pack(side="left", padx=4)
            tk.Button(bar, text="Refresh",
                      command=lambda c=cmd, t=None, i=info: load(c, t, i)
                      ).pack(side="right")
            txt = scrolledtext.ScrolledText(frame, font=("Consolas", 9), wrap="none")
            txt.pack(fill="both", expand=True)
            def load(c=cmd, t=txt, i=info):
                r = send_cmd(c, timeout=15)
                self.root.after(0, lambda: [
                    t.delete("1.0", "end"),
                    t.insert("end", r or "(empty)"),
                    i.config(text=f"{len(r or ''):,} chars", fg="green"),
                ])
            threading.Thread(target=load, daemon=True).start()

    # ============================================================ per-thread view
    def _open_threads(self, pid, pkg):
        win = tk.Toplevel(self.root)
        win.title(f"threads — pid {pid} · {pkg}")
        win.geometry("820x600")
        bar = tk.Frame(win); bar.pack(fill="x")
        paused = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause", variable=paused).pack(side="left")
        info = tk.Label(bar, text="sampling…", fg="orange"); info.pack(side="left", padx=8)
        hint = tk.Label(bar,
            text="hot=CPU>20%   wait=runqueue>10%",
            fg="gray", font=("Consolas", 8))
        hint.pack(side="right", padx=4)

        cols = ("tid", "state", "cpu", "wait", "name")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=24, selectmode="browse")
        widths = {"tid": 70, "state": 50, "cpu": 75, "wait": 75, "name": 420}
        anchors = {"tid": "e", "state": "c", "cpu": "e", "wait": "e", "name": "w"}
        headers = {"tid": "TID", "state": "ST", "cpu": "CPU%", "wait": "WAIT%", "name": "Thread name"}
        for c in cols:
            tv.heading(c, text=headers[c])
            tv.column(c, width=widths[c], anchor=anchors[c], stretch=(c == "name"))
        tv.tag_configure("hot", background="#ffe0e0", font=("Segoe UI", 9, "bold"))
        tv.tag_configure("wait", background="#fff0c0")
        sb = ttk.Scrollbar(win, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(fill="both", expand=True)

        stop = threading.Event()
        # Per-tid history: tid -> (cum_ticks, cum_wait_ns, ts_ns)
        history = {"last": None}

        def parse_raw(raw):
            ts_ns = None
            rows = []  # (tid, state, ticks, wait_ns, name)
            for line in raw.splitlines():
                if line.startswith("TIME "):
                    try:
                        ts_ns = int(line.split(None, 1)[1])
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("TS "):
                    # TS <tid> <wait_ns> <statline>
                    bits = line.split(None, 3)
                    if len(bits) < 4:
                        continue
                    _, tid, wstr, statline = bits
                    try:
                        wait_ns = int(wstr)
                    except ValueError:
                        continue
                    lp = statline.find("(")
                    rp = statline.rfind(")")
                    if lp < 0 or rp < 0:
                        continue
                    comm = statline[lp+1:rp]
                    tail = statline[rp+2:].split()
                    if len(tail) < 13:
                        continue
                    st = tail[0]
                    try:
                        utime = int(tail[11])
                        stime = int(tail[12])
                    except ValueError:
                        continue
                    rows.append((tid, st, utime + stime, wait_ns, comm))
            return ts_ns, rows

        # clk_tck is per OS — Linux/Android default 100 Hz → 10ms per tick = 1e7 ns
        TICK_NS = 10_000_000

        def sample():
            while not stop.is_set():
                if paused.get():
                    time.sleep(0.5)
                    continue
                raw = send_cmd(f"THREADS {pid}", timeout=6)
                if not raw or raw.startswith("ERR"):
                    self.root.after(0, lambda r=raw: info.config(
                        text=f"err: {str(r)[:60]}", fg="red"))
                    time.sleep(2)
                    continue
                ts_ns, rows = parse_raw(raw)
                if ts_ns is None:
                    time.sleep(1.5)
                    continue
                last = history["last"]
                display = []
                snap = {}
                for tid, st, ticks, wait_ns, name in rows:
                    cpu_pct = 0.0
                    wait_pct = 0.0
                    if last and tid in last:
                        p_ticks, p_wait, p_ts = last[tid]
                        dt = ts_ns - p_ts
                        if dt > 0:
                            d_ticks = ticks - p_ticks
                            cpu_pct = max(0.0, min(100.0, d_ticks * TICK_NS * 100.0 / dt))
                            d_wait = wait_ns - p_wait
                            wait_pct = max(0.0, min(100.0, d_wait * 100.0 / dt))
                    display.append((tid, st, cpu_pct, wait_pct, name))
                    snap[tid] = (ticks, wait_ns, ts_ns)
                history["last"] = snap
                display.sort(key=lambda r: (-r[2], -r[3]))
                self.root.after(0, lambda d=display: render(d))
                time.sleep(1.5)

        def render(rows):
            sel = tv.selection()
            sel_tid = None
            if sel:
                vals = tv.item(sel[0])["values"]
                if vals:
                    sel_tid = str(vals[0])
            tv.delete(*tv.get_children())
            for tid, st, cpu, wait, name in rows:
                tag = ()
                if cpu > 20:
                    tag = ("hot",)
                elif wait > 10:
                    tag = ("wait",)
                iid = tv.insert("", "end",
                                values=(tid, st, f"{cpu:.1f}", f"{wait:.1f}", name),
                                tags=tag)
                if sel_tid is not None and str(tid) == sel_tid:
                    tv.selection_set(iid)
            info.config(text=f"{len(rows)} threads · 1.5s sample", fg="green")

        def on_double(_e):
            sel = tv.selection()
            if not sel:
                return
            vals = tv.item(sel[0])["values"]
            if not vals:
                return
            tid = str(vals[0])
            tname = str(vals[4]) if len(vals) > 4 else "?"
            self._open_thread_detail(pid, tid, tname)
        tv.bind("<Double-Button-1>", on_double)
        # Hint at the new affordance
        tk.Label(win, text="double-click a row → per-TID detail (CPU/state/wchan/stack)",
                 fg="#888", font=("Consolas", 9)).pack(side="bottom", anchor="w", padx=4)

        threading.Thread(target=sample, daemon=True).start()

        def close():
            stop.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ per-thread detail
    def _open_thread_detail(self, pid, tid, name):
        """Time-series CPU%/wait%/state + wchan + (best-effort) kernel stack for a TID."""
        win = tk.Toplevel(self.root)
        win.title(f"thread {tid} ({name}) — pid {pid}")
        win.geometry("900x700")
        win.minsize(600, 400)

        # Persistent top toolbar (outside the scrollable area so it's always visible)
        top = tk.Frame(win); top.pack(fill="x", padx=8, pady=4)
        paused = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Pause", variable=paused).pack(side="left")
        status_lbl = tk.Label(top, text="sampling…", fg="orange", font=("Consolas", 9))
        status_lbl.pack(side="left", padx=8)

        def copy_to_clipboard(text):
            win.clipboard_clear()
            win.clipboard_append(text)
            win.update()  # keep on clipboard after window closes

        def copy_all():
            parts = [
                f"thread {tid} ({name}) — pid {pid}",
                "",
                meta_lbl.cget("text"),
                "",
                "--- kernel stack / wchan ---",
                stack_txt.get("1.0", "end"),
            ]
            copy_to_clipboard("\n".join(parts))
            status_lbl.config(text="copied (meta + stack) → clipboard", fg="green")

        def copy_stack():
            copy_to_clipboard(stack_txt.get("1.0", "end"))
            status_lbl.config(text="stack copied → clipboard", fg="green")

        tk.Button(top, text="Copy all", command=copy_all).pack(side="right", padx=2)
        tk.Button(top, text="Copy stack", command=copy_stack).pack(side="right", padx=2)

        # ----- Scrollable body -----
        body_wrap = tk.Frame(win)
        body_wrap.pack(fill="both", expand=True)
        body_canvas = tk.Canvas(body_wrap, highlightthickness=0,
                                bg=win.cget("bg"))
        body_sb = ttk.Scrollbar(body_wrap, orient="vertical",
                                command=body_canvas.yview)
        body_canvas.configure(yscrollcommand=body_sb.set)
        body_sb.pack(side="right", fill="y")
        body_canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(body_canvas)
        body_id = body_canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync_inner(_e=None):
            body_canvas.configure(scrollregion=body_canvas.bbox("all"))
        body.bind("<Configure>", _sync_inner)

        def _sync_width(e):
            body_canvas.itemconfigure(body_id, width=e.width)
        body_canvas.bind("<Configure>", _sync_width)

        # Mouse-wheel scrolling only while pointer is over our window.
        def _on_mw(e):
            try:
                if win.winfo_exists():
                    body_canvas.yview_scroll(-1 * (e.delta // 120), "units")
            except tk.TclError:
                pass
        win.bind("<Enter>", lambda _e: win.bind_all("<MouseWheel>", _on_mw))
        win.bind("<Leave>", lambda _e: win.unbind_all("<MouseWheel>"))

        meta_lbl = tk.Label(body, text="", font=("Consolas", 9),
                            justify="left", anchor="w")
        meta_lbl.pack(fill="x", padx=8, pady=(4, 0))

        tk.Label(body, text="CPU% (red) · runqueue WAIT% (yellow)  — 1Hz sample, ~5min window",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(8, 0))
        chart = tk.Canvas(body, height=160, bg="#0e0e0e", highlightthickness=0)
        chart.pack(fill="x", padx=8)

        tk.Label(body, text="State (R run · S sleep · D disk-wait · Z zombie · T stop · I idle)",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(8, 0))
        state_strip = tk.Canvas(body, height=24, bg="#0e0e0e", highlightthickness=0)
        state_strip.pack(fill="x", padx=8)

        tk.Label(body, text="wchan & kernel stack (root may be required for stack)",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(8, 0))
        stack_txt = scrolledtext.ScrolledText(body, height=18, font=("Consolas", 9))
        stack_txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Right-click context menu on the stack/wchan area
        stack_menu = tk.Menu(stack_txt, tearoff=0)
        stack_menu.add_command(label="Copy selection",
                               command=lambda: copy_to_clipboard(
                                   stack_txt.selection_get()
                                   if stack_txt.tag_ranges("sel") else ""))
        stack_menu.add_command(label="Copy all",
                               command=lambda: copy_to_clipboard(
                                   stack_txt.get("1.0", "end")))
        stack_txt.bind("<Button-3>", lambda e: stack_menu.tk_popup(e.x_root, e.y_root))

        history = collections.deque(maxlen=300)  # (ts, cpu_pct, wait_pct, state)
        prev = {"ticks": None, "wait_ns": None, "ts_ns": None}
        TICK_NS = 10_000_000
        STATE_COLOR = {
            "R": "#4ad991", "S": "#5b8bff", "D": "#ff5252",
            "Z": "#ff6bd6", "T": "#cf9f3a", "t": "#cf9f3a",
            "I": "#888888", "X": "#444444", "K": "#888888",
            "P": "#888888", "?": "#333333",
        }
        stop = threading.Event()

        def parse(raw):
            sections = {"STAT": "", "SCHED": "", "WCHAN": "", "STATUS": "", "STACK": ""}
            cur = None
            for line in raw.splitlines():
                if line.startswith("===") and line.endswith("==="):
                    cur = line.strip("=").strip()
                    continue
                if cur in sections:
                    sections[cur] += line + "\n"
            # stat parsing
            comm = state = "?"
            ticks = None
            stat = sections["STAT"].strip()
            lp = stat.find("(")
            rp = stat.rfind(")")
            if lp >= 0 and rp > lp:
                comm = stat[lp+1:rp]
                tail = stat[rp+2:].split()
                if len(tail) >= 13:
                    state = tail[0]
                    try:
                        ticks = int(tail[11]) + int(tail[12])
                    except ValueError:
                        ticks = None
            # schedstat: <run_ns> <wait_ns> <pcount>
            wait_ns = None
            sc = sections["SCHED"].strip().split()
            if len(sc) >= 2:
                try:
                    wait_ns = int(sc[1])
                except ValueError:
                    pass
            wchan = sections["WCHAN"].strip() or "—"
            # status: voluntary_ctxt_switches / nonvoluntary_ctxt_switches
            vctx = nvctx = "?"
            for ln in sections["STATUS"].splitlines():
                if ln.startswith("voluntary_ctxt_switches:"):
                    vctx = ln.split(":", 1)[1].strip()
                elif ln.startswith("nonvoluntary_ctxt_switches:"):
                    nvctx = ln.split(":", 1)[1].strip()
            stack = sections["STACK"].strip()
            return {
                "comm": comm, "state": state, "ticks": ticks, "wait_ns": wait_ns,
                "wchan": wchan, "vctx": vctx, "nvctx": nvctx, "stack": stack,
            }

        def redraw():
            chart.delete("all")
            state_strip.delete("all")
            w = chart.winfo_width(); h = chart.winfo_height()
            if w < 10 or h < 10 or not history:
                return
            # grid
            for pct in (25, 50, 75):
                y = h - int(h * pct / 100)
                chart.create_line(0, y, w, y, fill="#222")
                chart.create_text(4, y - 6, anchor="w", text=f"{pct}%",
                                  fill="#444", font=("Consolas", 8))
            n = len(history)
            step = max(1.0, w / max(1, n - 1)) if n > 1 else 0
            # CPU line
            pts_cpu = []
            pts_wait = []
            for i, (_ts, cpu, wait, _st) in enumerate(history):
                x = i * step
                pts_cpu.append((x, h - h * cpu / 100))
                pts_wait.append((x, h - h * wait / 100))
            if len(pts_cpu) >= 2:
                flat = [c for p in pts_cpu for c in p]
                chart.create_line(*flat, fill="#ff5252", width=2)
                flat = [c for p in pts_wait for c in p]
                chart.create_line(*flat, fill="#ffd34a", width=1)
            # State strip
            sw = state_strip.winfo_width(); sh = state_strip.winfo_height()
            if sw > 0 and n > 0:
                bar_w = max(1.0, sw / n)
                for i, (_ts, _cpu, _wait, st) in enumerate(history):
                    color = STATE_COLOR.get(st, STATE_COLOR["?"])
                    state_strip.create_rectangle(
                        i * bar_w, 0, (i + 1) * bar_w, sh,
                        fill=color, outline="")

        def loop():
            while not stop.is_set():
                if paused.get():
                    time.sleep(0.5); continue
                cmd = (
                    "SH echo ===STAT===; cat /proc/" + pid + "/task/" + tid + "/stat 2>/dev/null; "
                    "echo ===SCHED===; cat /proc/" + pid + "/task/" + tid + "/schedstat 2>/dev/null; "
                    "echo ===WCHAN===; cat /proc/" + pid + "/task/" + tid + "/wchan 2>/dev/null; "
                    "echo ===STATUS===; cat /proc/" + pid + "/task/" + tid + "/status 2>/dev/null; "
                    "echo ===STACK===; cat /proc/" + pid + "/task/" + tid + "/stack 2>/dev/null"
                )
                raw = send_cmd(cmd, timeout=6)
                if not raw or raw.startswith("ERR"):
                    self.root.after(0, lambda r=raw: status_lbl.config(
                        text=f"err: {str(r)[:60]}", fg="red"))
                    time.sleep(2); continue
                try:
                    d = parse(raw)
                except Exception as e:
                    self.root.after(0, lambda er=e: status_lbl.config(
                        text=f"parse err: {er}", fg="red"))
                    time.sleep(2); continue
                ts_ns = time.time_ns()
                cpu_pct = wait_pct = 0.0
                if (prev["ticks"] is not None and prev["ts_ns"] is not None
                        and d["ticks"] is not None):
                    dt = ts_ns - prev["ts_ns"]
                    if dt > 0:
                        d_ticks = d["ticks"] - prev["ticks"]
                        cpu_pct = max(0.0, min(100.0, d_ticks * TICK_NS * 100.0 / dt))
                        if d["wait_ns"] is not None and prev["wait_ns"] is not None:
                            d_wait = d["wait_ns"] - prev["wait_ns"]
                            wait_pct = max(0.0, min(100.0, d_wait * 100.0 / dt))
                prev["ticks"] = d["ticks"]
                prev["wait_ns"] = d["wait_ns"]
                prev["ts_ns"] = ts_ns
                history.append((ts_ns, cpu_pct, wait_pct, d["state"]))

                stack_show = d["stack"] if d["stack"] and "Permission denied" not in d["stack"] \
                    else "(kernel stack unavailable — needs root on this device)"
                meta_text = (
                    f"name={d['comm']}   state={d['state']}   wchan={d['wchan']}\n"
                    f"voluntary_ctxt_switches={d['vctx']}   nonvoluntary={d['nvctx']}\n"
                    f"CPU%={cpu_pct:.1f}   WAIT%={wait_pct:.1f}   samples={len(history)}"
                )

                def apply(mt=meta_text, st=stack_show):
                    meta_lbl.config(text=mt)
                    stack_txt.delete("1.0", "end")
                    stack_txt.insert("1.0", st)
                    status_lbl.config(text=f"sampling 1Hz · {len(history)} pts", fg="green")
                    redraw()

                self.root.after(0, apply)
                time.sleep(1.0)

        chart.bind("<Configure>", lambda _e: redraw())
        state_strip.bind("<Configure>", lambda _e: redraw())

        threading.Thread(target=loop, daemon=True).start()

        def close():
            stop.set()
            try:
                win.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ jank timeline
    # Frame pipeline stages — name + (start_col, end_col) + color.
    # Δ in ms decomposes FrameCompleted - IntendedVsync into where the time actually went.
    JANK_STAGES = [
        ("input",      "HandleInputStart",       "AnimationStart",         "#4ec9ff"),
        ("animation",  "AnimationStart",         "PerformTraversalsStart", "#b48cff"),
        ("traversal",  "PerformTraversalsStart", "DrawStart",              "#ff9f43"),
        ("draw",       "DrawStart",              "SyncQueued",             "#ffd34a"),
        ("sync wait",  "SyncQueued",             "SyncStart",              "#888888"),
        ("issue cmds", "SyncStart",              "IssueDrawCommandsStart", "#ff6bd6"),
        ("gpu work",   "IssueDrawCommandsStart", "SwapBuffers",            "#4ad991"),
        ("gpu swap",   "SwapBuffers",            "FrameCompleted",         "#5b8bff"),
    ]

    def _open_jank(self, pkg):
        win = tk.Toplevel(self.root)
        win.title(f"jank timeline — {pkg}")
        win.geometry("1180x540")
        bar = tk.Frame(win); bar.pack(fill="x")
        paused = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause", variable=paused).pack(side="left")
        tk.Button(bar, text="Reset", command=lambda: state["frames"].clear() or redraw()
                  ).pack(side="left", padx=4)
        tk.Button(bar, text="Layer stats (SurfaceFlinger)",
                  command=lambda: self._open_sf_latency(pkg)).pack(side="left", padx=4)
        tk.Button(bar, text="All Frames",
                  command=lambda: open_all_frames()).pack(side="left", padx=4)
        uia_auto = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Auto UIA on jank", variable=uia_auto).pack(side="left", padx=8)
        uia_lbl = tk.Label(bar, text="UIA: 0", fg="gray", font=("Consolas", 9))
        uia_lbl.pack(side="left")
        info = tk.Label(bar, text="sampling…", fg="orange"); info.pack(side="left", padx=8)
        stats_lbl = tk.Label(bar, text="", font=("Consolas", 9), fg="gray")
        stats_lbl.pack(side="right", padx=8)

        body = tk.Frame(win); body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg="#111", highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)

        # Right detail panel — populated when user clicks a bar
        detail = tk.Frame(body, width=360, bg="#181818")
        detail.pack(side="right", fill="y")
        detail.pack_propagate(False)
        tk.Label(detail, text="Click a bar for stage breakdown",
                 bg="#181818", fg="#888", font=("Segoe UI", 9, "italic")
                 ).pack(anchor="w", padx=8, pady=6)
        detail_canvas = tk.Canvas(detail, bg="#181818", highlightthickness=0)
        detail_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        state = {"frames": []}      # rolling list[(fc_ns, dur_ms, stages_dict)]
        MAX_FRAMES = 240
        stop = threading.Event()
        seen = set()
        selected_idx = {"i": None}

        # UIA throttling state — at most one dump per 30s. Triggered from sampler
        # when a janky frame appears AND uia_auto is checked.
        uia_count = {"n": 0, "last": 0.0}

        def trigger_uia_dump(dur_ms_int, fc):
            now = time.time()
            if now - uia_count["last"] < 30:
                return
            uia_count["last"] = now
            threading.Thread(
                target=lambda: do_uia_dump(dur_ms_int, fc),
                daemon=True,
            ).start()

        def do_uia_dump(dur_ms_int, fc):
            ts = time.strftime("%Y%m%d_%H%M%S")
            if self.session_dir:
                out_dir = os.path.join(self.session_dir, "uia")
            else:
                out_dir = os.path.join(HERE, "artifacts", "_uia")
            remote = "/sdcard/_uia_pc.xml"
            try:
                os.makedirs(out_dir, exist_ok=True)
                adb("shell", "uiautomator", "dump", remote, timeout=15)
                local = os.path.join(out_dir, f"uia_{ts}_fc{fc}_{dur_ms_int}ms.xml")
                adb("pull", remote, local, timeout=15)
                adb("shell", "rm", "-f", remote, timeout=5)
                node_count = -1
                pkg_name = "?"
                try:
                    with open(local, encoding="utf-8") as fp:
                        data = fp.read()
                    node_count = data.count("<node ")
                    m = re.search(r'package="([^"]+)"', data)
                    if m:
                        pkg_name = m.group(1)
                except Exception:
                    pass
                uia_count["n"] += 1
                txt = f"UIA: {uia_count['n']} (last {pkg_name}, {node_count} nodes)"
                self.root.after(0, lambda: uia_lbl.config(text=txt, fg="#4ec9ff"))
                self.root.after(0, lambda p=local: self._log(f"uiautomator dump → {p}"))
            except Exception as e:
                self.root.after(0, lambda err=e: self._log(f"uia dump failed: {err}"))

        # ----- All Frames table window — shares state["frames"] -----
        all_frames_win = {"w": None}

        def open_all_frames():
            if all_frames_win["w"] is not None:
                try:
                    all_frames_win["w"].lift()
                    return
                except tk.TclError:
                    all_frames_win["w"] = None
            af = tk.Toplevel(win)
            af.title(f"All Frames — {pkg}")
            af.geometry("820x520")
            all_frames_win["w"] = af

            info_lbl = tk.Label(af, text="", font=("Consolas", 9), fg="gray")
            info_lbl.pack(fill="x", padx=8, pady=4)

            cols = ("idx", "t", "total", "ui", "rt", "status", "dominant")
            tv = ttk.Treeview(af, columns=cols, show="headings", height=22)
            headers = {"idx": "#", "t": "t(s)", "total": "Total ms",
                       "ui": "UI ms", "rt": "RT ms",
                       "status": "Status", "dominant": "Dominant stage"}
            widths = {"idx": 50, "t": 70, "total": 80, "ui": 70, "rt": 70,
                      "status": 80, "dominant": 240}
            anchors = {"idx": "e", "t": "e", "total": "e", "ui": "e", "rt": "e",
                       "status": "w", "dominant": "w"}
            for c in cols:
                tv.heading(c, text=headers[c])
                tv.column(c, width=widths[c], anchor=anchors[c],
                          stretch=(c == "dominant"))
            tv.tag_configure("ontime", foreground="#4ad991")
            tv.tag_configure("slow",   foreground="#cf9f3a")
            tv.tag_configure("janky",  foreground="#ff5252")
            tv.tag_configure("frozen", foreground="#8b1414")
            sb = ttk.Scrollbar(af, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            tv.pack(fill="both", expand=True, padx=8, pady=4)

            follow = tk.BooleanVar(value=True)
            bot = tk.Frame(af); bot.pack(fill="x", padx=8, pady=(0, 6))
            tk.Checkbutton(bot, text="Follow newest", variable=follow).pack(side="left")
            tk.Button(bot, text="Close", command=lambda: af_close()).pack(side="right")

            af_stop = threading.Event()

            def refresh():
                frames = list(state["frames"])
                if not frames:
                    info_lbl.config(text="no frames yet")
                    return
                deadline_ms = 1000.0 / max(24.0, self.refresh_hz)
                t0 = frames[0]["fc"]
                tv.delete(*tv.get_children())
                miss1 = 0
                for i, fr in enumerate(frames):
                    dur = fr["dur"]
                    if dur < deadline_ms:
                        tag, status = "ontime", "on-time"
                    elif dur < deadline_ms * 2:
                        tag, status = "slow", "slow"
                    elif dur < 700:
                        tag, status = "janky", "janky"
                        miss1 += 1
                    else:
                        tag, status = "frozen", "frozen"
                        miss1 += 1
                    iv, sq, ss, fc = fr["iv"], fr["sync_q"], fr["sync_s"], fr["fc"]
                    ui_ms = (sq - iv) / 1_000_000.0 if sq > iv else 0.0
                    rt_ms = (fc - ss) / 1_000_000.0 if ss > 0 and fc > ss else 0.0
                    stages = fr.get("stages") or {}
                    if stages:
                        name, val = max(stages.items(), key=lambda kv: kv[1])
                        dom = f"{name} ({val:.1f}ms)"
                    else:
                        dom = "—"
                    t_rel = (fc - t0) / 1_000_000_000.0
                    tv.insert("", "end", values=(
                        i + 1, f"{t_rel:.2f}", f"{dur:.1f}",
                        f"{ui_ms:.1f}", f"{rt_ms:.1f}", status, dom,
                    ), tags=(tag,))
                if follow.get():
                    items = tv.get_children()
                    if items:
                        tv.see(items[-1])
                info_lbl.config(
                    text=f"{len(frames)} frames · deadline {deadline_ms:.1f}ms"
                         f" · janky/frozen {miss1}")

            def loop():
                while not af_stop.is_set():
                    try:
                        af.after(0, refresh)
                    except tk.TclError:
                        break
                    time.sleep(1.0)

            def af_close():
                af_stop.set()
                all_frames_win["w"] = None
                try:
                    af.destroy()
                except tk.TclError:
                    pass

            af.protocol("WM_DELETE_WINDOW", af_close)
            threading.Thread(target=loop, daemon=True).start()
            refresh()

        def parse_framestats(raw):
            """Return list of frame dicts with absolute ns timestamps + stage durations."""
            out = []
            in_data = False
            cols = None
            for line in raw.splitlines():
                if line.startswith("---PROFILEDATA---"):
                    in_data = not in_data
                    cols = None
                    continue
                if not in_data:
                    continue
                if cols is None:
                    cols = [c.strip() for c in line.split(",")]
                    continue
                vals = line.split(",")
                if len(vals) < len(cols):
                    continue
                row = dict(zip(cols, vals))
                try:
                    iv = int(row.get("IntendedVsync", "0"))
                    fc = int(row.get("FrameCompleted", "0"))
                    sync_q = int(row.get("SyncQueued", "0"))
                    sync_s = int(row.get("SyncStart", "0"))
                except ValueError:
                    continue
                if iv == 0 or fc <= iv:
                    continue
                dur_ms = (fc - iv) / 1_000_000.0
                if not (0 < dur_ms < 1000):
                    continue
                stages = {}
                for label, c_start, c_end, _color in self.JANK_STAGES:
                    try:
                        s = int(row[c_start])
                        e = int(row[c_end])
                        if s > 0 and e >= s:
                            stages[label] = (e - s) / 1_000_000.0
                    except (KeyError, ValueError):
                        pass
                # UI thread span: iv → sync_q  (Choreographer + traversal + draw)
                # RT span:        sync_s → fc (RenderThread sync + GPU work + swap)
                out.append({
                    "fc": fc, "iv": iv, "sync_q": sync_q, "sync_s": sync_s,
                    "dur": dur_ms, "stages": stages,
                })
            return out

        def sampler():
            nonlocal seen
            while not stop.is_set():
                if paused.get():
                    time.sleep(0.5)
                    continue
                raw = send_cmd(f"JANK {pkg}", timeout=10)
                if raw and not raw.startswith("ERR"):
                    frames = parse_framestats(raw)
                    if not frames:
                        self.root.after(0, lambda: info.config(
                            text="no frame data (app must be foreground & drawing)",
                            fg="orange"))
                    new_count = 0
                    for fr in frames:
                        if fr["fc"] in seen:
                            continue
                        seen.add(fr["fc"])
                        state["frames"].append(fr)
                        new_count += 1
                    if len(state["frames"]) > MAX_FRAMES:
                        state["frames"] = state["frames"][-MAX_FRAMES:]
                    if len(seen) > MAX_FRAMES * 4:
                        seen = set(fr["fc"] for fr in state["frames"])
                    if new_count > 0:
                        jank_thresh = 2 * 1000.0 / max(24.0, self.refresh_hz)
                        for fr in state["frames"][-new_count:]:
                            if fr["dur"] >= jank_thresh:
                                # Feed the main chart's jank-marker buffer
                                self.jank_events.append((time.time(), fr["dur"]))
                                if uia_auto.get():
                                    trigger_uia_dump(int(fr["dur"]), fr["fc"])
                                    break
                        self.root.after(0, redraw)
                time.sleep(1.5)

        # Per-frame hit geometry — list of (x0, y0, x1, y1, frame_idx, lane)
        # lane: "ui" or "rt"
        bar_geom = []
        # Time window shown on canvas, in ns. Auto-scrolls so newest frame at right.
        TIME_WINDOW_NS = 5_000_000_000  # 5 seconds

        def redraw():
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w < 10 or h < 10:
                return
            frames = state["frames"]
            n = len(frames)
            if n == 0:
                canvas.create_text(w // 2, h // 2, fill="#666",
                                   text="waiting for frames…", font=("Segoe UI", 11))
                return
            deadline_ms = 1000.0 / max(24.0, self.refresh_hz)
            deadline_ns = int(deadline_ms * 1_000_000)

            # Layout — Android Studio inspired:
            #   ┌─────────────────────────────────────────────────────────┐
            #   │ stage legend  ───  vsync ticks  ───        donut summary │
            #   │ ─────────────────────────────────────────────────────── │
            #   │ UI ▮▮ ▮▮▮ ▮▮ ▮▮▮▮ ▮▮ ▮▮ ▮▮                          │
            #   │ RT  ▮▮ ▮▮ ▮▮▮ ▮▮ ▮▮ ▮▮▮▮ ▮▮                       │
            #   │ ───────────────────────────────────────────────────────│
            #   │ −5s                                              now    │
            #   └─────────────────────────────────────────────────────────┘
            legend_h = 110     # top area for legend + donut
            axis_h = 18
            margin_left = 60
            margin_right = 12
            plot_top = legend_h
            plot_bottom = h - axis_h - 4
            plot_h = plot_bottom - plot_top
            lane_h = max(20, (plot_h - 6) / 2)
            ui_y0 = plot_top + 4
            ui_y1 = ui_y0 + lane_h
            rt_y0 = ui_y1 + 6
            rt_y1 = rt_y0 + lane_h

            t_now = max(fr["fc"] for fr in frames)
            t_min = t_now - TIME_WINDOW_NS
            ns_per_px = TIME_WINDOW_NS / max(1, w - margin_left - margin_right)

            def t2x(ns_t):
                return margin_left + (ns_t - t_min) / ns_per_px

            # Background lanes
            canvas.create_rectangle(margin_left, ui_y0, w - margin_right, ui_y1,
                                    fill="#1a1a1a", outline="#2a2a2a")
            canvas.create_rectangle(margin_left, rt_y0, w - margin_right, rt_y1,
                                    fill="#1a1a1a", outline="#2a2a2a")
            canvas.create_text(margin_left - 6, (ui_y0 + ui_y1) // 2, anchor="e",
                               fill="#aaa", font=("Consolas", 9, "bold"), text="UI")
            canvas.create_text(margin_left - 6, (rt_y0 + rt_y1) // 2, anchor="e",
                               fill="#aaa", font=("Consolas", 9, "bold"), text="RT")

            # Vsync deadline ticks — vertical dashed lines at every deadline.
            # Align to the most-recent IntendedVsync so ticks land where they should.
            anchor_iv = frames[-1]["iv"]
            t_start = anchor_iv - ((anchor_iv - t_min) // deadline_ns) * deadline_ns
            t = t_start
            while t < t_now + deadline_ns:
                x = t2x(t)
                if margin_left <= x <= w - margin_right:
                    canvas.create_line(x, plot_top, x, plot_bottom + 4,
                                       fill="#2c2c2c", dash=(2, 3))
                t += deadline_ns

            # Determine per-frame color from total duration
            def frame_color(dur_ms):
                if dur_ms < deadline_ms:
                    return "#3a8a3a"           # on-time green
                elif dur_ms < deadline_ms * 2:
                    return "#cf9f3a"           # slow yellow
                elif dur_ms < 700:
                    return "#d04040"           # janky red
                else:
                    return "#8b1414"           # frozen dark red

            # Cumulative for donut
            cum_stage = {name: 0.0 for name, _, _, _ in self.JANK_STAGES}
            visible_count = 0
            bar_geom.clear()

            for idx, fr in enumerate(frames):
                fc = fr["fc"]
                iv = fr["iv"]
                sync_q = fr["sync_q"]
                sync_s = fr["sync_s"]
                dur = fr["dur"]
                stages = fr["stages"]

                # Skip frames outside visible window
                if fc < t_min:
                    continue
                visible_count += 1
                for name in cum_stage:
                    cum_stage[name] += stages.get(name, 0)

                fcolor = frame_color(dur)
                ui_end = sync_q if sync_q > iv else (sync_s if sync_s > iv else fc)
                rt_start = sync_s if sync_s > 0 else sync_q
                rt_end = fc

                # UI box
                ux0 = max(margin_left, t2x(iv))
                ux1 = min(w - margin_right, t2x(ui_end))
                if ux1 > ux0:
                    canvas.create_rectangle(ux0, ui_y0 + 3, ux1, ui_y1 - 3,
                                            fill=fcolor, width=0)
                    if selected_idx["i"] == idx:
                        canvas.create_rectangle(ux0, ui_y0 + 3, ux1, ui_y1 - 3,
                                                outline="#fff", width=2)
                    bar_geom.append((ux0, ui_y0, ux1, ui_y1, idx, "ui"))

                # RT box
                if rt_start > 0 and rt_end > rt_start:
                    rx0 = max(margin_left, t2x(rt_start))
                    rx1 = min(w - margin_right, t2x(rt_end))
                    if rx1 > rx0:
                        canvas.create_rectangle(rx0, rt_y0 + 3, rx1, rt_y1 - 3,
                                                fill=fcolor, width=0)
                        if selected_idx["i"] == idx:
                            canvas.create_rectangle(rx0, rt_y0 + 3, rx1, rt_y1 - 3,
                                                    outline="#fff", width=2)
                        bar_geom.append((rx0, rt_y0, rx1, rt_y1, idx, "rt"))

                # Sync-wait connector — thin line between UI end and RT start
                if sync_q > 0 and sync_s > sync_q:
                    cx0 = t2x(sync_q)
                    cx1 = t2x(sync_s)
                    if cx1 > cx0 and margin_left <= cx0 and cx1 <= w - margin_right:
                        cy = (ui_y1 + rt_y0) // 2
                        canvas.create_line(cx0, ui_y1 - 3, cx0, cy,
                                           fill="#666", width=1)
                        canvas.create_line(cx1, cy, cx1, rt_y0 + 3,
                                           fill="#666", width=1)
                        canvas.create_line(cx0, cy, cx1, cy,
                                           fill="#666", dash=(2, 2))

            # Time axis labels
            axis_y = plot_bottom + 12
            canvas.create_line(margin_left, plot_bottom + 2, w - margin_right, plot_bottom + 2,
                               fill="#444")
            for ratio, label in [(0.0, "−5s"), (0.25, "−3.75s"), (0.5, "−2.5s"),
                                 (0.75, "−1.25s"), (1.0, "now")]:
                x = margin_left + ratio * (w - margin_left - margin_right)
                canvas.create_line(x, plot_bottom + 2, x, plot_bottom + 6, fill="#666")
                canvas.create_text(x, axis_y, anchor="n", fill="#888",
                                   font=("Consolas", 8), text=label)

            # Legend (top-left)
            lx = 8
            ly = 6
            canvas.create_text(lx, ly, anchor="nw", fill="#ccc",
                font=("Segoe UI", 8, "bold"),
                text=f"deadline {deadline_ms:.1f}ms @ {self.refresh_hz:.0f}Hz")
            ly += 14
            for col, txt in [("#3a8a3a", "on-time"), ("#cf9f3a", "slow (1–2× deadline)"),
                             ("#d04040", "janky (≥2×)"), ("#8b1414", "frozen (≥700ms)")]:
                canvas.create_rectangle(lx, ly + 2, lx + 12, ly + 10,
                                        fill=col, width=0)
                canvas.create_text(lx + 16, ly + 2, anchor="nw",
                                   fill="#aaa", font=("Consolas", 8), text=txt)
                ly += 12

            # Stage legend (continued, top-center)
            lx2 = 240
            ly2 = 6
            canvas.create_text(lx2, ly2, anchor="nw", fill="#ccc",
                font=("Segoe UI", 8, "bold"),
                text="pipeline stages (in detail panel):")
            ly2 += 14
            for name, _, _, color in self.JANK_STAGES:
                canvas.create_rectangle(lx2, ly2 + 2, lx2 + 10, ly2 + 10,
                                        fill=color, width=0)
                canvas.create_text(lx2 + 14, ly2 + 2, anchor="nw",
                                   fill="#888", font=("Consolas", 8), text=name)
                ly2 += 11

            # Donut (top-right)
            total_cum = sum(cum_stage.values())
            if total_cum > 0 and visible_count > 0:
                cx = w - 80
                cy = legend_h // 2
                r_outer = 42
                r_inner = 22
                start = 90
                for name, _, _, color in self.JANK_STAGES:
                    ms = cum_stage[name]
                    if ms <= 0:
                        continue
                    extent = -360.0 * ms / total_cum
                    canvas.create_arc(cx - r_outer, cy - r_outer,
                                      cx + r_outer, cy + r_outer,
                                      start=start, extent=extent,
                                      fill=color, outline="#111", width=1,
                                      style="pieslice")
                    start += extent
                canvas.create_oval(cx - r_inner, cy - r_inner,
                                   cx + r_inner, cy + r_inner,
                                   fill="#111", outline="#111")
                canvas.create_text(cx, cy - 5, anchor="c", fill="#ccc",
                                   font=("Consolas", 9), text=f"{visible_count}")
                canvas.create_text(cx, cy + 7, anchor="c", fill="#777",
                                   font=("Consolas", 7), text="frames")
                dom = max(cum_stage.items(), key=lambda kv: kv[1])
                if dom[1] > 0:
                    pct = dom[1] * 100 / total_cum
                    canvas.create_text(cx, cy + r_outer + 8, anchor="c",
                        fill="#bbb", font=("Consolas", 8),
                        text=f"dom: {dom[0]} ({pct:.0f}%)")

            # Stats
            durations = [fr["dur"] for fr in frames]
            miss1 = sum(1 for d in durations if d >= deadline_ms)
            miss2 = sum(1 for d in durations if d >= deadline_ms * 2)
            p95 = sorted(durations)[max(0, int(n * 0.95) - 1)] if n else 0
            stats_lbl.config(text=(
                f"n={n}  p95={p95:.1f}ms  miss={miss1}({miss1*100//max(1,n)}%)  ≥2×={miss2}"))
            info.config(text=f"package: {pkg}  ({self.refresh_hz:.0f}Hz, deadline {deadline_ms:.1f}ms)",
                        fg="green")

        def render_detail(idx):
            detail_canvas.delete("all")
            frames = state["frames"]
            if idx is None or idx >= len(frames):
                return
            _, dur, stages = frames[idx]
            dw = detail_canvas.winfo_width()
            dh = detail_canvas.winfo_height()
            if dw < 50 or dh < 50:
                return
            # Header
            detail_canvas.create_text(10, 10, anchor="nw", fill="#eee",
                font=("Segoe UI", 10, "bold"),
                text=f"Frame #{idx}    total {dur:.1f}ms")
            detail_canvas.create_text(10, 28, anchor="nw", fill="#888",
                font=("Consolas", 8),
                text=f"deadline {1000/self.refresh_hz:.1f}ms @ {self.refresh_hz:.0f}Hz  "
                     f"({'OK' if dur < 1000/self.refresh_hz else 'JANK'})")
            # Stage waterfall — horizontal bars proportional to ms
            y = 60
            stage_h = 18
            stage_gap = 6
            label_w = 110
            bar_x0 = label_w + 10
            bar_w = max(40, dw - bar_x0 - 60)
            # Find max stage for scaling
            stage_max = max((stages.get(name, 0) for name, _, _, _ in self.JANK_STAGES), default=1)
            stage_max = max(stage_max, dur / 2, 5.0)
            # Identify dominant stage
            dominant = max(stages.items(), key=lambda kv: kv[1])[0] if stages else None
            for name, _, _, stage_color in self.JANK_STAGES:
                ms = stages.get(name, 0)
                is_dom = name == dominant and ms > 0
                # Color square for stage identity
                detail_canvas.create_rectangle(10, y + 4, 22, y + stage_h - 4,
                    fill=stage_color, width=0)
                # Label
                txt_color = "#fff" if is_dom else "#ccc"
                font_style = ("Consolas", 9, "bold") if is_dom else ("Consolas", 9)
                detail_canvas.create_text(28, y + stage_h // 2, anchor="w",
                    fill=txt_color, font=font_style, text=name)
                # Bar track
                ratio = ms / stage_max if stage_max > 0 else 0
                bw_px = bar_w * ratio
                detail_canvas.create_rectangle(bar_x0, y, bar_x0 + bar_w, y + stage_h,
                    fill="#222", outline="#333")
                if bw_px > 0:
                    detail_canvas.create_rectangle(bar_x0, y, bar_x0 + bw_px, y + stage_h,
                        fill=stage_color, width=0)
                # Value
                detail_canvas.create_text(bar_x0 + bar_w + 4, y + stage_h // 2,
                    anchor="w", fill=txt_color, font=font_style,
                    text=f"{ms:.1f}ms")
                y += stage_h + stage_gap
            # Interpretation hint
            y += 8
            if dominant:
                hints = {
                    "input":     "input dispatch slow — main thread handler busy?",
                    "animation": "animation callbacks heavy",
                    "traversal": "measure/layout/draw traversal — view hierarchy depth or overdraw",
                    "draw":      "RecordingCanvas — Canvas/View.onDraw 비용",
                    "sync wait": "RenderThread busy from prior frame",
                    "issue cmds": "GL/Vulkan 명령 발행 비용",
                    "gpu work":  "GPU 처리 — shader/texture upload/overdraw",
                    "gpu swap":  "buffer swap / display compositor 대기",
                }
                detail_canvas.create_text(10, y, anchor="nw", fill="#bbb",
                    font=("Segoe UI", 9), width=dw - 20,
                    text=f"→ dominant: {dominant}\n   {hints.get(dominant, '')}")

        def on_click(e):
            x, y = e.x, e.y
            best = None
            best_dist = 999999
            for x0, y0, x1, y1, idx, lane in bar_geom:
                if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                    # closest bar (handles overlapping narrow bars)
                    cx = (x0 + x1) / 2
                    d = abs(x - cx)
                    if d < best_dist:
                        best_dist = d
                        best = idx
            if best is not None:
                selected_idx["i"] = best
                render_detail(best)
                redraw()

        def on_hover(e):
            # Tooltip-like hover showing frame summary on the bar under cursor
            x, y = e.x, e.y
            frames = state["frames"]
            for x0, y0, x1, y1, idx, lane in bar_geom:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    fr = frames[idx]
                    info.config(text=(f"frame #{idx} · {fr['dur']:.1f}ms · "
                                      f"hover lane: {lane.upper()}"),
                                fg="#ffae42")
                    return
            info.config(text=f"package: {pkg}  ({self.refresh_hz:.0f}Hz)", fg="green")

        canvas.bind("<Button-1>", on_click)
        canvas.bind("<Motion>", on_hover)
        detail_canvas.bind("<Configure>", lambda _e: render_detail(selected_idx["i"]))

        canvas.bind("<Configure>", lambda _e: redraw())
        threading.Thread(target=sampler, daemon=True).start()

        def close():
            stop.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

    # ============================================================ stats parsing
    def _apply_stats(self, raw):
        cpu_pct = None
        mem_pct = None
        cpu_temp = None
        bat = {}
        thermal_lines = []
        freqs = {}
        gpu_mem = None
        gpu_model = None
        gpu_busy = None  # 0..100
        gpu_freq_mhz = None
        pwr_cur_ua = None   # microamps (can be negative when discharging on some kernels)
        pwr_volt_uv = None  # microvolts
        pwr_now_uw = None   # microwatts (Qualcomm power_now if present)

        for line in raw.splitlines():
            parts = line.split()
            if not parts:
                continue
            k = parts[0]
            if k == "CPU" and len(parts) >= 8:
                vals = list(map(int, parts[1:8]))  # u,n,s,i,iow,irq,sirq
                idle = vals[3] + vals[4]
                total = sum(vals)
                if self._last_cpu is not None:
                    di = idle - self._last_cpu[1]
                    dt = total - self._last_cpu[0]
                    if dt > 0:
                        cpu_pct = max(0.0, min(100.0, (dt - di) * 100.0 / dt))
                self._last_cpu = (total, idle)
            elif k == "MEM" and len(parts) >= 3:
                tot, avail = int(parts[1]), int(parts[2])
                if tot > 0:
                    mem_pct = (tot - avail) * 100.0 / tot
            elif k == "TZ" and len(parts) >= 4:
                zid, name, temp = parts[1], parts[2], int(parts[3])
                c = temp / 1000.0
                thermal_lines.append((name, c))
                if name.startswith("cpu-0-0") or name.startswith("cpu-1-0"):
                    if cpu_temp is None or c > cpu_temp:
                        cpu_temp = c
            elif k == "CPUFREQ" and len(parts) >= 3:
                freqs[int(parts[1])] = int(parts[2])
            elif k == "BAT_LEVEL":
                bat["level"] = parts[1]
            elif k == "BAT_TEMP":
                bat["temp"] = int(parts[1]) / 10.0
            elif k == "BAT_VOLT":
                bat["volt"] = int(parts[1]) / 1000.0
            elif k == "BAT_USB":
                bat["usb"] = parts[1] == "true"
            elif k == "GPU_MEM":
                try:
                    gpu_mem = int(parts[1])
                except Exception:
                    pass
            elif k == "GPU_MODEL":
                gpu_model = parts[1] if len(parts) > 1 else ""
            elif k == "GPU_BUSY" and len(parts) >= 3:
                src = parts[1]
                vals = parts[2:]
                # Two-number forms: cumulative (busy, total). Single-number: instantaneous %.
                if src == "adreno" and len(vals) >= 2:
                    try:
                        b, t = int(vals[0]), int(vals[1])
                    except ValueError:
                        b = t = None
                    if b is not None:
                        if self._last_gpu_busy is not None:
                            db = b - self._last_gpu_busy[0]
                            dt = t - self._last_gpu_busy[1]
                            if dt > 0 and db >= 0:
                                gpu_busy = max(0.0, min(100.0, db * 100.0 / dt))
                        self._last_gpu_busy = (b, t)
                elif len(vals) >= 2 and vals[0].isdigit() and vals[1].isdigit():
                    # devfreq cumulative "<busy> <total>"
                    b, t = int(vals[0]), int(vals[1])
                    if self._last_gpu_busy is not None:
                        db = b - self._last_gpu_busy[0]
                        dt = t - self._last_gpu_busy[1]
                        if dt > 0 and db >= 0:
                            gpu_busy = max(0.0, min(100.0, db * 100.0 / dt))
                    self._last_gpu_busy = (b, t)
                else:
                    try:
                        v = float(vals[0])
                        gpu_busy = max(0.0, min(100.0, v))
                    except ValueError:
                        pass
            elif k == "GPU_FREQ" and len(parts) >= 2:
                try:
                    f = int(parts[1])
                    # Normalize to MHz. Common units: Hz (>1e6), kHz (>1e3), MHz (<1e4).
                    if f > 1_000_000:
                        gpu_freq_mhz = f // 1_000_000
                    elif f > 10_000:
                        gpu_freq_mhz = f // 1_000
                    else:
                        gpu_freq_mhz = f
                except ValueError:
                    pass
            elif k == "PWR_CUR" and len(parts) >= 2:
                try: pwr_cur_ua = int(parts[1])
                except ValueError: pass
            elif k == "PWR_VOLT" and len(parts) >= 2:
                try: pwr_volt_uv = int(parts[1])
                except ValueError: pass
            elif k == "PWR_NOW" and len(parts) >= 2:
                try: pwr_now_uw = int(parts[1])
                except ValueError: pass

        def apply():
            if cpu_pct is not None:
                self.cpu_var.set(f"{cpu_pct:.1f}%")
                self.cpu_var._bar["value"] = cpu_pct
            if mem_pct is not None:
                self.mem_var.set(f"{mem_pct:.1f}%")
                self.mem_var._bar["value"] = mem_pct
            if cpu_temp is not None:
                self.temp_var.set(f"{cpu_temp:.1f}°C")
                # scale: 30-90°C
                pct = max(0, min(100, (cpu_temp - 30) * 100 / 60))
                self.temp_var._bar["value"] = pct
            if bat:
                lvl = bat.get("level", "?")
                t = bat.get("temp", "?")
                plug = " ⚡" if bat.get("usb") else ""
                self.bat_var.set(f"{lvl}%{plug} {t}°C")
                try:
                    self.bat_var._bar["value"] = float(lvl)
                except Exception:
                    pass
            if gpu_mem is not None or gpu_model or gpu_busy is not None or gpu_freq_mhz is not None:
                mb = (gpu_mem or 0) / 1024 / 1024
                parts_disp = []
                if gpu_model:
                    parts_disp.append(gpu_model)
                if gpu_busy is not None:
                    parts_disp.append(f"{gpu_busy:.0f}%")
                if gpu_freq_mhz is not None:
                    parts_disp.append(f"{gpu_freq_mhz}MHz")
                if mb > 0:
                    parts_disp.append(f"{mb:.0f}MB")
                self.gpu_var.set(" ".join(parts_disp) or "—")
                self.gpu_var._bar["value"] = gpu_busy if gpu_busy is not None else 0
            # Power: prefer kernel-reported power_now (µW); else compute V × I.
            # current_now sign convention varies — some kernels positive=charging,
            # others positive=discharging. Use magnitude.
            mw = None
            if pwr_now_uw is not None:
                mw = abs(pwr_now_uw) / 1000.0
            elif pwr_cur_ua is not None and pwr_volt_uv is not None:
                mw = abs(pwr_cur_ua) * pwr_volt_uv / 1e9  # µA × µV → pW; /1e9 = mW
            if mw is not None:
                self.pwr_var.set(f"{mw:.0f}mW")
                # Scale 0–10W onto 0–100 bar
                self.pwr_var._bar["value"] = max(0, min(100, mw / 100))
                self._pwr_seen = True
            else:
                self._pwr_polls += 1
                if self._pwr_polls == 3 and not self._pwr_seen:
                    self.pwr_var.set("n/a")
                    self._log(
                        "POWER: kernel power sysfs unavailable on this device "
                        "(needs root/userdebug). Click the ⓘ next to POWER for details."
                    )
        # Build sample dict — used for both the rolling chart buffer and JSONL.
        mw_calc = None
        if pwr_now_uw is not None:
            mw_calc = abs(pwr_now_uw) / 1000.0
        elif pwr_cur_ua is not None and pwr_volt_uv is not None:
            mw_calc = abs(pwr_cur_ua) * pwr_volt_uv / 1e9
        sample = {
            "ts": time.time(),
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "cpu_temp": cpu_temp,
            "gpu_busy": gpu_busy,
            "gpu_freq_mhz": gpu_freq_mhz,
            "power_mw": mw_calc,
            "cpu_freqs": freqs,
            "bat": bat,
            "thermal": [{"name": n, "c": c} for n, c in thermal_lines],
        }
        self.stats_history.append(sample)
        if self.session_dir:
            self._regression_check(sample)
        # Thermal hysteresis — enter at THRESH, exit 3°C below to avoid flicker.
        if cpu_temp is not None:
            if not self._thermal_state and cpu_temp >= self.THERMAL_THRESH:
                self._thermal_state = True
                self.thermal_events.append((sample["ts"], "in"))
            elif self._thermal_state and cpu_temp < self.THERMAL_THRESH - 3:
                self._thermal_state = False
                self.thermal_events.append((sample["ts"], "out"))
        self.root.after(0, self._redraw_chart)
        if self.session_dir:
            self._jsonl_write("realtime", sample)
        for i in range(8):
            f = freqs.get(i)
            self.core_labels[i].config(
                text=f"C{i}: {f/1000:.0f}" if f else f"C{i}: —")
        if thermal_lines:
            thermal_lines.sort(key=lambda x: -x[1])
            self.therm_text.config(
                text="\n".join(f"{n:24s} {c:5.1f}°C" for n, c in thermal_lines[:6]))
        self.root.after(0, apply)

    # ============================================================ setup
    def _setup_thr(self):
        threading.Thread(target=self._setup, daemon=True).start()

    def _refresh_devices(self):
        threading.Thread(target=self._auto_connect, daemon=True).start()

    def _auto_connect(self):
        """On startup / refresh: enum devices, populate combobox, pick first,
        then try lightweight (forward+ping); fall back to full setup."""
        global _active_serial
        self.root.after(0, lambda: self._set_status("scanning devices...", "orange"))
        devices = adb_list_devices()
        if not devices:
            _active_serial = None
            self.root.after(0, lambda: [
                self._device_map.clear(),
                self.device_combo.configure(values=[]),
                self.device_var.set(""),
                self._log("auto-connect: no adb device — plug in phone & enable USB debug"),
                self._set_status("no device", "red"),
            ])
            return
        # Populate combobox; keep selection if still attached
        labels = [lbl for _, lbl in devices]
        prev_serial = _active_serial
        keep = prev_serial and any(s == prev_serial for s, _ in devices)
        target_serial, target_label = (
            next((s, l) for s, l in devices if s == prev_serial)
            if keep else devices[0]
        )
        _active_serial = target_serial
        self._device_map = {lbl: s for s, lbl in devices}

        def update_ui():
            self.device_combo.configure(values=labels)
            self.device_var.set(target_label)
        self.root.after(0, update_ui)
        self._log_async(f"auto-connect: {len(devices)} device(s) → using {target_label}")
        self._connect_active()

    def _connect_active(self):
        """Forward + ping against current _active_serial. Falls back to full setup."""
        try:
            adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", timeout=5)
            for _ in range(3):
                if send_cmd("PING", timeout=2) == "PONG":
                    self.root.after(0, lambda: [
                        self._log("connect: ok (daemon already up)"),
                        self._set_status("connected", "green"),
                        self.refresh_shot(),
                    ])
                    self._detect_refresh_rate()
                    return
                time.sleep(0.5)
            self.root.after(0, lambda: self._log("connect: ping failed → full setup"))
            self._setup()
            self._detect_refresh_rate()
        except Exception as e:
            self.root.after(0, lambda: [
                self._log(f"connect err: {e}"),
                self._set_status("setup failed", "red"),
            ])

    def _detect_refresh_rate(self):
        """Query DISPLAY and pick the highest plausible refresh rate. Used as jank baseline."""
        raw = send_cmd("DISPLAY", timeout=4)
        if not raw or raw.startswith("ERR"):
            return
        import re
        candidates = []
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:fps|Hz)", raw, re.IGNORECASE):
            try:
                v = float(m.group(1))
                if 24 <= v <= 240:
                    candidates.append(v)
            except ValueError:
                pass
        # Also catch "refresh-rate=120.0" without unit
        for m in re.finditer(r"refresh[- ]?rate=(\d+(?:\.\d+)?)", raw, re.IGNORECASE):
            try:
                v = float(m.group(1))
                if 24 <= v <= 240:
                    candidates.append(v)
            except ValueError:
                pass
        if candidates:
            self.refresh_hz = max(candidates)
            self.root.after(0, lambda: self._log(
                f"display refresh rate: {self.refresh_hz:.0f}Hz "
                f"(jank deadline {1000/self.refresh_hz:.1f}ms)"))
        # Permission probe — used to explain why some sysfs reads fail
        raw_p = send_cmd("GETENFORCE", timeout=4)
        if raw_p and not raw_p.startswith("ERR"):
            enforce = (raw_p.splitlines() or ["?"])[0].strip()
            self.root.after(0, lambda e=enforce: self._log(
                f"selinux={e}  (root needed for some power/sysfs reads)"))

    def _on_device_select(self, _evt=None):
        global _active_serial
        label = self.device_var.get()
        serial = self._device_map.get(label)
        if not serial or serial == _active_serial:
            return
        _active_serial = serial
        self._log(f"device switched → {label}")
        # Reset transient per-device state
        self._last_cpu = None
        self._last_gpu_busy = None
        threading.Thread(target=self._connect_active, daemon=True).start()

    def _log_async(self, msg):
        self.root.after(0, lambda: self._log(msg))

    def _setup(self):
        steps = []
        try:
            self.root.after(0, lambda: self._set_status("setting up...", "orange"))
            for fn in ("daemon.sh", "handler.sh"):
                normalize_lf(os.path.join(HERE, fn))

            r = adb("devices")
            if "device" not in (r.stdout or ""):
                raise RuntimeError("adb devices: no device")
            steps.append("device ok")

            for fn in ("daemon.sh", "handler.sh"):
                adb("push", os.path.join(HERE, fn), f"/data/local/tmp/{fn}")
            adb("shell", "chmod 755 /data/local/tmp/daemon.sh /data/local/tmp/handler.sh")
            steps.append("pushed")

            kill = (
                "for pid in $(ps -ef 2>/dev/null | "
                "awk '/daemon\\.sh|handler\\.sh|toybox nc -L|nc -L/ && !/awk/ {print $2}'); "
                "do kill -9 $pid 2>/dev/null; done; "
                "rm -f /data/local/tmp/daemon.log; true"
            )
            adb("shell", kill)
            time.sleep(0.6)

            adb("forward", "--remove", f"tcp:{PORT}", capture=True)
            adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
            steps.append(f"forward {PORT}")

            adb("shell", f"setsid sh /data/local/tmp/daemon.sh "
                          f"</dev/null >>/data/local/tmp/daemon.log 2>&1 &")
            steps.append("daemon up")

            time.sleep(0.8)
            for _ in range(3):
                if send_cmd("PING", timeout=3) == "PONG":
                    break
                time.sleep(0.6)
            else:
                raise RuntimeError("ping never returned PONG")
            steps.append("ping ok")

            self.root.after(0, lambda: [
                self._log("Setup OK:\n  " + " → ".join(steps)),
                self._set_status("connected", "green"),
                self.refresh_shot(),
            ])
        except Exception as e:
            msg = f"Setup FAIL after {steps}\n  → {e}"
            self.root.after(0, lambda: [
                self._log(msg),
                self._set_status("setup failed", "red"),
                messagebox.showerror("Setup failed", msg),
            ])

    # ============================================================ shutdown
    # ============================================================ keyboard capture
    # Map: Tk keysym → Android keyevent code. Only special / non-printable keys.
    _SPECIAL_KEYS = {
        "Return": "ENTER",
        "KP_Enter": "ENTER",
        "BackSpace": "DEL",
        "Tab": "TAB",
        "Escape": "BACK",
        "Left": "DPAD_LEFT",
        "Right": "DPAD_RIGHT",
        "Up": "DPAD_UP",
        "Down": "DPAD_DOWN",
        "Home": "MOVE_HOME",
        "End": "MOVE_END",
        "Delete": "FORWARD_DEL",
        "Prior": "PAGE_UP",
        "Next": "PAGE_DOWN",
        "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4",
        "F6": "F6", "F7": "F7", "F8": "F8", "F9": "F9",
        "F10": "F10", "F11": "F11", "F12": "F12",
        # Convenience global hotkeys (mapped to phone hardware keys):
        # F5 → refresh shot (PC-side action, handled separately)
    }

    def _toggle_kbd(self):
        if self.kbd_var.get():
            self.root.bind_all("<KeyPress>", self._on_key)
            self._set_status("connected (keyboard ON)", "green")
        else:
            try:
                self.root.unbind_all("<KeyPress>")
            except Exception:
                pass

    def _on_key(self, e):
        # Pass through to Entry/Text/Combobox so the controls themselves still work.
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return  # let widget handle
        try:
            if isinstance(focused, ttk.Combobox):
                return
        except Exception:
            pass

        keysym = e.keysym
        char = e.char

        # Local hotkey: F5 refresh
        if keysym == "F5":
            self.refresh_shot()
            return "break"

        # Special key → keyevent
        code = self._SPECIAL_KEYS.get(keysym)
        if code:
            self._async(f"KEY {code}")
            return "break"

        # Modifier-only events (Shift, Ctrl, Alt by itself) → ignore
        if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                      "Alt_L", "Alt_R", "Super_L", "Super_R", "Caps_Lock",
                      "Num_Lock", "Scroll_Lock"):
            return "break"

        # Printable char (includes Hangul / IME-composed chars).
        # ASCII → fast `input text`; non-ASCII → ADBKeyboard broadcast.
        if char and (char.isprintable() or char == " "):
            if ord(char[0]) < 128:
                self._async(f"TEXT {char}")
            else:
                self._async(f"IME_TEXT {char}")
            return "break"

    def _on_close(self):
        self._stop.set()
        self._stop_video_proc()
        self.root.after(150, self.root.destroy)


def main():
    root = tk.Tk()
    if not ADB:
        root.withdraw()
        messagebox.showerror(
            "adb.exe not found",
            "Could not locate adb.exe.\n\n"
            "Searched:\n"
            f"  1. {os.path.join(HERE, 'vendor', 'platform-tools', 'adb.exe')}\n"
            "  2. %LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe\n"
            "  3. PATH\n\n"
            "Reinstall Phone Controller or install Android platform-tools "
            "from https://developer.android.com/tools/releases/platform-tools",
        )
        root.destroy()
        return
    PhoneController(root)
    root.mainloop()


if __name__ == "__main__":
    main()
