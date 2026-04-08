@echo off
chcp 936 >nul
echo ============================================================
echo 企业内部历史询价复用系统
echo ============================================================
echo.

echo [1/3] 检查Python环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python，请先安装Python 3.8+
    pause
    exit /b 1
)
echo [√] Python环境正常

echo.
echo [2/3] 检查依赖...
python -c "import flask; import pandas; import openpyxl" >nul 2>&1
if errorlevel 1 (
    echo [!] 依赖未安装，正在安装...
    pip install -r requirements.txt -q
)
echo [√] 依赖检查完成

echo.
echo [3/3] 启动系统...
cd backend
python app.py

pause