import os, sys, json, time, threading, subprocess, platform, shutil
from aqt import mw, gui_hooks
from aqt.qt import (
    QSystemTrayIcon, QMenu, QAction, QIcon, QPixmap,
    QPainter, QColor, QTimer, Qt, QShortcut, QKeySequence
)

# Load local packages from the Packages subfolder
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(ADDON_DIR, "Packages")
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

IPC_PATH = os.path.expanduser("~/.force_focus.json")
IS_WINDOWS = platform.system() == "Windows"

# Import Windows-specific packages
if IS_WINDOWS:
    import psutil
    from plyer import notification
    import win32gui, win32con, win32api, win32process, win32com.client

# --- Desktop Providers ---
class BaseProvider:
    def get_active_info(self): return ""
    def force_focus_anki(self): pass
    def notify(self, msg):
        dur = str((ff.config.get("notification_duration", 2) or 2) * 1000)
        try:
            subprocess.run(["notify-send", "-t", dur, "Force Focus", msg], capture_output=True)
        except Exception:
            print(f"NOTIFICATION: {msg}")

class WindowsProvider(BaseProvider):
    def get_active_info(self):
        try:
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return f"{psutil.Process(pid).name().lower()} {win32gui.GetWindowText(hwnd).lower()}"
        except Exception:
            return ""

    def force_focus_anki(self):
        cfg = ff.config if ff.initialized else {}
        hwnds = []
        def cb(hwnd, res):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if t.endswith(" - Anki") or t == "Anki":
                    res.append(hwnd)
        win32gui.EnumWindows(cb, hwnds)
        if hwnds:
            hwnd = hwnds[0]
            if cfg.get("WINDOWS_UNMINIMIZE", True):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            shell = win32com.client.Dispatch("WScript.Shell")
            shell.SendKeys('%')
            win32gui.SetForegroundWindow(hwnd)
            if cfg.get("WINDOWS_FULLSCREEN", False):
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME)
                win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
                mon = win32api.GetMonitorInfo(
                    win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
                )['Monitor']
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOP, mon[0], mon[1],
                    mon[2] - mon[0], mon[3] - mon[1], win32con.SWP_SHOWWINDOW
                )
            elif cfg.get("WINDOWS_MAXIMIZE", True):
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)

    def notify(self, msg):
        dur = ff.config.get("notification_duration", 2) or 2
        notification.notify(title="Force Focus", message=msg, timeout=dur)

class HyprlandProvider(BaseProvider):
    def get_active_info(self):
        try:
            out = subprocess.check_output(["hyprctl", "activewindow", "-j"], text=True)
            d = json.loads(out)
            return f"{d.get('class', '')} {d.get('title', '')}".lower()
        except Exception:
            return ""

    def force_focus_anki(self):
        subprocess.run(["hyprctl", "dispatch", "focuswindow", "class:anki"], capture_output=True)

class SwayProvider(BaseProvider):
    def get_active_info(self):
        try:
            tree = json.loads(subprocess.check_output(["swaymsg", "-t", "get_tree"], text=True))
            def find_f(n):
                if n.get("focused"):
                    return f"{n.get('app_id') or ''} {n.get('window_properties', {}).get('class', '')}".lower()
                for c in n.get("nodes", []) + n.get("floating_nodes", []):
                    res = find_f(c)
                    if res:
                        return res
                return None
            return find_f(tree) or ""
        except Exception:
            return ""

    def force_focus_anki(self):
        subprocess.run(["swaymsg", '[app_id="(?i)anki" or class="(?i)anki"] focus'], capture_output=True)

