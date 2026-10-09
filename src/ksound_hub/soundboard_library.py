"""On-demand sound discovery/import. No background polling or audio playback."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MEDIA_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus', '.aiff', '.aif', '.mp4', '.webm', '.mkv', '.mov'}
MAX_ITEMS = 2000
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024 * 1024
SUGGESTED_BANKS = (
    {'name': 'Kenney — Interface sounds', 'url': 'https://kenney.nl/assets/interface-sounds', 'note': 'CC0 sound pack · download, extract, then Add folder'},
    {'name': 'Kenney — Audio packs', 'url': 'https://kenney.nl/assets/category:Audio', 'note': 'Browse downloadable sound packs'},
    {'name': 'Freesound', 'url': 'https://freesound.org/', 'note': 'Community sounds · license shown per sound · optional API search'},
)


@dataclass(frozen=True)
class SoundEntry:
    title: str
    url: str = ''
    path: str = ''
    creator: str = ''
    license: str = ''
    source: str = ''

    @property
    def identity(self) -> str:
        return self.url or str(Path(self.path).expanduser().resolve())


def web_url(value: str) -> str:
    value = str(value or '').strip()
    parts = urlsplit(value)
    if (parts.scheme.lower() not in {'https', 'http'} or not parts.hostname
            or parts.username or parts.password or len(value) > 4096
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Use an HTTP or HTTPS URL without embedded credentials.')
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ''))


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = web_url(newurl)
        redirect = super().redirect_request(req, fp, code, msg, headers, target)
        before, after = urlsplit(req.full_url), urlsplit(target)
        if redirect is not None and (before.scheme, before.netloc) != (after.scheme, after.netloc):
            redirect.remove_header('Authorization')
        return redirect


def _open(url: str, api_key: str = ''):
    url = web_url(url)
    headers = {'User-Agent': 'K-Sounds-Hub/0.4.7', 'Accept-Encoding': 'identity'}
    if api_key:
        if urlsplit(url).netloc != 'freesound.org' or urlsplit(url).scheme != 'https':
            raise ValueError('API credentials may only be sent to Freesound over HTTPS.')
        headers['Authorization'] = 'Token ' + api_key.strip()
    try:
        return build_opener(_Redirects()).open(Request(url, headers=headers), timeout=15)
    except HTTPError as exc:
        if exc.code == 429:
            raise ValueError('This source is rate-limiting requests. Please try again later.') from None
        if exc.code in {401, 403}:
            raise ValueError('The source refused access. Check the API key or open its website.') from None
        raise ValueError(f'The source returned HTTP {exc.code}.') from None


def local_entries(paths) -> list[SoundEntry]:
    entries, seen = [], set()
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path in seen or path.suffix.lower() not in MEDIA_EXTENSIONS or not path.is_file():
            continue
        seen.add(path)
        entries.append(SoundEntry(path.stem, path=str(path)))
        if len(entries) > MAX_ITEMS:
            raise ValueError(f'Choose at most {MAX_ITEMS} sounds per import.')
    return entries


def folder_entries(directory: str) -> list[SoundEntry]:
    paths = []
    for root, directories, files in os.walk(directory, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith('.'))
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix.lower() in MEDIA_EXTENSIONS and not path.is_symlink():
                paths.append(path)
                if len(paths) > MAX_ITEMS:
                    raise ValueError(f'This folder contains more than {MAX_ITEMS} sounds. Choose a subfolder.')
    return local_entries(paths)


def parse_bank(document, base_url: str) -> list[SoundEntry]:
    items = document if isinstance(document, list) else document.get('sounds', document.get('results', [])) if isinstance(document, dict) else []
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError(f'A bank must contain a list of at most {MAX_ITEMS} sounds.')
    entries, seen = [], set()
    for raw in items:
        item = {'url': raw} if isinstance(raw, str) else raw
        if not isinstance(item, dict):
            continue
        value = item.get('download_url') or item.get('url')
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            url = web_url(urljoin(base_url, value))
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        entries.append(SoundEntry(
            str(item.get('title') or item.get('name') or Path(unquote(urlsplit(url).path)).stem or 'Sound')[:160],
            url=url, creator=str(item.get('creator') or item.get('author') or '')[:200],
            license=str(item.get('license') or 'Not specified')[:300],
            source=str(item.get('source') or base_url)[:4096],
        ))
    return entries


class _MediaLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {'href', 'src'} and value and len(self.links) < MAX_ITEMS:
                if Path(unquote(urlsplit(value).path)).suffix.lower() in MEDIA_EXTENSIONS:
                    self.links.append(value)


def load_bank(url: str) -> list[SoundEntry]:
    url = web_url(url)
    if Path(unquote(urlsplit(url).path)).suffix.lower() in MEDIA_EXTENSIONS:
        return parse_bank([url], url)
    with _open(url) as response:
        content = response.read(2 * 1024 * 1024 + 1)
        base = web_url(response.geturl())
    if len(content) > 2 * 1024 * 1024:
        raise ValueError('This bank is too large. Use a smaller bank or an API search.')
    try:
        return parse_bank(json.loads(content), base)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parser = _MediaLinks()
        parser.feed(content.decode('utf-8', 'replace'))
        return parse_bank(parser.links, base)


def search_freesound(query: str, api_key: str, page: int = 1) -> list[SoundEntry]:
    if not api_key.strip():
        raise ValueError('Enter your Freesound API key, or browse the suggested websites without a key.')
    url = 'https://freesound.org/apiv2/search/?' + urlencode({
        'query': query[:200], 'page': max(1, page), 'page_size': 30,
        'fields': 'id,name,url,username,license,previews',
    })
    with _open(url, api_key) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('The API response is too large.')
    result = json.loads(raw)
    sounds = []
    for item in result.get('results', [])[:30]:
        previews = item.get('previews') or {}
        url = previews.get('preview-hq-mp3') or previews.get('preview-lq-mp3')
        if url:
            sounds.append({'title': item.get('name'), 'url': url, 'creator': item.get('username'),
                           'license': item.get('license'), 'source': item.get('url')})
    return parse_bank(sounds, 'https://freesound.org/')


def read_library(config_dir: Path) -> dict:
    try:
        data = json.loads((config_dir / 'soundboard_library.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_library(config_dir: Path, data: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / 'soundboard_library.json'
    fd, name = tempfile.mkstemp(prefix='.soundboard-library-', dir=config_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def save_bank(config_dir: Path, name: str, url: str) -> None:
    url = web_url(url)
    data = read_library(config_dir)
    banks = [b for b in data.get('banks', []) if isinstance(b, dict) and b.get('url') != url]
    banks.append({'name': name.strip()[:100] or urlsplit(url).hostname, 'url': url})
    data['banks'] = banks[-100:]
    write_library(config_dir, data)


def active_identities(document: dict, config_dir: Path) -> set[str]:
    paths = {str(Path(s['path']).expanduser().resolve()) for s in document.get('slots', [])
             if isinstance(s, dict) and s.get('path')}
    receipts = read_library(config_dir).get('imports', {})
    return paths | {key for key, value in receipts.items()
                    if isinstance(value, dict) and value.get('path') in paths}


def _download(entry: SoundEntry, directory: Path, cancelled, allowance: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    url = web_url(entry.url)
    suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
    fd, name = tempfile.mkstemp(prefix='.incoming-', dir=directory)
    deadline, size, digest = time.monotonic() + 90, 0, hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as output, _open(url) as response:
            mime = response.headers.get_content_type()
            if mime in {'text/html', 'application/json', 'text/plain'}:
                raise ValueError('This link points to a page, not a downloadable sound.')
            if suffix not in MEDIA_EXTENSIONS:
                suffix = {'audio/mpeg': '.mp3', 'audio/wav': '.wav', 'audio/ogg': '.ogg',
                          'audio/flac': '.flac', 'video/mp4': '.mp4', 'audio/mp4': '.m4a',
                          'video/webm': '.webm'}.get(mime, '')
            if not suffix:
                raise ValueError('This URL does not identify a supported audio or video file.')
            while True:
                if cancelled() or time.monotonic() > deadline:
                    raise ValueError('Download cancelled or timed out.')
                block = response.read(64 * 1024)
                if not block:
                    break
                size += len(block)
                if size > min(MAX_MEDIA_BYTES, allowance):
                    raise ValueError('Download limit exceeded (100 MB per sound, 512 MB per batch).')
                digest.update(block)
                output.write(block)
        if not size:
            raise ValueError('The downloaded file is empty.')
        target = directory / (digest.hexdigest() + suffix)
        os.replace(name, target)
        return target
    finally:
        Path(name).unlink(missing_ok=True)


def media_thumbnail(path: Path, directory: Path) -> str:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return ''
    stat = path.stat()
    key = hashlib.sha256(f'{path}:{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (key + '.jpg')
    if target.is_file():
        return str(target)
    temporary = directory / (key + '.tmp.jpg')
    try:
        result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-threads', '1',
            '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0',
            '-frames:v', '1', '-vf', 'scale=320:320:force_original_aspect_ratio=decrease',
            '-threads', '1', '-y', str(temporary)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
        if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
            os.replace(temporary, target)
            return str(target)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return ''


def prepare_import(entries, config_dir: Path, cancelled=lambda: False, progress=lambda text: None):
    """Worker phase: prepare media only; the UI commits to the fresh live document."""
    ready, errors, seen, total = [], [], set(), 0
    for number, entry in enumerate(entries[:MAX_ITEMS], 1):
        if cancelled():
            break
        if entry.identity in seen:
            continue
        seen.add(entry.identity)
        progress(f'Preparing {number}/{len(entries)}: {entry.title}')
        try:
            path = _download(entry, config_dir / 'library-media', cancelled, MAX_BATCH_BYTES - total) if entry.url else Path(entry.path)
            if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
                raise ValueError('File missing or unsupported.')
            if entry.url:
                total += path.stat().st_size
            if cancelled():
                break
            ready.append({'entry': asdict(entry), 'path': str(path.resolve()),
                          'background_path': media_thumbnail(path, config_dir / 'library-thumbnails')})
        except (OSError, ValueError) as exc:
            errors.append(f'{entry.title}: {exc}')
    return {'ready': ready, 'errors': errors, 'cancelled': cancelled()}


def append_import(document: dict, prepared: list[dict], folder: str) -> tuple[dict, int]:
    """Preserve unrelated live edits and skip existing paths; never replace pads."""
    data = json.loads(json.dumps(document))
    slots = data.setdefault('slots', [])
    existing = {str(Path(s['path']).expanduser().resolve()) for s in slots if isinstance(s, dict) and s.get('path')}
    label = ' '.join(str(folder or 'Main').split())[:32] or 'Main'
    if label.casefold() in {'main', 'all'}:
        label = 'Main'
    folders = data.setdefault('folders', [])
    canonical = next((f for f in folders if str(f).casefold() == label.casefold()), label)
    count = 0
    for item in prepared:
        if item['path'] in existing:
            continue
        entry = item['entry']
        slots.append({'id': 'library-' + secrets.token_hex(10), 'label': entry['title'],
            'path': item['path'], 'background_path': item['background_path'],
            'folder': '' if canonical.casefold() == 'main' else canonical,
            'volume': 80, 'shortcut': '', 'output_channel': 'media', 'send_to_micro': False,
            'auto_gain': 1.0, 'analyzed_path': '', 'trim_db': 0.0,
            'source_url': entry['url'], 'creator': entry['creator'], 'license': entry['license']})
        existing.add(item['path'])
        count += 1
    if count and all(str(f).casefold() != canonical.casefold() for f in folders):
        folders.append(canonical)
    return data, count


def move_slots_to_folder(document: dict, indices: list[int], folder: str) -> tuple[dict, int]:
    data = json.loads(json.dumps(document))
    label = ' '.join(str(folder or 'Main').split())[:32] or 'Main'
    if label.casefold() in {'all', 'main'}:
        label = 'Main'
    folders = data.setdefault('folders', [])
    label = next((str(f) for f in folders if str(f).casefold() == label.casefold()), label)
    storage = '' if label.casefold() == 'main' else label
    slots, moved = data.get('slots', []), 0
    for index in set(indices):
        if 0 <= index < len(slots) and isinstance(slots[index], dict):
            if slots[index].get('folder', '') != storage:
                slots[index]['folder'] = storage
                moved += 1
    if moved and storage and all(str(f).casefold() != label.casefold() for f in folders):
        folders.append(label)
    return data, moved


def record_imports(config_dir: Path, prepared: list[dict]) -> None:
    data = read_library(config_dir)
    receipts = data.setdefault('imports', {})
    for item in prepared:
        entry = SoundEntry(**item['entry'])
        receipts[entry.identity] = {**item['entry'], 'path': item['path']}
    write_library(config_dir, data)


def remote_folder_move(document: dict, ids, folder, expected_folder=None) -> tuple[dict, int, str]:
    """Resolve stable IDs against the live document; stale selections fail as a whole."""
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 2000
            or any(not isinstance(i, str) or not i or len(i) > 256 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Select between 1 and 2000 different sounds.')
    if not isinstance(folder, str) or any(ord(c) < 32 for c in folder):
        raise ValueError('Invalid folder name.')
    label = ' '.join(folder.split()) or 'Main'
    known = [str(f) for f in document.get('folders', [])]
    known += [str(s.get('folder', '')) for s in document.get('slots', []) if isinstance(s, dict)]
    label = next((f for f in known if f.casefold() == label.casefold()), label)
    if len(label) > 48 or (len(label) > 32 and label not in known):
        raise ValueError('New folder names can contain up to 32 characters.')
    if label.casefold() in {'all', '__all__'}:
        raise ValueError('All is a view, not a destination folder.')
    storage = '' if label.casefold() == 'main' else label
    data = json.loads(json.dumps(document))
    matches = {}
    wanted = set(ids)
    for slot in data.get('slots', []):
        if isinstance(slot, dict) and slot.get('id') in wanted:
            if slot['id'] in matches:
                raise ValueError('Duplicate sound IDs. Reload the soundboard first.')
            matches[slot['id']] = slot
    if len(matches) != len(ids):
        raise ValueError('Some selected sounds changed. Reload and select them again.')
    if expected_folder is not None:
        if not isinstance(expected_folder, str) or not expected_folder.strip() or expected_folder.casefold() in {'all', '__all__', 'main'}:
            raise ValueError('Choose a specific folder before removing sounds.')
        if any(str(slot.get('folder', '')).casefold() != expected_folder.casefold() for slot in matches.values()):
            raise ValueError('Some sounds changed folders. Reload before removing them.')
    moved = sum(slot.get('folder', '') != storage for slot in matches.values())
    for slot in matches.values():
        slot['folder'] = storage
    folders = data.setdefault('folders', [])
    if storage and not any(str(f).casefold() == storage.casefold() for f in folders):
        folders.append(storage)
    return data, moved, storage
