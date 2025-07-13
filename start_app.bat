@echo off
chcp 65001
cd /d "%~dp0"

:: Check if virtual environment exists
if not exist ".venv" (
    echo Virtual environment does not exist, creating...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment. Please make sure Python is installed and added to environment variables.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully
)

:: Activate virtual environment
call .\.venv\Scripts\activate.bat
if errorlevel 1 (
    echo Failed to activate virtual environment
    pause
    exit /b 1
)

:: 安装依赖
pip install -r requirements.txt

:: 运行应用
python app.py
