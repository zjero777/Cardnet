@echo off
set VENV_PIP=.venv\Scripts\pip.exe
set VENV_PYINSTALLER=.venv\Scripts\pyinstaller.exe

echo Building Cardnet client...

REM Очистка артефактов от предыдущих сборок для чистоты эксперимента
echo Cleaning up previous builds...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"

REM Этот скрипт использует исполняемые файлы прямо из папки .venv,
REM что надежнее, чем полагаться на активированное окружение в консоли.

REM Устанавливаем PyInstaller, если его нет
echo Installing/Updating PyInstaller in venv...
%VENV_PIP% install --upgrade pyinstaller

REM Запускаем сборку с помощью .spec файла
echo Running PyInstaller with build.spec...
%VENV_PYINSTALLER% build.spec

echo.
echo Build finished. The executable can be found in the 'dist' folder.
pause