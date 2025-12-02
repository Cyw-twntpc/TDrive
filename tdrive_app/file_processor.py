import os
import re
from . import crypto_handler as cr

def split_file(file_path, key, file_hash, temp_dir):
    chunk_size = int(1024 * 1024 * 1024 * 1.5)  # 1.5 GB
    file_name_base = file_hash[:20]
    split_files = []
    with open(file_path, 'rb') as f_in:
        chunk = f_in.read(chunk_size)
        i = 1
        while chunk:
            output_path = os.path.join(temp_dir, f"{file_name_base}.part_{i}")
            with open(output_path, 'wb') as f_out:
                f_out.write(cr.encrypt(chunk, key))
            split_files.append(output_path)
            i += 1
            chunk = f_in.read(chunk_size)
    return split_files

def merge_files(file_list, save_file, key):
    with open(save_file, 'wb') as f_out:
        for file_part_path in file_list:
            with open(file_part_path, 'rb') as f_in:
                f_out.write(cr.decrypt(f_in.read(), key))

def find_part_files(folder_path):
    file_list = []
    pattern = r".*\.part_(\d+)"
    for file in os.listdir(folder_path):
        match = re.match(pattern, file)
        if match:
            number = int(match.group(1))
            file_list.append((number, os.path.join(folder_path, file)))
    file_list.sort(key=lambda x: x[0])
    return [f[1] for f in file_list]