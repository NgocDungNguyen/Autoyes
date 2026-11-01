"""
AutoYes - scans VS Code windows for permission prompts like

    1. Yes
    2. Yes, allow ... (for this project / session)
    3. No

and clicks option 2 ("Yes, allow ...") or, when there is no option 2, option 1 ("Yes").

Only windows that belong to VS Code (Code.exe / Code - Insiders.exe) are captured
and read. Text is read with the built-in Windows OCR engine.
"""

import asyncio
import ctypes
import ctypes.wintypes as wt
import io
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

import mss
from PIL import Image
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

# --------------------------------------------------------------------------- #
# Win32 plumbing
# --------------------------------------------------------------------------- #

try:  # physical pixel coordinates everywhere (screenshots, OCR boxes, clicks)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

VSCODE_EXES = {"code.exe", "code - insiders.exe"}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_RENDERFULLCONTENT = 0x2
SW_RESTORE = 9
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WM_HOTKEY = 0x0312

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _process_exe(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def _window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_vscode_windows():
    """Visible, non-minimised top-level VS Code windows: [(hwnd, title, (l, t, r, b))]."""
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        title = _window_title(hwnd)
        if not title or _process_exe(hwnd) not in VSCODE_EXES:
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left > 100 and r.bottom - r.top > 100:
            found.append((hwnd, title, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def capture_window(hwnd, rect):
    """Capture a window even if it is behind other windows (PrintWindow).
    Falls back to a plain screen grab if PrintWindow returns a black image."""
    l, t, r, b = rect
    w, h = r - l, b - t
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    img = None
    try:
        if user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            if gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0):
                img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

    if img is None or max(img.convert("L").getextrema()) < 16:
        with mss.mss() as sct:
            shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img


def bring_to_front(hwnd):
    if user32.GetForegroundWindow() == hwnd:
        return
    # Windows only lets the "last input" process change focus; a tap of ALT unlocks it.
    _send_key(VK_MENU)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)


def _send_inputs(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _send_key(vk):
    down = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0)))
    up = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)))
    _send_inputs(down, up)


def click_at(x, y, restore_cursor=True):
    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    _send_inputs(INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0))),
                 INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0))))
    time.sleep(0.03)
    if restore_cursor:
        user32.SetCursorPos(old.x, old.y)


def press_digit(d):
    _send_key(ord(str(d)))


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #

@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self):
        return self.x + self.w / 2, self.y + self.h / 2


class Ocr:
    def __init__(self):
        self.engine = OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise RuntimeError("No Windows OCR language installed (Settings > Language).")
        self.loop = asyncio.new_event_loop()

    async def _recognize(self, img):
        data = io.BytesIO()
        img.save(data, "BMP")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data.getvalue())
        await writer.store_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        return await self.engine.recognize_async(bitmap)

    def lines(self, img, scale=2.0):
        """OCR an image, returning lines with boxes in the ORIGINAL image's coordinates."""
        if scale != 1:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        limit = OcrEngine.max_image_dimension
        if max(img.size) > limit:
            f = limit / max(img.size)
            img = img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)
            scale *= f
        result = self.loop.run_until_complete(self._recognize(img))
        out = []
        for line in result.lines:
            rects = [w.bounding_rect for w in line.words]
            if not rects:
                continue
            x0 = min(r.x for r in rects)
            y0 = min(r.y for r in rects)
            x1 = max(r.x + r.width for r in rects)
            y1 = max(r.y + r.height for r in rects)
            out.append(Line(line.text, x0 / scale, y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale))
        return out


# --------------------------------------------------------------------------- #
# Prompt detection
# --------------------------------------------------------------------------- #

# Optional leading marker/number: "1.", "1)", "> 1.", "❯ 2.", OCR'd "l." / "I." for "1"
_PREFIX = r"^[\s>›❯»•*\-|]*(?:([1-3lIi|])\s*[.):]?\s+)?"
RE_OPTION = re.compile(_PREFIX + r"(yes|no)\b(.*)$", re.IGNORECASE)


@dataclass
class Prompt:
    target: Line      # the line to click
    option: int       # 1 or 2
    no_line: Line | None


def classify(line):
    """Return 'yes1', 'yes2', 'no' or None for one OCR line."""
    m = RE_OPTION.match(line.text.strip().replace("’", "'"))
    if not m:
        return None
    word, rest = m.group(2).lower(), m.group(3).strip()
    if word == "no":
        return "no"
    rest_l = rest.lower()
    if not rest or re.fullmatch(r"[\s.,!:;]*", rest):
        return "yes1"
    # "Yes, allow all edits during this session", "Yes, and don't ask again for ... in <project>"
    if re.match(r"[,.]?\s*(and\s+)?(allow|don'?t|do not|always|for\b|auto)", rest_l):
        return "yes2"
    return None


