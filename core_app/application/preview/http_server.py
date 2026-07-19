import logging
import secrets
import gzip
from aiohttp import web
from typing import Optional

from core_app.application.file_system.preview_cache_manager import PreviewCacheManager

logger = logging.getLogger(__name__)


class PreviewServer:
    """Standalone HTTP server for serving cached text/document previews.
    Decoupled from VideoStreamer — no video dependency."""

    def __init__(self, preview_cache: Optional[PreviewCacheManager] = None):
        self.preview_cache = preview_cache
        self.app = web.Application()
        self.app.router.add_get('/preview/{token}/{content_id}/preview.pdf', self.handle_preview)
        self.app.router.add_options('/preview/{token}/{content_id}/preview.pdf', self.handle_options)
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
            logger.info(f"Preview HTTP server started on http://127.0.0.1:{self.port}")
        else:
            logger.error("Failed to retrieve bound port for Preview HTTP server.")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("Preview HTTP server stopped.")

    async def get_preview_url(self, content_id: int) -> str:
        """Lazy-start the server and return the preview URL."""
        if not self.runner:
            await self.start()
        if not self.port:
            return ""
        return f"http://127.0.0.1:{self.port}/preview/{self.session_token}/{content_id}/preview.pdf"
    async def handle_options(self, request: web.Request):
        """處理 CORS 預檢請求"""
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Range',
            'Access-Control-Max-Age': '86400',
        }
        return web.Response(status=200, headers=headers)

    async def handle_preview(self, request: web.Request):
        token = request.match_info.get('token', '')
        if token != self.session_token:
            return web.Response(status=403, text='Forbidden')
        try:
            content_id = int(request.match_info['content_id'])
        except (ValueError, KeyError):
            return web.Response(status=400, text='Invalid content_id')
        cached = self.preview_cache.get(content_id) if self.preview_cache else None
        if not cached:
            return web.Response(status=404, text='Not found')
        data = gzip.decompress(cached['data'])
        total_size = len(data)
        
        content_type_map = {
            'text': 'text/plain; charset=utf-8',
            'document_preview': 'application/pdf',
            'document_full': 'application/pdf',
        }
        content_type = content_type_map.get(cached['content_type'], 'application/octet-stream')
        
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Expose-Headers': 'Accept-Ranges, Content-Range, Content-Length',
            'Content-Disposition': 'inline; filename="preview.pdf"',
            'Accept-Ranges': 'bytes'
        }
        
        range_header = request.headers.get('Range')
        if range_header:
            try:
                range_match = range_header.replace('bytes=', '').split('-')
                start = int(range_match[0]) if range_match[0] else 0
                end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else total_size - 1
                
                if start >= total_size:
                    return web.Response(status=416, headers=headers)
                end = min(end, total_size - 1)
                
                chunk = data[start:end+1]
                headers['Content-Range'] = f'bytes {start}-{end}/{total_size}'
                headers['Content-Length'] = str(len(chunk))
                
                return web.Response(
                    body=chunk, 
                    status=206, 
                    headers=headers, 
                    content_type=content_type
                )
            except Exception as e:
                logger.error(f"Error parsing Range header: {e}")
                pass
                
        headers['Content-Length'] = str(total_size)
        return web.Response(body=data, headers=headers, content_type=content_type)
