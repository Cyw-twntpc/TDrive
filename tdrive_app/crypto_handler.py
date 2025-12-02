from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidTag
import hashlib
import base64
import os
import json
import logging
import machineid

logger = logging.getLogger(__name__)

# A hardcoded secret pepper. In a real-world scenario, this might be handled
# more securely, but for this application, it provides a good layer of obfuscation.
APP_PEPPER = b'TDRIVE_SECRET_PEPPER_!@#$%'

# --- Key Generation ---
def _get_encryption_key(api_id: str) -> bytes:
    """Generates a deterministic encryption key from the public api_id, a secret pepper, and the machine's hardware ID."""
    # The salt is still user-specific
    salt = hashlib.sha256(f"tdrive-salt-{api_id}".encode()).digest()
    
    # The key's secret component is now tied to the machine
    try:
        hwid = machineid.id().encode('utf-8')
    except Exception as e:
        logger.warning(f"無法獲取硬體ID，將使用備用安全字串。錯誤: {e}")
        # Fallback for systems where machineid might fail
        hwid = b'fallback_entropy_for_tdrive_!@#$%' 
    
    machine_specific_pepper = APP_PEPPER + hwid

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=500000, # Increased iterations
        backend=default_backend()
    )
    return kdf.derive(machine_specific_pepper)

# --- Encryption/Decryption for the secure data blob ---
def encrypt_secure_data(data_dict: dict, api_id: str) -> str:
    """Encrypts a dictionary of secrets and returns a base64 string."""
    key = _get_encryption_key(str(api_id))
    plaintext = json.dumps(data_dict).encode('utf-8')
    
    iv = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    tag = encryptor.tag
    
    encrypted_payload = iv + ciphertext + tag
    return base64.b64encode(encrypted_payload).decode('utf-8')

def decrypt_secure_data(encrypted_str: str, api_id: str) -> dict | None:
    """Decrypts a base64 string into a dictionary using the api_id."""
    key = _get_encryption_key(str(api_id))
    try:
        encrypted_payload = base64.b64decode(encrypted_str.encode('utf-8'))
        iv = encrypted_payload[:12]
        tag = encrypted_payload[-16:]
        ciphertext = encrypted_payload[12:-16]

        cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return json.loads(plaintext.decode('utf-8'))
    except (InvalidTag, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("解密使用者資訊失敗。可能是 API ID 錯誤或檔案已損毀。")
        return None

# --- File Hashing (for file parts and integrity checks) ---
def hash_data(data_source):
    sha256_hash = hashlib.sha256()
    if os.path.exists(data_source):
        with open(data_source, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
    else:
        sha256_hash.update(data_source.encode('utf-8'))
    return sha256_hash.hexdigest()

# --- File Part Encryption (used for uploads/downloads) ---
def generate_key(password: str, salt: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt.encode('utf-8'),
        iterations=480000,
        backend=default_backend()
    )
    return kdf.derive(password.encode('utf-8'))

def encrypt(plaintext: bytes, key: bytes) -> bytes:
    iv = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    tag = encryptor.tag
    return iv + ciphertext + tag

def decrypt(encrypted_data: bytes, key: bytes) -> bytes:
    iv = encrypted_data[:12]
    tag = encrypted_data[-16:]
    ciphertext = encrypted_data[12:-16]
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag), backend=default_backend())
    decryptor = cipher.decryptor()
    try:
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext
    except InvalidTag:
        raise ValueError("密文或金鑰無效，或資料已被篡改！")