def find_prompt(lines, prefer_option2=True):
    """Find a Yes / [Yes, allow…] / No option group. Returns a Prompt or None."""
    tagged = [(classify(l), l) for l in lines]
    yes1 = [l for t, l in tagged if t == "yes1"]
    yes2 = [l for t, l in tagged if t == "yes2"]
    nos = [l for t, l in tagged if t == "no"]

    for y1 in yes1:
        lh = max(y1.h, 8)
        near = lambda l: 0 < l.y - y1.y < lh * 10 and abs(l.x - y1.x) < lh * 6
        # The other options sit below "Yes", left-aligned with it, within a few lines.
        no = next((n for n in nos if near(n)), None)
        y2 = next((l for l in yes2 if near(l) and (not no or l.y < no.y)), None)
        # Need "Yes" + "No", or "Yes" + "Yes, allow…" (OCR sometimes drops the short "No").
        if not no and not y2:
            continue
        if y2 and prefer_option2:
            return Prompt(y2, 2, no)
        return Prompt(y1, 1, no)
    return None


# --------------------------------------------------------------------------- #
# Scanner thread
# --------------------------------------------------------------------------- #

class Scanner:
    def __init__(self, log, settings):
        self.log = log                      # callable(str)
        self.settings = settings            # callable() -> dict
        self.ocr = None
        self.running = threading.Event()    # set = scanning, clear = paused
        self.stop_flag = threading.Event()
        self.thread = None
        self.clicks = 0
        self.lock = threading.Lock()        # one scan at a time (auto + manual)

    # ---- lifecycle -------------------------------------------------------- #
    def start(self):
        self.stop_flag.clear()
        self.running.set()
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def pause(self):
        self.running.clear()

    def stop(self):
        self.running.clear()
        self.stop_flag.set()

    @property
    def state(self):
        if not self.thread or not self.thread.is_alive() or self.stop_flag.is_set():
            return "Stopped"
        return "Running" if self.running.is_set() else "Paused"

    def _loop(self):
        while not self.stop_flag.is_set():
            if not self.running.wait(timeout=0.2):
                continue
            started = time.time()
            try:
                self.scan_once()
            except Exception as e:  # keep the loop alive whatever happens
                self.log(f"Error: {e}")
            interval = self.settings()["interval"]
            self.stop_flag.wait(max(0.1, interval - (time.time() - started)))

    # ---- one pass --------------------------------------------------------- #
    def scan_once(self, manual=False):
        with self.lock:
            if self.ocr is None:
                self.ocr = Ocr()
            cfg = self.settings()
            windows = find_vscode_windows()
            if manual:
                self.log(f"Manual scan: {len(windows)} VS Code window(s)")
            hit = False
            for hwnd, title, rect in windows:
                if self._handle_window(hwnd, title, rect, cfg):
                    hit = True
            if manual and not hit:
                self.log("Manual scan: no Yes/No prompt found")
            return hit

    def _detect(self, hwnd, rect, cfg):
        img = capture_window(hwnd, rect)
        return find_prompt(self.ocr.lines(img, cfg["ocr_scale"]), cfg["prefer_option2"])

    def _handle_window(self, hwnd, title, rect, cfg):
        prompt = self._detect(hwnd, rect, cfg)
        if not prompt:
            return False

        short = title if len(title) < 50 else title[:47] + "..."
        self.log(f"Found prompt in [{short}] -> option {prompt.option}: \"{prompt.target.text}\"")

        if cfg["bring_front"]:
            bring_to_front(hwnd)
        cx, cy = prompt.target.center
        click_at(rect[0] + cx, rect[1] + cy, cfg["restore_cursor"])
        self.clicks += 1

        # Terminal prompts (Claude Code CLI) don't react to mouse clicks; if the prompt is
        # still on screen afterwards, select it with its number key instead.
        if cfg["key_fallback"]:
            time.sleep(0.6)
            again = self._detect(hwnd, rect, cfg)
            if again and abs(again.target.y - prompt.target.y) < 5:
                press_digit(prompt.option)
                self.log(f"Click had no effect, pressed key '{prompt.option}'")
        time.sleep(cfg["cooldown"])
        return True


# --------------------------------------------------------------------------- #
# Global hotkeys (work even when the GUI isn't focused)
# --------------------------------------------------------------------------- #

