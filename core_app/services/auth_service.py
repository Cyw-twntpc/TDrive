import logging
import os
import json
import asyncio
import base64
import qrcode
import io
import shutil
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, ApiIdInvalidError, PasswordHashInvalidError, PhoneNumberInvalidError

from typing import TYPE_CHECKING, Dict, Any, Optional, Callable
if TYPE_CHECKING:
    from core_app.data.shared_state import SharedState

from . import utils
from core_app.api import crypto_handler
from core_app.api import telegram_comms


logger = logging.getLogger(__name__)

class AuthService:
    def __init__(self, shared_state: 'SharedState'):
        self.shared_state = shared_state

    # --- Credential Management ---

    def _get_saved_api_credentials(self) -> Optional[tuple[int, str]]:
        try:
            info_path = './file/info.json'
            if not os.path.exists(info_path):
                logger.info("Local credentials file 'info.json' not found.")
                return None, None
            
            with open(info_path, 'r') as f:
                info = json.load(f)
            
            api_id = info.get("api_id")
            encrypted_blob = info.get("secure_data_blob")
            if not (api_id and encrypted_blob):
                logger.warning("Credentials file 'info.json' is incomplete.")
                return None, None

            decrypted_data = crypto_handler.decrypt_secure_data(encrypted_blob, str(api_id))
            if decrypted_data:
                logger.info("Successfully loaded and decrypted API credentials from local storage.")
                return int(api_id), decrypted_data.get("api_hash")
            else:
                logger.warning("Failed to decrypt local API credentials.")
                return None, None
        except Exception as e:
            logger.error(f"An unexpected error occurred while reading credentials: {e}", exc_info=True)
            return None, None

    def _save_api_credentials(self):
        try:
            os.makedirs('./file', exist_ok=True)
            
            api_id_str = str(self.shared_state.api_id)
            secure_data = {"api_hash": self.shared_state.api_hash}
            
            if os.path.exists('./file/info.json'):
                try:
                    with open('./file/info.json', 'r') as f:
                        current_info = json.load(f)
                    decrypted_blob = crypto_handler.decrypt_secure_data(current_info.get("secure_data_blob"), api_id_str)
                    if decrypted_blob and 'group_id' in decrypted_blob:
                        secure_data['group_id'] = decrypted_blob['group_id']
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Could not read existing info.json, it will be overwritten.")

            encrypted_blob = crypto_handler.encrypt_secure_data(secure_data, api_id_str)
            final_info = { "api_id": self.shared_state.api_id, "secure_data_blob": encrypted_blob }

            with open('./file/info.json', 'w') as f:
                json.dump(final_info, f)
            logger.info("API credentials have been successfully encrypted and saved.")
        except Exception as e:
            logger.error(f"Failed to save credentials: {e}", exc_info=True)

    # --- Core Authentication Logic ---
    
    async def check_startup_login(self) -> Dict[str, Any]:
        api_id, api_hash = self._get_saved_api_credentials()
        if not (api_id and api_hash):
            logger.info("No saved API credentials found. Login required.")
            return {"logged_in": False}
            
        session_file = f'./file/user_{api_id}.session'
        client = TelegramClient(session_file, api_id, api_hash)
        try:
            await client.connect()
            if await client.is_user_authorized():
                logger.info("Valid session detected. Proceeding with startup initialization.")
                self.shared_state.client = client
                self.shared_state.api_id = api_id
                self.shared_state.api_hash = api_hash
                self.shared_state.is_logged_in = True
                
                init_result = await self.initialize_drive()
                if init_result.get("success"):
                    logger.info("Startup initialization complete.")
                    return {"logged_in": True}
                else:
                    logger.warning("Session is valid but drive initialization failed. Redirecting to login.")
                    await client.disconnect()
                    return {"logged_in": False, "expired_session": True, "api_id": api_id, "api_hash": api_hash}
            else:
                logger.info("Session is invalid or expired. Re-login required.")
                await client.disconnect()
                return {"logged_in": False, "expired_session": True, "api_id": api_id, "api_hash": api_hash}
        except Exception as e:
            logger.error(f"Error during startup check: {e}", exc_info=True)
            if client and client.is_connected(): await client.disconnect()
            if api_id and api_hash:
                return {"logged_in": False, "expired_session": True, "api_id": api_id, "api_hash": api_hash}
            return {"logged_in": False}

    async def verify_api_credentials(self, api_id: int, api_hash: str) -> Dict[str, Any]:
        client = None
        try:
            os.makedirs('./file', exist_ok=True)
            session_file = f'./file/user_{api_id}.session'

            if os.path.exists(session_file):
                logger.info(f"Removing old session file to ensure a clean login: {session_file}")
                os.remove(session_file)

            client = TelegramClient(session_file, api_id, api_hash)
            await client.connect()
            
            self.shared_state.client = client
            self.shared_state.api_id = api_id
            self.shared_state.api_hash = api_hash
            
            return {"success": True, "authorized": False}
        except ApiIdInvalidError:
            logger.warning(f"API credential verification failed for api_id: {api_id}")
            if client and client.is_connected(): await client.disconnect()
            return {"success": False, "error_code": "INVALID_API_CREDENTIALS", "message": "無效的 API ID 或 API Hash。"}
        except Exception as e:
            logger.error(f"Connection failed during API credential verification: {e}", exc_info=True)
            if client and client.is_connected(): await client.disconnect()
            return {"success": False, "error_code": "CONNECTION_FAILED", "message": f"連線失敗：{e}"}

    async def start_qr_login(self, event_callback: Callable) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client or not client.is_connected():
            logger.warning("QR login requested, but client is not connected.")
            return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "用戶端未連線。"}
            
        try:
            qr_login = await client.qr_login()
            
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(qr_login.url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            img_str = base64.b64encode(buffer.getvalue()).decode()

            self.shared_state.loop.create_task(self._wait_for_qr_login(qr_login, event_callback))

            return {"success": True, "qr_url": f"data:image/png;base64,{img_str}"}
        except ApiIdInvalidError:
            logger.warning(f"QR code generation failed due to invalid API credentials.")
            return {"success": False, "error_code": "INVALID_API_CREDENTIALS", "message": "無效的 API ID 或 API Hash，請返回重新輸入。"}
        except Exception as e:
            logger.error(f"QR code generation failed: {e}", exc_info=True)
            return {"success": False, "error_code": "QR_GENERATION_FAILED", "message": f"QR 碼產生失敗：{e}"}

    async def _wait_for_qr_login(self, qr_login, event_callback: Callable):
        try:
            await qr_login.wait()
            self.shared_state.is_logged_in = True
            self._save_api_credentials()
            event_callback({"status": "completed"})
        except SessionPasswordNeededError:
            event_callback({"status": "password_needed"})
        except Exception as e:
            logger.warning(f"Error during QR login wait: {e}")
            event_callback({"status": "failed", "error": str(e)})

    async def send_code_request(self, phone_number: str) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client: return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "用戶端尚未初始化。"}
        try:
            sent_code = await client.send_code_request(phone_number)
            self.shared_state.phone = phone_number
            self.shared_state.phone_code_hash = sent_code.phone_code_hash
            return {"success": True}
        except PhoneNumberInvalidError:
            logger.warning(f"Invalid phone number provided: {phone_number}")
            return {"success": False, "error_code": "PHONE_NUMBER_INVALID", "message": "電話號碼無效，請檢查並重新輸入。"}
        except Exception as e:
            logger.error(f"Failed to send code to {phone_number}: {e}", exc_info=True)
            return {"success": False, "error_code": "SEND_CODE_FAILED", "message": f"傳送驗證碼失敗：{e}"}

    async def submit_verification_code(self, code: str) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client: return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "用戶端尚未初始化。"}
        try:
            await client.sign_in(self.shared_state.phone, code, phone_code_hash=self.shared_state.phone_code_hash)
            self.shared_state.is_logged_in = True
            self._save_api_credentials()
            return {"success": True}
        except PhoneCodeInvalidError:
            logger.warning("An invalid verification code was submitted.")
            return {"success": False, "error_code": "INVALID_VERIFICATION_CODE", "message": "驗證碼錯誤。"}
        except SessionPasswordNeededError:
            return {"success": True, "password_needed": True}
        except Exception as e:
            logger.error(f"Unknown error while submitting verification code: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": f"驗證失敗：{e}"}

    async def submit_password(self, password: str) -> Dict[str, Any]:
        client = self.shared_state.client
        if not client: return {"success": False, "error_code": "CLIENT_NOT_CONNECTED", "message": "用戶端尚未初始化。"}
        try:
            await client.sign_in(password=password)
            self.shared_state.is_logged_in = True
            self._save_api_credentials()
            return {"success": True}
        except PasswordHashInvalidError:
            logger.warning("An invalid 2FA password was submitted.")
            return {"success": False, "error_code": "INVALID_PASSWORD", "message": "密碼錯誤。"}
        except Exception as e:
            logger.error(f"Unknown error while submitting password: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": f"登入失敗：{e}"}

    async def reset_client_for_new_login_method(self) -> Dict[str, bool]:
        try:
            if self.shared_state.client and self.shared_state.client.is_connected():
                await self.shared_state.client.disconnect()
            
            api_id = self.shared_state.api_id
            api_hash = self.shared_state.api_hash
            if not (api_id and api_hash):
                 logger.warning("Cannot reset client: api_id or api_hash is missing.")
                 return {"success": False}

            session_file = f'./file/user_{api_id}.session'
            if os.path.exists(session_file):
                os.remove(session_file)

            new_client = TelegramClient(session_file, api_id, api_hash)
            await new_client.connect()
            self.shared_state.client = new_client
            logger.info("Client has been reset for a new login method.")
            return {"success": True}
        except Exception as e:
            logger.error(f"An error occurred while resetting the client: {e}", exc_info=True)
            return {"success": False}

    async def initialize_drive(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client or not self.shared_state.api_id:
            msg = "Cannot initialize drive: Client or App API ID is missing."
            logger.error(msg)
            return {"success": False, "error_code": "INITIALIZATION_FAILED", "message": "無法初始化雲端硬碟：缺少用戶端或 App API ID。"}

        try:
            logger.info("Initializing TDrive...")
            async with asyncio.timeout(30):
                self.shared_state.group_id = await telegram_comms.get_group(client, self.shared_state.api_id)
                await telegram_comms.sync_database_file(client, self.shared_state.group_id, mode='sync')

            logger.info("TDrive initialization successful.")
            return {"success": True}
        except asyncio.TimeoutError:
            logger.error("Drive initialization timed out after 30 seconds.")
            return {"success": False, "error_code": "DRIVE_INITIALIZATION_TIMEOUT", "message": "初始化逾時，請檢查您的網路連線並重試。"}
        except Exception as e:
            logger.error(f"An error occurred during drive initialization: {e}", exc_info=True)
            return {"success": False, "error_code": "DRIVE_INITIALIZATION_FAILED", "message": f"初始化失敗：{e}"}

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
            }
        except Exception as e:
            logger.error(f"Failed to get user info: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "無法取得使用者資訊"}

    async def get_user_avatar(self) -> Dict[str, Any]:
        client = await utils.ensure_client_connected(self.shared_state)
        if not client: return {"success": False, "error_code": "CONNECTION_FAILED", "message": "連線失敗"}
        
        avatar_path = './file/user_avatar.jpg'
        try:
            path = await client.download_profile_photo('me', file=avatar_path)
            if not path: 
                logger.info("User has no profile picture set.")
                return {"success": False, "error_code": "AVATAR_NOT_FOUND", "message": "找不到個人圖片"}
            
            with open(path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            os.remove(path)
            return {"success": True, "avatar_base64": f"data:image/jpeg;base64,{encoded_string}"}
        except Exception as e:
            logger.error(f"Failed to get user avatar: {e}", exc_info=True)
            return {"success": False, "error_code": "INTERNAL_ERROR", "message": "無法取得使用者頭像"}

    async def perform_logout(self) -> Dict[str, Any]:
        logger.info("Performing logout...")
        client = self.shared_state.client
        if client and client.is_connected():
            await client.disconnect()
            logger.info("Successfully disconnected the client.")
        
        try:
            if os.path.exists('./file'):
                shutil.rmtree('./file')
                logger.info("Successfully cleaned up local file cache.")
        except Exception as e:
            logger.error(f"Error during local file cleanup on logout: {e}", exc_info=True)
            return {"success": True, "warning": "登出完成，但無法清除部分本機檔案。"}
        
        self.shared_state.client = None
        self.shared_state.api_id = None
        self.shared_state.api_hash = None
        self.shared_state.is_logged_in = False
        logger.info("In-memory session state has been cleared.")
        return {"success": True}