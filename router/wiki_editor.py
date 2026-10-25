"""Local visual editing; routes are mounted only for local development."""

import hashlib
import tempfile
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import Constants
from template_env import STATIC_DIR, static_url, templates

from .extensions.meta_extension import META_LINE_RE
from .utils.editor_document import (
    EditorBlock,
    parse_blocks,
    render_document,
    serialize_blocks,
)
from .utils.editor_media import (
    IMAGE_SUFFIXES,
    MAX_UPLOAD_BYTES,
    image_digest,
    list_media,
    store_upload,
    thumbnail,
)
from .utils.editor_settings import (
    DictionaryUpdate,
    read_settings,
    serialize_settings,
    settings_paths,
)
from .utils.markdown_engine import WIKI_DIR

META_FIELDS = {
    "title": "Title",
    "author": "Author",
    "background": "Background",
    "buttonimage": "ButtonImage",
    "buttondesc": "ButtonDesc",
    "aitext": "AIText",
    "aiart": "AIArt",
}


def local_access(request: Request):
    try:
        local = (
            request.client is not None and ip_address(request.client.host).is_loopback
        )

    except ValueError:
        local = False

    if not local or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "Редактор доступен только с этого компьютера")

    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Запрос должен приходить со страницы редактора")


router = APIRouter(prefix="/editor", dependencies=[Depends(local_access)])
_SAVE_LOCK = Lock()


def write_file(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            if target.exists():
                temporary.chmod(target.stat().st_mode & 0o777)

        temporary.replace(target)

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class MetadataUpdate(BaseModel):
    path: str
    meta: dict[str, str]
    revision: str
    blocks: list[EditorBlock] | None = None


def page_path(name: str) -> Path:
    relative = Path(name)
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix != ".md"
    ):
        raise HTTPException(400, "Укажи относительный путь с расширением .md")

    target = (WIKI_DIR / relative).resolve()
    if not target.is_relative_to(WIKI_DIR.resolve()):
        raise HTTPException(400, "Страница должна находиться в папке wiki")

    return target


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def page_url(path: str) -> str:
    relative = Path(path).with_suffix("").as_posix()
    if relative == "index":
        relative = ""

    elif relative.endswith("/index"):
        relative = relative[:-5]

    return "/wiki/" + quote(relative, safe="/")


def read_page(path: str) -> bytes:
    try:
        return page_path(path).read_bytes()

    except FileNotFoundError:
        raise HTTPException(404, "Страница не найдена") from None


def split_metadata(source: str) -> tuple[list[str], str, dict[str, str]]:
    """Use the renderer's header grammar; keep original lines and article intact."""
    lines = source.splitlines(keepends=True)
    header = []
    meta = {}
    for line in lines:
        match = META_LINE_RE.match(line.rstrip("\r\n"))
        if match is None:
            break

        header.append(line)
        meta[match[1].lower()] = match[2].strip()

    return header, "".join(lines[len(header) :]), meta


def update_metadata(source: str, updates: dict[str, str]) -> str:
    header, body, meta = split_metadata(source)
    changes = {}
    for key, value in updates.items():
        if key == "date":
            # Older open editor tabs can still submit it; automation owns it.
            continue

        if key not in META_FIELDS or any(char in value for char in "\r\n\x00"):
            raise HTTPException(400, "Некорректное поле метаданных")

        value = value.strip()
        if value != meta.get(key, ""):
            changes[key] = value

    if not changes:
        return source

    newline = "\r\n" if "\r\n" in source else "\n"
    result = []
    handled = set()
    for line in header:
        name = line.split(":", 1)[0].strip()
        key = name.lower()
        if key not in changes:
            result.append(line)

        elif key not in handled:
            if changes[key]:
                result.append(f"{name}: {changes[key]}{newline}")

            handled.add(key)

    for key, value in changes.items():
        if key not in handled and value:
            if result and not result[-1].endswith("\n"):
                result.append(newline)

            result.append(f"{META_FIELDS[key]}: {value}{newline}")

    prefix = "".join(result)
    if not header and prefix and body and not body.startswith(("\r", "\n")):
        prefix += newline

    return prefix + body


def updated_source(document: MetadataUpdate, annotate=False) -> tuple[bytes, str]:
    data = read_page(document.path)
    if document.revision != revision(data):
        raise HTTPException(
            409, "Файл изменился на диске. Открой страницу заново перед сохранением"
        )

    template = document.path.startswith("_tech/template/")
    source = data.decode("utf-8")
    if not template:
        source = update_metadata(source, document.meta)

    if document.blocks is not None:
        header = [] if template else split_metadata(source)[0]
        try:
            body = serialize_blocks(document.blocks, annotate)

        except ValueError as error:
            raise HTTPException(400, str(error)) from error

        source = "".join(header) + body

    return data, source


@router.get("", response_class=HTMLResponse)
def editor(request: Request):
    return templates.TemplateResponse(request, "editor.html", {})


