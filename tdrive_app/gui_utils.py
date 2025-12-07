import tkinter as tk
from tkinter import filedialog

def core_select_files(multiple=False, title="選取檔案", initial_dir=None):
    """開啟一個用於選擇單一或多個檔案的對話方塊的核心邏輯。"""
    root = tk.Tk()
    root.withdraw() # 隱藏主視窗
    root.attributes('-topmost', True) # 讓對話方塊置頂
    
    file_paths = []
    if multiple:
        file_paths_tuple = filedialog.askopenfilenames(title=title, initialdir=initial_dir)
        file_paths = list(file_paths_tuple)
    else:
        file_path_str = filedialog.askopenfilename(title=title, initialdir=initial_dir)
        if file_path_str:
            file_paths = [file_path_str]
    
    root.destroy() # 銷毀 tkinter 根視窗
    return file_paths if file_paths else []

def core_select_directory(title="選取資料夾", initial_dir=None):
    """開啟一個用於選擇資料夾的對話方塊的核心邏輯。"""
    root = tk.Tk()
    root.withdraw() # 隱藏主視窗
    root.attributes('-topmost', True) # 讓對話方塊置頂
    folder_path = filedialog.askdirectory(title=title, initialdir=initial_dir)
    root.destroy() # 銷毀 tkinter 根視窗
    return folder_path if folder_path else ""
