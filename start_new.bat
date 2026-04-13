@echo off
chcp 936 >nul
setlocal

echo ============================================================
echo 企业内部历史询价复用系统
echo ============================================================
echo.

set "ROOT_DIR=%~dp0"
set "VENV_PY=%ROOT_DIR%.venv\Scripts\python.exe"

echo [1/4] 检查Python环境...
py --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python启动器(py)，请先安装Python 3.12+
    pause
    exit /b 1
)
echo [√] Python环境正常

echo.
echo [2/4] 检查虚拟环境...
if not exist "%VENV_PY%" (
    echo [!] 未找到 .venv，正在创建...
    py -3.12 -m venv "%ROOT_DIR%.venv"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
)
echo [√] 虚拟环境正常

echo.
echo [3/4] 检查依赖...
"%VENV_PY%" -c "import flask, flask_login, pandas, openpyxl, sqlalchemy" >nul 2>&1
if errorlevel 1 (
    echo [!] 依赖未安装或版本不兼容，正在安装...
    "%VENV_PY%" -m pip install -r "%ROOT_DIR%requirements.txt"
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)
echo [√] 依赖检查完成

echo.
echo [4/4] 启动系统...
cd /d "%ROOT_DIR%backend"
"%VENV_PY%" app.py

pause
