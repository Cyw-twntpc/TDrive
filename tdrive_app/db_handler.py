import sqlite3
import os
import time
import datetime
import logging
import math

from . import errors

logger = logging.getLogger(__name__) # Use module-level logger

class DatabaseHandler:
    def __init__(self, db_path='./file/tdrive.db'):
        """
        建構函式，初始化資料庫路徑並確保表格結構存在。
        """
        self.db_path = db_path
        # 確保資料庫檔案所在的目錄存在
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self, db_path=None):
        """建立並回傳一個資料庫連線，並設定 row_factory。"""
        path_to_connect = db_path if db_path else self.db_path
        conn = sqlite3.connect(path_to_connect)
        conn.row_factory = sqlite3.Row # 使結果可以透過欄位名稱存取
        return conn

    def _init_db(self):
        """
        執行 CREATE TABLE IF NOT EXISTS 語句，建立所有表格，並初始化根目錄和 metadata。
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        logger.info("正在初始化資料庫表格結構...")

        # 建立 folders 表格
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            name TEXT NOT NULL,
            total_size REAL DEFAULT 0, -- REAL for sums of REAL sizes
            modif_date REAL, -- Unix timestamp
            FOREIGN KEY (parent_id) REFERENCES folders (id),
            UNIQUE (parent_id, name)
        )
        ''')

        # 建立 files 表格
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            size REAL NOT NULL, -- REAL for file sizes
            hash TEXT NOT NULL,
            modif_date REAL, -- Unix timestamp
            FOREIGN KEY (parent_id) REFERENCES folders (id),
            UNIQUE (parent_id, name)
        )
        ''')

        # 建立 chunks 表格
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            part_num INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            part_hash TEXT NOT NULL,
            FOREIGN KEY (file_id) REFERENCES files (id),
            UNIQUE (file_id, part_num)
        )
        ''')

        # 建立 metadata 表格
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''')
        
        # 檢查並初始化根目錄 'TDrive'
        cursor.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name = 'TDrive'")
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO folders (parent_id, name, modif_date) VALUES (?, ?, ?)", 
                           (None, 'TDrive', time.time()))
        
        # 檢查並初始化 db_version
        cursor.execute("SELECT value FROM metadata WHERE key = 'db_version'")
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO metadata (key, value) VALUES ('db_version', '0')")
        
        conn.commit()
        conn.close()
        logger.info("資料庫表格初始化完成。")

    def _format_timestamp(self, ts):
        """
        將 Unix 時間戳轉換為使用者偏好的字串格式 (例如 '2023/11/21 上午 02:30')。
        """
        if ts is None:
            return "-"
        try:
            dt_obj = datetime.datetime.fromtimestamp(ts)
            # 使用 %p 處理 AM/PM，然後替換為中文
            return dt_obj.strftime("%Y/%m/%d %p %I:%M").replace("AM", "上午").replace("PM", "下午")
        except (ValueError, TypeError):
            return "-"

    def _format_size(self, bytes_num):
        """將位元組數轉換為易於閱讀的格式 (KB, MB, GB)。"""
        if not isinstance(bytes_num, (int, float)):
            return "N/A"
        if bytes_num is None or bytes_num == 0:
            return "0 B"
        k = 1024
        sizes = ['B', 'KB', 'MB', 'GB', 'TB']
        # Handle log(0) case
        if bytes_num < 1:
            i = 0
        else:
            i = int(math.floor(math.log(bytes_num, k)))
        return f"{bytes_num / (k ** i):.1f} {sizes[i]}"
    
    def _is_valid_item_name(self, name):
        """檢查名稱是否有效，防止路徑遍歷和無效檔名。"""
        if not name or name == "." or name == "..":
            return False
        # 檢查是否包含任何路徑分隔符或常見的無效字元
        if any(c in name for c in r'\/<>:"|?*'):
            return False
        return True

    def _update_folder_size_recursively(self, cursor, folder_id, size_delta):
        """
        遞迴更新父資料夾的大小。
        """
        current_id = folder_id
        while current_id is not None:
            cursor.execute("UPDATE folders SET total_size = total_size + ? WHERE id = ?", (size_delta, current_id))
            cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (current_id,))
            result = cursor.fetchone()
            current_id = result['parent_id'] if result else None

    def _increment_db_version(self, cursor):
        """
        在 metadata 表中將 db_version 加 1。
        """
        cursor.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'db_version'")

    def _search_single_folder_items(self, search_term, folder_id):
        """
        根據資料夾id搜尋名稱包含search_term的資料夾與檔案。
        不包含子資料夾的內容，不區分大小寫。
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            search_pattern = f'%{search_term}%'
            
            folders = []
            files = []

            # 查詢子資料夾，並包含 parent_id
            cursor.execute("SELECT id, name, total_size, modif_date, parent_id FROM folders WHERE parent_id = ? AND name LIKE ? COLLATE NOCASE", 
                           (folder_id, search_pattern))
            for row in cursor.fetchall():
                folders.append({
                    "id": row['id'],
                    "parent_id": row['parent_id'],
                    "name": row['name'],
                    "raw_size": row['total_size'],
                    "size": self._format_size(row['total_size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })

            # 查詢檔案
            cursor.execute("SELECT id, name, size, modif_date FROM files WHERE parent_id = ? AND name LIKE ? COLLATE NOCASE",
                           (folder_id, search_pattern))
            for row in cursor.fetchall():
                files.append({
                    "id": row['id'],
                    "parent_id": folder_id, # 檔案的 parent_id 就是當前搜尋的 folder_id
                    "name": row['name'],
                    "raw_size": row['size'],
                    "size": self._format_size(row['size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })
            
            return {"folders": folders, "files": files}
        finally:
            if conn:
                conn.close()

    # --- 公開的 API ---
    def get_folder_contents(self, folder_id):
        """
        根據資料夾id回傳資料夾內容。
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            
            folders = []
            files = []

            # 查詢子資料夾
            cursor.execute("SELECT id, name, total_size, modif_date FROM folders WHERE parent_id = ?", (folder_id,))
            for row in cursor.fetchall():
                folders.append({
                    "id": row['id'],
                    "name": row['name'],
                    "raw_size": row['total_size'],
                    "size": self._format_size(row['total_size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })

            # 查詢檔案
            cursor.execute("SELECT id, name, size, modif_date FROM files WHERE parent_id = ?", (folder_id,))
            for row in cursor.fetchall():
                files.append({
                    "id": row['id'],
                    "name": row['name'],
                    "raw_size": row['size'],
                    "size": self._format_size(row['size']),
                    "modif_date": self._format_timestamp(row['modif_date'])
                })
            
            return {"folders": folders, "files": files}
        finally:
            if conn:
                conn.close()

    def get_folder_tree(self):
        """
        獲取資料庫中所有的資料夾，並按名稱排序。
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            
            cursor.execute("SELECT id, parent_id, name FROM folders ORDER BY name COLLATE NOCASE")
            all_folders = [dict(row) for row in cursor.fetchall()]
            
            return all_folders
        finally:
            if conn:
                conn.close()

    def find_file_by_hash(self, file_hash):
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT id, parent_id, name, size, hash, modif_date FROM files WHERE hash = ?", (file_hash,))
            file_row = cursor.fetchone()
        finally:
            if conn:
                conn.close()

        if file_row:
            chunks_data = []
            conn_chunks = None
            try:
                conn_chunks = self._get_conn()
                cursor_chunks = conn_chunks.cursor()
                cursor_chunks.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ?", (file_row['id'],))
                for chunk_row in cursor_chunks.fetchall():
                    chunks_data.append([chunk_row['part_num'], chunk_row['message_id'], chunk_row['part_hash']])
            finally:
                if conn_chunks:
                    conn_chunks.close()

            return {
                "id": file_row['id'],
                "parent_id": file_row['parent_id'],
                "name": file_row['name'],
                "size": file_row['size'],
                "hash": file_row['hash'],
                "modif_date": file_row['modif_date'],
                "split_files": chunks_data
            }
        return None
    
    def add_folder(self, parent_id, name):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"資料夾名稱 '{name}' 包含無效字元。")
        
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)",
                    (parent_id, name, time.time(), 0)
                )
                self._increment_db_version(cursor)
        except sqlite3.IntegrityError:
            raise errors.ItemAlreadyExistsError(f"資料夾 '{name}' 已存在。")
        finally:
            if conn:
                conn.close()

    def add_file(self, folder_id, name, size, file_hash, modif_date_ts, chunks_data):
        if not self._is_valid_item_name(name):
            raise errors.InvalidNameError(f"檔案名稱 '{name}' 包含無效字元。")

        conn = self._get_conn()
        try:
            with conn: # 使用 with 陳述句自動處理交易
                cursor = conn.cursor()
                # 插入檔案紀錄，使用 parent_id
                cursor.execute(
                    "INSERT INTO files (parent_id, name, size, hash, modif_date) VALUES (?, ?, ?, ?, ?)",
                    (folder_id, name, size, file_hash, modif_date_ts)
                )
                new_file_id = cursor.lastrowid
                
                # 插入分塊紀錄
                if chunks_data:
                    chunk_records = [(new_file_id, c[0], c[1], c[2]) for c in chunks_data]
                    cursor.executemany("INSERT INTO chunks (file_id, part_num, message_id, part_hash) VALUES (?, ?, ?, ?)", chunk_records)
                
                # 遞迴更新資料夾大小
                self._update_folder_size_recursively(cursor, folder_id, size)
                self._increment_db_version(cursor)
        except sqlite3.IntegrityError:
            raise errors.ItemAlreadyExistsError(f"檔案 '{name}' 或其 hash 已存在。")
        finally:
            if conn:
                conn.close()

    def remove_file(self, file_id):
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                # 獲取檔案資訊以便後續更新，使用 parent_id
                cursor.execute("SELECT parent_id, size FROM files WHERE id = ?", (file_id,))
                file_info = cursor.fetchone()
                if not file_info:
                    raise errors.PathNotFoundError(f"檔案 ID '{file_id}' 不存在。")
                
                # 獲取待刪除的 message_ids
                cursor.execute("SELECT message_id FROM chunks WHERE file_id = ?", (file_id,))
                message_ids = [row['message_id'] for row in cursor.fetchall()]

                # 執行刪除
                cursor.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
                cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
                
                # 更新資料夾大小
                self._update_folder_size_recursively(cursor, file_info['parent_id'], -file_info['size']) # 使用 parent_id
                self._increment_db_version(cursor)
                
                return message_ids
        finally:
            if conn:
                conn.close()

    def remove_folder(self, folder_id):
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                
                # 1. 獲取資料夾資訊以更新父資料夾大小
                cursor.execute("SELECT parent_id, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"資料夾 ID '{folder_id}' 不存在。")

                # 2. 使用 CTE 遞迴獲取所有後代資料夾和檔案的 ID
                get_descendants_query = """
                WITH RECURSIVE folder_hierarchy(id) AS (
                    SELECT ?
                    UNION ALL
                    SELECT f.id FROM folders f JOIN folder_hierarchy fh ON f.parent_id = fh.id
                )
                SELECT id FROM folder_hierarchy;
                """
                cursor.execute(get_descendants_query, (folder_id,))
                all_folder_ids = [row['id'] for row in cursor.fetchall()]
                
                id_placeholders = ','.join(['?'] * len(all_folder_ids))
                
                cursor.execute(f"SELECT id FROM files WHERE parent_id IN ({id_placeholders})", all_folder_ids)
                all_file_ids = [row['id'] for row in cursor.fetchall()]

                all_message_ids = []
                if all_file_ids:
                    file_id_placeholders = ','.join(['?'] * len(all_file_ids))
                    # 3. 批次獲取所有 message_id
                    cursor.execute(f"SELECT message_id FROM chunks WHERE file_id IN ({file_id_placeholders})", all_file_ids)
                    all_message_ids = [row['message_id'] for row in cursor.fetchall()]
                    
                    # 4. 批次刪除 chunks 和 files
                    cursor.execute(f"DELETE FROM chunks WHERE file_id IN ({file_id_placeholders})", all_file_ids)
                    cursor.execute(f"DELETE FROM files WHERE id IN ({file_id_placeholders})", all_file_ids)

                # 5. 批次刪除資料夾
                cursor.execute(f"DELETE FROM folders WHERE id IN ({id_placeholders})", all_folder_ids)
                
                # 6. 更新父資料夾的大小
                if folder_info['parent_id'] is not None:
                    self._update_folder_size_recursively(cursor, folder_info['parent_id'], -folder_info['total_size'])
                
                self._increment_db_version(cursor)
                
                return all_message_ids
        finally:
            if conn:
                conn.close()

    def rename_folder(self, folder_id, new_name):
        """
        根據資料夾id重命名資料夾。
        """
        if not self._is_valid_item_name(new_name):
            raise errors.InvalidNameError(f"資料夾名稱 '{new_name}' 包含無效字元。")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                # 獲取要重新命名資料夾的 parent_id
                cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"資料夾 ID '{folder_id}' 不存在。")
                parent_id = folder_info['parent_id']

                # 檢查同一個 parent_id 下是否已存在同名資料夾 (排除自身)
                cursor.execute("SELECT id FROM folders WHERE parent_id = ? AND name = ? AND id != ?",
                               (parent_id, new_name, folder_id))
                if cursor.fetchone():
                    raise errors.ItemAlreadyExistsError(f"在相同的父資料夾下，目標名稱 '{new_name}' 已存在。")

                cursor.execute("UPDATE folders SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), folder_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def rename_file(self, file_id, new_name):
        """
        根據檔案id重命名資料夾。
        """
        if not self._is_valid_item_name(new_name):
            raise errors.InvalidNameError(f"檔案名稱 '{new_name}' 包含無效字元。")

        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                # 獲取要重新命名檔案的 parent_id
                cursor.execute("SELECT parent_id FROM files WHERE id = ?", (file_id,))
                file_info = cursor.fetchone()
                if not file_info:
                    raise errors.PathNotFoundError(f"檔案 ID '{file_id}' 不存在。")
                parent_id = file_info['parent_id']

                # 檢查同一個 parent_id 下是否已存在同名檔案 (排除自身)
                cursor.execute("SELECT id FROM files WHERE parent_id = ? AND name = ? AND id != ?",
                               (parent_id, new_name, file_id))
                if cursor.fetchone():
                    raise errors.ItemAlreadyExistsError(f"在相同的父資料夾下，目標名稱 '{new_name}' 已存在。")

                cursor.execute("UPDATE files SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), file_id))
                self._increment_db_version(cursor)
        finally:
            if conn:
                conn.close()

    def get_file_details(self, file_id):
        """
        專門用來獲取單一檔案的完整下載資訊。
        """
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # 獲取檔案基本資訊
            cursor.execute("SELECT id, name, size, hash FROM files WHERE id = ?", (file_id,))
            file_row = cursor.fetchone()

            if not file_row:
                logger.warning(f"在 get_file_details 中找不到檔案 ID: {file_id}")
                return None

            # 獲取分塊資訊
            cursor.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ? ORDER BY part_num", (file_id,))
            chunks_data = cursor.fetchall()

            return {
                "id": file_row['id'],
                "name": file_row['name'],
                "size": file_row['size'],
                "hash": file_row['hash'],
                "chunks": [{"part_num": r['part_num'], "message_id": r['message_id'], "part_hash": r['part_hash']} for r in chunks_data]
            }
        except Exception as e:
            logger.error(f"獲取檔案 {file_id} 詳細資訊時發生錯誤: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def get_folder_contents_recursive(self, folder_id):
        """
        專門用來遞迴獲取一個資料夾內的所有內容。
        """
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # 1. 獲取根資料夾的名稱
            cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
            root_folder_row = cursor.fetchone()
            if not root_folder_row:
                raise errors.PathNotFoundError(f"找不到根資料夾 ID: {folder_id}")
            root_folder_name = root_folder_row['name']

            # 2. 使用遞迴 CTE 獲取所有後代項目
            query = """
            WITH RECURSIVE
              folder_hierarchy(id, parent_id, name, path) AS (
                SELECT id, parent_id, name, name FROM folders WHERE id = :folder_id
                UNION ALL
                SELECT f.id, f.parent_id, f.name, fh.path || '/' || f.name FROM folders f JOIN folder_hierarchy fh ON f.parent_id = fh.id
              )
            SELECT
              fh.id, 'folder' as type, fh.name, SUBSTR(fh.path, LENGTH(:root_name) + 2) as relative_path,
              NULL as size, NULL as hash
            FROM folder_hierarchy fh WHERE fh.id != :folder_id
            UNION ALL
            SELECT
              f.id, 'file' as type, f.name,
              CASE WHEN fh.id = :folder_id THEN f.name ELSE SUBSTR(fh.path, LENGTH(:root_name) + 2) || '/' || f.name END as relative_path,
              f.size, f.hash
            FROM files f JOIN folder_hierarchy fh ON f.parent_id = fh.id;
            """
            cursor.execute(query, {"folder_id": folder_id, "root_name": root_folder_name})
            items = [dict(row) for row in cursor.fetchall()]
            
            # 4. 批次獲取所有相關檔案的分塊資訊
            file_ids = tuple([item['id'] for item in items if item['type'] == 'file'])
            
            chunks_map = {}
            if file_ids:
                chunk_query = f"SELECT file_id, part_num, message_id, part_hash FROM chunks WHERE file_id IN ({','.join(['?'] * len(file_ids))}) ORDER BY file_id, part_num"
                cursor.execute(chunk_query, file_ids)
                for chunk_row in cursor.fetchall():
                    f_id = chunk_row['file_id']
                    if f_id not in chunks_map: chunks_map[f_id] = []
                    chunks_map[f_id].append(dict(chunk_row))

            # 5. 將分塊資訊附加到檔案項目中
            for item in items:
                if item['type'] == 'file': item['chunks'] = chunks_map.get(item['id'], [])

            return {"folder_name": root_folder_name, "items": items}

        except Exception as e:
            logger.error(f"遞迴獲取資料夾 {folder_id} 內容時發生錯誤: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def search_db_items(self, search_term, base_folder_id, progress_callback):
        """
        在資料庫中串流式搜尋檔案和資料夾，並透過回呼函式分批回傳結果。
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            
            folders_to_visit = [base_folder_id]
            visited_folders = set()
            
            # --- 新增：批次處理機制 ---
            folders_batch = []
            files_batch = []
            BATCH_SIZE = 50

            def yield_batch():
                nonlocal folders_batch, files_batch
                if folders_batch or files_batch:
                    progress_callback({"folders": folders_batch, "files": files_batch})
                    folders_batch, files_batch = [], []

            while folders_to_visit:
                current_folder_id = folders_to_visit.pop(0)
                if current_folder_id in visited_folders:
                    continue
                visited_folders.add(current_folder_id)

                local_results = self._search_single_folder_items(search_term, current_folder_id)
                
                if local_results["folders"]:
                    folders_batch.extend(local_results["folders"])
                if local_results["files"]:
                    files_batch.extend(local_results["files"])

                # 檢查是否達到批次大小
                if len(folders_batch) + len(files_batch) >= BATCH_SIZE:
                    yield_batch()

                cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (current_folder_id,))
                for sub_folder_row in cursor.fetchall():
                    folders_to_visit.append(sub_folder_row['id'])

            # 迴圈結束後，處理剩餘的批次
            yield_batch()
            
        finally:
            if conn:
                conn.close()
    
    def get_db_version(self, db_path=None):
        """
        獲取資料庫的版本號。
        :param db_path: 可選的資料庫路徑，如果提供則連接到該路徑。
        :return: 版本號 (int) 或 0。
        """
        target_db_path = db_path if db_path else self.db_path
        
        if not os.path.exists(target_db_path):
            logger.warning(f"資料庫檔案 '{target_db_path}' 不存在，無法獲取版本號。")
            return 0

        conn = self._get_conn(db_path=target_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM metadata WHERE key = 'db_version'")
            result = cursor.fetchone()
            if result:
                return int(result['value'])
            return 0 # 如果找不到，預設為 0
        except Exception as e:
            logger.error(f"從資料庫 '{target_db_path}' 獲取版本號失敗: {e}", exc_info=True)
            return 0
        finally:
            if conn:
                conn.close()
