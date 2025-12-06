import eel
import eel.browsers
import os
import platform
import tkinter as tk
from tkinter import filedialog
import logging
import json

from tdrive_app import logger_config
from tdrive_app.main_service import TDriveService

# 初始化日誌系統
logger_config.setup_logging()
logger = logging.getLogger(__name__)

# --- UI 互動函式 (保留在 GUI 層) ---

# --- UI 互動函式 (保留在 GUI 層) ---

@eel.expose
def select_files(multiple=False, title="選取檔案", initial_dir=None):
    """開啟一個用於選擇單一或多個檔案的對話方塊。"""
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

@eel.expose
def select_directory(title="選取資料夾", initial_dir=None):
    """開啟一個用於選擇資料夾的對話方塊。"""
    root = tk.Tk()
    root.withdraw() # 隱藏主視窗
    root.attributes('-topmost', True) # 讓對話方塊置頂
    folder_path = filedialog.askdirectory(title=title, initialdir=initial_dir)
    root.destroy() # 銷毀 tkinter 根視窗
    return folder_path if folder_path else ""

# --- 進度回呼 ---
# 這個函式本身不加 @eel.expose，它是由後端服務呼叫，然後再由它呼叫前端的 eel 函式
def gui_progress_callback(task_id, name, current, total, status, speed, message=None, parent_task_id=None, children=None, total_files=None):
    """將進度更新傳送到使用者介面，支援樹狀結構。"""
    try:
        payload = {
            "id": task_id,
            "name": name,
            "progress": current,
            "size": total,
            "speed": speed,
            "status": status,
            "message": message,
            "parent_id": parent_task_id,
            "children": children,
            "total_files": total_files
        }
        logger.debug(f"向前端傳送進度更新: {json.dumps(payload, ensure_ascii=False)}")
        eel.update_transfer_progress(payload)()
    except Exception as e:
        logger.warning(f"更新傳輸進度至 UI 時失敗 (Task ID: {task_id}): {e}")


# --- 程式主入口 ---

if __name__ == "__main__":
    logger.info("正在建立 TDriveService...")
    # 1. 建立 TDriveService 的唯一實例
    tdrive_service = TDriveService()

    # 2. 透過全域包裝函式將 TDriveService 的方法暴露給 eel
    @eel.expose
    def verify_api_credentials(api_id, api_hash):
        return tdrive_service.verify_api_credentials(api_id, api_hash)

    @eel.expose
    def start_qr_login():
        return tdrive_service.start_qr_login()

    @eel.expose
    def send_code_request(phone_number):
        return tdrive_service.send_code_request(phone_number)

    @eel.expose
    def submit_verification_code(code):
        return tdrive_service.submit_verification_code(code)

    @eel.expose
    def submit_password(password):
        return tdrive_service.submit_password(password)
        
    @eel.expose
    def perform_post_login_initialization():
        return tdrive_service.perform_post_login_initialization()

    @eel.expose
    def get_user_info():
        return tdrive_service.get_user_info()

    @eel.expose
    def get_user_avatar():
        return tdrive_service.get_user_avatar()

    @eel.expose
    def logout():
        return tdrive_service.logout()

    @eel.expose
    def get_folder_tree_data():
        return tdrive_service.get_folder_tree_data()

    @eel.expose
    def get_folder_contents(folder_id):
        return tdrive_service.get_folder_contents(folder_id)

    @eel.expose
    def get_folder_contents_recursive(folder_id):
        return tdrive_service.get_folder_contents_recursive(folder_id)

    @eel.expose
    def search_db_items(base_folder_id, search_term):
        return tdrive_service.search_db_items(base_folder_id, search_term)

    @eel.expose
    def create_folder(parent_id, folder_name):
        return tdrive_service.create_folder(parent_id, folder_name)

    @eel.expose
    def rename_item(item_id, new_name, item_type):
        return tdrive_service.rename_item(item_id, new_name, item_type)

    @eel.expose
    def delete_items(items):
        return tdrive_service.delete_items(items)

    @eel.expose
    def upload_files(parent_id, local_paths, concurrency_limit):
        return tdrive_service.upload_files(parent_id, local_paths, concurrency_limit, gui_progress_callback)

    @eel.expose
    def download_items(items, destination_dir, concurrency_limit):
        return tdrive_service.download_items(items, destination_dir, concurrency_limit, gui_progress_callback)

    @eel.expose
    def cancel_transfer(task_id):
        return tdrive_service.cancel_transfer(task_id)

    @eel.expose
    def get_os_sep():
        return os.sep
    
    # 3. 設定瀏覽器路徑
    bits, _ = platform.architecture()
    arch = "x64" if "64bit" in bits else "x32"
    browser_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome', arch, 'chrome.exe')
    eel.browsers.set_path('chrome', browser_path)
    
    # 4. 初始化 eel
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
    eel.init(web_dir)
    
    logger.info("正在啟動 TDrive GUI...")
    
    # 5. 檢查登入狀態並決定起始頁面
    if tdrive_service.check_startup_login():
        logger.info("偵測到已登入的會話，直接進入主介面。")
        start_page = 'index.html'
    else:
        logger.info("需要登入，顯示登入頁面。")
        start_page = 'login.html'
    
    try:
        # 6. 啟動 eel
        chrome_cmd = ["--disable-cache", "--new-window", "--start-maximized", "--disable-features=Translate", "--disable-infobars", "--disable-extensions", "--disable-notifications", "--disable-component-update", "--disable-popup-blocking", "--disable-sync", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-default-apps"]
        eel.start(
            start_page,
            mode='chrome',
            cmdline_args=chrome_cmd,
            # 傳遞 progress callback 給 upload/download 方法
            # 這一步驟現在改為在 JS 呼叫時直接傳遞，或由 TDriveService 內部處理
        )
    except (SystemExit, MemoryError, KeyboardInterrupt):
        logger.info("偵測到退出訊號。")
    finally:
        # 7. 關閉後端服務
        logger.info("正在關閉 TDriveService...")
        tdrive_service.close()

    logger.info("TDrive GUI 已成功關閉。")
