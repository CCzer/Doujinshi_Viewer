import json
import re
import time
import unicodedata
from email.utils import parsedate_to_datetime
from functools import lru_cache
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from library import TAG_GROUPS, title_key, titles

API_URL = "https://nhentai.net/api/v2"
USER_AGENT = "Doujinshi_Viewer/1.0 (https://github.com/CCzer/Doujinshi_Viewer)"
TAG_KINDS = {
    "parody": "works",
    "character": "actors",
    "tag": "tags",
    "artist": "authors",
}
BRACKET_LABELS = re.compile(r"\[([^\[\]]*)\]")
TITLE_SEPARATOR = re.compile(r"\s+\|\s+")
JAPANESE_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
LANGUAGE_MARKERS = {
    "chinese": ("中国翻译", "中國翻譯", "中文", "汉化", "漢化", "chinese"),
    "english": ("英訳", "英译", "英譯", "english"),
    "japanese": ("日文", "日本語", "japanese"),
}


def author_hints(filename):
    hints = set()
    for bracket in BRACKET_LABELS.findall(unicodedata.normalize("NFKC", filename)):
        for part in re.split(r"[()]", bracket):
            if key := title_key(part):
                hints.add(key)
    return hints


def edition_language(filename):
    labels = BRACKET_LABELS.findall(unicodedata.normalize("NFKC", filename).casefold())
    return next((language for language, markers in LANGUAGE_MARKERS.items()
                 if any(marker in label for label in labels for marker in markers)), None)


def edition_score(gallery, authors, language):
    tags = gallery["tags"]
    author_matches = any(
        tag["type"] in ("artist", "group") and title_key(tag["name"]) in authors for tag in tags
    )
    language_matches = any(tag["type"] == "language" and tag["name"].casefold() == language for tag in tags)
    return author_matches, language_matches


def matching_title(key, *fields):
    for field in fields:
        if not isinstance(field, str):
            continue
        for candidate in titles(field):
            if key == title_key(candidate):
                return True
            if any(key == title_key(part) for part in TITLE_SEPARATOR.split(candidate)):
                return True
    return False


@lru_cache(maxsize=1)
def japanese_converters():
    try:
        from opencc import OpenCC
    except ImportError:
        # Existing installations can still search the original titles before upgrading dependencies.
        return None
    return OpenCC("s2t.json"), OpenCC("t2jp.json")


def search_candidates(candidates):
    originals = dict.fromkeys(candidates)
    yield from originals

    japanese = [title for title in originals if JAPANESE_KANA.search(title)]
    if not japanese:
        return
    converters = japanese_converters()
    if converters is None:
        return
    to_traditional, to_japanese = converters
    seen = set(originals)
    for title in japanese:
        variant = to_japanese.convert(to_traditional.convert(title))
        if variant not in seen:
            seen.add(variant)
            yield variant


def retry_delay(value):
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return 60


class NhentaiClient:
    def __init__(self, api_key=""):
        self.api_key = api_key
        self.intervals = {"search": 3.1, "gallery": 1.4} if api_key else {"search": 6.1, "gallery": 3.1}
        self.ready_at = dict.fromkeys(self.intervals, 0.0)

    def get(self, endpoint, path, params=None):
        if params:
            path += "?" + urlencode(params)
        remaining = self.ready_at[endpoint] - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.ready_at[endpoint] = time.monotonic() + self.intervals[endpoint]
        request = Request(API_URL + path, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        if self.api_key:
            request.add_unredirected_header("Authorization", "Key " + self.api_key)
        try:
            with urlopen(request, timeout=15) as response:
                result = json.load(response)
        except HTTPError as error:
            if error.code == 429:
                self.ready_at[endpoint] = time.monotonic() + max(
                    self.intervals[endpoint], retry_delay(error.headers.get("Retry-After"))
                )
            raise
        if not isinstance(result, dict):
            raise ValueError("Unexpected nHentai response")
        return result

    def search(self, title, page):
        return self.get("search", "/search", {"query": title, "page": page})

    def gallery(self, gallery_id):
        result = self.get("gallery", f"/galleries/{int(gallery_id)}")
        if result.get("id") != int(gallery_id) or not isinstance(result.get("tags"), list):
            raise ValueError("Invalid nHentai gallery")
        return result

    def find_album(self, candidates, filename):
        hints = author_hints(filename)
        language = edition_language(filename)
        for candidate in search_candidates(candidates):
            key = title_key(candidate)
            if not key:
                continue
            for page in range(1, 4):
                result = self.search(candidate, page)
                matches = dict.fromkeys(
                    item["id"] for item in result["result"]
                    if matching_title(key, item.get("english_title"), item.get("japanese_title"))
                )
                if matches:
                    if len(matches) > 1 and (hints or language):
                        gallery = max(
                            (self.gallery(gallery_id) for gallery_id in matches),
                            key=lambda detail: edition_score(detail, hints, language),
                        )
                    else:
                        gallery = self.gallery(next(iter(matches)))
                    tags = {kind: [] for kind in TAG_GROUPS}
                    for tag in gallery["tags"]:
                        kind = TAG_KINDS.get(tag["type"])
                        name = tag["name"].strip()
                        if kind and name:
                            tags[kind].append(name)
                    return SimpleNamespace(album_id=str(gallery["id"]), **tags)
                if page >= result["num_pages"]:
                    break
        return None
