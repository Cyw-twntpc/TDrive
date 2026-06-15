import os
import logging
from telethon.sessions import StringSession
from core_app.core import crypto_handler

logger = logging.getLogger(__name__)

class SessionManager:
    """
    Manages the secure storage and retrieval of Telegram sessions.
    Encrypts the session string using hardware-bound keys to prevent session hijacking.
    """
    
    @staticmethod
    def get_session_path(api_id: int) -> str:
        return f'./file/user_{api_id}.session.enc'

    @staticmethod
    def load_session(api_id: int) -> StringSession:
        """
        Loads and decrypts the session from disk. 
        Returns a new StringSession if no file exists or decryption fails.
        """
        path = SessionManager.get_session_path(api_id)
        if not os.path.exists(path):
            logger.info(f"No encrypted session found at {path}. Starting fresh.")
            return StringSession()

        try:
            with open(path, 'r', encoding='utf-8') as f:
                encrypted_data = f.read()
            
            # Decrypt using hardware-bound key
            data = crypto_handler.decrypt_secure_data(encrypted_data, str(api_id))
            if data and 'session_string' in data:
                logger.info("Session loaded and decrypted successfully.")
                return StringSession(data['session_string'])
            else:
                logger.warning("Session decryption failed or invalid format. Starting fresh.")
                return StringSession()
                
        except Exception as e:
            logger.error(f"Error loading session: {e}", exc_info=True)
            return StringSession()

    @staticmethod
    def save_session(session, api_id: int):
        """
        Encrypts and saves the current session state to disk.
        """
        try:
            # StringSession.save() returns the auth key string
            session_string = session.save()
            if not session_string:
                logger.warning("Session string is empty, skipping save.")
                return

            data = {'session_string': session_string}
            encrypted_data = crypto_handler.encrypt_secure_data(data, str(api_id))
            
            path = SessionManager.get_session_path(api_id)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            
            with open(path, 'w', encoding='utf-8') as f:
                f.write(encrypted_data)
            
            logger.info(f"Session saved securely to {path}")
            
        except Exception as e:
            logger.error(f"Error saving session: {e}", exc_info=True)
