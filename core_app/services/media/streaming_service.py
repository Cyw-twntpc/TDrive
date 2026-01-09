import logging
import asyncio
import secrets
import mimetypes
from aiohttp import web
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stream_buffer import StreamBuffer
    from core_app.data.db_handler import DatabaseHandler

logger = logging.getLogger(__name__)

class StreamingService:
    def __init__(self, stream_buffer: 'StreamBuffer', db_handler: 'DatabaseHandler'):
        self.buffer = stream_buffer
        self.db = db_handler
        self.app = web.Application()
        self.app.router.add_get('/stream/{file_id}', self.handle_stream)
        self.runner = None
        self.site = None
        self.port = None
        self.session_token = secrets.token_urlsafe(16) # Security Token

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        # Bind to localhost with random port (0)
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        
        # Retrieve the actual assigned port
        # socket info structure: (address, port)
        if self.site._server and self.site._server.sockets:
            self.port = self.site._server.sockets[0].getsockname()[1]
            logger.info(f"Streaming Proxy started on http://127.0.0.1:{self.port} (Token: {self.session_token})")
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
        # 1. Security Check
        token = request.query.get('token')
        if token != self.session_token:
            return web.Response(status=403, text="Forbidden")

        try:
            file_id_str = request.match_info['file_id']
            # file_id here is the 'files' table ID (content ID), or map ID?
            # Let's assume Map ID (item.id) is passed, so we resolve to Content ID & Size.
            map_id = int(file_id_str)
            
            file_info = await self._get_file_info(map_id)
            if not file_info:
                return web.Response(status=404, text="File not found")

            content_id = file_info['content_id']
            file_size = int(file_info['size'])
            file_hash = file_info['hash']
            file_name = file_info['name']

            # MIME Type
            mime_type, _ = mimetypes.guess_type(file_name)
            if not mime_type:
                mime_type = 'application/octet-stream'

            # Range Parsing
            range_header = request.headers.get('Range')
            start_byte = 0
            end_byte = file_size - 1
            
            if range_header:
                try:
                    # Example: bytes=0- or bytes=100-200
                    unit, ranges = range_header.split('=')
                    if unit == 'bytes':
                        r_start, r_end = ranges.split('-')
                        if r_start:
                            start_byte = int(r_start)
                        if r_end:
                            end_byte = int(r_end)
                except ValueError:
                    pass # Invalid range, ignore

            # Length to serve
            chunk_length = end_byte - start_byte + 1
            
            headers = {
                'Content-Type': mime_type,
                'Accept-Ranges': 'bytes',
                'Content-Range': f'bytes {start_byte}-{end_byte}/{file_size}',
                'Content-Length': str(chunk_length)
            }

            response = web.StreamResponse(status=206, headers=headers)
            await response.prepare(request)

            # Stream Loop
            offset = start_byte
            remaining = chunk_length
            
            # Read in smaller chunks to keep memory usage low and response responsive
            READ_BLOCK = 64 * 1024 # 64KB chunks to socket

            while remaining > 0:
                # We request from buffer. Buffer handles the big 8MB chunks caching.
                # Here we just ask for what we need to push to socket.
                # To avoid blocking loop, we read reasonable amount.
                read_size = min(remaining, READ_BLOCK)
                
                data = await self.buffer.read(content_id, offset, read_size, file_size, file_hash)
                if not data:
                    break
                
                await response.write(data)
                
                offset += len(data)
                remaining -= len(data)

            return response

        except (ConnectionResetError, BrokenPipeError):
            # Client (VLC) closed connection, this is normal behavior during seeking or stop.
            pass
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            return web.Response(status=500)

    async def _get_file_info(self, map_id: int):
        loop = asyncio.get_running_loop()
        def query():
            conn = self.db._get_conn()
            try:
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
            finally:
                conn.close()
        
        return await loop.run_in_executor(None, query)
