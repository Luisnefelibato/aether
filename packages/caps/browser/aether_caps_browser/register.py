from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec

_browser = None


async def _get_browser(settings: Settings):
    global _browser
    if _browser is not None:
        return _browser
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("playwright no instalado") from exc

    pw = await async_playwright().start()
    profile = settings.resolve_path(settings.browser.user_data_dir)
    profile.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "user_data_dir": str(profile),
        "headless": settings.browser.headless,
        "accept_downloads": True,
        "args": [f"--profile-directory={settings.browser.profile_directory}"],
    }
    if settings.browser.channel:
        options["channel"] = settings.browser.channel
    try:
        context = await pw.chromium.launch_persistent_context(**options)
    except Exception:
        # Keep ADAM functional if the configured Chrome channel is unavailable.
        options.pop("channel", None)
        context = await pw.chromium.launch_persistent_context(**options)
    page = context.pages[0] if context.pages else await context.new_page()
    _browser = {"pw": pw, "context": context, "page": page}
    return _browser


async def _page(settings: Settings):
    b = await _get_browser(settings)
    page = b["page"]
    if page.is_closed():
        page = await b["context"].new_page()
        b["page"] = page
    return page


def _result(ok: bool, **payload: Any) -> str:
    return json.dumps(
        {"ok": ok, "verified": ok, **payload},
        ensure_ascii=False,
        default=str,
    )


async def _verify_browser_expectation(
    page: Any,
    args: dict[str, Any],
    before_url: str,
    before_title: str,
    source_selector: str | None,
) -> bool:
    expected_url = str(args.get("expect_url_contains") or "")
    if expected_url:
        return expected_url.lower() in page.url.lower()
    expected_text = str(args.get("expect_text") or "")
    if expected_text:
        return await page.get_by_text(expected_text, exact=False).count() > 0
    expected_selector = str(args.get("expect_selector") or "")
    if expected_selector:
        return await page.locator(expected_selector).first.is_visible()
    hidden_selector = str(args.get("expect_hidden") or "")
    if hidden_selector:
        return not await page.locator(hidden_selector).first.is_visible()
    if page.url != before_url or await page.title() != before_title:
        return True
    if source_selector:
        try:
            return not await page.locator(source_selector).first.is_visible()
        except Exception:  # noqa: BLE001
            return True
    return False


