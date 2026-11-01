# AutoYes for VS Code

A small Windows desktop tool that watches your VS Code windows for permission prompts such as the ones Claude Code shows:

```
1. Yes
2. Yes, allow all edits during this session
3. No
```

and clicks option 2 ("Yes, allow …") automatically, or option 1 ("Yes") when there is no option 2.

## How it works

1. Finds visible, non-minimised windows owned by `Code.exe` / `Code - Insiders.exe`. Other applications are never captured.
2. Captures each window with `PrintWindow`, so it works even when the window is behind other windows (falls back to a screen grab if the capture is black).
3. Reads the text with the built-in **Windows OCR engine** (no cloud, no extra downloads).
4. Looks for a `Yes` / `Yes, allow…` / `No` option group and clicks the chosen option.
5. For terminal prompts that ignore mouse clicks (Claude Code CLI), it re-checks and presses the option's number key instead.

Everything runs locally. The only thing the app does is take screenshots of VS Code windows, run OCR on them in memory, and send mouse/keyboard input.

## Requirements

| Requirement | Notes |
|---|---|
| **Windows 10 / 11** | Uses Win32 APIs and Windows OCR; will not run on macOS or Linux. |
| **Python 3.10 or newer** | Install from [python.org](https://www.python.org/downloads/windows/) and tick *"Add python.exe to PATH"*. Tkinter ships with the standard installer (keep the *tcl/tk* option ticked). Tested on 3.13. |
| **A Windows OCR language pack** | At least one language under *Settings → Time & language → Language & region* (English is installed by default on most machines). If missing, the app logs `No Windows OCR language installed`. |
| **Internet (first run only)** | To download the Python packages in `requirements.txt`. |

## Quick start

```bat
git clone https://github.com/NgocDungNguyen/Autoyes.git
cd Autoyes
AutoYes.bat
```

`AutoYes.bat` installs the dependencies on first run (if missing) and launches the app without a console window.

### Manual / virtual-environment setup

If you prefer to keep dependencies isolated, or if the batch file does not work:

```powershell
git clone https://github.com/NgocDungNguyen/Autoyes.git
cd Autoyes
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python autoyes.py
```

## Using it

Click **▶ Start**. Whenever a prompt appears in a VS Code window it is clicked automatically, and the click counter and log update.

| Control | What it does |
|---|---|
| ▶ Start / ⏸ Pause / ⏹ Stop | Control the background scanner. |
| ⚡ Scan & Click Now | Run one scan immediately. |
| Scan every *N* seconds | Scan interval (minimum 0.3 s). |
| Prefer option 2 | Click "Yes, allow…" when present; otherwise always click option 1 "Yes". |
| Bring VS Code window to front | Focuses the window before clicking. |
| Press the number key if click fails | Fallback for terminal-based prompts. |
| Put mouse cursor back | Restores your cursor position after a click. |
| Keep this window on top | Pins the AutoYes window above others. |

### Global hotkeys (work even when AutoYes is not focused)

| Hotkey | Action |
|---|---|
| `Ctrl+Alt+S` | Start / pause / resume |
| `Ctrl+Alt+X` | Stop |
| `Ctrl+Alt+C` | Scan now |

## Troubleshooting

| Problem | Fix |
|---|---|
| `'python' is not recognized` | Reinstall Python with *Add to PATH* ticked, or run `py -m pip install -r requirements.txt` then `py autoyes.py`. |
| `ModuleNotFoundError: No module named 'mss'` / `winrt` | Run `python -m pip install -r requirements.txt` with the same Python you launch the app with. |
| `No Windows OCR language installed` | Add a language in Windows Settings (*Language & region*), including its *Optical character recognition* feature, then restart the app. |
| Nothing gets clicked | Use **Scan & Click Now** and read the log. The prompt text must be visible in the window (not scrolled off-screen) and VS Code must not be minimised. Raise your VS Code zoom level if the text is small. |
| Clicks land in the wrong place | The app is DPI-aware; make sure you launch it with the same user session as VS Code. If VS Code runs as Administrator, run AutoYes as Administrator too (Windows blocks input into elevated windows otherwise). |
| Want to see errors | Run `python autoyes.py` from a terminal instead of `AutoYes.bat` (which uses `pythonw` and hides the console). |

## Project structure

```
Autoyes/
├── autoyes.py         # the whole application (OCR, detection, clicking, GUI)
├── AutoYes.bat        # one-click launcher: installs deps if missing, then starts the app
├── requirements.txt   # Python dependencies
├── README.md
└── .gitignore
```

## Dependencies

`mss` (screen grab fallback), `pillow` (image handling), and the `winrt-*` projection packages (access to Windows OCR, imaging and storage APIs). See [requirements.txt](requirements.txt).

## Notes

- The tool sends real mouse clicks and key presses. Auto-approving permission prompts means you are trusting every action your AI assistant requests, so use it only in workspaces and sessions where you are comfortable with that. Press `Ctrl+Alt+X` to stop immediately.
- No licence file is included, so reuse rights are not explicitly granted.
