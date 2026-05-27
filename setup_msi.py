"""cx_Freeze build script — produces a Windows MSI installer.

Usage:
    python setup_msi.py bdist_msi

Output:
    dist\\PhoneController-<version>-win64.msi
"""
import sys
import uuid
from cx_Freeze import setup, Executable

APP_NAME = "PhoneController"
APP_VERSION = "1.0.0"
APP_GUID = "{B9F4D1F0-2A1A-4F1E-9F50-7C5C9C9C9C00}"  # stable upgrade code

build_exe_options = {
    "packages": ["av", "PIL", "tkinter"],
    "includes": [],
    "include_files": [
        ("daemon.sh", "daemon.sh"),
        ("handler.sh", "handler.sh"),
        ("ADBKeyboard.apk", "ADBKeyboard.apk"),
    ],
    "excludes": ["test", "unittest", "pydoc_data"],
    "optimize": 1,
}

bdist_msi_options = {
    "upgrade_code": APP_GUID,
    "add_to_path": False,
    "initial_target_dir": rf"[ProgramFiles64Folder]\{APP_NAME}",
    "install_icon": None,
    "all_users": False,
    "summary_data": {
        "author": "personaAI",
        "comments": "ADB-based phone controller GUI",
    },
}

base = "Win32GUI" if sys.platform == "win32" else None

setup(
    name=APP_NAME,
    version=APP_VERSION,
    description="Phone Controller — live mirror + system monitor",
    options={
        "build_exe": build_exe_options,
        "bdist_msi": bdist_msi_options,
    },
    executables=[
        Executable(
            "gui.py",
            base=base,
            target_name="PhoneController.exe",
            shortcut_name="Phone Controller",
            shortcut_dir="ProgramMenuFolder",
        )
    ],
)
