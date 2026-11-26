import argparse
import io
import logging
import lzma
import os
import re
import shutil
import stat
import subprocess
import zlib
from functools import lru_cache
from pathlib import Path, PurePosixPath
from threading import Lock, Thread
from zipfile import BadZipFile, ZipFile
from urllib.parse import unquote

from PIL import Image, ImageOps

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.security import check_password_hash, generate_password_hash

from library import IMAGE_TYPES, TAG_GROUPS, connect, image_pages, index_pending, make_client, natural_key, scan
from settings import default_paths, load_config, roots, save_config, validate

BASE = Path(__file__).resolve().parent
PAGE_SIZE = re.compile(r"^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+)", re.M)
MAX_ZIP_PAGE_BYTES = 64 * 1024 * 1024
ZIP_ERRORS = (BadZipFile, RuntimeError, EOFError, lzma.LZMAError, zlib.error)
PAGE_ROT = re.compile(r"^Page\s+(\d+) rot:\s+(\d+)", re.M)


def poppler(*command):
    return subprocess.run(command, check=True, capture_output=True, timeout=30,
                          env={**os.environ, "LC_ALL": "C"}).stdout


@lru_cache(maxsize=128)
def page_sizes(path, mtime, size):
    info = poppler("pdfinfo", "-f", "1", "-l", "2147483647", str(path)).decode(errors="replace")
    rotations = dict(PAGE_ROT.findall(info))
    sizes = []
    for page, width, height in PAGE_SIZE.findall(info):
        dimensions = [float(width), float(height)]
        if int(rotations.get(page, 0)) % 180:
            dimensions.reverse()
        sizes.append(dimensions)
    return sizes


@lru_cache(maxsize=128)
def cached_image_pages(folder, signature):
    return image_pages(folder)


def dimensions_of(source):
    with Image.open(source) as image:
        width, height = image.size
        if image.getexif().get(274) in (5, 6, 7, 8):
            return [height, width]
        return [width, height]


@lru_cache(maxsize=1024)
def image_dimensions(path, mtime, size):
    return dimensions_of(path)


