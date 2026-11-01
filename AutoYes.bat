@echo off
cd /d "%~dp0"
python -c "import mss, PIL, winrt.windows.media.ocr" 2>nul || python -m pip install -r requirements.txt
start "" pythonw autoyes.py
