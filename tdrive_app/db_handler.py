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
        執行 CREATE TABLE IF NOT EXISTS 語句，建立所有表格，並初始化根目錄。
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
        i = int(math.floor(math.log(bytes_num, k)))
        return f"{bytes_num / (k ** i):.1f} {sizes[i]}"
    
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

    def _update_modification_time(self, cursor):
        """
        在 metadata 表中更新資料庫的最後修改時間。
        """
        cursor.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                       ('db_last_modified', time.time()))

    def _search_single_folder_items(self, search_term, folder_id):
        """
        根據資料夾id搜尋名稱包含search_term的資料夾與檔案。
        不包含子資料夾的內容，不區分大小寫。
        回傳格式:
        {
            "folders": [
                { "id": 15, "parent_id": 1, "name": "工作文件", "raw_size": 15728640, "size": "15.0 MB", "modif_date": "2025/11/22 下午 03:45" }
            ],
            "files": [
                { "id": 22, "parent_id": 15, "name": "工作簡報.pptx", "raw_size": 5242880, "size": "5.0 MB", "modif_date": "2025/11/22 下午 03:40" }
            ]
        }
        """
        conn = self._get_conn()
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
        
        conn.close()
        return {"folders": folders, "files": files}

    # --- 公開的 API ---
    def get_folder_contents(self, folder_id):
        """
        根據資料夾id回傳資料夾內容。
        回傳格式:
        {
            "folders": [
                { "id": 15, "name": "工作文件", "raw_size": 15728640, "size": "15.0 MB", "modif_date": "2025/11/22 下午 03:45" }
            ],
            "files": [
                { "id": 22, "name": "專案簡報.pptx", "raw_size": 5242880, "size": "5.0 MB", "modif_date": "2025/11/22 下午 03:40" }
            ]
        }
        """
        conn = self._get_conn()
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
        
        conn.close()
        return {"folders": folders, "files": files}

    def get_folder_tree(self):
        """
        獲取資料庫中所有的資料夾，並按名稱排序。主要用於在 UI 左側建立可展開的資料夾樹狀結構。
        回傳格式:
        [
            { "id": 1, "parent_id": null, "name": "TDrive" },
            { "id": 15, "parent_id": 1, "name": "工作文件" }
        ]
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, parent_id, name FROM folders ORDER BY name COLLATE NOCASE")
        all_folders = []
        for row in cursor.fetchall():
            all_folders.append({
                "id": row['id'],
                "parent_id": row['parent_id'],
                "name": row['name']
            })
        
        conn.close()
        return all_folders

    def find_file_by_hash(self, file_hash):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, parent_id, name, size, hash, modif_date FROM files WHERE hash = ?", (file_hash,))
        file_row = cursor.fetchone()
        conn.close()

        if file_row:
            # 獲取分塊資訊
            chunks_data = []
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT part_num, message_id, part_hash FROM chunks WHERE file_id = ?", (file_row['id'],))
            for chunk_row in cursor.fetchall():
                chunks_data.append([chunk_row['part_num'], chunk_row['message_id'], chunk_row['part_hash']])
            conn.close()

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
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO folders (parent_id, name, modif_date, total_size) VALUES (?, ?, ?, ?)",
                    (parent_id, name, time.time(), 0)
                )
                self._update_modification_time(cursor)
        except sqlite3.IntegrityError:
            raise errors.ItemAlreadyExistsError(f"資料夾 '{name}' 已存在。")
        finally:
            if conn:
                conn.close()

    def add_file(self, folder_id, name, size, file_hash, modif_date_ts, chunks_data):
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
                self._update_modification_time(cursor)
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
                self._update_modification_time(cursor)
                
                return message_ids
        finally:
            if conn:
                conn.close()

    def remove_folder(self, folder_id):
        conn = self._get_conn()
        all_message_ids = []
        
        def _recursive_delete(cursor, f_id):
            # 獲取所有子資料夾
            cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (f_id,))
            subfolder_ids = [row['id'] for row in cursor.fetchall()]
            for sub_id in subfolder_ids:
                _recursive_delete(cursor, sub_id)
            
            # 獲取並刪除當前資料夾下的檔案
            cursor.execute("SELECT id FROM files WHERE parent_id = ?", (f_id,))
            file_ids = [row['id'] for row in cursor.fetchall()]
            for file_id in file_ids:
                cursor.execute("SELECT message_id FROM chunks WHERE file_id = ?", (file_id,))
                all_message_ids.extend([row['message_id'] for row in cursor.fetchall()])
                cursor.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
                cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))

            # 刪除當前資料夾
            cursor.execute("DELETE FROM folders WHERE id = ?", (f_id,))

        try:
            with conn:
                cursor = conn.cursor()
                # 獲取資料夾資訊以更新父資料夾大小
                cursor.execute("SELECT parent_id, total_size FROM folders WHERE id = ?", (folder_id,))
                folder_info = cursor.fetchone()
                if not folder_info:
                    raise errors.PathNotFoundError(f"資料夾 ID '{folder_id}' 不存在。")

                _recursive_delete(cursor, folder_id)
                
                # 更新父資料夾的大小 (保留防禦性檢查)
                if folder_info['parent_id'] is not None:
                    self._update_folder_size_recursively(cursor, folder_info['parent_id'], -folder_info['total_size'])
                self._update_modification_time(cursor)
            
            return all_message_ids
        finally:
            if conn:
                conn.close()

    def rename_folder(self, folder_id, new_name):
        """
        根據資料夾id重命名資料夾。
        """
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE folders SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), folder_id))
                self._update_modification_time(cursor)
        except sqlite3.IntegrityError:
            # Check if an item with the new_name already exists in the same parent folder.
            # We need to get the parent_id of the folder being renamed.
            cursor.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
            parent_id_row = cursor.fetchone()
            if parent_id_row:
                parent_id = parent_id_row['parent_id']
                raise errors.ItemAlreadyExistsError(f"在相同的父資料夾下，目標名稱 '{new_name}' 已存在。")
            else:
                # Fallback for unexpected integrity error
                raise errors.TDriveError(f"重命名資料夾 '{folder_id}' 為 '{new_name}' 時發生整合性錯誤。")
        finally:
            if conn:
                conn.close()

    def rename_file(self, file_id, new_name):
        """
        根據檔案id重命名資料夾。
        """
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE files SET name = ?, modif_date = ? WHERE id = ?",
                               (new_name, time.time(), file_id))
                self._update_modification_time(cursor)
        except sqlite3.IntegrityError:
            # Check if an item with the new_name already exists in the same parent folder.
            # We need to get the parent_id of the file being renamed.
            cursor.execute("SELECT parent_id FROM files WHERE id = ?", (file_id,))
            parent_id_row = cursor.fetchone()
            if parent_id_row:
                parent_id = parent_id_row['parent_id']
                raise errors.ItemAlreadyExistsError(f"在相同的父資料夾下，目標名稱 '{new_name}' 已存在。")
            else:
                # Fallback for unexpected integrity error
                raise errors.TDriveError(f"重命名檔案 '{file_id}' 為 '{new_name}' 時發生整合性錯誤。")
        finally:
            if conn:
                conn.close()

    def get_file_details(self, file_id):
        """
        專門用來獲取單一檔案的完整下載資訊。
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        try:
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
        返回一個包含根資料夾名稱和扁平化後代項目列表的物件。
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        try:
            # 1. 獲取根資料夾的名稱
            cursor.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
            root_folder_row = cursor.fetchone()
            if not root_folder_row:
                raise errors.PathNotFoundError(f"找不到根資料夾 ID: {folder_id}")
            root_folder_name = root_folder_row['name']

            # 2. 使用遞迴 CTE 獲取所有後代項目
            # path 為從指定的 folder_id 開始的相對路徑
            query = """
            WITH RECURSIVE
              folder_hierarchy(id, parent_id, name, path) AS (
                -- Anchor member: select the starting folder
                SELECT id, parent_id, name, name
                FROM folders
                WHERE id = :folder_id
                UNION ALL
                -- Recursive member: select children
                SELECT f.id, f.parent_id, f.name, fh.path || '/' || f.name
                FROM folders f
                JOIN folder_hierarchy fh ON f.parent_id = fh.id
              )
            -- 3. 組合資料夾和檔案的結果
            SELECT
              fh.id,
              'folder' as type,
              fh.name,
              SUBSTR(fh.path, LENGTH(:root_name) + 2) as relative_path,
              NULL as size,
              NULL as hash
            FROM folder_hierarchy fh
            WHERE fh.id != :folder_id -- 不包含根目錄本身在列表中

            UNION ALL

            SELECT
              f.id,
              'file' as type,
              f.name,
              CASE
                WHEN fh.id = :folder_id THEN f.name -- 檔案在根目錄下
                ELSE SUBSTR(fh.path, LENGTH(:root_name) + 2) || '/' || f.name
              END as relative_path,
              f.size,
              f.hash
            FROM files f
            JOIN folder_hierarchy fh ON f.parent_id = fh.id;
            """
            cursor.execute(query, {"folder_id": folder_id, "root_name": root_folder_name})
            items = cursor.fetchall()
            
            all_items = [dict(row) for row in items]
            
            # 4. 批次獲取所有相關檔案的分塊資訊
            file_ids = tuple([item['id'] for item in all_items if item['type'] == 'file'])
            
            chunks_map = {}
            if file_ids:
                chunk_query = f"""
                SELECT file_id, part_num, message_id, part_hash
                FROM chunks
                WHERE file_id IN ({','.join(['?'] * len(file_ids))})
                ORDER BY file_id, part_num
                """
                cursor.execute(chunk_query, file_ids)
                for chunk_row in cursor.fetchall():
                    f_id = chunk_row['file_id']
                    if f_id not in chunks_map:
                        chunks_map[f_id] = []
                    chunks_map[f_id].append(dict(chunk_row))

            # 5. 將分塊資訊附加到檔案項目中
            for item in all_items:
                if item['type'] == 'file':
                    item['chunks'] = chunks_map.get(item['id'], [])

            return {
                "folder_name": root_folder_name,
                "items": all_items
            }

        except Exception as e:
            logger.error(f"遞迴獲取資料夾 {folder_id} 內容時發生錯誤: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

    def search_db_items(self, search_term, base_folder_id):
        """
        在資料庫中搜尋檔案和資料夾，並返回格式化的結果。
        搜尋範圍包含base_folder_id及其子資料夾內所有檔案和資料夾。
        使用_search_single_folder_items遞迴。
        回傳格式:
        {
            "folders": [
                { "id": 15, "name": "工作文件", "raw_size": 15728640, "size": "15.0 MB", "modif_date": "2025/11/22 下午 03:45" }
            ],
            "files": [
                { "id": 22, "name": "工作簡報.pptx", "raw_size": 5242880, "size": "5.0 MB", "modif_date": "2025/11/22 下午 03:40" }
            ]
        }
        """
        all_found_folders = []
        all_found_files = []
        
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # 使用佇列進行廣度優先搜尋 (BFS)
        folders_to_visit = [base_folder_id]
        visited_folders = set() # 避免重複處理

        while folders_to_visit:
            current_folder_id = folders_to_visit.pop(0)

            if current_folder_id in visited_folders:
                continue
            visited_folders.add(current_folder_id)

            # 搜尋當前資料夾層級的項目
            local_results = self._search_single_folder_items(search_term, current_folder_id)
            all_found_folders.extend(local_results['folders'])
            all_found_files.extend(local_results['files'])
            
            # 獲取所有子資料夾，將其加入待處理佇列
            cursor.execute("SELECT id FROM folders WHERE parent_id = ?", (current_folder_id,))
            for sub_folder_row in cursor.fetchall():
                folders_to_visit.append(sub_folder_row['id'])
        
        conn.close()
        
        return {"folders": all_found_folders, "files": all_found_files}
    
    def get_modification_time(self, db_path=None):
        """
        獲取資料庫的最後修改時間。
        :param db_path: 可選的資料庫路徑，如果提供則連接到該路徑。
        :return: 最後修改時間 (Unix timestamp) 或 None。
        """
        target_db_path = db_path if db_path else self.db_path
        
        # 檢查檔案是否存在，避免連接不存在的檔案
        if not os.path.exists(target_db_path):
            logger.warning(f"資料庫檔案 '{target_db_path}' 不存在，無法獲取修改時間。")
            return None

        conn = None
        try:
            conn = self._get_conn(db_path=target_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM metadata WHERE key = 'db_last_modified'")
            result = cursor.fetchone()
            if result:
                return float(result['value'])
            return None
        except Exception as e:
            logger.error(f"從資料庫 '{target_db_path}' 獲取修改時間失敗: {e}", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()

