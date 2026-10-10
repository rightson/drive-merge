"""Coarse file categories, used for search filters and as a rule input.

The table is deliberately small and boring; reorganisation rules (classify.py)
can match on extensions directly when they need finer control.
"""

from __future__ import annotations

CATEGORIES: dict[str, frozenset[str]] = {
    "photo": frozenset("jpg jpeg png gif heic heif webp bmp tif tiff raw cr2 cr3 nef arw dng orf rw2 raf svg".split()),
    "video": frozenset("mp4 mov m4v avi mkv wmv flv webm mts m2ts 3gp mpg mpeg".split()),
    "audio": frozenset("mp3 m4a aac wav flac aiff aif ogg opus wma mid midi".split()),
    "document": frozenset("doc docx odt rtf txt md pages tex epub mobi azw3 gdoc".split()),
    "pdf": frozenset({"pdf"}),
    "spreadsheet": frozenset("xls xlsx xlsm ods csv tsv numbers gsheet".split()),
    "presentation": frozenset("ppt pptx odp key gslides".split()),
    "archive": frozenset("zip rar 7z tar gz tgz bz2 xz dmg iso".split()),
    "code": frozenset("py js ts tsx jsx go rs java kt c h cpp hpp cs rb php swift sh ipynb json yaml yml toml sql html css".split()),
    "design": frozenset("psd ai sketch fig xd indd afdesign afphoto".split()),
}

_BY_EXT: dict[str, str] = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}

_MIME_PREFIX = (("image/", "photo"), ("video/", "video"), ("audio/", "audio"))


def category_of(ext: str, mime: str | None = None) -> str:
    """Category for a lowercase extension (no dot), falling back to MIME type."""
    cat = _BY_EXT.get(ext)
    if cat:
        return cat
    if mime:
        if mime.startswith("application/vnd.google-apps."):
            return "document"
        for prefix, c in _MIME_PREFIX:
            if mime.startswith(prefix):
                return c
    return "other"
