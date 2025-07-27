# -*- mode: python ; coding: utf-8 -*-

# Этот .spec файл был сконфигурирован для проекта Cardnet.

block_cipher = None

# Analysis — это основной этап, где PyInstaller анализирует ваш код
# и находит все зависимости.
a = Analysis(
    ['src\\client\\main.py'],
    # Ключевое исправление №1: Указываем корень проекта в pathex.
    # Это нужно, чтобы PyInstaller правильно понял импорты вида `from src.client...`
    pathex=['d:\\Docs\\prj\\Cardnet'],
    binaries=[],
    datas=[],
    # Ключевое исправление №2: Явно указываем "скрытые" импорты.
    # Это решает проблему 'ModuleNotFoundError: No module named 'pygame''.
    hiddenimports=[
        'pygame',
        'esper'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False
)

# PYZ — это сжатый архив со всеми Python-модулями.
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# EXE — определяет, как будет создан сам .exe файл.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Cardnet',
    debug=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,  # `False` для GUI-приложений, чтобы не открывалась консоль.
    icon=None  # Здесь можно указать путь к иконке .ico
)