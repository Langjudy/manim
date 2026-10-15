@echo off
rem ManimGL launcher. Keep this file pure ASCII: cmd misparses multi-byte text in .bat files.
setlocal
cd /d "%~dp0"

set "PY=%~dp0venv\python.exe"
rem Ignore packages in the global AppData\Roaming\Python; use only the project venv
set "PYTHONNOUSERSITE=1"
set "CONDA=C:\Users\Administrator\miniconda3\Scripts\conda.exe"

rem ---- 1. Project-local conda env (create if missing) ----
if not exist "%PY%" (
    echo [setup] Creating conda env in .\venv ...
    "%CONDA%" create -p "%~dp0venv" python=3.13.9 -y
    if errorlevel 1 (
        echo [error] Failed to create conda env
        pause
        exit /b 1
    )
)

rem ---- 2. Install dependencies (only if manimgl is not installed) ----
"%PY%" -c "import manimlib" >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing dependencies and manimgl ...
    "%PY%" -m pip install -r requirements.txt -e .
    if errorlevel 1 (
        echo [error] Failed to install dependencies
        pause
        exit /b 1
    )
)

rem ---- 2b. AI Studio dependencies (anthropic / openai SDKs) ----
"%PY%" -c "import anthropic, openai" >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing AI Studio dependencies ...
    "%PY%" -m pip install -r ai_studio\requirements.txt
    if errorlevel 1 (
        echo [error] Failed to install AI Studio dependencies
        pause
        exit /b 1
    )
)

rem ---- 3. Launch ----
rem With args: passed straight to manimgl once, e.g.  run.bat example_scenes.py OpeningManimExample -w
if not "%~1"=="" (
    "%PY%" -m manimlib %*
    echo.
    pause
    exit /b
)

rem No args: main menu
:menu
cls
echo ============================================
echo   ManimGL launcher
echo ============================================
echo   1. Browse example scenes  (example_scenes.py)
echo   2. AI Studio - generate animation scripts with
echo      DeepSeek / OpenAI / Claude in the browser
echo   Q. Quit
echo.
rem choice errorlevels: 1, 2, Q -> 3, no input / EOF -> 255; "if errorlevel 3" catches 3 and 255
choice /c 12Q /n /m "Select 1, 2 or Q: "
if errorlevel 3 goto :end
if errorlevel 2 goto :studio
goto :examples

rem Loop over the example_scenes.py picker
:examples
cls
"%PY%" -m manimlib example_scenes.py
echo.
rem N -> 1, M -> 2, Q -> 3, EOF -> 255
choice /c NMQ /n /m "N = another scene, M = main menu, Q = quit: "
if errorlevel 3 goto :end
if errorlevel 2 goto :menu
goto :examples

rem AI Studio runs in its own window, so this menu stays usable; closing that window stops it
:studio
start "ManimGL AI Studio" "%PY%" "%~dp0ai_studio\server.py"
echo.
echo AI Studio is starting in a new window and will open in your browser.
echo Close the "ManimGL AI Studio" window to stop it.
echo.
pause
goto :menu

:end
endlocal
