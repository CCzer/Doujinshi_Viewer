import logging
import hashlib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

TAG_GROUPS = ("works", "actors", "tags", "authors")
BRACKETS = re.compile(r"\[[^\[\]]*\]|\([^()]*\)")
EDGE = " .-_　"
LOG = logging.getLogger(__name__)


def titles(stem):
    """Return the original title first, then any separate translated title."""
    text = unicodedata.normalize("NFKC", stem)
    blocks = list(BRACKETS.finditer(text))

    def clean(value):
        return " ".join(BRACKETS.sub(" ", value).split()).strip(EDGE)

    # A run of bracketed labels often separates a leading translation
    # from the original title. Trailing labels alone leave no suffix.
    for block in reversed(blocks):
        if not block.group().startswith("["):
            continue
        suffix = clean(text[block.end():])
        if suffix:
            prefix = clean(text[:block.start()])
            return list(dict.fromkeys(filter(None, (suffix, prefix))))
    title = clean(text)
    return [title] if title else []


def title_key(title):
    return "".join(
        char for char in unicodedata.normalize("NFKC", title).casefold()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


@contextmanager
def connect(database):
    conn = sqlite3.connect(database, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


IMAGE_TYPES = {
    ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def natural_key(path):
    return [
        tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
              for part in re.split(r"([0-9]+)", name))
        for name in path.parts
    ], path.as_posix()


def image_pages(folder):
    return sorted(
        (path for path in folder.rglob("*") if path.suffix.lower() in IMAGE_TYPES
         and not path.is_symlink() and path.is_file()
         and path.resolve().is_relative_to(folder)),
        key=lambda path: natural_key(path.relative_to(folder)),
    )


def discover(source, root):
    files = {}
    if source.startswith("images:"):
        for folder in root.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            pages = image_pages(folder)
            if pages:
                digest = hashlib.sha256()
                size = 0
                for page in pages:
                    stat = page.stat()
                    digest.update(repr((page.relative_to(folder).as_posix(),
                                        stat.st_mtime_ns, stat.st_size)).encode())
                    size += stat.st_size
                files[folder.name] = (int.from_bytes(digest.digest()[:7], "big"), size)
    else:
        for path in root.rglob("*"):
            if path.suffix.lower() != ".pdf" or path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                continue
            stat = path.stat()
            files[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return files


def scan(sources, database):
    discovered = {source: discover(source, root) for source, root in sources.items()}
    with connect(database) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                album TEXT,
                UNIQUE(source, path)
            );
            CREATE TABLE IF NOT EXISTS book_tags (
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                PRIMARY KEY (book_id, kind, name)
            );
            CREATE INDEX IF NOT EXISTS tags_lookup ON book_tags(kind, name, book_id);
        """)
        for source, files in discovered.items():
            existing = conn.execute("SELECT * FROM books WHERE source = ?", (source,))
            for row in existing.fetchall():
                if files.get(row["path"]) != (row["mtime"], row["size"]):
                    conn.execute("DELETE FROM books WHERE id = ?", (row["id"],))
            conn.executemany("""
                INSERT INTO books(source, path, mtime, size) VALUES (?, ?, ?, ?)
                ON CONFLICT(source, path) DO NOTHING
            """, ((source, path, *signature) for path, signature in files.items()))


def make_client(credentials):
    import jmcomic

    jmcomic.JmModuleConfig.disable_jm_log()
    username, password = credentials
    option = jmcomic.JmOption.default()
    option.client.postman.src_dict["meta_data"].setdefault("timeout", 20)
    client = option.build_jm_client()
    if username:
        client.login(username, password)
    return client


def find_album(client, candidates):
    ambiguous = False
    for title in candidates:
        key = title_key(title)
        if not key:
            continue
        matches = {}
        for number in range(1, 4):
            page = client.search_site(search_query=title, page=number)
            if page.is_single_album:
                album = page.single_album
                if key in {title_key(part) for part in titles(album.name)}:
                    return album, "indexed"
                break
            for album_id, result_title in page:
                if key in {title_key(part) for part in titles(result_title)}:
                    matches[str(album_id)] = result_title
            if len(matches) > 1 or number >= page.page_count:
                break
        if len(matches) == 1:
            return client.get_album_detail(next(iter(matches))), "indexed"
        ambiguous |= len(matches) > 1
    return None, "ambiguous" if ambiguous else "unmatched"


def index_pending(sources, database, client_factory, refresh=False):
    with connect(database) as conn:
        pending = [
            row
            for source in sources
            for row in conn.execute("""
                SELECT id, source, path, status FROM books
                WHERE source = ? AND (? OR status != 'indexed') ORDER BY path
            """, (source, refresh))
        ]
        conn.executemany("UPDATE books SET status = 'pending' WHERE id = ?",
                         ((book["id"],) for book in pending if book["status"] != "indexed"))
    if not pending:
        return

    try:
        client = client_factory()
    except Exception as error:
        LOG.error("JM 客户端初始化失败 (%s)", type(error).__name__)
        with connect(database) as conn:
            conn.executemany("UPDATE books SET status = 'error' WHERE id = ?",
                             ((book["id"],) for book in pending if book["status"] != "indexed"))
        return

    for book in pending:
        try:
            path = Path(book["path"])
            title = path.name if book["source"].startswith("images:") else path.stem
            album, status = find_album(client, titles(title))
            tags = [
                (book["id"], kind, name.strip())
                for kind in TAG_GROUPS if album
                for name in getattr(album, kind)
                if name.strip()
            ]
        except Exception as error:
            LOG.warning("索引失败 %s (%s)", book["path"], type(error).__name__)
            album, status, tags = None, "error", []

        if album is None and book["status"] == "indexed":
            LOG.warning("刷新未获得新标签，保留已有标签 %s", book["path"])
            continue

        with connect(database) as conn:
            conn.execute("DELETE FROM book_tags WHERE book_id = ?", (book["id"],))
            conn.executemany("INSERT OR IGNORE INTO book_tags VALUES (?, ?, ?)", tags)
            conn.execute("UPDATE books SET status = ?, album = ? WHERE id = ?",
                         (status, album.album_id if album else None, book["id"]))
        LOG.info("%s %s", status, book["path"])
