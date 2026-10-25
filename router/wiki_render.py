from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from template_env import templates

from .utils.markdown_engine import WIKI_DIR, get_wiki_page

router = APIRouter()


@router.get("/wiki/{page:path}", response_class=HTMLResponse)
def wiki_page(request: Request, page: Path):
    md_path = WIKI_DIR / page
    if md_path.is_dir() or str(page).endswith("/"):
        md_path = md_path / "index.md"

    else:
        md_path = md_path.with_suffix(".md")

    status_code = 200

    if not md_path.resolve().is_relative_to(WIKI_DIR.resolve()):
        md_path = WIKI_DIR / "_tech/error/403.md"
        status_code = 403

    try:
        content = md_path.read_text(encoding="utf-8")

    except FileNotFoundError:
        content = (WIKI_DIR / "_tech/error/404.md").read_text(encoding="utf-8")
        status_code = 404

    rendered_html, title, date, author, background_url, ai_text, ai_art = get_wiki_page(
        md_path, content
    )

    return templates.TemplateResponse(
        request,
        "wiki_template.html",
        {
            "content": rendered_html,
            "title": title,
            "date": date,
            "author": author,
            "background_url": background_url,
            "ai_text": ai_text,
            "ai_art": ai_art,
        },
        status_code,
    )
