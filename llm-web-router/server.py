"""
server.py – LLM Web Router (v2.0)
==================================
- Manages persistent browser sessions for DeepSeek / Claude
- Implements a reliable completion endpoint with stability detection
- Supports image uploads and session state persistence
- Robust error handling and graceful shutdown
"""

import os
import asyncio
import base64
import json
import mimetypes
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import httpx

from config import MODEL_CONFIG, DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Lifespan manager
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager

logger = logging.getLogger("llm-web-router")

browser_instance: Optional[Browser] = None
contexts: Dict[str, BrowserContext] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_instance, contexts
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=False,
        channel="msedge",
        args=["--start-maximized"]
    )
    browser_instance = browser

    for key, cfg in MODEL_CONFIG.items():
        state_path = f"{cfg['profile_dir']}/state.json"
        if not os.path.exists(state_path):
            logger.warning("No session for %s; skipping", key)
            continue
        logger.info("Loading browser context for %s", key)
        context = await browser.new_context(storage_state=state_path, no_viewport=True, color_scheme="dark")
        page = await context.new_page()
        await page.goto(cfg["url"], timeout=0)
        
        # Check if we landed on a login page
        current_url = page.url
        if "sign_in" in current_url or "login" in current_url:
            logger.warning("⚠️ %s session expired or login required! Navigate to: %s", key, current_url)
            logger.warning("Please run: python manual_login.py %s", key)
        else:
            logger.info("✅ %s ready and logged in.", key)
            
        contexts[key] = context

    yield
    # Shutdown
    for ctx in contexts.values():
        await ctx.close()
    if browser:
        await browser.close()
    await playwright.stop()

app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def find_and_act(page: Page, selectors: List[str], action: str, data: Optional[str] = None) -> bool:
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=3000)
            loc = page.locator(sel)
            if action == "fill":
                await loc.fill("")
                await loc.fill(data)
            elif action == "click":
                await loc.click()
            return True
        except Exception:
            continue
    return False


def extract_content_parts(content) -> Tuple[str, List[str]]:
    if isinstance(content, list):
        text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        images = [p["image_url"]["url"] for p in content if p.get("type") == "image_url" and "image_url" in p]
        return text, images
    return str(content) if content else "", []


def data_url_to_file_payload(data_url: str, idx: int) -> Dict:
    match = re.match(r"data:([^;]+);base64,(.+)", data_url)
    if not match:
        raise ValueError("Only base64 data URLs supported")
    mime, data = match.groups()
    ext = mimetypes.guess_extension(mime) or ".bin"
    return {"name": f"upload_{idx}{ext}", "mimeType": mime, "buffer": base64.b64decode(data)}


async def upload_images(page: Page, cfg: Dict, urls: List[str]):
    payloads = [data_url_to_file_payload(u, i+1) for i, u in enumerate(urls)]
    for sel in cfg.get("upload_selectors", ["input[type='file']"]):
        try:
            count = await page.locator(sel).count()
            for idx in range(count):
                target = page.locator(sel).nth(idx)
                multi = await target.evaluate("el => !!el.multiple")
                if len(payloads) > 1 and not multi:
                    continue
                await target.set_input_files(payloads if multi else payloads[0])
                await page.wait_for_timeout(cfg.get("upload_wait_ms", 2000))
                return
        except Exception:
            continue
    raise RuntimeError("Could not upload images")


