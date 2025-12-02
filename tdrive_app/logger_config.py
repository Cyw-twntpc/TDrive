import logging
import logging.handlers
import os
import datetime
import glob
import json # 匯入 json 模組

LOG_DIR = './file/log'
MAX_LOG_FILES = 5 # 最多保留 5 份日誌

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_object = {
            "timestamp": datetime.datetime.fromtimestamp(record.created).strftime('%m-%d %H:%M:%S'),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        }
        return json.dumps(log_object, ensure_ascii=False)

def setup_logging():
    """配置應用程式的日誌系統。"""
    # 確保日誌目錄存在
    os.makedirs(LOG_DIR, exist_ok=True)

    # 日誌輪替邏輯：刪除最舊的日誌檔
    existing_logs = sorted(glob.glob(os.path.join(LOG_DIR, 'tdrive_*.log')))
    if len(existing_logs) >= MAX_LOG_FILES:
        for old_log in existing_logs[:len(existing_logs) - MAX_LOG_FILES + 1]:
            try:
                os.remove(old_log)
                # print(f"已刪除舊日誌檔: {old_log}") # 避免在設定日誌前有 print
            except OSError as e:
                # print(f"刪除舊日誌檔 {old_log} 失敗: {e}")
                pass

    # 產生帶時間戳的日誌檔名
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = os.path.join(LOG_DIR, f'tdrive_{timestamp}.json.log') # 修改副檔名

    # 配置根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) # 設定最低日誌級別為 INFO

    # --- 兩種日誌格式 ---
    # 檔案格式：JSON
    json_formatter = JSONFormatter()

    # 終端格式：人類可讀的純文字 (保留原先格式，方便閱讀)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        datefmt='%m-%d %H:%M:%S'
    )

    # --- 處理器設定 ---
    # 檔案處理器 (寫入到新日誌檔，使用 JSON 格式)
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    # 終端處理器 (輸出到控制台，使用純文字格式)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    root_logger.info("日誌系統初始化完成。日誌將儲存到 %s", log_filename)

if __name__ == '__main__':
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("這是日誌模組的測試訊息。")
    logger.warning("這是一條警告訊息。")
    logger.error("這是一條錯誤訊息。")
