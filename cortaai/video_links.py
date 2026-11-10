"""Safe public-platform link import for permitted source videos.

Do not use this module to bypass authentication, DRM, private videos,
or platform restrictions. Only known public video site hosts are accepted.
"""
from __future__ import annotations

import ipaddress
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException

# Domains explicitly chosen to avoid turning yt-dlp into an arbitrary-URL
# fetcher (and thereby an SSRF proxy). An extractor working isn't guaranteed.
SUPPORTED_DOMAINS = {
    'youtube.com', 'youtu.be', 'youtube-nocookie.com',
    'vimeo.com', 'dailymotion.com', 'dai.ly',
    'tiktok.com', 'instagram.com', 'twitch.tv', 'clips.twitch.tv',
    'twitter.com', 'x.com', 'facebook.com', 'fb.watch',
    'streamable.com', 'reddit.com', 'redd.it', 'kick.com',
    'rumble.com', 'loom.com', 'bilibili.com', 'b23.tv',
}
YOUTUBE_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')


def validate_video_url(raw: str) -> tuple[str, str]:
    """Return (canonical_url, platform), or reject unsafe/unsupported URLs."""
    if not isinstance(raw, str) or len(raw) > 500 or not raw.strip():
        raise HTTPException(400, 'Insira um link de vídeo válido.')
    p = urlsplit(raw.strip())
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise HTTPException(400, 'Use um link HTTPS público de uma plataforma compatível.')
    if p.fragment or '\\' in raw or any(ord(c) < 32 for c in raw):
        raise HTTPException(400, 'Link contém caracteres não permitidos.')
    host = p.hostname.lower().rstrip('.')
    try:
        ipaddress.ip_address(host)
        raise HTTPException(400, 'Endereço IP não permitido.')
    except ValueError:
        pass
    platform = next((d for d in SUPPORTED_DOMAINS if host == d or host.endswith('.' + d)), None)
    if not platform:
        raise HTTPException(400, 'Site não suportado nesta versão. Use YouTube, TikTok, Twitch, Vimeo ou outra plataforma da lista; também é possível enviar um arquivo.')
    if host == 'clips.twitch.tv':
        platform = 'twitch.tv'
    if platform in {'youtube.com', 'youtu.be', 'youtube-nocookie.com'}:
        if platform == 'youtu.be':
            key = p.path.strip('/').split('/')[0]
        elif p.path == '/watch':
            key = parse_qs(p.query).get('v', [''])[0]
        elif p.path.startswith(('/shorts/', '/live/', '/embed/')):
            key = p.path.strip('/').split('/')[1]
        else:
            raise HTTPException(400, 'Use o link de um vídeo individual do YouTube (watch, Shorts ou live gravada).')
        if not YOUTUBE_ID.fullmatch(key):
            raise HTTPException(400, 'ID do vídeo do YouTube inválido.')
        return f'https://www.youtube.com/watch?v={key}', 'YouTube'
    if p.path in ('', '/'):
        raise HTTPException(400, 'Cole um link direto para uma publicação ou vídeo, não para a página inicial.')
    # Only HTTPS and known platform hostnames are accepted. Query params may be
    # relevant for video IDs on Instagram, Facebook, Twitch, etc.
    return p.geturl(), platform


class LimitedLogger:
    """Never log raw URLs or provider credentials in server output."""
    def debug(self, _: str): pass
    def warning(self, _: str): pass
    def error(self, _: str): pass


def download_source(url: str, job: dict, dest: Path, *, max_bytes: int, max_duration: int = 7200):
    """Download one authorized public video (plus optional public subtitles)."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    root = dest.parent
    stem = dest.stem

    def match_filter(info, *, incomplete):
        if info.get('is_live') or info.get('live_status') == 'is_live':
            return 'Transmissões ao vivo ainda não finalizadas não são aceitas.'
        duration = info.get('duration')
        if duration and duration > max_duration:
            return f'O vídeo excede o limite de {max_duration // 60} minutos.'
        if info.get('age_limit', 0) >= 18:
            return 'Vídeo com restrição etária não é aceito.'
        return None

    def progress_hook(d):
        if d.get('status') == 'downloading':
            downloaded = d.get('downloaded_bytes') or 0
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            if downloaded > max_bytes:
                raise ValueError('O vídeo ultrapassou o limite de tamanho deste servidor.')
            job.update(status='downloading', progress=round(min(.96, .05 + .9 * downloaded / total), 3) if total else .1,
                       message='Baixando vídeo…')
        elif d.get('status') == 'finished':
            job.update(status='processing', progress=.96, message='Finalizando áudio, vídeo e legendas…')

    opts = {
        'format': 'b[height<=720][ext=mp4]/bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/best[height<=720]',
        'js_runtimes': {'node': {'path': '/usr/local/bin/node'}},
        'outtmpl': str(root / (stem + '.%(ext)s')),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'playlistend': 1,
        'max_downloads': 1,
        'max_filesize': max_bytes,
        'match_filter': match_filter,
        'quiet': True,
        'no_warnings': True,
        'logger': LimitedLogger(),
        'progress_hooks': [progress_hook],
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['pt-BR', 'pt', 'en'],
        'subtitlesformat': 'vtt/srt',
        'socket_timeout': 20,
        'retries': 2,
        'fragment_retries': 2,
        'continuedl': False,
        'nopart': True,
        'restrictfilenames': True,
        'cachedir': False,
    }
    job.update(status='fetching_info', progress=.02, message='Consultando informações do vídeo…')
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except (DownloadError, ValueError) as ex:
        raw = str(ex)
        if 'Sign in' in raw or 'bot' in raw or '403' in raw or '429' in raw:
            raise RuntimeError('A plataforma bloqueou o download neste servidor (login, limite ou verificação automática). Envie o arquivo original, quando autorizado.') from None
        if 'Video unavailable' in raw or 'not available' in raw:
            raise RuntimeError('Vídeo indisponível, privado ou bloqueado na região.') from None
        raise RuntimeError('Não foi possível importar este link. O site pode exigir login, impedir downloads ou usar um formato não suportado.') from None

    if not info or not isinstance(info, dict):
        raise RuntimeError('A plataforma não retornou dados de vídeo compatíveis.')
    candidates = [p for p in root.glob(stem + '.*') if p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov') and p.is_file()]
    if not candidates:
        raise RuntimeError('A plataforma não disponibilizou um arquivo de vídeo.')
    selected = max(candidates, key=lambda p: p.stat().st_size)
    if selected.stat().st_size > max_bytes:
        raise RuntimeError('Vídeo acima do limite desta hospedagem.')
    selected.replace(dest)
    subtitles = [p for p in root.glob(stem + '.*') if p.suffix.lower() in ('.vtt', '.srt') and p.stat().st_size < 2_000_000]
    subtitle_path = None
    if subtitles:
        # Prefer Portuguese when available.
        subtitles.sort(key=lambda p: (0 if '.pt-BR.' in p.name else (1 if '.pt.' in p.name else 2), str(p)))
        selected_sub = subtitles[0]
        subtitle_path = root / (stem + '.subtitle' + selected_sub.suffix.lower())
        selected_sub.replace(subtitle_path)
    return {'title': str(info.get('title') or 'Vídeo importado')[:120],
            'uploader': str(info.get('uploader') or '')[:90],
            'subtitle_path': subtitle_path,
            'ext': selected.suffix.lower()}