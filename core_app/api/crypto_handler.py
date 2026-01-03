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

# Hardcoded secret pepper
APP_PEPPER = b'TDRIVE_SECRET_PEPPER_!@#$%'

def _get_encryption_key(api_id: str) -> bytes:
    """Generates a deterministic 32-byte key from api_id, pepper, and hardware ID."""
    salt = hashlib.sha256(f"tdrive-salt-{api_id}".encode()).digest()
    
    try:
        hwid = machineid.id().encode('utf-8')
    except Exception as e:
        logger.warning(f"Could not retrieve hardware ID, using a fallback secret. Error: {e}")
        hwid = b'fallback_entropy_for_tdrive_!@#$%' 
    
    machine_specific_pepper = APP_PEPPER + hwid

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=500000,
        backend=default_backend()
    )
    return kdf.derive(machine_specific_pepper)

def encrypt_secure_data(data_dict: dict, api_id: str) -> str:
    """Encrypts data using AES-GCM; returns base64(iv + ciphertext + tag)."""
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
    """Decrypts a base64 string back into a dictionary."""
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
    except (InvalidTag, ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to decrypt user credentials. This can happen if the API ID is incorrect, "
                       f"the data is corrupt, or it's from a different machine. Error: {e}")
        return None

def hash_data(data_source: str) -> str:
    """Computes SHA256 hash of a file or string."""
    sha256_hash = hashlib.sha256()
    if os.path.exists(data_source):
        with open(data_source, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
    else:
        sha256_hash.update(data_source.encode('utf-8'))
    return sha256_hash.hexdigest()

def hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

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
    try:
        iv = encrypted_data[:12]
        tag = encrypted_data[-16:]
        ciphertext = encrypted_data[12:-16]
        cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext
    except InvalidTag:
        raise ValueError("Decryption failed: The ciphertext is invalid or the key is incorrect. Data may be tampered with.")
