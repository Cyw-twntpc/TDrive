import os
import logging
import asyncio

from core_app.core import crypto_handler
from core_app.core import utils
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
        
        source_hash = crypto_handler.hash_data(file_path)

        return preview_bytes, thumb_bytes, source_hash
    finally:
        if doc is not None:
            doc.close()


class DocumentPreviewer:
    @staticmethod
    def _decode_cache(cached_or_bytes):
        import base64
        import gzip
        if isinstance(cached_or_bytes, dict):
            raw = cached_or_bytes["data"]
            if not cached_or_bytes.get("decoded") and cached_or_bytes.get("is_compressed", True):
                raw = gzip.decompress(raw)
            return base64.b64encode(raw).decode("utf-8")
        return base64.b64encode(gzip.decompress(cached_or_bytes)).decode("utf-8")

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
    def _read_cache(file_service, content_id: int, expected_hash: str, allowed_types: tuple[str, ...]):
        """Read document cache entry while guarding None preview_cache and validating source_hash."""
        preview_cache = getattr(file_service, 'preview_cache', None)
        if not preview_cache:
            return None
        try:
            cached = preview_cache.get(
                content_id, expected_hash=expected_hash, expected_types=allowed_types
            )
        except TypeError:
            cached = preview_cache.get(content_id)
        if not cached:
            return None
        cached_hash = cached.get('source_hash')
        if cached_hash and expected_hash and cached_hash != expected_hash:
            preview_cache.delete(content_id)
            return None
        if cached.get('content_type') not in allowed_types:
            return None
        return cached

    @staticmethod
    async def get_preview(map_id: int, file_service) -> dict:
        logger.debug(f"DocumentPreviewer.get_preview: map_id={map_id}")
        file_info = await file_service._get_file_info(map_id)

        cached = await asyncio.to_thread(
            DocumentPreviewer._read_cache,
            file_service,
            file_info['content_id'],
            file_info['file_hash'],
            ('document_preview', 'document_full'),
        )
        if cached:
            logger.debug(f"DocumentPreviewer.get_preview: cache hit for content_id={file_info['content_id']}")
            base64_data = await asyncio.to_thread(DocumentPreviewer._decode_cache, cached)
            return {'base64_data': base64_data, 'is_full': cached['content_type'] == 'document_full',
                    'file_name': file_info['name']}

        ext = os.path.splitext(file_info['name'])[1].lower()
        if ext != '.pdf' or file_info['size'] < 8 * 1024 * 1024:
            logger.debug(f"DocumentPreviewer.get_preview: ext={ext} or size < 8MB, delegating to load_full")
            return await DocumentPreviewer.load_full(map_id, file_service, _skip_cache_check=True)

        active_map = getattr(file_service, '_active_doc_downloads', None)
        dedup_key = (file_info['content_id'], 'preview')
        if isinstance(active_map, dict) and dedup_key in active_map:
            try:
                shared_res = await active_map[dedup_key]
                return {**shared_res, 'file_name': file_info['name']}
            except Exception as e:
                logger.debug(f"In-flight document download wait failed for {dedup_key}: {e}")

        download_future = None
        if isinstance(active_map, dict):
            download_future = asyncio.get_running_loop().create_future()
            active_map[dedup_key] = download_future

        try:
            client = await file_service._ensure_client()
            row = await asyncio.to_thread(DocumentPreviewer._get_preview_msg_id, map_id)
            preview_existed = row and row[0] is not None

            if preview_existed:
                g_idx, raw_id = utils.decode_global_msg_id(row[0])
                logger.debug(f"DocumentPreviewer.get_preview: decoded existing preview -> group_index={g_idx}, raw_msg_id={raw_id}")
                target_gid = file_service.shared_state.get_telegram_group_id(g_idx)
                pdf_bytes = await download_data_as_bytes(
                    client, target_gid,
                    [raw_id], row[1]
                )
            else:
                logger.debug(f"DocumentPreviewer.get_preview: no preview exists, generating from original file")
                original_bytes = await file_service._download_full_file(
                    file_info['content_id'], file_info['size'], file_info['file_hash']
                )
                if not original_bytes:
                    logger.error(f"DocumentPreviewer.get_preview: downloaded file is empty for map_id={map_id}")
                    raise ValueError("Downloaded file is empty or missing.")
                ext = os.path.splitext(file_info['name'])[1]
                pdf_bytes, thumb_bytes, source_hash = await asyncio.to_thread(DocumentPreviewer._process_pdf_preview, original_bytes, ext)

                # Upload generated preview to Telegram for future use
                preview_hash = await asyncio.to_thread(crypto_handler.hash_bytes, pdf_bytes)
                upload_group_index, active_gid = utils.get_upload_target(
                    file_service.shared_state, file_service.shared_state.group_id
                )
                logger.debug(f"DocumentPreviewer.get_preview: uploading generated preview to active group {upload_group_index}")
                preview_upload_info = await upload_data_as_file(
                    client, active_gid,
                    pdf_bytes, preview_hash
                )
                if preview_upload_info:
                    raw_preview_msg_id = preview_upload_info[0][1]
                    preview_msg_id = utils.encode_global_msg_id(upload_group_index, raw_preview_msg_id)
                    logger.debug(f"DocumentPreviewer.get_preview: encoded preview_msg_id -> global_id={preview_msg_id}")
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

            if getattr(file_service, 'preview_cache', None):
                logger.debug(f"DocumentPreviewer.get_preview: saving to cache for content_id={file_info['content_id']}")
                await asyncio.to_thread(
                    file_service.preview_cache.put,
                    file_info['content_id'], pdf_bytes, 'document_preview',
                    file_info['file_hash'], len(pdf_bytes), None, False
                )

            base64_data = await asyncio.to_thread(lambda b: __import__('base64').b64encode(b).decode('utf-8'), pdf_bytes)
            result = {'base64_data': base64_data, 'is_full': False, 'file_name': file_info['name']}
            if download_future is not None and not download_future.done():
                download_future.set_result(result)
            return result
        except Exception as exc:
            if download_future is not None and not download_future.done():
                download_future.set_exception(exc)
            raise
        finally:
            if isinstance(active_map, dict):
                active_map.pop(dedup_key, None)

    @staticmethod
    async def load_full(map_id: int, file_service, _skip_cache_check: bool = False) -> dict:
        logger.debug(f"DocumentPreviewer.load_full: map_id={map_id}")
        file_info = await file_service._get_file_info(map_id)

        if not _skip_cache_check:
            cached = await asyncio.to_thread(
                DocumentPreviewer._read_cache,
                file_service,
                file_info['content_id'],
                file_info['file_hash'],
                ('document_full',),
            )
            if cached:
                logger.debug(f"DocumentPreviewer.load_full: cache hit for content_id={file_info['content_id']}")
                base64_data = await asyncio.to_thread(DocumentPreviewer._decode_cache, cached)
                return {'base64_data': base64_data, 'is_full': True, 'file_name': file_info['name']}

        active_map = getattr(file_service, '_active_doc_downloads', None)
        dedup_key = (file_info['content_id'], 'full')
        if isinstance(active_map, dict) and dedup_key in active_map:
            try:
                shared_res = await active_map[dedup_key]
                return {**shared_res, 'file_name': file_info['name']}
            except Exception as e:
                logger.debug(f"In-flight document download wait failed for {dedup_key}: {e}")

        download_future = None
        if isinstance(active_map, dict):
            download_future = asyncio.get_running_loop().create_future()
            active_map[dedup_key] = download_future

        try:
            original_bytes = await file_service._download_full_file(
                file_info['content_id'], file_info['size'], file_info['file_hash']
            )
            if not original_bytes:
                logger.error(f"DocumentPreviewer.load_full: downloaded file is empty for map_id={map_id}")
                raise ValueError("Downloaded file is empty or missing.")

            ext = os.path.splitext(file_info['name'])[1]
            if ext.lower() == '.pdf':
                full_bytes = original_bytes
            else:
                full_bytes = await asyncio.to_thread(DocumentPreviewer._process_full_document, original_bytes, ext)

            if getattr(file_service, 'preview_cache', None):
                await asyncio.to_thread(
                    file_service.preview_cache.put,
                    file_info['content_id'], full_bytes, 'document_full',
                    file_info['file_hash'], len(full_bytes), None, False
                )

            base64_data = await asyncio.to_thread(lambda b: __import__('base64').b64encode(b).decode('utf-8'), full_bytes)
            result = {'base64_data': base64_data, 'is_full': True, 'file_name': file_info['name']}
            if download_future is not None and not download_future.done():
                download_future.set_result(result)
            return result
        except Exception as exc:
            if download_future is not None and not download_future.done():
                download_future.set_exception(exc)
            raise
        finally:
            if isinstance(active_map, dict):
                active_map.pop(dedup_key, None)

    @staticmethod
    def _get_preview_msg_id(map_id: int):
        db = DatabaseConnection()
        with db.locked_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT f.preview_msg_id, f.preview_hash FROM file_folder_map m JOIN files f ON m.file_id = f.id WHERE m.id = ?',
                (map_id,)
            )
            return cur.fetchone()