def start_hotkeys(handlers):
    """handlers: {(modifiers, vk): callable}. Runs a message loop on a daemon thread."""
    def run():
        ids = {}
        for i, ((mods, vk), fn) in enumerate(handlers.items(), start=1):
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                ids[i] = fn
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                ids[msg.wParam]()
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AutoYes for VS Code")
        self.root.geometry("520x470")
        self.root.minsize(440, 380)
        self.msgs = queue.Queue()
        self.calls = queue.Queue()  # actions from the hotkey thread, run on the Tk thread

        self.interval = tk.DoubleVar(value=1.0)
        self.prefer2 = tk.BooleanVar(value=True)
        self.bring_front = tk.BooleanVar(value=True)
        self.key_fallback = tk.BooleanVar(value=True)
        self.restore_cursor = tk.BooleanVar(value=True)
        self.topmost = tk.BooleanVar(value=True)

        self.scanner = Scanner(self.log, self.settings)
        self._build()
        self.root.attributes("-topmost", True)
        start_hotkeys({
            (MOD_CONTROL | MOD_ALT, ord("S")): lambda: self.calls.put(self.toggle),
            (MOD_CONTROL | MOD_ALT, ord("X")): lambda: self.calls.put(self.stop),
            (MOD_CONTROL | MOD_ALT, ord("C")): lambda: self.calls.put(self.scan_now),
        })
        self.root.after(100, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def settings(self):
        try:
            interval = max(0.3, float(self.interval.get()))
        except (tk.TclError, ValueError):
            interval = 1.0
        return {
            "interval": interval,
            "prefer_option2": self.prefer2.get(),
            "bring_front": self.bring_front.get(),
            "key_fallback": self.key_fallback.get(),
            "restore_cursor": self.restore_cursor.get(),
            "ocr_scale": 2.0,
            "cooldown": 1.5,
        }

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status = ttk.Label(top, text="Stopped", font=("Segoe UI", 13, "bold"), foreground="#b00")
        self.status.pack(side="left")
        self.counter = ttk.Label(top, text="Clicks: 0")
        self.counter.pack(side="right")

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶ Start", command=self.start)
        self.pause_btn = ttk.Button(btns, text="⏸ Pause", command=self.toggle_pause)
        self.stop_btn = ttk.Button(btns, text="⏹ Stop", command=self.stop)
        self.now_btn = ttk.Button(btns, text="⚡ Scan & Click Now", command=self.scan_now)
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.now_btn):
            b.pack(side="left", expand=True, fill="x", padx=2)

        opts = ttk.LabelFrame(self.root, text="Options")
        opts.pack(fill="x", **pad)
        row = ttk.Frame(opts)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Scan every").pack(side="left")
        ttk.Spinbox(row, from_=0.3, to=30, increment=0.5, width=5,
                    textvariable=self.interval).pack(side="left", padx=4)
        ttk.Label(row, text="seconds").pack(side="left")
        for text, var in [
            ("Prefer option 2 \"Yes, allow…\" (otherwise always option 1 \"Yes\")", self.prefer2),
            ("Bring VS Code window to front before clicking", self.bring_front),
            ("If click doesn't work (terminal prompt), press the number key", self.key_fallback),
            ("Put mouse cursor back after clicking", self.restore_cursor),
        ]:
            ttk.Checkbutton(opts, text=text, variable=var).pack(anchor="w", padx=6)
        ttk.Checkbutton(opts, text="Keep this window on top", variable=self.topmost,
                        command=lambda: self.root.attributes("-topmost", self.topmost.get())
                        ).pack(anchor="w", padx=6, pady=(0, 4))

        ttk.Label(self.root, text="Hotkeys:  Ctrl+Alt+S start/pause   Ctrl+Alt+X stop   "
                                  "Ctrl+Alt+C scan now", foreground="#555").pack(anchor="w", padx=10)

        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(logf, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)
        self._refresh()

    # ---- actions ---------------------------------------------------------- #
    def start(self):
        self.scanner.start()
        self.log("Scanning started")
        self._refresh()

    def toggle_pause(self):
        state = self.scanner.state
        if state == "Running":
            self.scanner.pause()
            self.log("Paused")
        elif state == "Paused":
            self.scanner.start()
            self.log("Resumed")
        self._refresh()

    def toggle(self):
        if self.scanner.state == "Stopped":
            self.start()
        else:
            self.toggle_pause()

    def stop(self):
        if self.scanner.state != "Stopped":
            self.scanner.stop()
            self.log("Stopped")
        self._refresh()

    def scan_now(self):
        def work():
            try:
                self.scanner.scan_once(manual=True)
            except Exception as e:
                self.log(f"Error: {e}")
        threading.Thread(target=work, daemon=True).start()

    # ---- plumbing --------------------------------------------------------- #
    def log(self, text):
        self.msgs.put(f"{time.strftime('%H:%M:%S')}  {text}")

    def _refresh(self):
        state = self.scanner.state
        color = {"Running": "#080", "Paused": "#c80", "Stopped": "#b00"}[state]
        self.status.configure(text=state, foreground=color)
        self.pause_btn.configure(text="▶ Resume" if state == "Paused" else "⏸ Pause",
                                 state="disabled" if state == "Stopped" else "normal")
        self.start_btn.configure(state="normal" if state == "Stopped" else "disabled")
        self.counter.configure(text=f"Clicks: {self.scanner.clicks}")

    def _pump(self):
        while not self.calls.empty():
            self.calls.get_nowait()()
        lines = []
        while not self.msgs.empty():
            lines.append(self.msgs.get_nowait())
        if lines:
            self.logbox.configure(state="normal")
            self.logbox.insert("end", "\n".join(lines) + "\n")
            if int(self.logbox.index("end-1c").split(".")[0]) > 500:
                self.logbox.delete("1.0", "100.0")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self._refresh()
        self.root.after(150, self._pump)

    def _close(self):
        self.scanner.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
