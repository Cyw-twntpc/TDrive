import logging
import asyncio
import secrets
import mimetypes
from aiohttp import web
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core_app.application.preview.buffer import StreamBuffer
    from core_app.infrastructure.database.main_db.database import DatabaseConnection

logger = logging.getLogger(__name__)


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

    async def handle_stream(self, request: web.Request):
        token = request.query.get('token')
        if token != self.session_token:
            return web.Response(status=403, text="Forbidden")

        try:
            file_id_str = request.match_info['file_id']
            map_id = int(file_id_str)

            file_info = await self._get_file_info(map_id)
            if not file_info:
                return web.Response(status=404, text="File not found")

            content_id = file_info['content_id']
            file_size = int(file_info['size'])
            file_hash = file_info['hash']
            file_name = file_info['name']

            mime_type, _ = mimetypes.guess_type(file_name)
            if not mime_type:
                mime_type = 'application/octet-stream'

            range_header = request.headers.get('Range')
            start_byte = 0
            end_byte = file_size - 1

            if range_header:
                try:
                    unit, ranges = range_header.split('=')
                    if unit == 'bytes':
                        r_start, r_end = ranges.split('-')
                        if r_start and r_end:
                            start_byte = int(r_start)
                            end_byte = min(int(r_end), file_size - 1)
                        elif r_start:
                            start_byte = int(r_start)
                            end_byte = file_size - 1
                        elif r_end:
                            suffix_length = int(r_end)
                            start_byte = max(0, file_size - suffix_length)
                            end_byte = file_size - 1
                except ValueError:
                    pass

            chunk_length = end_byte - start_byte + 1

            headers = {
                'Content-Type': mime_type,
                'Accept-Ranges': 'bytes',
                'Content-Length': str(chunk_length)
            }

            status_code = 200
            if range_header:
                status_code = 206
                headers['Content-Range'] = f'bytes {start_byte}-{end_byte}/{file_size}'

            response = web.StreamResponse(status=status_code, headers=headers)
            await response.prepare(request)

            offset = start_byte
            remaining = chunk_length

            READ_BLOCK = 64 * 1024

            while remaining > 0:
                if request.transport and request.transport.is_closing():
                    break

                read_size = min(remaining, READ_BLOCK)

                data = await self.buffer.read(content_id, offset, read_size, file_size, file_hash)
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
        def query():
            conn = self.db._get_conn()

            cur = conn.cursor()
            query = """
                SELECT m.name, f.id as content_id, f.size, f.hash
                FROM file_folder_map m
                JOIN files f ON m.file_id = f.id
                WHERE m.id = ?
            """
            cur.execute(query, (map_id,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return None

        return await loop.run_in_executor(None, query)