@lru_cache(maxsize=128)
def zip_entries(path, mtime, size):
    with ZipFile(path) as archive:
        pages = []
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename.replace('\\', "/"))
            if (entry.is_dir() or name.suffix.lower() not in IMAGE_TYPES
                    or any(part.startswith(".") or part == "__MACOSX" for part in name.parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                continue
            if entry.file_size > MAX_ZIP_PAGE_BYTES or entry.flag_bits & 1:
                continue
            pages.append((entry, name))
        return sorted(pages, key=lambda page: natural_key(page[1]))


@lru_cache(maxsize=128)
def zip_page_sizes(path, mtime, size):
    dimensions = []
    with ZipFile(path) as archive:
        for entry, _ in zip_entries(path, mtime, size):
            try:
                with archive.open(entry) as source:
                    dimensions.append(dimensions_of(source))
            except (OSError, Image.DecompressionBombError) + ZIP_ERRORS:
                dimensions.append([1, 1])
    return dimensions


def thumbnail_response(source):
    with Image.open(source) as original:
        thumbnail = ImageOps.exif_transpose(original).convert("RGB")
        thumbnail.thumbnail((420, 420))
        output = io.BytesIO()
        thumbnail.save(output, "JPEG", quality=85)
        output.seek(0)
    return send_file(output, mimetype="image/jpeg", max_age=86400)


def create_app(config_path, database, no_jm=False, base=BASE):
    app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/assets")
    app.config["MAX_CONTENT_LENGTH"] = 32_768
    lock = Lock()
    config = load_config(config_path, base)
    setup_required = not config["admin_password_hash"]
    restart_required = False
    sources = {} if setup_required else roots(config, base)
    index_thread = None

    def start_index(refresh=False):
        nonlocal index_thread
        if no_jm or not sources:
            return
        credentials = (config["jm_username"], config["jm_password"])
        index_thread = Thread(
            target=index_pending, args=(sources, database, lambda: make_client(credentials)),
            kwargs={"refresh": refresh}, daemon=True,
        )
        index_thread.start()

    if not setup_required:
        database.parent.mkdir(parents=True, exist_ok=True)
        scan(sources, database)
        start_index()

    @app.route("/api/setup", methods=["GET", "POST"])
    def setup():
        nonlocal config, setup_required, restart_required
        with lock:
            if request.method == "POST":
                if not setup_required:
                    return jsonify(error="首次配置已关闭"), 409
                values = request.get_json(silent=True)
                if not isinstance(values, dict):
                    return jsonify(error="配置格式无效"), 400
                password = values.get("admin_password")
                if not isinstance(password, str) or not password.strip():
                    return jsonify(error="请输入管理员密码"), 400
                try:
                    updated = validate({
                        **config,
                        **{key: values[key] for key in ("pdf_dir", "images_dir", "zip_dir",
                                                        "jm_username", "jm_password") if key in values},
                        "admin_password_hash": generate_password_hash(password),
                    }, base)
                    save_config(config_path, updated)
                except (ValueError, OSError) as error:
                    return jsonify(error=str(error)), 400
                config = updated
                setup_required = False
                restart_required = True
            response = jsonify(
                setup_required=setup_required,
                restart_required=restart_required,
                default_paths=default_paths(base) if setup_required else {},
            )
            response.headers["Cache-Control"] = "no-store"
            return response

    def check_admin():
        password = unquote(request.headers.get("X-Admin-Password", ""))
        if not password or not check_password_hash(config["admin_password_hash"], password):
            abort(403)

    @app.route("/api/settings", methods=["GET", "POST"])
    def settings():
        nonlocal config, sources
        with lock:
            if setup_required or restart_required:
                return jsonify(error="请完成首次配置并重启 Viewer"), 409
            check_admin()
            if request.method == "POST":
                if not request.is_json:
                    return jsonify(error="请提交 JSON 配置"), 400
                if index_thread and index_thread.is_alive():
                    return jsonify(error="标签更新正在进行，请稍后再试"), 409
                values = request.get_json()
                if not isinstance(values, dict):
                    return jsonify(error="配置格式无效"), 400
                updated = {**config, **{
                    key: values[key] for key in ("pdf_dir", "images_dir", "zip_dir",
                                                 "jm_username", "jm_password") if key in values
                }}
                if values.get("clear_password") is True:
                    updated["jm_password"] = ""
                elif values.get("jm_username") == "" and not values.get("jm_password"):
                    updated["jm_password"] = ""
                elif not values.get("jm_password"):
                    updated["jm_password"] = config["jm_password"]
                try:
                    updated = validate(updated, base)
                    updated_sources = roots(updated, base)
                    scan(updated_sources, database)
                    save_config(config_path, updated)
                except (ValueError, OSError) as error:
                    return jsonify(error=str(error)), 400
                config, sources = updated, updated_sources
                start_index(refresh=values.get("refresh") is True)

            response = jsonify(
                pdf_dir=config["pdf_dir"], images_dir=config["images_dir"], zip_dir=config["zip_dir"],
                jm_username=config["jm_username"], has_password=bool(config["jm_password"]),
            )
            response.headers["Cache-Control"] = "no-store"
            return response

    def book_path(book_id):
        with lock:
            current = sources
        if not current:
            abort(404)
        with connect(database) as conn:
            book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None or book["source"] not in current:
            abort(404)
        root = current[book["source"]]
        candidate = root / book["path"]
        path = candidate.resolve()
        kind = book["source"].split(":", 1)[0] if book["source"].startswith(("images:", "zips:")) else "pdf"
        if candidate.is_symlink() or not path.is_relative_to(root):
            abort(404)
        if not (path.is_dir() if kind == "images" else path.is_file()):
            abort(404)
        return path, kind, book["mtime"]

    @app.get("/")
    def home():
        return app.send_static_file("index.html")

    @app.get("/api/library")
    def library():
        with lock:
            current = tuple(sources)
            indexing = index_thread is not None and index_thread.is_alive()
        books = {}
        if current:
            placeholders = ",".join("?" for _ in current)
            with connect(database) as conn:
                rows = conn.execute(f"""
                    SELECT b.id, b.path,
                        CASE WHEN m.book_id IS NULL THEN b.status ELSE 'manual' END AS status,
                        t.kind, t.name
                    FROM books b
                    LEFT JOIN manually_classified_books m ON b.id = m.book_id
                    LEFT JOIN effective_book_tags t ON b.id = t.book_id
                    WHERE b.source IN ({placeholders})
                    ORDER BY b.path, t.kind, t.name
                """, current).fetchall()
            for row in rows:
                book = books.setdefault(row["id"], {
                    "id": row["id"], "name": Path(row["path"]).name,
                    "status": row["status"], "tags": [],
                })
                if row["kind"]:
                    book["tags"].append({"kind": row["kind"], "name": row["name"]})
        response = jsonify(list(books.values()))
        response.headers["X-Indexing"] = "1" if indexing else "0"
        return response

    @app.post("/api/books/<int:book_id>/tags")
    def edit_tag(book_id):
        values = request.get_json(silent=True)
        if not isinstance(values, dict):
            return jsonify(error="标签数据无效"), 400
        kind, name, present = values.get("kind"), values.get("name"), values.get("present")
        if kind not in TAG_GROUPS or not isinstance(name, str) or not name.strip() or type(present) is not bool:
            return jsonify(error="请填写标签类别和名称"), 400

        with lock:
            if not sources:
                abort(404)
            with connect(database) as conn:
                book = conn.execute("SELECT source FROM books WHERE id = ?", (book_id,)).fetchone()
                if book is None or book["source"] not in sources:
                    abort(404)
                conn.execute("""
                    INSERT INTO tag_overrides(book_id, kind, name, present) VALUES (?, ?, ?, ?)
                    ON CONFLICT(book_id, kind, name) DO UPDATE SET present = excluded.present
                """, (book_id, kind, name.strip(), present))
                tags = conn.execute("""
                    SELECT kind, name FROM effective_book_tags WHERE book_id = ? ORDER BY kind, name
                """, (book_id,)).fetchall()
        return jsonify([dict(tag) for tag in tags])

    @app.get("/api/books/<int:book_id>/pages")
    def pages(book_id):
        path, kind, signature = book_path(book_id)
        try:
            if kind == "images":
                dimensions = []
                for image in cached_image_pages(path, signature):
                    if image.is_symlink() or not image.resolve().is_relative_to(path):
                        dimensions.append([1, 1])
                        continue
                    try:
                        stat = image.stat()
                        dimensions.append(image_dimensions(image, stat.st_mtime_ns, stat.st_size))
                    except (OSError, Image.DecompressionBombError):
                        dimensions.append([1, 1])
            else:
                stat = path.stat()
                get_sizes = zip_page_sizes if kind == "zips" else page_sizes
                dimensions = get_sizes(path, stat.st_mtime_ns, stat.st_size)
        except (OSError, subprocess.SubprocessError) + ZIP_ERRORS:
            abort(422)
        if not dimensions:
            abort(422)
        return jsonify(dimensions)

    @app.get("/api/books/<int:book_id>/cover")
    def cover_image(book_id):
        return page_image(book_id, 1, cover=True)

    @app.get("/api/books/<int:book_id>/pages/<int:page>")
    def page_image(book_id, page, cover=False):
        path, kind, signature = book_path(book_id)
        try:
            if kind == "images":
                entries = cached_image_pages(path, signature)
                if not 1 <= page <= len(entries):
                    abort(404)
                entry = entries[page - 1]
                image = entry.resolve()
                if entry.is_symlink() or not image.is_relative_to(path) or not image.is_file():
                    abort(404)
                if not cover:
                    return send_file(image, mimetype=IMAGE_TYPES[entry.suffix.lower()])
                return thumbnail_response(image)

            stat = path.stat()
            if kind == "zips":
                entries = zip_entries(path, stat.st_mtime_ns, stat.st_size)
                if not 1 <= page <= len(entries):
                    abort(404)
                entry, name = entries[page - 1]
                with ZipFile(path) as archive, archive.open(entry) as source:
                    if cover:
                        return thumbnail_response(source)
                    data = source.read(MAX_ZIP_PAGE_BYTES + 1)
                    if len(data) > MAX_ZIP_PAGE_BYTES:
                        abort(422)
                return send_file(io.BytesIO(data), mimetype=IMAGE_TYPES[name.suffix.lower()], max_age=86400)

            if not 1 <= page <= len(page_sizes(path, stat.st_mtime_ns, stat.st_size)):
                abort(404)
            image = poppler("pdftoppm", "-f", str(page), "-l", str(page), "-scale-to",
                            "420" if cover else "1800", "-singlefile", "-jpeg", str(path))
        except (OSError, subprocess.SubprocessError, Image.DecompressionBombError) + ZIP_ERRORS:
            abort(422)
        return send_file(io.BytesIO(image), mimetype="image/jpeg", max_age=86400)

    return app


def main():
    parser = argparse.ArgumentParser(description="内网同人志阅读器")
    parser.add_argument("--config", type=Path, default=BASE / "data" / "config.json")
    parser.add_argument("--db", type=Path, default=BASE / "data" / "library.sqlite3")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-jm", action="store_true", help="跳过联网打标签")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if any(not shutil.which(command) for command in ("pdfinfo", "pdftoppm")):
        logging.warning("缺少 Poppler，PDF 暂无法阅读；图片阅读仍可用")
    try:
        app = create_app(args.config.resolve(), args.db.resolve(), args.no_jm)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
