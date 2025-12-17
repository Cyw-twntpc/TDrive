"""
Provides core, GUI-framework-specific utility functions for invoking native
dialogs, such as file and folder selection dialogs.

This module abstracts the direct calls to PySide6's QFileDialog, making it
easier to call these common dialogs from various parts of the application.
"""
from PySide6.QtWidgets import QFileDialog, QApplication

def core_select_files(multiple: bool = False, title: str = "Select File(s)", initial_dir: str = "") -> list[str]:
    """
    Opens a native dialog for selecting one or multiple files.

    Args:
        multiple: If True, allows selecting multiple files. Defaults to False.
        title: The title of the dialog window.
        initial_dir: The directory to open the dialog in.

    Returns:
        A list of selected absolute file paths. Returns an empty list if the
        dialog is canceled.
    """
    start_dir = initial_dir if initial_dir else ""
    parent = QApplication.activeWindow()
    
    if multiple:
        # getOpenFileNames returns a tuple: (list_of_filenames, filter_string)
        file_paths, _ = QFileDialog.getOpenFileNames(
            parent, 
            title, 
            start_dir, 
            "All Files (*)"
        )
        return file_paths
    else:
        # getOpenFileName returns a tuple: (filename, filter_string)
        file_path, _ = QFileDialog.getOpenFileName(
            parent, 
            title, 
            start_dir, 
            "All Files (*)"
        )
        return [file_path] if file_path else []

def core_select_directory(title: str = "Select Folder", initial_dir: str = "") -> str:
    """
    Opens a native dialog for selecting a single directory.

    Args:
        title: The title of the dialog window.
        initial_dir: The directory to open the dialog in.

    Returns:
        The selected absolute folder path. Returns an empty string if the
        dialog is canceled.
    """
    start_dir = initial_dir if initial_dir else ""
    parent = QApplication.activeWindow()
    
    folder_path = QFileDialog.getExistingDirectory(
        parent, 
        title, 
        start_dir
    )
    
    return folder_path if folder_path else ""