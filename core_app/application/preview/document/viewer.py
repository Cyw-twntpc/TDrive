import os
import logging
import hashlib
import asyncio

from core_app.core import crypto_handler
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository
from core_app.infrastructure.telegram.telegram_comms import download_data_as_bytes, upload_data_as_file

logger = logging.getLogger(__name__)

EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp'}


def generate_pdf_preview(file_path: str, ext: str) -> tuple:
    """Generate 10-page PDF preview + thumbnail from a document file.
    Returns (preview_pdf_bytes, thumbnail_jpg_bytes, source_hash).
    If file size < 8MB, preview_pdf_bytes will be None to signal bypass."""
    import fitz
    
    doc = None
    try:
        is_small = os.path.getsize(file_path) < 8 * 1024 * 1024
        
        doc = fitz.open(file_path)

        preview_bytes = None
        if not is_small:
            if len(doc) > 10:
                doc.select(list(range(10)))
            preview_bytes = doc.tobytes(garbage=3, clean=True, deflate=True)

        page = doc.load_page(0)
        mat = fitz.Matrix(400 / page.rect.width, 400 / page.rect.width)
        pix = page.get_pixmap(matrix=mat)
        thumb_bytes = pix.tobytes('jpeg')
        
        with open(file_path, 'rb') as f:
            source_hash = hashlib.sha256(f.read()).hexdigest()

        return preview_bytes, thumb_bytes, source_hash
    finally:
        if doc is not None:
            doc.close()


class DocumentPreviewer:
    @staticmethod
    def _decode_cache(data):
        import gzip
        uncompressed = gzip.decompress(data)
        return __import__('base64').b64encode(uncompressed).decode('utf-8')

    @staticmethod
    def _process_pdf_preview(original_bytes: bytes, ext: str):
        import tempfile
        import os
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, 'wb') as f:
            f.write(original_bytes)
        try:
            return generate_pdf_preview(tmp_path, ext)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def _process_full_document(original_bytes: bytes, ext: str):
        import os
        import tempfile
        import subprocess
        from core_app.core.utils import ensure_extracted
        
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, 'wb') as f:
            f.write(original_bytes)
        try:
            import fitz
            soffice = ensure_extracted('LibreOffice')
            soffice_exe = os.path.join(soffice, 'App', 'libreoffice', 'program', 'soffice.exe')
            outdir = os.path.dirname(tmp_path)
            subprocess.run(
                [soffice_exe, '--headless', '--convert-to', 'pdf',
                 '--outdir', outdir, tmp_path],
                check=True
            )
            base_name = os.path.splitext(os.path.basename(tmp_path))[0]
            pdf_path = os.path.join(outdir, f"{base_name}.pdf")
            doc = fitz.open(pdf_path)
            try:
                full_bytes = doc.tobytes()
            finally:
                doc.close()
            return full_bytes
        finally:
            if 'pdf_path' in locals() and os.path.exists(pdf_path):
                os.unlink(pdf_path)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    async def get_preview(map_id: int, file_service) -> dict:
        file_info = await file_service._get_file_info(map_id)
        
        cached = await asyncio.to_thread(file_service.preview_cache.get, file_info['content_id'])
        if cached and cached['content_type'] in ['document_preview', 'document_full']:
            base64_data = await asyncio.to_thread(DocumentPreviewer._decode_cache, cached['data'])
            return {'base64_data': base64_data, 'is_full': cached['content_type'] == 'document_full',
                    'file_name': file_info['name']}

        ext = os.path.splitext(file_info['name'])[1].lower()
        if ext != '.pdf' or file_info['size'] < 8 * 1024 * 1024:
            return await DocumentPreviewer.load_full(map_id, file_service)
            
        client = await file_service._ensure_client()
        row = await asyncio.to_thread(DocumentPreviewer._get_preview_msg_id, map_id)
        preview_existed = row and row[0] is not None

        if preview_existed:
            pdf_bytes = await download_data_as_bytes(
                client, file_service.shared_state.group_id,
                [row[0]], row[1]
            )
        else:
            original_bytes = await file_service._download_full_file(
                file_info['content_id'], file_info['size'], file_info['file_hash']
            )
            if not original_bytes:
                raise ValueError("Downloaded file is empty or missing.")
                
            ext = os.path.splitext(file_info['name'])[1]
            pdf_bytes, thumb_bytes, source_hash = await asyncio.to_thread(DocumentPreviewer._process_pdf_preview, original_bytes, ext)

            # Upload generated preview to Telegram for future use
            preview_hash = await asyncio.to_thread(crypto_handler.hash_bytes, pdf_bytes)
            preview_upload_info = await upload_data_as_file(
                client, file_service.shared_state.group_id,
                pdf_bytes, preview_hash
            )
            if preview_upload_info:
                preview_msg_id = preview_upload_info[0][1]
                content_id = file_info['content_id']
                def _update_preview():
                    db = DatabaseConnection()
                    FileRepository(db).update_file_preview_info(content_id, preview_msg_id, preview_hash)
                await asyncio.to_thread(_update_preview)

            # Save thumbnail locally
            if thumb_bytes and hasattr(file_service, 'thumb_manager') and file_service.thumb_manager:
                await asyncio.to_thread(
                    file_service.thumb_manager.save_thumbnails,
                    {file_info['content_id']: thumb_bytes}
                )

        if file_service.preview_cache:
            await asyncio.to_thread(
                file_service.preview_cache.put,
                file_info['content_id'], pdf_bytes, 'document_preview',
                file_info['file_hash'], len(pdf_bytes), last_chunk=None
            )

        base64_data = await asyncio.to_thread(lambda b: __import__('base64').b64encode(b).decode('utf-8'), pdf_bytes)
        return {'base64_data': base64_data, 'is_full': False, 'file_name': file_info['name']}

    @staticmethod
    async def load_full(map_id: int, file_service) -> dict:
        file_info = await file_service._get_file_info(map_id)
        
        import base64
        cached = await asyncio.to_thread(file_service.preview_cache.get, file_info['content_id'])
        if cached and cached['content_type'] == 'document_full':
            base64_data = await asyncio.to_thread(DocumentPreviewer._decode_cache, cached['data'])
            return {'base64_data': base64_data, 'is_full': True, 'file_name': file_info['name']}


        original_bytes = await file_service._download_full_file(
            file_info['content_id'], file_info['size'], file_info['file_hash']
        )
        if not original_bytes:
            raise ValueError("Downloaded file is empty or missing.")
            
        ext = os.path.splitext(file_info['name'])[1]
        if ext.lower() == '.pdf':
            full_bytes = original_bytes
        else:
            full_bytes = await asyncio.to_thread(DocumentPreviewer._process_full_document, original_bytes, ext)

        if file_service.preview_cache:
            await asyncio.to_thread(
                file_service.preview_cache.put,
                file_info['content_id'], full_bytes, 'document_full',
                file_info['file_hash'], len(full_bytes), last_chunk=None
            )

        base64_data = await asyncio.to_thread(lambda b: __import__('base64').b64encode(b).decode('utf-8'), full_bytes)
        return {'base64_data': base64_data, 'is_full': True, 'file_name': file_info['name']}

    @staticmethod
    def _get_preview_msg_id(map_id: int):
        db = DatabaseConnection()
        conn = db._get_conn()
        cur = conn.cursor()
        cur.execute(
            'SELECT f.preview_msg_id, f.preview_hash FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.id = ?',
            (map_id,)
        )
        return cur.fetchone()