@router.get("/api/constants")
def constants():
    return Constants.get_all_const()


@router.get("/api/pages")
def pages():
    result = []
    for path in sorted(WIKI_DIR.rglob("*.md")):
        if (
            path.relative_to(WIKI_DIR).parts[0] == "_tech"
            and path.relative_to(WIKI_DIR).as_posix() != "_tech/example.md"
        ):
            continue

        if not path.resolve().is_relative_to(WIKI_DIR.resolve()):
            continue

        name = path.relative_to(WIKI_DIR).as_posix()
        _, _, meta = split_metadata(path.read_text(encoding="utf-8"))
        result.append(
            {
                "path": name,
                "title": meta.get("title") or path.stem,
                "url": page_url(name),
            }
        )

    folders = [""]
    for path in sorted(WIKI_DIR.rglob("*")):
        if path.is_dir() and path.resolve().is_relative_to(WIKI_DIR.resolve()):
            name = path.relative_to(WIKI_DIR).as_posix()
            if not name.startswith("_tech") and not any(
                part.startswith(".") for part in path.relative_to(WIKI_DIR).parts
            ):
                folders.append(name)

    return {"pages": result, "folders": folders}


class PageCreate(BaseModel):
    title: str
    folder: str = ""
    name: str


class PageDelete(BaseModel):
    path: str
    revision: str


@router.post("/api/pages/create")
def create_page(document: PageCreate):
    title = document.title.strip()
    name = document.name.strip().removesuffix(".md")
    if not title or any(char in title for char in "\r\n\x00"):
        raise HTTPException(400, "Укажи название страницы одной строкой")

    if not name or name in {".", ".."} or any(char in name for char in "/\\\r\n\x00"):
        raise HTTPException(400, "Укажи имя страницы без слешей")

    relative = (Path(document.folder) / (name + ".md")).as_posix()
    if Path(relative).parts[0] == "_tech":
        raise HTTPException(400, "Выбери обычный раздел вики")

    target = page_path(relative)
    with _SAVE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write("Title: " + title + "\n\n")

        except FileExistsError:
            raise HTTPException(
                409, "Страница с таким адресом уже существует"
            ) from None

    return {"path": relative}


@router.post("/api/pages/delete")
def delete_page(document: PageDelete):
    with _SAVE_LOCK:
        data = read_page(document.path)
        if revision(data) != document.revision:
            raise HTTPException(
                409, "Страница изменилась на диске. Открой её заново перед удалением"
            )

        page_path(document.path).unlink()

    return {"deleted": document.path}


class TemplateCreate(BaseModel):
    name: str


@router.get("/api/templates")
def list_templates():
    directory = WIKI_DIR / "_tech" / "template"
    result = []
    for path in sorted(directory.rglob("*.md")):
        if not path.resolve().is_relative_to(directory.resolve()):
            continue

        name = path.relative_to(directory).with_suffix("").as_posix()
        result.append({"name": name, "path": path.relative_to(WIKI_DIR).as_posix()})

    return {"templates": result}


@router.post("/api/templates")
def create_template(document: TemplateCreate):
    name = document.name.strip()
    if not name or any(char in name for char in "[]\r\n"):
        raise HTTPException(400, "Укажи имя шаблона")

    relative = "_tech/template/" + name + ".md"
    target = page_path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write("Текст шаблона\n")

    except FileExistsError:
        raise HTTPException(409, "Шаблон с таким именем уже есть") from None

    return {"path": relative}


@router.get("/api/page")
def load_page(path: str):
    data = read_page(path)
    source = data.decode("utf-8")
    if path.startswith("_tech/template/"):
        body, meta = source, {}

    else:
        _, body, meta = split_metadata(source)

    return {
        "path": path,
        "url": page_url(path),
        "meta": meta,
        "revision": revision(data),
        "blocks": [node.model_dump() for node in parse_blocks(body)],
    }


@router.post("/api/save")
def save_page(document: MetadataUpdate):
    target = page_path(document.path)
    with _SAVE_LOCK:
        current, source = updated_source(document)
        data = source.encode("utf-8")
        if data != current:
            write_file(target, data)

    meta = (
        {} if document.path.startswith("_tech/template/") else split_metadata(source)[2]
    )

    return {"meta": meta, "revision": revision(data)}


@router.get("/frame", response_class=HTMLResponse)
def preview_frame(request: Request, component: bool = False):
    return templates.TemplateResponse(
        request,
        "editor_preview.html" if component else "wiki_template.html",
        {
            "title": "",
            "content": "",
            "preview_base": "/wiki/",
            "component_preview": component,
        },
    )


