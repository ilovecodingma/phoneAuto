"""
Phone Controller GUI — live mirror + system monitor.

Layout:
  [screenshot panel] | [controls] | [stats + processes]

Workers (daemon threads, stopped by self._stop event on window close):
  - screen worker: when live mode on, polls SHOT at the configured interval
  - stats worker:  polls STATS every 1.5s, parses, updates labels
  - procs worker:  polls PROCS every 2.5s, updates process text
"""
import io
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from PIL import Image, ImageTk

try:
    import av  # PyAV — H.264 decoder
    HAVE_AV = True
except ImportError:
    HAVE_AV = False

ADB = os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")
HOST = "127.0.0.1"
PORT = 8889
PREVIEW_W = 320
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))


def adb(*args, capture=True, timeout=10):
    return subprocess.run(
        [ADB, *args],
        capture_output=capture,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


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

        self._build_ui()
        self._start_workers()

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
        self.fps_label = tk.Label(bar, text="0.0 fps", fg="gray", width=10, anchor="w")
        self.fps_label.pack(side="left", padx=8)
        self.status = tk.Label(bar, text="●  not connected", fg="gray")
        self.status.pack(side="right")

        # Main 3 columns
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=4, pady=4)

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
        metrics.columnconfigure(2, weight=1)

        # CPU progress bars per core
        cores = tk.LabelFrame(right, text="CPU cores (freq MHz)")
        cores.pack(fill="x", pady=6)
        self.core_labels = []
        for i in range(8):
            lab = tk.Label(cores, text=f"C{i}: —", font=("Consolas", 9),
                           width=12, anchor="w")
            lab.grid(row=i // 4, column=i % 4, padx=2, pady=1, sticky="w")

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

    def _metric_row(self, parent, row, label, unit):
        tk.Label(parent, text=label, width=10, anchor="w").grid(row=row, column=0, sticky="w", padx=4, pady=1)
        v = tk.StringVar(value="—")
        tk.Label(parent, textvariable=v, font=("Segoe UI", 11, "bold"),
                 width=10, anchor="e").grid(row=row, column=1, sticky="e", padx=2)
        bar = ttk.Progressbar(parent, length=180, maximum=100)
        bar.grid(row=row, column=2, sticky="ew", padx=4)
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
        while not self._stop.is_set():
            raw = send_cmd("STATS", timeout=5)
            if raw and not raw.startswith("ERR"):
                self._apply_stats(raw)
            time.sleep(1.5)

    def _procs_loop(self):
        while not self._stop.is_set():
            raw = send_cmd("APPS", timeout=5)
            if raw and not raw.startswith("ERR"):
                self.root.after(0, lambda r=raw: self._set_apps(r))
            time.sleep(2.5)

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

    def _open_logcat(self, pid, pkg):
        win = tk.Toplevel(self.root)
        win.title(f"logcat — pid {pid} · {pkg}")
        win.geometry("980x600")
        bar = tk.Frame(win); bar.pack(fill="x")
        paused = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause", variable=paused).pack(side="left")
        tk.Button(bar, text="Clear", command=lambda: txt.delete("1.0", "end")).pack(side="left")
        tk.Label(bar, text="  Filter:").pack(side="left")
        filt = tk.Entry(bar, width=30); filt.pack(side="left", padx=2)
        info = tk.Label(bar, text="streaming…", fg="green"); info.pack(side="right")

        txt = scrolledtext.ScrolledText(win, font=("Consolas", 9), wrap="none", bg="#111", fg="#ddd")
        txt.pack(fill="both", expand=True)
        for tag, color in [("V", "#888"), ("D", "#69c"), ("I", "#7c7"),
                           ("W", "#dc8"), ("E", "#e66"), ("F", "#f33")]:
            txt.tag_configure(tag, foreground=color)

        proc = subprocess.Popen(
            [ADB, "logcat", "-v", "time", "-T", "100", f"--pid={pid}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            bufsize=1, universal_newlines=False,
        )

        stop = threading.Event()

        def reader():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    if stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace")
                    self.root.after(0, append, line)
            except Exception:
                pass

        def append(line):
            if paused.get():
                return
            f = filt.get().strip()
            if f and f not in line:
                return
            tag = ""
            # logcat format: "MM-DD hh:mm:ss.sss L/tag(pid): msg"
            #  ^^^ level letter is at position offset ~18 after "-v time"
            parts = line.split(None, 3)
            if len(parts) >= 3 and len(parts[2]) >= 1:
                lvl = parts[2][0]
                if lvl in "VDIWEF":
                    tag = lvl
            txt.insert("end", line, tag)
            if int(txt.index("end-1c").split(".")[0]) > 5000:
                txt.delete("1.0", "1000.0")
            txt.see("end")

        threading.Thread(target=reader, daemon=True).start()

        def close():
            stop.set()
            try: proc.kill()
            except Exception: pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

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
                    [ADB] + adb_args, capture_output=True, text=True, timeout=30,
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
            if gpu_mem is not None or gpu_model:
                mb = (gpu_mem or 0) / 1024 / 1024
                self.gpu_var.set(f"{gpu_model or '—'} {mb:.0f}MB")
                self.gpu_var._bar["value"] = 0  # no busy% without root
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
    PhoneController(root)
    root.mainloop()


if __name__ == "__main__":
    main()
