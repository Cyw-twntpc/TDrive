"""
Configures the logging system for the TDrive application.

This module sets up a dual-logging system:
1.  A rotating JSON log file for structured, machine-readable logs.
2.  A human-readable console output for real-time monitoring during development.

It also filters out excessive noise from third-party libraries.
"""
import logging
import os
import datetime
import glob
import json

LOG_DIR = './file/log'
# Maximum number of log files to keep. Older files will be deleted.
MAX_LOG_FILES = 5 

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_object = {
            "timestamp": datetime.datetime.fromtimestamp(record.created).strftime('%m-%d %H:%M:%S'),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        }
        # Use ensure_ascii=False to correctly handle non-ASCII characters in logs.
        return json.dumps(log_object, ensure_ascii=False)

def setup_logging():
    """
    Sets up the application's root logger and handlers.
    """
    # Ensure the log directory exists.
    os.makedirs(LOG_DIR, exist_ok=True)

    # --- Log Rotation Logic ---
    # Clean up the oldest log files if the number of logs exceeds the maximum.
    try:
        existing_logs = sorted(glob.glob(os.path.join(LOG_DIR, 'tdrive_*.log')))
        if len(existing_logs) >= MAX_LOG_FILES:
            logs_to_delete = existing_logs[:len(existing_logs) - MAX_LOG_FILES + 1]
            for old_log in logs_to_delete:
                os.remove(old_log)
    except OSError as e:
        # Use a simple print here as logging might not be fully configured yet.
        print(f"Warning: Failed to remove old log file {e}")

    # Generate a new log filename with a timestamp.
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = os.path.join(LOG_DIR, f'tdrive_{timestamp}.json.log')

    # Configure the root logger.
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) # Set the lowest level to capture all logs.

    # --- Formatter Definitions ---
    # JSON formatter for file output.
    json_formatter = JSONFormatter()
    # Human-readable text formatter for console output.
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        datefmt='%m-%d %H:%M:%S'
    )

    # --- Handler Setup ---
    # 1. File Handler (writes to the new log file with JSON format).
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    # 2. Console Handler (writes to the console with plain text format).
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # --- Filter noisy third-party libraries ---
    # Set the log level for noisy libraries to ERROR to only record critical issues.
    logging.getLogger('telethon').setLevel(logging.ERROR)
    logging.getLogger('asyncio').setLevel(logging.ERROR)
    logging.getLogger('geventwebsocket').setLevel(logging.ERROR)
    logging.getLogger('qasync').setLevel(logging.ERROR)

    root_logger.info("Logging initialized. Logs will be saved to %s", log_filename)


