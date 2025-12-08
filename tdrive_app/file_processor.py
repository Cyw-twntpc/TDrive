import os
from . import crypto_handler as cr

CHUNK_SIZE = int(1024 * 1024 * 1024 * 1.5) # 分塊大小 1.5 GB

def stream_split_and_encrypt(file_path, key, file_hash, temp_dir):
    """
    以串流模式讀取檔案，逐塊加密並寫入暫存檔。
    這是一個生成器函式，每次產出一個 (分塊編號, 暫存路徑)。
    """
    file_name_base = file_hash[:20]
    with open(file_path, 'rb') as f_in:
        i = 1
        while True:
            chunk = f_in.read(CHUNK_SIZE)
            if not chunk:
                break
            
            output_path = os.path.join(temp_dir, f"{file_name_base}.part_{i}")
            with open(output_path, 'wb') as f_out:
                f_out.write(cr.encrypt(chunk, key))
            
            yield i, output_path # 產出分塊編號和路徑
            
            i += 1

def decrypt_and_write_chunk(part_path, output_path, key, offset):
    """
    解密單一分塊並將其寫入輸出檔案的指定偏移位置。
    """
    try:
        with open(output_path, 'r+b') as f_out:
            with open(part_path, 'rb') as f_in:
                encrypted_content = f_in.read()
            
            decrypted_content = cr.decrypt(encrypted_content, key)
            f_out.seek(offset)
            f_out.write(decrypted_content)
    except IOError as e:
        raise IOError(f"寫入檔案 '{output_path}' 時發生錯誤: {e}") from e