import logging
import os
import datetime
import glob
import json

LOG_DIR = './file/log'
MAX_LOG_FILES = 5 

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
    os.makedirs(LOG_DIR, exist_ok=True)

    try:
        existing_logs = sorted(glob.glob(os.path.join(LOG_DIR, 'tdrive_*.log')))
        if len(existing_logs) >= MAX_LOG_FILES:
            logs_to_delete = existing_logs[:len(existing_logs) - MAX_LOG_FILES + 1]
            for old_log in logs_to_delete:
                os.remove(old_log)
    except OSError as e:
        print(f"Warning: Failed to remove old log file {e}")

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = os.path.join(LOG_DIR, f'tdrive_{timestamp}.json.log')

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) 

    json_formatter = JSONFormatter()
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        datefmt='%m-%d %H:%M:%S'
    )

    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    logging.getLogger('telethon').setLevel(logging.ERROR)
    logging.getLogger('asyncio').setLevel(logging.ERROR)
    logging.getLogger('geventwebsocket').setLevel(logging.ERROR)
    logging.getLogger('qasync').setLevel(logging.ERROR)

    root_logger.info("Logging initialized. Logs will be saved to %s", log_filename)