@router.post("/api/preview")
def preview(request: Request, document: MetadataUpdate, component: bool = False):
    _, source = updated_source(document, annotate=not component)
    template = document.path.startswith("_tech/template/")
    if template:
        # A macro has no article metadata; its first line is always content.
        source = "\n" + source

    content, title, date, author, background, ai_text, ai_art = render_document(
        page_path(document.path), source
    )

    if component:
        return {"html": content}

    if template:
        title = "Шаблон: " + document.path[15:-3]

    context = {
        "request": request,
        "title": title,
        "date": date,
        "author": author,
        "ai_text": ai_text,
        "ai_art": ai_art,
        "preview_base": page_url(document.path),
    }

    header = templates.env.get_template("wiki_header.html").render(context)
    return {
        "html": content,
        "header": header,
        "title": title,
        "background": static_url(background) if background else None,
        "base": page_url(document.path),
        "template": template,
    }


@router.post("/api/media")
async def upload_media(request: Request, filename: str, folder: str = "uploads"):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Изображение больше 50 МБ")

    try:
        return await run_in_threadpool(
            store_upload, STATIC_DIR, filename, bytes(data), folder
        )

    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/media")
def browse_media(folder: str = ""):
    try:
        return list_media(STATIC_DIR, folder)

    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/thumbnail")
def media_thumbnail(path: str):
    target = (STATIC_DIR / path).resolve()
    if (
        not target.is_relative_to((STATIC_DIR / "images").resolve())
        or not target.is_relative_to(STATIC_DIR.resolve())
        or target.suffix.lower() not in IMAGE_SUFFIXES
    ):
        raise HTTPException(400, "Изображение должно находиться в static/images")

    if not target.is_file():
        raise HTTPException(404, "Изображение не найдено")

    if target.suffix.lower() == ".svg":
        return FileResponse(target, media_type="image/svg+xml")

    stat = target.stat()
    key = image_digest(target, stat.st_mtime_ns, stat.st_size)
    cache = Path(tempfile.gettempdir()) / "spf-wiki-editor-thumbnails"
    cache.mkdir(exist_ok=True)
    cached = cache / (key + ".webp")
    if not cached.exists():
        try:
            data = thumbnail(target)

        except (OSError, ValueError):
            raise HTTPException(400, "Не удалось прочитать изображение") from None

        cached.write_bytes(data)

    return Response(
        cached.read_bytes(),
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=300", "X-Image-Hash": key},
    )


@router.get("/api/settings")
def get_settings():
    first, second, autolinks, previews = read_settings(WIKI_DIR)
    return {
        "revision": revision(first + b"\x00" + second),
        "autolinks": autolinks,
        "previews": previews,
    }


@router.post("/api/settings")
def save_settings(document: DictionaryUpdate):
    try:
        first, second = serialize_settings(document)

    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    with _SAVE_LOCK:
        previous_first, previous_second, previous_links, previous_previews = (
            read_settings(WIKI_DIR)
        )

        if document.revision != revision(previous_first + b"\x00" + previous_second):
            raise HTTPException(409, "Настройки изменились на диске. Открой их заново")

        if (
            document.autolinks == previous_links
            and document.previews == previous_previews
        ):
            return {"revision": document.revision}

        for target, previous, data in zip(
            settings_paths(WIKI_DIR), (previous_first, previous_second), (first, second)
        ):
            if previous != data:
                write_file(target, data)

    return {"revision": revision(first + b"\x00" + second)}


@router.post("/api/settings/preview")
def preview_settings(request: Request, document: DictionaryUpdate):
    try:
        first, second = serialize_settings(document)

    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    from .extensions.link_dictionary import _AUTOLINK_CACHE, _PREVIEW_CACHE

    with tempfile.TemporaryDirectory(prefix="wiki-link-preview-") as directory:
        auto = Path(directory) / "autolinks.md"
        previews = Path(directory) / "previews.md"
        auto.write_bytes(first)
        previews.write_bytes(second)
        try:
            content, *_ = render_document(
                WIKI_DIR / "index.md", document.example, (auto, previews)
            )

        finally:
            _AUTOLINK_CACHE.pop(auto.resolve(), None)
            _PREVIEW_CACHE.pop(previews.resolve(), None)

    return {"html": content}


@router.get("/api/block-samples", response_class=HTMLResponse)
def block_samples(request: Request):
    import html

    from .utils.editor_samples import block_samples

    cards = []
    for kind, title, node in block_samples():
        source = serialize_blocks([node])
        content, *_ = render_document(WIKI_DIR / "index.md", source)
        cards.append(
            '<button type="button" class="editor-sample-tile" data-sample-kind="'
            + kind
            + '">'
            '<div class="editor-sample-canvas"><div class="content">'
            + content
            + "</div></div>"
            "<strong>" + html.escape(title) + "</strong></button>"
        )

    return templates.TemplateResponse(
        request,
        "editor_preview.html",
        {
            "title": "Блоки",
            "content": '<div class="editor-sample-grid">' + "".join(cards) + "</div>",
            "preview_base": "/wiki/",
            "component_preview": True,
            "block_palette": True,
        },
    )
