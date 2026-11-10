"""Tiny real image/PDF files for the embedded-keyword tests (#249/#252).

Not a test module (no `test_` prefix): shared bytes and two helpers for
tests/test_embedded_keywords.py and tests/test_process_document_keyword.py.

The files are embedded as base64 - a 2x2 grey JPEG, PNG and TIFF, and a
one-page PDF holding that JPEG - so the real-exiftool tests depend on the
exiftool binary alone, never on Pillow being installed as a second, quieter
reason to skip. `read_fields` reads a file back with exiftool directly, NOT
through the code under test, so a test can tell what really landed in each
keyword field.

exiftool-dependent tests skip when the binary is missing (the CI pytest job
does not install it). Patterns.md §13: a test that silently skips proves
nothing, so run these at least once on a machine that has exiftool.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

EXIFTOOL = shutil.which('exiftool')

_B64 = {
    '.jpg': (
        '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs'
        'IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAACAAIBAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcI'
        'CQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcY'
        'GRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKj'
        'pKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APf6'
        '/9k='),
    '.png': 'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAAAAABX3VL4AAAADklEQVR4nGP8z8DEwAAABQ0BA7hKXG8AAAAASUVORK5CYII=',
    '.tif': (
        'SUkqAAgAAAAJAAABBAABAAAAAgAAAAEBBAABAAAAAgAAAAIBAwABAAAACAAAAAMBAwABAAAAAQAAAAYBAwABAAAA'
        'AQAAABEBBAABAAAAegAAABYBBAABAAAAAgAAABcBBAABAAAABAAAABwBAwABAAAAAQAAAAAAAAD/////'),
}


def _pdf_bytes() -> bytes:
    """A one-page PDF drawing the JPEG above, with a correct xref table."""
    jpeg = base64.b64decode(_B64['.jpg'])
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        (b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2 2] '
         b'/Resources << /XObject << /Im 4 0 R >> >> /Contents 5 0 R >>'),
        (b'<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray '
         b'/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n' % len(jpeg)
         + jpeg + b'\nendstream'),
        b'<< /Length 22 >>\nstream\nq 2 0 0 2 0 0 cm /Im Do Q\nendstream',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % n + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return bytes(out)


def fixture_bytes(suffix: str) -> bytes:
    """The bytes of a tiny real file of this format (.jpg .png .tif .pdf)."""
    suffix = suffix.lower()
    if suffix == '.pdf':
        return _pdf_bytes()
    if suffix in ('.jpeg',):
        suffix = '.jpg'
    if suffix in ('.tiff',):
        suffix = '.tif'
    return base64.b64decode(_B64[suffix])


def write_fixture(path: Path) -> Path:
    """Write a tiny real file of `path`'s format; return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fixture_bytes(path.suffix))
    return path


def read_fields(path: Path) -> dict[str, list[str]]:
    """Each keyword field's values, read with exiftool directly (not fha)."""
    proc = subprocess.run(
        ['exiftool', '-j', '-a', '-G1', '-IPTC:Keywords', '-XMP-dc:Subject',
         '-XMP-lr:HierarchicalSubject', str(path)],
        check=True, capture_output=True, text=True, encoding='utf-8')
    row = json.loads(proc.stdout)[0]
    out = {}
    for tag in ('IPTC:Keywords', 'XMP-dc:Subject', 'XMP-lr:HierarchicalSubject'):
        raw = row.get(tag)
        out[tag] = [] if raw is None else [str(v) for v in (raw if isinstance(raw, list) else [raw])]
    return out


def legacy_iptc_write(path: Path, keyword: str) -> None:
    """What fha wrote before #252: IPTC Keywords only."""
    subprocess.run(['exiftool', f'-IPTC:Keywords+={keyword}', '-overwrite_original_in_place',
                    str(path)], check=True, capture_output=True, text=True)
