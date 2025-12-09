import logging
import os
import json
import asyncio
import base64
import qrcode
import io
import shutil
import time
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, ApiIdInvalidError, PasswordHashInvalidError

from typing import TYPE_CHECKING, Dict, Any, Optional
if TYPE_CHECKING:
    from ..shared_state import SharedState

from . import utils
from .. import crypto_handler, telegram_comms, errors
from ..db_handler import DatabaseHandler

logger = logging.getLogger(__name__)
db = DatabaseHandler()

class AuthService:
    """
    處理所有與使用者認證、授權和會話管理相關的服務。
    """
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    # --- 憑證管理 ---
    def _get_saved_api_credentials(self) -> (Optional[int], Optional[str]):
        try:
            if not os.path.exists('./file/info.json'):
                logger.info("本地 info.json 檔案不存在。")
                return None, None
            
            with open('./file/info.json', 'r') as f:
                info = json.load(f)
            api_id = info.get("api_id")
            encrypted_blob = info.get("secure_data_blob")
            
            if not (api_id and encrypted_blob):
                logger.warning("info.json 檔案不完整，缺少 api_id 或 secure_data_blob。")
                return None, None

            decrypted_data = crypto_handler.decrypt_secure_data(encrypted_blob, str(api_id))
            if decrypted_data:
                logger.info("成功從本地載入並解密 API 憑證。")
                return int(api_id), decrypted_data.get("api_hash")
            else:
                logger.warning("解密本地 API 憑證失敗。")
                return None, None
        except Exception as e:
            logger.error(f"讀取憑證時發生未預期錯誤: {e}", exc_info=True)
            return None, None

    def _save_api_credentials(self):
        try:
            os.makedirs('./file', exist_ok=True)
            current_info = {}
            if os.path.exists('./file/info.json'):
                try:
                    with open('./file/info.json', 'r') as f:
                        current_info = json.load(f)
                except json.JSONDecodeError:
                    logger.warning("本地 info.json 檔案損壞或為空，將重新創建。")
            
            secure_data = {"api_hash": self.shared_state.api_hash}
            if 'group_id' in current_info:
                secure_data['group_id'] = current_info['group_id']

            encrypted_blob = crypto_handler.encrypt_secure_data(secure_data, str(self.shared_state.api_id))
            final_info = { "api_id": self.shared_state.api_id, "secure_data_blob": encrypted_blob }

            with open('./file/info.json', 'w') as f:
                json.dump(final_info, f)
            logger.info("API 憑證已成功加密並儲存到本地。")
        except Exception as e:
            logger.error(f"儲存憑證失敗: {e}", exc_info=True)

    # --- 核心認證邏輯 ---
    
    async def check_startup_login(self) -> bool:
        api_id, api_hash = self._get_saved_api_credentials()
        if not (api_id and api_hash):
            logger.info("未找到本地儲存的 API 憑證，需要登入。")
            return False
            
        session_file = f'./file/user_{api_id}.session'
        client = TelegramClient(session_file, api_id, api_hash)
        try:
            await client.connect()
            if await client.is_user_authorized():
                self.shared_state.client = client
                self.shared_state.api_id = api_id
                self.shared_state.api_hash = api_hash
                self.shared_state.is_logged_in = True
                
                logger.info("檢測到有效會話，正在執行啟動時初始化...")
                await self.initialize_drive()
                logger.info("啟動時初始化完成。")
                return True
            else:
                logger.info("會話無效，需要重新登入。")
                await client.disconnect()
                return False
        except Exception as e:
            logger.error(f"啟動檢查或初始化時發生錯誤: {e}", exc_info=True)
            if client and client.is_connected(): await client.disconnect()
            return False

    async def verify_api_credentials(self, api_id: int, api_hash: str) -> Dict[str, Any]:
        client = None
        try:
            os.makedirs('./file', exist_ok=True)
            session_file = f'./file/user_{api_id}.session'
            client = TelegramClient(session_file, api_id, api_hash)
            await client.connect()
            
            self.shared_state.client = client
            self.shared_state.api_id = api_id
            self.shared_state.api_hash = api_hash
            
            if await client.is_user_authorized():
                self.shared_state.is_logged_in = True
                self._save_api_credentials()
                return {"success": True, "authorized": True}
            else:
                return {"success": True, "authorized": False}
        except ApiIdInvalidError as e:
            logger.warning(f"API 憑證驗證失敗: {e}")
            if client and client.is_connected(): await client.disconnect()
            return {"success": False, "error_code": "INVALID_API_CREDENTIALS", "message": "無效的 API ID 或 API Hash。"}
        except Exception as e:
            logger.error(f"驗證 API 憑證時連線失敗: {e}", exc_info=True)
            if client and client.is_connected(): await client.disconnect()
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": f"連線失敗: {e}"}

    async def start_qr_login(self, event_callback) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client or not client.is_connected():
            logger.warning("請求啟動 QR 登入，但客戶端未連線。")
            return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "客戶端未連線。"}
            
        try:
            qr_login = await client.qr_login()
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(qr_login.url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            img_str = base64.b64encode(buffer.getvalue()).decode()

            # 在背景中等待 QR 登入完成，並使用回呼函式發送事件
            self.shared_state.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._wait_for_qr_login(qr_login, event_callback)))

            return {"success": True, "qr_url": f"data:image/png;base64,{img_str}"}
        except Exception as e:
            logger.error(f"QR 碼產生失敗: {e}", exc_info=True)
            return {"success": False, "error_code": "QR_GENERATION_FAILED", "message": f"QR 碼產生失敗: {e}"}

    async def _wait_for_qr_login(self, qr_login, event_callback):
        try:
            await qr_login.wait()
            self.shared_state.is_logged_in = True
            self._save_api_credentials()
            event_callback({"status": "completed"})
        except SessionPasswordNeededError:
            event_callback({"status": "password_needed"})
        except Exception as e:
            error_message = str(e)
            logger.warning(f"QR 登入等待過程中發生錯誤: {error_message}")
            if "QR code expired" in error_message or "code expired" in error_message.lower():
                event_callback({"status": "expired", "error": "QR 碼已過期"})
            else:
                event_callback({"status": "failed", "error": f"登入失敗: {error_message}"})

    async def send_code_request(self, phone_number: str) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client: return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "客戶端未初始化"}
        try:
            sent_code = await client.send_code_request(phone_number)
            self.shared_state.phone = phone_number
            self.shared_state.phone_code_hash = sent_code.phone_code_hash
            return {"success": True}
        except Exception as e:
            logger.error(f"發送驗證碼到 {phone_number} 失敗: {e}", exc_info=True)
            return {"success": False, "error_code": "SEND_CODE_FAILED", "message": f"發送驗證碼失敗: {e}"}

    async def submit_verification_code(self, code: str) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client: return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "客戶端未初始化"}
        try:
            await client.sign_in(self.shared_state.phone, code, phone_code_hash=self.shared_state.phone_code_hash)
            self.shared_state.is_logged_in = True
            self._save_api_credentials()
            return {"success": True}
        except PhoneCodeInvalidError as e:
            logger.warning(f"提交了無效的驗證碼: {e}")
            return {"success": False, "error_code": "INVALID_VERIFICATION_CODE", "message": "驗證碼不正確。"}
        except SessionPasswordNeededError:
            return {"success": True, "password_needed": True}
        except Exception as e:
            logger.error(f"提交驗證碼時發生未知錯誤: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": f"驗證失敗: {e}"}

    async def submit_password(self, password: str) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client: return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "客戶端未初始化"}
        try:
            await client.sign_in(password=password)
            self.shared_state.is_logged_in = True
            self._save_api_credentials()
            return {"success": True}
        except PasswordHashInvalidError as e:
            logger.warning(f"提交了無效的兩步驟驗證密碼: {e}")
            return {"success": False, "error_code": "INVALID_PASSWORD", "message": "密碼不正確。"}
        except Exception as e:
            logger.error(f"提交密碼時發生未知錯誤: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": f"登入失敗: {e}"}

    async def initialize_drive(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client or not self.shared_state.api_id:
            msg = "無法初始化：Client 或 App API ID 不存在。"
            logger.error(msg)
            return {"success": False, "error_code": "INITIALIZATION_FAILED", "message": msg}

        try:
            logger.info("正在初始化 TDrive...")
            group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
            await telegram_comms.sync_database_file(client, group_id, mode='sync')
            return {"success": True}
        except Exception as e:
            logger.error(f"磁碟初始化過程中發生錯誤: {e}", exc_info=True)
            return {"success": False, "error_code": "DRIVE_INITIALIZATION_FAILED", "message": "初始化失敗，無法同步遠端資料庫。"}


    async def get_user_info(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗"}
        try:
            me = await client.get_me()
            return {
                "success": True,
                "name": f"{me.first_name} {me.last_name or ''}".strip(),
                "phone": f"+{me.phone}" if me.phone else "unknown",
                "username": me.username or "unknown",
                "storage_group": "TDrive"
            }
        except Exception as e:
            logger.error(f"獲取使用者資訊時發生未知錯誤: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "獲取使用者資訊失敗"}

    async def get_user_avatar(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗"}
        
        avatar_path = './file/user_avatar.jpg'
        try:
            path = await client.download_profile_photo('me', file=avatar_path)
            if not path: 
                logger.info("使用者沒有設定頭像。")
                return {"success": False, "error_code": "AVATAR_NOT_FOUND", "message": "沒有設定頭像"}
            with open(path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            os.remove(path)
            return {"success": True, "avatar_base64": f"data:image/jpeg;base64,{encoded_string}"}
        except Exception as e:
            logger.error(f"獲取使用者頭像時發生未知錯誤: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "獲取使用者頭像失敗"}

    async def perform_logout(self) -> Dict[str, Any]:
        logger.info("開始執行登出程序...")
        client = self.shared_state.client
        if client and client.is_connected():
            await client.disconnect()
            logger.info("已成功斷開連線。")
        try:
            if os.path.exists('./file'):
                shutil.rmtree('./file')
        except Exception as e:
            logger.error(f"登出時清理本機檔案失敗: {e}", exc_info=True)
            # Log the error but don't block the rest of the logout process
            # Let the frontend know something went wrong but logout can still proceed
            return {"success": True, "warning": "登出完成，但部分本地檔案清理失敗。"}
        
        self.shared_state.client = None
        self.shared_state.api_id = None
        self.shared_state.api_hash = None
        self.shared_state.is_logged_in = False
        logger.info("已清除記憶體中的登入狀態。")
        return {"success": True}