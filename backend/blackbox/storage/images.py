import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .db import uid, now
from .files import atomic_write

MAX_BYTES = 8 * 1024 * 1024


def normalize(data: bytes):
    if len(data) > MAX_BYTES:
        raise ValueError('Image exceeds 8 MiB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError('Only genuine PNG, JPEG and WebP images are accepted')
                if image.width > 4096 or image.height > 4096 or image.width < 1 or image.height < 1 or getattr(image, 'n_frames', 1) > 1:
                    raise ValueError('Image must be a still image up to 4096 pixels per side')
                image.load()
                fmt = 'JPEG' if image.format == 'JPEG' else 'PNG'
                # Copy pixels to remove EXIF, ICC, text chunks and all other original metadata.
                oriented = ImageOps.exif_transpose(image).convert('RGB' if fmt == 'JPEG' else 'RGBA')
                oriented.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                clean = Image.new(oriented.mode, oriented.size)
                clean.paste(oriented)
                buffer = io.BytesIO()
                clean.save(buffer, format=fmt, **({'quality': 90, 'optimize': True} if fmt == 'JPEG' else {}))
                return buffer.getvalue(), 'image/jpeg' if fmt == 'JPEG' else 'image/png', clean.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise ValueError('Invalid or unsafe image contents') from e


def store(settings, db, sid, normalized):
    data, mime, dimensions = normalized
    aid = uid()
    path = settings.data_dir/'attachments'/(aid + ('.jpg' if mime == 'image/jpeg' else '.png'))
    atomic_write(path, data)
    try:
        db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?,?)', (aid, sid, str(path), mime, f'{dimensions[0]}x{dimensions[1]}', len(data), now()))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return aid, path
