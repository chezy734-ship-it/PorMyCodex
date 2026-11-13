@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo ===================================================
echo   בניית תוכנת PorMyCodex לקובץ EXE עצמאי (Windows)
echo ===================================================
echo.

set PY_CMD=python
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [שגיאה] לא נמצא Python במחשב. יש להתקין Python 3.10+ ולסמן "Add Python to PATH".
        pause
        exit /b 1
    )
    set PY_CMD=py -3
)

echo [1/3] מתקין חבילות נדרשות באמצעות %PY_CMD%...
%PY_CMD% -m pip install --upgrade pip
%PY_CMD% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [שגיאה] התקנת החבילות נכשלה. בדוק חיבור לאינטרנט והרשאות.
    pause
    exit /b 1
)

echo.
echo [2/3] מקמפל את התוכנה לקובץ EXE באמצעות PyInstaller...
%PY_CMD% -m PyInstaller PorMyCodex.spec --clean --noconfirm
if errorlevel 1 (
    echo [שגיאה] קימפול התוכנה נכשל.
    pause
    exit /b 1
)

echo.
echo [3/3] הבנייה הסתיימה בהצלחה!
echo הקובץ המוכן נמצא בתיקייה: %~dp0dist\PorMyCodex.exe
echo.
if exist "dist\PorMyCodex.exe" (
    explorer /select,"%~dp0dist\PorMyCodex.exe"
)
pause
