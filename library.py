import logging
import hashlib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from functools import lru_cache
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


@lru_cache(maxsize=1)
def simplified_converter():
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    return OpenCC("t2s.json")


@lru_cache(maxsize=4096)
def tag_search_key(value):
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    converter = simplified_converter()
    return converter.convert(normalized) if converter else normalized


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
        extension = ".zip" if source.startswith("zips:") else ".pdf"
        for path in root.rglob("*"):
            if path.suffix.lower() != extension or path.is_symlink() or not path.is_file():
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
            BEGIN;
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
            CREATE TABLE IF NOT EXISTS tag_overrides (
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                present INTEGER NOT NULL,
                PRIMARY KEY (book_id, kind, name)
            );
            CREATE VIEW IF NOT EXISTS manually_classified_books AS
                SELECT b.id AS book_id
                FROM books b
                JOIN book_tags fallback ON fallback.book_id = b.id
                   AND fallback.kind = 'tags' AND fallback.name = '未分类'
                WHERE b.status != 'indexed'
                  AND EXISTS (
                      SELECT 1 FROM tag_overrides o
                      WHERE o.book_id = b.id AND o.present = 1
                        AND NOT (o.kind = 'tags' AND o.name = '未分类')
                  );
            DROP VIEW IF EXISTS effective_book_tags;
            CREATE VIEW effective_book_tags AS
                SELECT t.book_id, t.kind, t.name
                FROM book_tags t
                LEFT JOIN tag_overrides o USING (book_id, kind, name)
                WHERE o.present IS NULL
                  AND NOT (
                      t.kind = 'tags' AND t.name = '未分类'
                      AND EXISTS (SELECT 1 FROM manually_classified_books m WHERE m.book_id = t.book_id)
                  )
                UNION ALL
                SELECT book_id, kind, name FROM tag_overrides WHERE present = 1;
            CREATE TABLE IF NOT EXISTS tag_aliases (
                kind TEXT NOT NULL,
                alias TEXT NOT NULL,
                target TEXT NOT NULL,
                PRIMARY KEY (kind, alias),
                CHECK (alias != target)
            );
            CREATE VIEW IF NOT EXISTS resolved_tag_aliases AS
                WITH RECURSIVE chain(kind, alias, target) AS (
                    SELECT kind, alias, target FROM tag_aliases
                    UNION
                    SELECT c.kind, c.alias, a.target
                    FROM chain c JOIN tag_aliases a ON a.kind = c.kind AND a.alias = c.target
                )
                SELECT c.kind, c.alias, c.target FROM chain c
                WHERE NOT EXISTS (
                    SELECT 1 FROM tag_aliases a WHERE a.kind = c.kind AND a.alias = c.target
                );
            CREATE VIEW IF NOT EXISTS visible_book_tags AS
                WITH normalized AS (
                    SELECT t.book_id, t.kind, COALESCE(a.target, t.name) AS name
                    FROM effective_book_tags t
                    LEFT JOIN resolved_tag_aliases a ON a.kind = t.kind AND a.alias = t.name
                )
                SELECT DISTINCT n.book_id, n.kind, n.name
                FROM normalized n
                WHERE NOT EXISTS (
                    SELECT 1 FROM tag_overrides o
                    WHERE o.book_id = n.book_id AND o.kind = n.kind AND o.name = n.name
                      AND o.present = 0
                );
            COMMIT;
        """)
        if "album_source" not in {column["name"] for column in conn.execute("PRAGMA table_info(books)")}:
            conn.execute("ALTER TABLE books ADD COLUMN album_source TEXT")
            conn.execute("UPDATE books SET album_source = 'jm' WHERE album IS NOT NULL")
        for source, files in discovered.items():
            existing = conn.execute("SELECT * FROM books WHERE source = ?", (source,))
            for row in existing.fetchall():
                signature = files.get(row["path"])
                if signature == (row["mtime"], row["size"]):
                    continue
                overrides = conn.execute("""
                    SELECT kind, name, present FROM tag_overrides WHERE book_id = ?
                """, (row["id"],)).fetchall() if signature is not None else []
                conn.execute("DELETE FROM books WHERE id = ?", (row["id"],))
                if overrides:
                    book_id = conn.execute("""
                        INSERT INTO books(source, path, mtime, size) VALUES (?, ?, ?, ?)
                    """, (source, row["path"], *signature)).lastrowid
                    conn.executemany("INSERT INTO tag_overrides VALUES (?, ?, ?, ?)",
                                     ((book_id, *entry) for entry in overrides))
            conn.executemany("""
                INSERT INTO books(source, path, mtime, size) VALUES (?, ?, ?, ?)
                ON CONFLICT(source, path) DO NOTHING
            """, ((source, path, *signature) for path, signature in files.items()))
            conn.execute("""
                INSERT OR IGNORE INTO book_tags(book_id, kind, name)
                SELECT id, 'tags', '未分类' FROM books WHERE source = ? AND status = 'unmatched'
            """, (source,))


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
    for title in candidates:
        key = title_key(title)
        if not key:
            continue
        for number in range(1, 4):
            page = client.search_site(search_query=title, page=number)
            if page.is_single_album:
                album = page.single_album
                if key in {title_key(part) for part in titles(album.name)}:
                    return album, "indexed"
                break
            for album_id, result_title in page:
                if key in {title_key(part) for part in titles(result_title)}:
                    return client.get_album_detail(str(album_id)), "indexed"
            if number >= page.page_count:
                break
    return None, "unmatched"


def index_pending(sources, database, client_factory, refresh=False, retry_failed=False, nhentai_lookup=None):
    if not sources:
        return

    placeholders = ",".join("?" for _ in sources)
    with connect(database) as conn:
        pending = conn.execute(f"""
            SELECT id, source, path, status FROM books
            WHERE source IN ({placeholders})
              AND (status = 'pending' OR ? OR (? AND status != 'indexed'))
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'indexed' THEN 2 ELSE 1 END,
                     path, source
        """, (*sources, refresh, retry_failed)).fetchall()
    if not pending:
        return

    jm_client = None
    jm_init_error = None

    def lookup_jm(candidates, _title):
        nonlocal jm_client, jm_init_error
        if jm_init_error is not None:
            raise jm_init_error
        if jm_client is None:
            try:
                jm_client = client_factory()
            except Exception as error:
                jm_init_error = error
                raise
        return find_album(jm_client, candidates)[0]

    providers = [("jm", lookup_jm)]
    if nhentai_lookup is not None:
        providers.insert(0, ("nhentai", nhentai_lookup))

    for book in pending:
        path = Path(book["path"])
        title = path.name if book["source"].startswith("images:") else path.stem
        candidates = titles(title)
        failed = False
        for album_source, lookup in providers if candidates else ():
            try:
                album = lookup(candidates, title)
                if album is None:
                    continue
                tags = [
                    (book["id"], kind, name.strip())
                    for kind in TAG_GROUPS
                    for name in getattr(album, kind)
                    if name.strip()
                ]
            except Exception as error:
                LOG.warning("索引失败 [%s] %s (%s)", album_source, book["path"], type(error).__name__)
                failed = True
                continue
            status = "indexed"
            break
        else:
            album, album_source, tags = None, None, []
            status = "error" if failed else "unmatched"

        if album is None and book["status"] == "indexed":
            LOG.warning("刷新未获得新标签，保留已有标签 %s", book["path"])
            continue

        if status == "unmatched":
            tags.append((book["id"], "tags", "未分类"))

        with connect(database) as conn:
            updated = conn.execute(
                "UPDATE books SET status = ?, album = ?, album_source = ? WHERE id = ?",
                (status, album.album_id if album else None, album_source if album else None, book["id"]),
            )
            if not updated.rowcount:
                continue
            if status != "error":
                conn.execute("DELETE FROM book_tags WHERE book_id = ?", (book["id"],))
            conn.executemany("INSERT OR IGNORE INTO book_tags VALUES (?, ?, ?)", tags)
        LOG.info("%s %s", status, book["path"])
