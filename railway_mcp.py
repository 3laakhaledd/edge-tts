"""Railway Streamable HTTP MCP wrapper; LGPL-3.0-or-later."""
import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import edge_tts
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

KEY = os.environ.get('MCP_API_KEY', '')
if len(KEY) < 32:
    raise RuntimeError('MCP_API_KEY must be a random secret of at least 32 characters.')
BASE = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
if not BASE and os.environ.get('RAILWAY_PUBLIC_DOMAIN'):
    BASE = 'https://' + os.environ['RAILWAY_PUBLIC_DOMAIN']
origin = urlparse(BASE)
if (origin.scheme not in ('http', 'https') or not origin.netloc or origin.path
        or origin.query or origin.fragment or origin.username or origin.password):
    raise RuntimeError('PUBLIC_BASE_URL must be the public origin, without a path.')
if origin.scheme != 'https' and origin.hostname not in ('localhost', '127.0.0.1'):
    raise RuntimeError('Public deployments require HTTPS.')
DIR = Path(os.environ.get('AUDIO_DIR', '/tmp/edge-tts-audio'))
DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
TTL = int(os.environ.get('AUDIO_TTL_SECONDS', '3600'))
if not 60 <= TTL <= 86400:
    raise RuntimeError('AUDIO_TTL_SECONDS must be 60..86400.')
MAX_BYTES = 32 * 1024 * 1024
SLOTS = asyncio.Semaphore(2)
VOICE_LOCK = asyncio.Lock()
VOICE_CACHE = []
VOICE_AT = 0.0
logger = logging.getLogger('edge_tts_mcp')
mcp = FastMCP(
    'Edge TTS', stateless_http=True, json_response=True,
    instructions='Generate Arabic/English MP3 speech. Text is sent to Microsoft. Return download_url as a clickable link; links expire.',
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[origin.netloc, 'localhost:*', '127.0.0.1:*'],
        allowed_origins=[BASE],
    ),
)


def controls(rate, pitch, volume):
    for name, value, unit, lo, hi in (
        ('rate', rate, '%', -50, 100), ('pitch', pitch, 'Hz', -100, 100),
        ('volume', volume, '%', -100, 100),
    ):
        if not re.fullmatch(r'[+-]\d+' + re.escape(unit), value):
            raise ValueError(f'{name} requires a signed integer followed by {unit}.')
        if not lo <= int(value[:-len(unit)]) <= hi:
            raise ValueError(f'{name} must be {lo}..{hi}{unit}.')


def signature(file_id, expires):
    return hmac.new(KEY.encode(), f'audio:{file_id}:{expires}'.encode(), hashlib.sha256).hexdigest()


def cleanup():
    for pattern in ('*.mp3', '*.part'):
        for path in DIR.glob(pattern):
            try:
                if path.stat().st_mtime + TTL <= time.time():
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


async def voices():
    global VOICE_CACHE, VOICE_AT
    async with VOICE_LOCK:
        if not VOICE_CACHE or time.monotonic() - VOICE_AT > 21600:
            result = await asyncio.wait_for(edge_tts.list_voices(), 30)
            VOICE_CACHE = [
                {'name': v['ShortName'], 'locale': v['Locale'], 'gender': v['Gender']}
                for v in result if v['Locale'].lower().startswith(('ar-', 'en-'))
            ]
            VOICE_AT = time.monotonic()
    return VOICE_CACHE


@mcp.tool()
async def list_voices(language: str = 'all') -> dict:
    """List voices: all, ar, en, or a locale such as ar-SA, ar-EG, en-US."""
    language = language.strip().lower()
    if language not in ('all', 'ar', 'en') and not re.fullmatch(r'(ar|en)-[a-z]{2}', language):
        raise ValueError('Use all, ar, en, or a locale such as ar-SA.')
    try:
        available = await voices()
    except Exception:
        raise ValueError('Voice service unavailable. Retry later.') from None
    selected = [v for v in available if language == 'all' or v['locale'].lower() == language
                or v['locale'].lower().startswith(language + '-')]
    return {'voices': selected, 'count': len(selected)}


