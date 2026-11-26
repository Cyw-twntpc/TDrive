import os
import logging
import asyncio
import secrets
import mimetypes
from aiohttp import web
from typing import TYPE_CHECKING

from core_app.infrastructure.database.main_db.repositories.file_repository import AUDIO_EXTENSIONS, FileRepository

if TYPE_CHECKING:
    from core_app.application.preview.buffer import StreamBuffer
    from core_app.infrastructure.database.main_db.database import DatabaseConnection

logger = logging.getLogger(__name__)

# Explicit audio MIME mapping avoids OS registry gaps on Windows for FLAC/OGG/Opus/M4A.
AUDIO_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".wma": "audio/x-ms-wma",
    ".aac": "audio/aac",
}


class VideoStreamer:
    def __init__(self, stream_buffer: 'StreamBuffer', db_handler: 'DatabaseConnection'):
        self.buffer = stream_buffer
        self.db = db_handler
        self.app = web.Application()
        self.app.router.add_get('/stream/{file_id}', self.handle_stream)
        self.runner = None
        self.site = None
        self.port = None
        self.session_token = secrets.token_urlsafe(16)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()

        if self.site._server and self.site._server.sockets:
            self.port = self.site._server.sockets[0].getsockname()[1]
            logger.info(f"Streaming Proxy started on http://127.0.0.1:{self.port}")
        else:
            logger.error("Failed to retrieve bound port for Streaming Proxy.")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("Streaming Proxy stopped.")

    def get_stream_url(self, file_id: int) -> str:
        if not self.port:
            return ""
        return f"http://127.0.0.1:{self.port}/stream/{file_id}?token={self.session_token}"

    @staticmethod
    def _resolve_mime_type(file_name: str | None) -> str:
        """Resolve explicit audio MIME type or standard OS guess for the streamed filename."""
        ext = os.path.splitext(file_name or '')[1].lower()
        mime_type = AUDIO_MIME_TYPES.get(ext)
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_name)
        return mime_type or 'application/octet-stream'

    @staticmethod
    def _parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int, bool]:
        """Parse an HTTP Range header into (start_byte, end_byte, invalid_range)."""
        start_byte = 0
        end_byte = file_size - 1
        invalid_range = False

        if range_header:
            try:
                unit, ranges = range_header.split('=', 1)
                if unit.strip().lower() == 'bytes':
                    r_start, r_end = ranges.split('-', 1)
                    r_start = r_start.strip()
                    r_end = r_end.strip()
                    if r_start and r_end:
                        start_byte = int(r_start)
                        end_byte = min(int(r_end), file_size - 1)
                    elif r_start:
                        start_byte = int(r_start)
                        end_byte = file_size - 1
                    elif r_end:
                        suffix_length = int(r_end)
                        if suffix_length <= 0:
                            invalid_range = True
                        else:
                            start_byte = max(0, file_size - suffix_length)
                            end_byte = file_size - 1
                    else:
                        invalid_range = True
                    if not invalid_range and (start_byte < 0 or start_byte >= file_size or end_byte < start_byte):
                        invalid_range = True
            except ValueError as e:
                logger.debug(f"Invalid HTTP Range header '{range_header}': {e}")

        return start_byte, end_byte, invalid_range

    async def handle_stream(self, request: web.Request):
        """Serve HTTP 200/206 byte-range media streams backed by encrypted StreamBuffer chunks."""
        token = request.query.get('token')
        if token != self.session_token:
            return web.Response(status=403, text="Forbidden")

        try:
            map_id = int(request.match_info['file_id'])
            file_info = await self._get_file_info(map_id)
            if not file_info:
                return web.Response(status=404, text="File not found")

            content_id = file_info['content_id']
            file_size = int(file_info['size'])
            file_hash = file_info['hash']
            file_name = file_info['name']
            mime_type = self._resolve_mime_type(file_name)
            range_header = request.headers.get('Range')

            if file_size <= 0:
                if range_header:
                    return web.Response(status=416, headers={'Content-Range': 'bytes */0'}, text='Range Not Satisfiable')
                return web.Response(
                    status=200,
                    headers={'Content-Type': mime_type, 'Accept-Ranges': 'bytes', 'Content-Length': '0'},
                    body=b'',
                )

            start_byte, end_byte, invalid_range = self._parse_range_header(range_header, file_size)
            if invalid_range:
                return web.Response(status=416, headers={'Content-Range': f'bytes */{file_size}'}, text='Range Not Satisfiable')

            chunk_length = end_byte - start_byte + 1
            headers = {'Content-Type': mime_type, 'Accept-Ranges': 'bytes', 'Content-Length': str(chunk_length)}
            status_code = 200
            if range_header:
                status_code = 206
                headers['Content-Range'] = f'bytes {start_byte}-{end_byte}/{file_size}'

            response = web.StreamResponse(status=status_code, headers=headers)
            await response.prepare(request)

            offset = start_byte
            remaining = chunk_length
            category = "audio_chunk" if (file_name and file_name.lower().endswith(AUDIO_EXTENSIONS)) else "video_chunk"

            while remaining > 0:
                if request.transport and request.transport.is_closing():
                    break
                data = await self.buffer.read(content_id, offset, min(remaining, 64 * 1024), file_size, file_hash, category=category)
                if not data:
                    break
                await response.write(data)
                offset += len(data)
                remaining -= len(data)

            return response

        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, ConnectionError, asyncio.CancelledError):
            return web.Response()
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            return web.Response(status=500)

    async def _get_file_info(self, map_id: int):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: FileRepository(self.db).get_active_stream_file_info(map_id),
        )