async def get_response(page: Page, cfg: Dict, prompt: str, image_urls: Optional[List[str]] = None) -> str:
    """Send prompt and wait for complete response with stability detection."""
    msg_before = await page.locator(cfg["response_container"]).count()

    if image_urls:
        await upload_images(page, cfg, image_urls)

    if not await find_and_act(page, cfg["input_selectors"], "fill", prompt):
        raise RuntimeError("Could not find input box")
    if not await find_and_act(page, cfg["send_selectors"], "click"):
        await page.keyboard.press("Enter")

    # Wait for new response bubble
    for _ in range(30):
        if await page.locator(cfg["response_container"]).count() > msg_before:
            break
        await asyncio.sleep(1)

    # ── Phase 1: wait for generation to start ──
    started = False
    for _ in range(40):
        if await page.locator(cfg["stop_selector"]).count() > 0:
            started = True
            break
        await asyncio.sleep(0.5)

    # ── Phase 2: poll until stable ──
    stable_since = 0.0
    last_text = ""
    MAX_WAIT = 600
    STABILITY = 3.5
    start = asyncio.get_event_loop().time()

    while True:
        await asyncio.sleep(0.5)
        elapsed = asyncio.get_event_loop().time() - start
        try:
            current = await page.locator(cfg["response_container"]).last.inner_text()
        except Exception:
            continue

        generating = await page.locator(cfg["stop_selector"]).count() > 0

        # If we see </REPORT>, we are almost certainly done.
        # But we still check 'generating' to avoid cutting off trailing code block markers.
        if "</REPORT>" in current and not generating:
            last_text = current
            break

        if generating:
            stable_since = 0.0
        elif current == last_text and current:
            stable_since += 0.5
        else:
            stable_since = 0.0
        last_text = current

        # Standard stability exit (for ACTION-only responses or if tags are missing)
        if stable_since >= STABILITY:
            break
        if elapsed > MAX_WAIT:
            break

    # Final clean extraction (strip UI noise)
    try:
        await page.locator(cfg["response_container"]).last.scroll_into_view_if_needed()
        await page.wait_for_timeout(500)

        final = await page.locator(cfg["response_container"]).last.evaluate("""
            (container) => {
                return container.innerText
                    .replace(/text\\nCopy\\nDownload/g, '')
                    .replace(/Copy|Download/g, '')
                    .trim();
            }
        """)
    except Exception:
        final = last_text
    return final or last_text


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    data = await request.json()
    model_name = data.get("model", DEFAULT_MODEL)

    backend = None
    for key, cfg in MODEL_CONFIG.items():
        if model_name in cfg.get("model_name", []) or model_name == key:
            backend = key
            break
    if not backend:
        backend = DEFAULT_MODEL

    cfg = MODEL_CONFIG[backend]
    page = contexts[backend].pages[0]

    messages = data["messages"]
    # Build the prompt exactly as before (system injection on first turn)
    system_msg = next((m for m in messages if m["role"] == "system"), None)
    non_system = [m for m in messages if m["role"] != "system"]
    turns = sum(1 for m in non_system if m["role"] == "assistant")

    pending_images: List[str] = []
    if turns == 0:
        user_content = next((m for m in reversed(non_system) if m["role"] == "user"), {})
        user_text, pending_images = extract_content_parts(user_content.get("content", ""))
        sys_text = system_msg["content"] if system_msg else ""
        prompt = f"[SYSTEM CONFIGURATION]\n{sys_text}\n[END]\n\nNow respond to this first request:\n{user_text}"
    else:
        # Find last assistant index, send everything after
        last_ai = -1
        for i, m in enumerate(messages):
            if m["role"] == "assistant":
                last_ai = i
        new_msgs = messages[last_ai+1:] if last_ai >= 0 else messages
        parts = []
        for m in new_msgs:
            role = m["role"].upper()
            txt, imgs = extract_content_parts(m.get("content", ""))
            if imgs:
                pending_images.extend(imgs)
            if m.get("role") == "function":
                parts.append(f"[TOOL RESULT for {m.get('name', 'unknown')}]:\n{txt}\nWhat next?")
            else:
                parts.append(f"[{role}]:\n{txt}")
        prompt = "\n".join(parts)

    response_text = await get_response(page, cfg, prompt, image_urls=pending_images if pending_images else None)

    return {
        "id": f"chatcmpl-{abs(hash(response_text))}",
        "object": "chat.completion",
        "created": int(asyncio.get_event_loop().time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": "stop"
        }]
    }


@app.get("/v1/current_url")
async def current_url():
    try:
        page = contexts[DEFAULT_MODEL].pages[0]
        return {"url": page.url}
    except Exception:
        return {"url": None}


@app.post("/v1/navigate")
async def navigate_to(request: Request):
    data = await request.json()
    url = data.get("url")
    if not url:
        return {"status": "error", "message": "URL required"}
    try:
        page = contexts[DEFAULT_MODEL].pages[0]
        await page.goto(url, timeout=30000)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)