@mcp.tool()
async def generate_speech(text: str, voice: str = 'auto', rate: str = '+0%',
                          pitch: str = '+0Hz', volume: str = '+0%') -> dict:
    """Generate MP3 and a signed temporary download URL. Max 20,000 characters.

    auto selects ar-SA-ZariyahNeural for Arabic text, en-US-AriaNeural otherwise.
    Use list_voices to select dialect/gender. Text is sent to Microsoft.
    For bilingual announcements generate each language separately.
    """
    text = text.strip()
    if not text or len(text) > 20000:
        raise ValueError('Text must contain 1..20000 characters.')
    controls(rate, pitch, volume)
    if voice == 'auto':
        voice = 'ar-SA-ZariyahNeural' if re.search(r'[\u0600-\u06ff]', text) else 'en-US-AriaNeural'
    try:
        available = await voices()
    except Exception:
        raise ValueError('Voice service unavailable. Retry later.') from None
    if voice not in {v['name'] for v in available}:
        raise ValueError('Unknown Arabic/English voice. Call list_voices first.')
    try:
        await asyncio.wait_for(SLOTS.acquire(), 5)
    except TimeoutError:
        raise ValueError('Both generation slots are busy. Retry shortly.') from None
    file_id = secrets.token_hex(16)
    partial = DIR / f'{file_id}.part'
    output = DIR / f'{file_id}.mp3'
    try:
        # No await between checking disk use and opening the reserved generation.
        cleanup()
        if sum(p.stat().st_size for p in DIR.glob('*.mp3')) + 2 * MAX_BYTES > 256 * 1024 * 1024:
            raise ValueError('Temporary storage is full. Wait for existing links to expire.')
        size = 0
        async with asyncio.timeout(180):
            engine = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
            with partial.open('xb') as audio:
                async for chunk in engine.stream():
                    if chunk['type'] == 'audio':
                        size += len(chunk['data'])
                        if size > MAX_BYTES:
                            raise ValueError('Audio exceeds 32 MB. Split the text.')
                        audio.write(chunk['data'])
        if not size:
            raise ValueError('No audio returned.')
        partial.replace(output)
        os.utime(output, None)
        expires = int(time.time()) + TTL
        query = urlencode({'expires': expires, 'token': signature(file_id, expires)})
        return {'download_url': f'{BASE}/audio/{file_id}.mp3?{query}',
                'mime_type': 'audio/mpeg', 'bytes': size, 'voice': voice,
                'expires_at_unix': expires, 'expires_in_seconds': TTL,
                'notice': 'Anyone with this link can download until expiry. Save promptly.'}
    except asyncio.CancelledError:
        output.unlink(missing_ok=True)
        raise
    except Exception as exc:
        output.unlink(missing_ok=True)
        logger.warning('Speech generation failed (%s)', type(exc).__name__)
        raise ValueError('Speech generation failed. Retry later or split long text.') from None
    finally:
        partial.unlink(missing_ok=True)
        SLOTS.release()


async def health(request):
    return JSONResponse({'status': 'ok', 'service': 'edge-tts-mcp'})


async def download(request):
    file_id = request.path_params['file_id']
    if not re.fullmatch(r'[0-9a-f]{32}', file_id):
        return JSONResponse({'error': 'Not found'}, status_code=404)
    try:
        expires = int(request.query_params.get('expires', '0'))
    except ValueError:
        return JSONResponse({'error': 'Invalid link'}, status_code=403)
    token = request.query_params.get('token', '')
    if (expires <= time.time() or expires > time.time() + TTL + 5
            or not hmac.compare_digest(token, signature(file_id, expires))):
        return JSONResponse({'error': 'Invalid or expired link'}, status_code=403)
    path = DIR / f'{file_id}.mp3'
    if not path.is_file() or path.stat().st_mtime + TTL <= time.time():
        return JSONResponse({'error': 'Expired or service restarted'}, status_code=404)
    return FileResponse(path, media_type='audio/mpeg', filename=f'speech-{file_id}.mp3',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff'})


async def cleanup_loop():
    while True:
        await asyncio.sleep(60)
        cleanup()


@contextlib.asynccontextmanager
async def lifespan(app):
    cleanup()
    async with mcp.session_manager.run():
        task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


http_app = mcp.streamable_http_app()
web_app = Starlette(routes=[Route('/health', health),
                            Route('/audio/{file_id}.mp3', download),
                            Mount('/', app=http_app)], lifespan=lifespan)


class RequireAPIKey:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            path = scope.get('path', '')
            if path == '/mcp' or path.startswith('/mcp/'):
                supplied = dict(scope.get('headers', [])).get(b'authorization', b'')
                if not hmac.compare_digest(supplied, ('Bearer ' + KEY).encode()):
                    response = JSONResponse({'error': 'Unauthorized'}, status_code=401,
                                            headers={'WWW-Authenticate': 'Bearer'})
                    await response(scope, receive, send)
                    return
        await self.wrapped(scope, receive, send)


app = RequireAPIKey(web_app)
if __name__ == '__main__':
    import uvicorn
    # Signed download URLs contain secrets: disable access logging.
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), access_log=False)