def register_browser(tools: ToolRegistry, settings: Settings) -> None:
    async def goto(args: dict[str, Any]) -> str:
        url = str(args.get("url") or "")
        try:
            page = await _page(settings)
            response = await page.goto(url, timeout=settings.browser.timeout_ms)
            title = await page.title()
            reached = page.url
            ok = response is None or response.ok
            return _result(
                ok,
                title=title,
                url=reached,
                message=f"Página abierta: {title}" if ok else "",
                error=None if ok else f"HTTP {response.status}",
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    async def click(args: dict[str, Any]) -> str:
        selector = str(args.get("selector") or "")
        try:
            page = await _page(settings)
            before_url = page.url
            before_title = await page.title()
            await page.click(selector, timeout=settings.browser.timeout_ms)
            await page.wait_for_timeout(500)
            verified = await _verify_browser_expectation(
                page, args, before_url, before_title, selector
            )
            return _result(
                verified,
                message=f"Control activado y verificado: {selector}"
                if verified
                else "",
                error=None
                if verified
                else "El click fue enviado, pero no hubo un efecto verificable",
                url=page.url,
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    async def type_text(args: dict[str, Any]) -> str:
        selector = str(args.get("selector") or "")
        text = str(args.get("text") or "")
        try:
            page = await _page(settings)
            await page.fill(selector, text)
            value = await page.locator(selector).input_value()
            return _result(
                value == text,
                message="Texto escrito y verificado" if value == text else "",
                error=None if value == text else "El campo no conservó el texto",
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    async def extract(args: dict[str, Any]) -> str:
        selector = str(args.get("selector") or "body")
        try:
            page = await _page(settings)
            text = await page.inner_text(selector)
            return _result(True, text=text[:6000], url=page.url)
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    async def click_text(args: dict[str, Any]) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return _result(False, error="Falta el texto visible del control")
        try:
            page = await _page(settings)
            before_url = page.url
            before_title = await page.title()
            locator = page.get_by_text(text, exact=False).first
            await locator.wait_for(state="visible", timeout=settings.browser.timeout_ms)
            await locator.click()
            await page.wait_for_timeout(500)
            verified = await _verify_browser_expectation(
                page, args, before_url, before_title, None
            )
            return _result(
                verified,
                message=f"Abrí y verifiqué el elemento {text}" if verified else "",
                error=None
                if verified
                else "El elemento recibió el click, pero el efecto no se verificó",
                title=await page.title(),
                url=page.url,
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    async def youtube_play(args: dict[str, Any]) -> str:
        query = str(args.get("query") or args.get("song") or "").strip()
        if not query:
            return _result(False, error="Falta el nombre de la canción o video")
        try:
            page = await _page(settings)
            await page.goto(
                f"https://www.youtube.com/results?search_query={quote_plus(query)}",
                wait_until="domcontentloaded",
                timeout=settings.browser.timeout_ms,
            )
            if "accounts.google.com" in page.url:
                return _result(False, error="El perfil de ADAM necesita iniciar sesión")
            first = page.locator("ytd-video-renderer a#video-title").first
            await first.wait_for(state="visible", timeout=settings.browser.timeout_ms)
            requested_title = (await first.get_attribute("title")) or (
                await first.inner_text()
            )
            await first.click()
            await page.wait_for_url("**/watch?**", timeout=settings.browser.timeout_ms)
            video = page.locator("video").first
            await video.wait_for(state="attached", timeout=settings.browser.timeout_ms)
            paused = await video.evaluate("(element) => element.paused")
            if paused:
                await video.click()
            await page.wait_for_timeout(700)
            playing = not await video.evaluate("(element) => element.paused")
            title = await page.title()
            return _result(
                playing,
                title=title,
                url=page.url,
                message=f"Reproduciendo en YouTube: {requested_title.strip()}"
                if playing
                else "",
                error=None if playing else "YouTube abrió el video pero no inició reproducción",
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    async def drive_upload(args: dict[str, Any]) -> str:
        raw_path = str(args.get("folder_path") or args.get("path") or "")
        folder = Path(raw_path).expanduser().resolve()
        if not folder.is_dir():
            return _result(False, error=f"No existe la carpeta: {folder}")
        try:
            page = await _page(settings)
            await page.goto(
                "https://drive.google.com/drive/my-drive",
                wait_until="domcontentloaded",
                timeout=settings.browser.timeout_ms,
            )
            if "accounts.google.com" in page.url:
                return _result(False, error="El perfil de ADAM necesita iniciar sesión en Google")

            new_button = page.get_by_role(
                "button", name=re.compile(r"^(nuevo|new)$", re.IGNORECASE)
            ).first
            await new_button.wait_for(state="visible", timeout=settings.browser.timeout_ms)
            await new_button.click()
            folder_upload = page.get_by_text(
                re.compile(r"(subir carpeta|folder upload)", re.IGNORECASE)
            ).first
            await folder_upload.wait_for(
                state="visible", timeout=settings.browser.timeout_ms
            )
            async with page.expect_file_chooser(
                timeout=settings.browser.timeout_ms
            ) as chooser_info:
                await folder_upload.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(folder))

            # Google Drive exposes the folder name in upload progress/completion UI.
            marker = page.get_by_text(folder.name, exact=False).last
            await marker.wait_for(state="visible", timeout=120000)
            return _result(
                True,
                folder=str(folder),
                url=page.url,
                message=f"Google Drive recibió la carpeta {folder.name}",
            )
        except Exception as exc:  # noqa: BLE001
            return _result(False, error=str(exc))

    tools.register(
        ToolSpec(
            "browser.goto",
            "Navega a una URL con Playwright.",
            {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            goto,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "browser.click",
            "Click en un selector CSS.",
            {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "expect_url_contains": {"type": "string"},
                    "expect_text": {"type": "string"},
                    "expect_selector": {"type": "string"},
                    "expect_hidden": {"type": "string"},
                },
                "required": ["selector"],
            },
            click,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "browser.type",
            "Escribe en un input.",
            {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["selector", "text"],
            },
            type_text,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "browser.extract",
            "Extrae texto de la página.",
            {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
            },
            extract,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "browser.click_text",
            "Abre un perfil, enlace, botón o elemento por su texto visible en la página actual.",
            {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Texto visible del perfil, enlace o botón.",
                    },
                    "expect_url_contains": {"type": "string"},
                    "expect_text": {"type": "string"},
                    "expect_selector": {"type": "string"},
                    "expect_hidden": {"type": "string"},
                },
                "required": ["text"],
            },
            click_text,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "browser.youtube_play",
            "Busca y reproduce una canción o video en YouTube usando el perfil persistente de ADAM.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Canción, artista o video solicitado.",
                    }
                },
                "required": ["query"],
            },
            youtube_play,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "browser.drive_upload",
            "Sube una carpeta local completa a Mi unidad de Google Drive usando la sesión persistente de ADAM.",
            {
                "type": "object",
                "properties": {
                    "folder_path": {
                        "type": "string",
                        "description": "Ruta absoluta de la carpeta local.",
                    }
                },
                "required": ["folder_path"],
            },
            drive_upload,
            "L2",
        )
    )