class NiriProvider(BaseProvider):
    def _run_json(self, *args):
        out = subprocess.check_output(["niri", "msg", "--json", *args], text=True)
        return json.loads(out)

    def _unwrap(self, payload):
        if isinstance(payload, dict) and "Ok" in payload:
            return payload["Ok"]
        return payload

    def _windows_list(self):
        try:
            payload = self._unwrap(self._run_json("windows"))
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for key in ("Windows", "windows"):
                    if key in payload and isinstance(payload[key], list):
                        return payload[key]
        except Exception:
            pass
        return []

    def get_active_info(self):
        try:
            payload = self._unwrap(self._run_json("focused-window"))
            if isinstance(payload, dict):
                wnd = payload.get("FocusedWindow") or payload.get("focused-window") or payload
                if isinstance(wnd, dict):
                    return f"{wnd.get('app_id', '')} {wnd.get('title', '')}".lower()
        except Exception:
            pass
        return ""

    def force_focus_anki(self):
        try:
            windows = self._windows_list()
            matches = []
            for w in windows:
                app_id = str(w.get("app_id", "")).lower()
                title = str(w.get("title", "")).lower()
                if "anki" in app_id or "anki" in title:
                    matches.append(w)

            if not matches:
                return

            target = next((w for w in matches if w.get("is_focused")), matches[0])
            wid = target.get("id")
            if wid is None:
                return

            subprocess.run(
                ["niri", "msg", "action", "focus-window", "--id", str(wid)],
                capture_output=True
            )
        except Exception:
            pass

class DriftwmProvider(BaseProvider):
    """driftwm is an infinite-canvas compositor: windows sit at a fixed
    (x, y) on an infinite canvas and the screen is just a camera looking
    at it — there's no "restore/maximize" concept like other WMs. So
    instead we: focus the Anki window, pan the camera to its position
    (so it's actually inside the viewport — zoom is left untouched), and
    optionally force driftwm's fullscreen *viewport mode* on top.
    """

    def _msg(self, *args, timeout=2):
        try:
            return subprocess.run(
                ["driftwm", "msg", *args],
                capture_output=True, text=True, timeout=timeout
            )
        except Exception:
            return None

    def get_active_info(self):
        res = self._msg("focus")
        if res is None or res.returncode != 0:
            return ""
        return res.stdout.strip().lower()

    def _window_position(self, selector):
        # "move" with no x/y is a read: prints the window's current
        # center position as "<x> <y>". Kept as raw strings (rather than
        # parsed floats) so they're passed through to `camera` unchanged.
        res = self._msg("move", selector)
        if res is None or res.returncode != 0:
            return None
        parts = res.stdout.split()
        if len(parts) >= 2:
            return parts[0], parts[1]
        return None

    def force_focus_anki(self):
        cfg = ff.config if ff.initialized else {}

        focused = self._msg("focus", "anki")
        if focused is None or focused.returncode != 0:
            return  # no window with "anki" in its app_id right now

        # Pan the viewport to Anki's position. Deliberately not using the
        # "center-window" action here, since that also resets zoom to 1.0
        # and zoom should be left alone.
        pos = self._window_position("anki")
        if pos is not None:
            self._msg("camera", pos[0], pos[1])

        if cfg.get("DRIFTWM_FULLSCREEN", False):
            # driftwm's fullscreen is a viewport mode, not a per-window
            # flag, and "toggle-fullscreen" is the only lever IPC exposes
            # for it — any canvas action (including the focus/camera calls
            # above) already drops fullscreen, so by the time we get here
            # it should be off, meaning this toggle reliably turns it ON
            # rather than flipping an already-active fullscreen back off.
            self._msg("action", "toggle-fullscreen")

class AwesomeProvider(BaseProvider):
    def _run(self, *args):
        return subprocess.check_output(list(args), text=True, stderr=subprocess.DEVNULL)

    def get_active_info(self):
        for cmd in (
            ["xdotool", "getactivewindow", "getwindowclassname", "getwindowname"],
            ["wmctrl", "-lx"],
        ):
            try:
                out = self._run(*cmd)
                return out.lower().replace("\n", " ")
            except Exception:
                continue
        return ""

    def force_focus_anki(self):
        for cmd in (
            ["xdotool", "search", "--class", "anki", "windowactivate", "--sync"],
            ["xdotool", "search", "--name", "Anki", "windowactivate", "--sync"],
            ["wmctrl", "-xa", "anki"],
            ["wmctrl", "-xa", "Anki"],
        ):
            try:
                subprocess.run(cmd, capture_output=True, check=False)
                return
            except Exception:
                continue

