"""
Provides core, GUI-framework-specific utility functions for invoking native
dialogs, such as file and folder selection dialogs.

This module abstracts the direct calls to PySide6's QFileDialog, making it
easier to call these common dialogs from various parts of the application.
"""
import os
import subprocess
import logging
import ctypes
from PySide6.QtWidgets import QFileDialog, QApplication

logger = logging.getLogger(__name__)

def reveal_in_explorer(path: str) -> bool:
    """
    Opens Windows Explorer and selects the specified file or folder.
    Prioritizes Windows Native API (SHOpenFolderAndSelectItems) for instant performance.
    Falls back to 'explorer /select' command line if native API fails.
    """
    logger.debug(f"Attempting to reveal path in explorer: {path}")
    if not os.path.exists(path):
        logger.warning(f"Reveal failed: Path does not exist: {path}")
        return False
    
    abs_path = os.path.normpath(os.path.abspath(path))

    # --- Method 1: Native API (Fastest) ---
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        
        # Define argument types and return types for 64-bit compatibility
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

        shell32.ILFree.argtypes = [ctypes.c_void_p]

        ole32.CoInitialize(None)
        try:
            pidl = shell32.ILCreateFromPathW(abs_path)
            if pidl:
                result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
                shell32.ILFree(pidl)
                
                if result == 0: # S_OK
                    logger.debug("Native reveal successful.")
                    return True
        finally:
            ole32.CoUninitialize()
            
    except Exception as e:
        logger.warning(f"Native API reveal failed: {e}. Falling back to CLI.", exc_info=True)

    # --- Method 2: Command Line (Fallback) ---
    try:
        cmd = f'explorer /select,"{abs_path}"'
        logger.debug(f"Executing fallback command: {cmd}")
        subprocess.Popen(cmd)
        return True
    except Exception as e:
        logger.error(f"Exception occurred while revealing in explorer (fallback): {e}", exc_info=True)
        return False

def core_select_files(multiple: bool = False, title: str = "選擇檔案", initial_dir: str = "") -> list[str]:
    """Opens a native dialog for selecting one or multiple files."""
    start_dir = initial_dir if initial_dir else ""
    parent = QApplication.activeWindow()
    
    if multiple:
        file_paths, _ = QFileDialog.getOpenFileNames(
            parent, 
            title, 
            start_dir, 
            "All Files (*)"
        )
        return file_paths
    else:
        file_path, _ = QFileDialog.getOpenFileName(
            parent, 
            title, 
            start_dir, 
            "All Files (*)"
        )
        return [file_path] if file_path else []

def core_select_directory(title: str = "選擇資料夾", initial_dir: str = "") -> str:
    """Opens a native dialog for selecting a single directory."""
    start_dir = initial_dir if initial_dir else ""
    parent = QApplication.activeWindow()
    
    folder_path = QFileDialog.getExistingDirectory(
        parent, 
        title, 
        start_dir
    )
    
    return folder_path if folder_path else ""