class KDEProvider(BaseProvider):
    def get_active_info(self):
        try:
            out = subprocess.check_output(
                ["qdbus6", "org.kde.KWin", "/KWin", "org.kde.KWin.queryWindowInfo"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            classes = []
            for line in out.split('\n'):
                if 'resourceClass:' in line or 'resourceName:' in line:
                    classes.append(line.split(':', 1)[1].strip().lower())
            return " ".join(classes)
        except Exception:
            pass

        for cmd in (
            ["kdotool", "getactivewindow", "getwindowclassname", "getwindowname"],
            ["xdotool", "getactivewindow", "getwindowclassname", "getwindowname"],
            ["wmctrl", "-lx"],
        ):
            try:
                out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
                return out.lower().replace("\n", " ")
            except Exception:
                continue
        return ""

    def force_focus_anki(self):
        try:
            out = subprocess.check_output(
                ["xdotool", "search", "--class", "anki"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if out:
                wid = out.split('\n')[0]
                subprocess.run(["xdotool", "windowactivate", "--sync", wid], capture_output=True)
                return
        except Exception:
            pass

        for cmd in (
            ["kdotool", "search", "--class", "anki", "windowactivate", "--sync"],
            ["kdotool", "search", "--name", "Anki", "windowactivate", "--sync"],
            ["wmctrl", "-xa", "anki"],
            ["wmctrl", "-xa", "Anki"],
        ):
            try:
                subprocess.run(cmd, capture_output=True, check=False)
                return
            except Exception:
                continue

def _driftwm_active():
    if not shutil.which("driftwm"):
        return False
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return False
    # driftwm writes this file itself while running, so its presence is a
    # reliable "is a driftwm session actually up" signal. There's no
    # dedicated env var for it like SWAYSOCK / HYPRLAND_INSTANCE_SIGNATURE /
    # NIRI_SOCKET.
    return os.path.exists(os.path.join(runtime_dir, "driftwm", "state"))

def get_provider():
    if IS_WINDOWS:
        return WindowsProvider()
    if "NIRI_SOCKET" in os.environ:
        return NiriProvider()
    if "driftwm" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() or _driftwm_active():
        return DriftwmProvider()
    if "HYPRLAND_INSTANCE_SIGNATURE" in os.environ:
        return HyprlandProvider()
    if "SWAYSOCK" in os.environ:
        return SwayProvider()
    if "awesome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() or "AWESOME_VERSION" in os.environ:
        return AwesomeProvider()
    if "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() or "plasma" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return KDEProvider()
    return BaseProvider()

provider = get_provider()

# --- Core Logic ---
class ForceFocus:
    def __init__(self):
        self.config = {}
        self.state = {}
        self.last_gain = 0
        self.initialized = False
        self._lock = threading.Lock()
        self._shortcuts = []

    def init_full(self):
        self.load_config()
        self.load_state()
        self.save_state()
        self.initialized = True
        self._install_shortcuts()
        if not hasattr(self, "_undo_started"):
            self._undo_started = True
            threading.Thread(target=self.undo_check_loop, daemon=True).start()

    def _install_shortcuts(self):
        if self._shortcuts:
            return
        sc = QShortcut(QKeySequence("Alt+P"), mw)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(self.pause_focus_for)
        self._shortcuts.append(sc)

    def load_config(self):
        self.config = mw.addonManager.getConfig(__name__) or {}

    def load_state(self):
        if os.path.exists(IPC_PATH):
            try:
                with open(IPC_PATH, "r") as f:
                    self.state = json.load(f)
                self.state.setdefault("unlock_timestamp", time.time())
                self.state.setdefault("last_notification_time", 0.0)
                self.state.setdefault("focus_pause_until", 0.0)
            except:
                self.init_state()
        else:
            self.init_state()

    def init_state(self):
        self.state = {
            "unlock_timestamp": time.time(),
            "cards_today": 0,
            "last_period": self._get_period_id(),
            "quota_reached": False,
            "last_review_time": 0.0,
            "last_notification_time": 0.0,
            "focus_pause_until": 0.0,
        }

    def _get_period_id(self):
        rt = self.config.get("reset_time", "04:00")
        h, m = map(int, rt.split(":"))
        now = time.localtime()
        cutoff = time.mktime(time.struct_time(
            (now.tm_year, now.tm_mon, now.tm_mday, h, m, 0, now.tm_wday, now.tm_yday, now.tm_isdst)
        ))
        if time.time() < cutoff:
            cutoff -= 86400
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff))

    def save_state(self):
        cur_period = self._get_period_id()
        if self.state.get("last_period") != cur_period:
            self.state["cards_today"] = 0
            self.state["last_period"] = cur_period
            self.state["quota_reached"] = False
            self.state["last_review_time"] = 0.0
            self.state["last_notification_time"] = 0.0
            self.state["focus_pause_until"] = 0.0

        # Defensive correction so stale state cannot keep quota stuck on.
        if self.state.get("cards_today", 0) < self.config.get("daily_quota", 50):
            self.state["quota_reached"] = False

        try:
            with open(IPC_PATH, "w") as f:
                json.dump(self.state, f)
        except Exception as e:
            print(f"ForceFocus Error saving state: {e}")

    def pause_focus_for(self, seconds=None):
        if not self.initialized:
            return
        with self._lock:
            self.load_state()
            self.load_config()
            seconds = int(seconds if seconds is not None else self.config.get("pause_focus_duration", 300))
            if seconds < 1:
                seconds = 1
            now = time.time()
            base = max(self.state.get("focus_pause_until", 0.0), now)
            self.state["focus_pause_until"] = base + seconds
            self.save_state()
        provider.notify(f"Window focus paused for {seconds // 60}m {seconds % 60}s")

    def on_card_answered(self, reviewer, card, ease):
        if not self.initialized:
            return
        with self._lock:
            self.load_state()
            self.load_config()
            if ease not in self.config.get("reward_ratings", [2, 3, 4]):
                self.last_gain = 0
                self.save_state()
                return

            gain = 0
            if self.config.get("mode") == "per_card":
                gain = self.config.get("time_per_card", 10)
            else:
                actual_seconds = card.time_taken() / 1000
                if self.config.get("auto_pause"):
                    actual_seconds = min(actual_seconds, self.config.get("recent_activity_window", 30))
                gain = actual_seconds * self.config.get("time_per_second", 1.0)

            self.last_gain = gain
            self.state["unlock_timestamp"] += gain

            max_t = self.config.get("max_time", 0)
            if max_t > 0:
                self.state["unlock_timestamp"] = min(self.state["unlock_timestamp"], time.time() + max_t)

            self.state["cards_today"] += 1
            self.state["last_review_time"] = time.time()

            if self.state["cards_today"] >= self.config.get("daily_quota", 50):
                self.state["quota_reached"] = True
            self.save_state()

    def on_undo(self):
        if not self.initialized:
            return
        if not self.config.get("subtract_on_undo", True):
            return
        with self._lock:
            self.load_state()
            if self.last_gain <= 0:
                return

            self.state["unlock_timestamp"] -= self.last_gain
            if not self.config.get("allow_negative_time", False):
                self.state["unlock_timestamp"] = max(self.state["unlock_timestamp"], time.time())

            self.state["cards_today"] = max(0, self.state["cards_today"] - 1)
            if self.state["cards_today"] < self.config.get("daily_quota", 50):
                self.state["quota_reached"] = False
            self.last_gain = 0
            self.save_state()

    def undo_check_loop(self):
        last_count = 0
        while True:
            try:
                time.sleep(1)
                if not self.initialized or not mw.col:
                    continue
                today_stats = mw.col.db.scalar(
                    "select count() from revlog where id > ?",
                    (mw.col.sched.day_cutoff - 86400) * 1000
                )
                if today_stats < last_count and last_count > 0:
                    self.on_undo()
                last_count = today_stats
            except Exception as e:
                print(f"ForceFocus undo_check_loop error: {e}")


# --- Tray & Enforcement ---
class ForceFocusTray(QSystemTrayIcon):
    def __init__(self):
        super().__init__()
        menu = QMenu()

        pause_action = QAction("Pause focus", menu)
        pause_action.triggered.connect(self._pause_from_menu)
        menu.addAction(pause_action)

        hide_action = QAction("Close tray icon", menu)
        hide_action.triggered.connect(self.hide)
        menu.addAction(hide_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

        self._is_force_locked = False
        self._refresh_icon("gray", "Waiting for Anki data...")
        self.show()

    def _pause_from_menu(self):
        ff.pause_focus_for(ff.config.get("pause_focus_duration", 300))

    def _on_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._pause_from_menu()

    def _refresh_icon(self, color, tooltip):
        px = QPixmap(64, 64)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(8, 8, 48, 48)
        p.end()
        self.setIcon(QIcon(px))
        self.setToolTip(tooltip)

    def _tick(self):
        if not ff.initialized or not os.path.exists(IPC_PATH):
            self._refresh_icon("gray", "Waiting for Anki data...")
            return

        try:
            ff.load_config()
            ff.load_state()
            state = ff.state
            cfg = ff.config

            # Auto pause adds time to offset countdown
            grace = cfg.get("recent_activity_window", 30)
            last_rev = state.get("last_review_time", 0.0)
            is_reviewing = bool(last_rev) and (time.time() - last_rev) <= grace
            should_pause = cfg.get("auto_pause") and is_reviewing

            if should_pause:
                state["unlock_timestamp"] += 1.0
                ff.save_state()

            if not cfg.get("allow_negative_time", False):
                if state["unlock_timestamp"] < time.time():
                    state["unlock_timestamp"] = time.time()
                    ff.save_state()

            remaining = state["unlock_timestamp"] - time.time()
            cards = state.get("cards_today", 0)
            quota_reached = state.get("quota_reached", False)
            min_unlock = cfg.get("min_unlock_time", 30)
            focus_pause_until = state.get("focus_pause_until", 0.0)
            focus_paused = time.time() < focus_pause_until

            abs_rem = abs(int(remaining))
            time_str = f"{'-' if remaining < 0 else ''}{abs_rem // 60}m {abs_rem % 60}s"
            status_parts = []
            if should_pause:
                status_parts.append("Paused (reviewing)")
            if focus_paused:
                rem = max(0, int(focus_pause_until - time.time()))
                status_parts.append(f"Focus paused {rem // 60}m {rem % 60}s")
            if not status_parts:
                status_parts.append("Timer running")
            status = " | ".join(status_parts)
            tip = f"Time: {time_str}\nCards: {cards}\n{status}"

            # --- LOCK STATE ---
            if self._is_force_locked:
                if remaining >= min_unlock:
                    self._is_force_locked = False
                    provider.notify("Threshold met. Unlocked!")
            else:
                if remaining <= 0:
                    self._is_force_locked = True

            is_locked = self._is_force_locked

            # --- UI ---
            if quota_reached:
                self._refresh_icon("gold", tip + "\nQUOTA REACHED")
            elif focus_paused:
                self._refresh_icon("blue", tip + "\nFOCUS PAUSED")
            elif remaining < 0 and cfg.get("allow_negative_time", False):
                self._refresh_icon("purple", tip + "\nDEBT / LOCKED")
            elif is_locked:
                time_to_free = max(0, min_unlock - remaining)
                ttf_str = f"{int(time_to_free) // 60}m {int(time_to_free) % 60}s"
                self._refresh_icon("red", tip + f"\nFree in: {ttf_str}\nLOCKED")
            elif remaining <= min_unlock:
                self._refresh_icon("red", tip + "\nLOW TIME")
            else:
                self._refresh_icon("green", tip)

            if quota_reached:
                return

            # --- WARNINGS (only when unlocked) ---
            notif_enabled = cfg.get("notifications_enabled", True)
            if notif_enabled and not is_locked and not focus_paused and remaining > 0:
                notif_start = cfg.get("notification_start_time", 120)
                notif_interval = cfg.get("notification_interval", 30)
                if remaining <= notif_start:
                    last_notif = state.get("last_notification_time", 0.0)
                    if (time.time() - last_notif) >= notif_interval:
                        provider.notify(f"Warning: {int(remaining)}s remaining!")
                        state["last_notification_time"] = time.time()
                        ff.save_state()

            # --- ENFORCEMENT (CONTINUOUS) ---
            if focus_paused:
                return

            if is_locked:
                active_info = provider.get_active_info()
                is_anki_focused = "anki" in active_info
                exempt_ids = cfg.get("exempt_identifiers", ["anki", "org.ankiweb.anki", "anki.exe"])
                is_exempt = any(ex in active_info for ex in exempt_ids)

                if not is_exempt and not is_anki_focused:
                    provider.force_focus_anki()

        except Exception as e:
            print(f"[ForceFocus tray] {e}")

# --- Wiring ---
ff = ForceFocus()
_tray = None

def on_answer(reviewer, card, ease):
    ff.on_card_answered(reviewer, card, ease)

def on_profile_ready():
    ff.init_full()
    global _tray
    _tray = ForceFocusTray()

gui_hooks.reviewer_did_answer_card.append(on_answer)
gui_hooks.profile_did_open.append(on_profile_ready)
