"""
server.py – LLM Web Router (v2.2.0)
==================================
- Manages persistent browser sessions for DeepSeek / Claude
- Implements a reliable completion endpoint with stability detection
- Supports image uploads and session state persistence
- Robust error handling, session recovery, and fallback browser support
- OpenAI-compatible streaming (SSE) support for Marimo and other clients
- Passthrough auth: accepts any API key (browser sessions handle auth)
- Deduplicates browser windows for -api variants sharing a profile_dir
"""

import os
import asyncio
import base64
import json
import mimetypes
import re
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import httpx

from config import MODEL_CONFIG, DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Configuration & Logging
# ---------------------------------------------------------------------------
PORT = int(os.getenv("ROUTER_PORT", 8000))
HOST = os.getenv("ROUTER_HOST", "0.0.0.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=getattr(logging, LOG_LEVEL), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("llm-web-router")

browser_instance: Optional[Browser] = None
contexts: Dict[str, BrowserContext] = {}
# Per-backend locks to prevent concurrent typing into the same page
backend_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# ---------------------------------------------------------------------------
# Lifespan manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_instance, contexts
    playwright = await async_playwright().start()

    # Browser fallback logic
    browser = None
    for channel in ["msedge", "chrome", "chromium"]:
        try:
            logger.info("Attempting to launch browser: %s", channel)
            browser = await playwright.chromium.launch(
                headless=False,
                channel=channel,
                args=["--start-maximized", "--no-sandbox"]
            )
            logger.info("Successfully launched %s", channel)
            break
        except Exception as e:
            logger.warning("Failed to launch %s: %s", channel, e)

    if not browser:
        raise RuntimeError("No compatible browser found (Edge/Chrome/Chromium)")

    browser_instance = browser

    # Track which profile_dirs we've already opened a context for (avoid duplicate windows)
    loaded_profile_dirs: Dict[str, str] = {}  # profile_dir -> context_key

    for key, cfg in MODEL_CONFIG.items():
        state_path = f"{cfg['profile_dir']}/state.json"
        if not os.path.exists(state_path):
            logger.warning("No session for %s; skipping. Run manual_login.py first.", key)
            continue

        profile_dir = cfg["profile_dir"]
        if profile_dir in loaded_profile_dirs:
            # Reuse the already-opened context — no new browser window
            existing_key = loaded_profile_dirs[profile_dir]
            contexts[key] = contexts[existing_key]
            logger.info("♻️  %s shares profile with %s — reusing context (no new window)", key, existing_key)
            continue

        logger.info("Loading browser context for %s", key)
        context = await browser.new_context(
            storage_state=state_path,
            no_viewport=True,
            color_scheme="dark",
            permissions=["clipboard-read", "clipboard-write"]
        )
        page = await context.new_page()
        await page.goto(cfg["url"], timeout=0)

        # Check if we landed on a login page
        if "sign_in" in page.url or "login" in page.url:
            logger.warning("⚠️ %s session expired or login required!", key)
        else:
            logger.info("✅ %s ready and logged in.", key)

        contexts[key] = context
        loaded_profile_dirs[profile_dir] = key

    yield

    # Shutdown — avoid closing the same context twice (shared contexts)
    closed = set()
    for ctx in contexts.values():
        if id(ctx) not in closed:
            await ctx.close()
            closed.add(id(ctx))
    if browser_instance:
        await browser_instance.close()
    await playwright.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def passthrough_auth(request: Request, call_next):
    """Accept any Authorization header (or none at all).

    External OpenAI-compatible clients (Marimo, Continue, etc.) require an
    api_key to be set on their end, but ZeroBound uses browser sessions — so
    we simply ignore whatever token they send and allow all requests through.
    """
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("%s %s", request.method, request.url.path)
    start = asyncio.get_event_loop().time()
    response = await call_next(request)
    elapsed = asyncio.get_event_loop().time() - start
    logger.info("Completed %s in %.2fs", request.url.path, elapsed)
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def find_and_act(page: Page, selectors: List[str], action: str, data: Optional[str] = None) -> bool:
    # Wait ONCE for any of the selectors (max 5 seconds)
    combined = ", ".join(selectors)
    try:
        await page.wait_for_selector(combined, timeout=5000, state="visible")
    except Exception:
        pass  # Fall through to JS heuristics

    # Strategy 1: Try CSS selectors instantly
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                if action == "fill":
                    await loc.click(timeout=1000)
                    await loc.fill("", timeout=1000)
                    await loc.fill(data, timeout=1000)
                elif action == "click":
                    await loc.click(timeout=1000)
                return True
        except Exception:
            continue

    # Strategy 2: JavaScript value injection
    if action == "fill" and data is not None:
        try:
            safe_data = data.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            result = await page.evaluate(f"""
                () => {{
                    const candidates = [
                        ...document.querySelectorAll('textarea'),
                        ...document.querySelectorAll('[contenteditable="true"]'),
                        ...document.querySelectorAll('input[type="text"]')
                    ];
                    const el = candidates.find(e => {{
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && window.getComputedStyle(e).display !== 'none';
                    }});
                    if (!el) return false;
                    el.focus();
                    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value') ||
                                                        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                        nativeInputValueSetter.set.call(el, `{safe_data}`);
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }} else {{
                        el.textContent = `{safe_data}`;
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                    return true;
                }}
            """)
            if result:
                logger.info("JS value injection succeeded")
                return True
        except Exception as e:
            logger.warning("JS fallback failed: %s", e)

    # Strategy 3: JS fallback for click
    if action == "click":
        try:
            target_rect = await page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button, div[role="button"], .ds-icon-button, [aria-label]'));
                    const visibleBtns = btns.filter(b => b.offsetWidth > 0 && b.offsetHeight > 0 && window.getComputedStyle(b).display !== 'none' && !b.disabled);

                    let target = visibleBtns.find(b => {
                        const text = (b.innerText || '').toLowerCase();
                        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        return text.includes('send') || aria.includes('send');
                    });

                    if (!target) {
                        const iconBtns = visibleBtns.filter(b => b.matches('.ds-icon-button'));
                        target = iconBtns.find(b => {
                            const bg = window.getComputedStyle(b).backgroundColor;
                            return bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent' && bg !== 'rgb(255, 255, 255)' && !bg.includes('var(');
                        });
                        if (!target && iconBtns.length > 0) {
                            target = iconBtns[iconBtns.length - 1];
                        }
                    }

                    if (target) {
                        const r = target.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2};
                    }
                    return null;
                }
            """)
            if target_rect and isinstance(target_rect, dict) and 'x' in target_rect:
                await page.mouse.click(target_rect['x'], target_rect['y'])
                logger.info("JS click fallback succeeded (via mouse.click)")
                return True
        except Exception as e:
            logger.warning("JS click fallback failed: %s", e)

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


async def is_generating(page: Page, cfg: Dict) -> bool:
    try:
        if await page.locator(cfg["stop_selector"]).count() > 0:
            return True
        return await page.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('button, div[role="button"], .ds-icon-button, [aria-label]'));
                return btns.some(b => {
                    if (b.offsetWidth === 0 || b.offsetHeight === 0 || window.getComputedStyle(b).display === 'none') return false;
                    const text = b.innerText.toLowerCase();
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    const hasStopSquare = !!b.querySelector('svg rect:not([fill="none"])');
                    return text.includes('stop') || aria.includes('stop') || hasStopSquare;
                });
            }
        """)
    except Exception:
        return False


async def get_markdown_via_copy_button(page: Page) -> Optional[str]:
    """Click the copy button on the last AI response and read the raw markdown
    directly from the system clipboard. Falls back gracefully on failure."""
    try:
        # Step 1 - Inject interceptors for BOTH modern and legacy clipboard APIs
        await page.evaluate("""
            () => {
                window.__zb_md = null;
                
                // Intercept modern clipboard API
                if (navigator.clipboard) {
                    window.__zb_orig_write = navigator.clipboard.writeText;
                    navigator.clipboard.writeText = async function(text) {
                        window.__zb_md = text;
                        return Promise.resolve();
                    };
                }

                // Intercept legacy execCommand / copy events
                window.__zb_copy_listener = function(e) {
                    if (e.clipboardData) {
                        const text = e.clipboardData.getData('text/plain');
                        if (text) window.__zb_md = text;
                    }
                };
                document.addEventListener('copy', window.__zb_copy_listener, true);
            }
        """)

        # Step 2 – Find the copy button element on the last response
        btn_handle = await page.evaluate_handle("""
            () => {
                const visible = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 &&
                           window.getComputedStyle(el).display !== 'none' &&
                           window.getComputedStyle(el).visibility !== 'hidden';
                };

                // Accurately find the chat input box to exclude its buttons (Add File, Submit, etc)
                const editor = document.querySelector('#chat-input, [contenteditable=\"true\"], textarea');
                const maxY = editor ? editor.getBoundingClientRect().top : window.innerHeight - 150;

                const svgs = Array.from(document.querySelectorAll('svg'))
                    .filter(visible)
                    .filter(svg => svg.getBoundingClientRect().top < maxY - 10); // Buffer above input
                
                // Group SVGs into horizontal rows by Y coordinate
                const rows = [];
                for (const svg of svgs) {
                    const r = svg.getBoundingClientRect();
                    let added = false;
                    for (const row of rows) {
                        if (Math.abs(row.y - r.top) < 20) {
                            row.items.push({svg, r});
                            added = true;
                            break;
                        }
                    }
                    if (!added) {
                        rows.push({y: r.top, items: [{svg, r}]});
                    }
                }
                
                // Sort rows by Y descending to find the bottom-most row (last message)
                rows.sort((a, b) => b.y - a.y);
                
                // The last message's action row will have multiple SVGs (Copy, Regenerate, Good, Bad)
                for (const row of rows) {
                    if (row.items.length >= 2) {
                        // Sort left-to-right
                        row.items.sort((a, b) => a.r.left - b.r.left);
                        
                        // The copy button is almost always the leftmost one in the row
                        // But as an extra safety check, verify if it's the copy icon SVG
                        let targetSvg = row.items[0].svg;
                        
                        // DeepSeek's copy button SVG path starts with M9.67272...
                        // If the first button is Regenerate, we might need to adjust, but typically Copy is first.
                        if (!targetSvg.innerHTML.includes('M9.672') && row.items.length > 1 && row.items[1].svg.innerHTML.includes('M9.672')) {
                            targetSvg = row.items[1].svg;
                        }

                        // Return its clickable container
                        return targetSvg.closest('button, div[role=\"button\"], .ds-icon-button') || targetSvg;
                    }
                }

                return null;
            }
        """)

        btn_el = btn_handle.as_element() if btn_handle else None
        if not btn_el:
            logger.debug("get_markdown_via_copy_button: no copy button found")
            return None

        logger.info("Copy button located natively, performing click...")

        # Step 3 - Perform a trusted Playwright click directly on the element
        await btn_el.scroll_into_view_if_needed()
        await btn_el.hover()
        await btn_el.click(delay=50)
        
        # Wait for the async click handlers to fire and populate our variable
        await asyncio.sleep(1.0)

        # Step 4 – Read the intercepted markdown
        markdown = await page.evaluate("window.__zb_md")

        # Step 5 – Cleanup hooks
        await page.evaluate("""
            () => {
                if (navigator.clipboard && window.__zb_orig_write) {
                    navigator.clipboard.writeText = window.__zb_orig_write;
                }
                if (window.__zb_copy_listener) {
                    document.removeEventListener('copy', window.__zb_copy_listener, true);
                }
                delete window.__zb_md;
                delete window.__zb_orig_write;
                delete window.__zb_copy_listener;
            }
        """)

        if markdown:
            logger.info("Raw markdown captured via clipboard hook (%d chars)", len(markdown))
        return markdown or None

    except Exception as e:
        logger.warning("get_markdown_via_copy_button failed: %s", e)
        return None


async def get_response(page: Page, cfg: Dict, prompt: str, request: Request, image_urls: Optional[List[str]] = None) -> str:
    """Send prompt and wait for complete response with stability detection."""

    # Session recovery check
    if "sign_in" in page.url or "login" in page.url:
        logger.warning("Session expired mid-run, attempting reload...")
        state_path = f"{cfg['profile_dir']}/state.json"
        if os.path.exists(state_path):
            await page.context.storage_state(path=state_path)
            await page.goto(cfg["url"])
            await page.wait_for_load_state("networkidle")

    msg_before = await page.locator(cfg["response_container"]).count()

    if image_urls:
        await upload_images(page, cfg, image_urls)

    if not await find_and_act(page, cfg["input_selectors"], "fill", prompt):
        logger.warning("Input box not found, attempting page reload recovery...")
        try:
            await page.reload(timeout=30000, wait_until="networkidle")
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("Reload failed: %s", e)
        if not await find_and_act(page, cfg["input_selectors"], "fill", prompt):
            raise RuntimeError("Could not find input box (even after reload)")
    if not await find_and_act(page, cfg["send_selectors"], "click"):
        await page.keyboard.press("Enter")

    # Wait for new response bubble or generation to start
    for _ in range(30):
        if await page.locator(cfg["response_container"]).count() > msg_before:
            break
        if await is_generating(page, cfg):
            break
        await asyncio.sleep(1)

    # Phase 1: wait for generation to start
    started = False
    for _ in range(40):
        if await is_generating(page, cfg):
            started = True
            break
        await asyncio.sleep(0.5)

    # Phase 2: poll until stable
    stable_since = 0.0
    last_text = ""
    MAX_WAIT = 600
    STABILITY = 3.5
    start = asyncio.get_event_loop().time()

    while True:
        await asyncio.sleep(0.5)
        if await request.is_disconnected():
            logger.warning("Client disconnected; aborting wait.")
            return "Disconnected"

        elapsed = asyncio.get_event_loop().time() - start
        try:
            current = await page.locator(cfg["response_container"]).last.inner_text(timeout=100)
        except Exception:
            current = await page.evaluate("""
                () => {
                    const containers = Array.from(document.querySelectorAll('div.prose, div[class*="message"], div[class*="markdown"], div[class*="content"]'));
                    return containers.length > 0 ? containers[containers.length - 1].innerText : "";
                }
            """)

        generating = await is_generating(page, cfg)

        if "</REPORT>" in current and not generating:
            last_text = current
            break

        if started and not generating:
            target_stability = 1.0
        else:
            target_stability = STABILITY

        if generating:
            stable_since = 0.0
        elif current == last_text and current:
            stable_since += 0.5
        else:
            stable_since = 0.0
        last_text = current

        if stable_since >= target_stability:
            break
        if elapsed > MAX_WAIT:
            break

    # Final clean extraction
    container_locator = page.locator(cfg["response_container"]).last
    container_exists = False
    try:
        if await container_locator.count() > 0:
            container_exists = True
    except Exception:
        pass

    try:
        if container_exists:
            await container_locator.scroll_into_view_if_needed(timeout=100)
            await page.wait_for_timeout(500)
            final = await container_locator.evaluate("""
                (container) => {
                    const clone = container.cloneNode(true);
                    clone.style.display = 'none';
                    document.body.appendChild(clone);

                    try {
                        const mathElems = clone.querySelectorAll('.katex, .math, mjx-container, .math-inline, .math-block');
                        mathElems.forEach(el => {
                            let tex = null;
                            let isBlock = el.classList.contains('katex-display') || el.tagName.toLowerCase() === 'mjx-container' && el.hasAttribute('display');

                            const mathTag = el.querySelector('math[alttext]');
                            if (mathTag) {
                                tex = mathTag.getAttribute('alttext');
                                if (mathTag.getAttribute('display') === 'block') isBlock = true;
                            }
                            if (!tex) {
                                const annotation = el.querySelector('annotation[encoding="application/x-tex"]');
                                if (annotation) tex = annotation.textContent;
                            }
                            if (!tex) {
                                const script = el.querySelector('script[type^="math/tex"]');
                                if (script) {
                                    tex = script.textContent;
                                    if (script.type.includes('mode=display')) isBlock = true;
                                }
                            }

                            if (tex) {
                                const delim = isBlock ? '$$\\n' : '$';
                                const replacement = document.createTextNode(delim + tex + (isBlock ? '\\n$$' : '$'));
                                el.replaceWith(replacement);
                            }
                        });

                        clone.style.display = 'block';
                        clone.style.position = 'absolute';
                        clone.style.opacity = '0';
                        clone.style.whiteSpace = 'pre-wrap';

                        return clone.innerText
                            .replace(/text\\nCopy\\nDownload/g, '')
                            .replace(/Copy|Download/g, '')
                            .trim();
                    } finally {
                        document.body.removeChild(clone);
                    }
                }
            """, timeout=1000)
        else:
            logger.warning("Main response container selector failed; using visual DOM auto-healing fallback")
            final = await page.evaluate("""
                () => {
                    const containers = Array.from(document.querySelectorAll('div.prose, div[class*="message"], div[class*="markdown"], div[class*="content"], article'));
                    const visible = containers.filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && window.getComputedStyle(el).display !== 'none';
                    });
                    if (visible.length === 0) return "";
                    const container = visible[visible.length - 1];

                    const clone = container.cloneNode(true);
                    clone.style.display = 'none';
                    document.body.appendChild(clone);

                    try {
                        const mathElems = clone.querySelectorAll('.katex, .math, mjx-container, .math-inline, .math-block');
                        mathElems.forEach(el => {
                            let tex = null;
                            let isBlock = el.classList.contains('katex-display') || el.tagName.toLowerCase() === 'mjx-container' && el.hasAttribute('display');
                            const mathTag = el.querySelector('math[alttext]');
                            if (mathTag) {
                                tex = mathTag.getAttribute('alttext');
                                if (mathTag.getAttribute('display') === 'block') isBlock = true;
                            }
                            if (!tex) {
                                const annotation = el.querySelector('annotation[encoding="application/x-tex"]');
                                if (annotation) tex = annotation.textContent;
                            }
                            if (tex) {
                                const delim = isBlock ? '$$\\n' : '$';
                                const replacement = document.createTextNode(delim + tex + (isBlock ? '\\n$$' : '$'));
                                el.replaceWith(replacement);
                            }
                        });

                        clone.style.display = 'block';
                        clone.style.position = 'absolute';
                        clone.style.opacity = '0';
                        clone.style.whiteSpace = 'pre-wrap';

                        return clone.innerText
                            .replace(/text\\nCopy\\nDownload/g, '')
                            .replace(/Copy|Download/g, '')
                            .trim();
                    } finally {
                        document.body.removeChild(clone);
                    }
                }
            """)
    except Exception as e:
        logger.warning(f"Clean extraction failed: {e}")
        final = last_text

    # ── Primary extraction: raw markdown via copy-button clipboard interception ──
    # This gives us the exact markdown DeepSeek/Claude would put on the clipboard
    # (bold, numbered lists, bullet lists, code blocks — all intact).
    try:
        with open('page_dump.html', 'w', encoding='utf-8') as f:
            f.write(await page.content())
    except Exception as e:
        pass
    markdown = await get_markdown_via_copy_button(page)
    if markdown:
        return markdown

    # ── Fallback: innerText-based extraction (plain text, no markdown) ──
    logger.info("Copy-button extraction unavailable; using innerText fallback")
    return final or last_text


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "running",
        "models": list(contexts.keys()),
        "browser": browser_instance is not None
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    data = await request.json()
    model_name = data.get("model", DEFAULT_MODEL)
    stream = data.get("stream", False)

    backend = None
    clean_model_name = model_name
    if "/" in model_name:
        clean_model_name = model_name.split("/", 1)[1]

    for key, cfg in MODEL_CONFIG.items():
        if clean_model_name in cfg.get("model_name", []) or clean_model_name == key:
            backend = key
            break
    if not backend:
        backend = DEFAULT_MODEL

    async with backend_locks[backend]:
        cfg = MODEL_CONFIG[backend]
        page = contexts[backend].pages[0]

        messages = data["messages"]
        system_msg = next((m for m in messages if m["role"] == "system"), None)
        non_system = [m for m in messages if m["role"] != "system"]
        turns = sum(1 for m in non_system if m["role"] == "assistant")

        pending_images: List[str] = []
        if turns == 0:
            # For stateless API calls, start a fresh conversation by navigating to the base URL
            current_url = page.url
            base_url = cfg["url"]
            if current_url != base_url and ("/chat" in current_url or "?" in current_url or current_url.rstrip("/") != base_url.rstrip("/")):
                logger.info("Stateless query detected; resetting page to base URL: %s", base_url)
                try:
                    await page.goto(base_url, timeout=15000)
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.warning("Could not reset page to base URL: %s", e)

            user_content = next((m for m in reversed(non_system) if m["role"] == "user"), {})
            user_text, pending_images = extract_content_parts(user_content.get("content", ""))
            sys_text = system_msg["content"] if system_msg else ""
            prompt = f"[SYSTEM CONFIGURATION]\n{sys_text}\n[END]\n\nNow respond to this first request:\n{user_text}"
        else:
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

        try:
            response_text = await get_response(page, cfg, prompt, request, image_urls=pending_images if pending_images else None)
        except RuntimeError as e:
            logger.error("get_response failed: %s", e)
            error_msg = f"[Router Error] {e}"
            if stream:
                async def error_stream(msg=error_msg):
                    chunk_id = "chatcmpl-error"
                    ts = int(asyncio.get_event_loop().time())
                    yield "data: " + json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": model_name, "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg}, "finish_reason": None}]}) + "\n\n"
                    yield "data: " + json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(error_stream(), media_type="text/event-stream")
            return {
                "id": "error",
                "object": "chat.completion",
                "created": 0,
                "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": error_msg}, "finish_reason": "stop"}]
            }

    if stream:
        # Marimo (and other OpenAI-compatible clients) use stream=True.
        # Since the browser response arrives all at once, we simulate streaming
        # by emitting one SSE chunk per token. json.dumps ensures any quotes or
        # newlines in the response text never corrupt the SSE JSON.
        async def generate(text=response_text, mdl=model_name):
            chunk_id = f"chatcmpl-{abs(hash(text))}"
            ts = int(asyncio.get_event_loop().time())
            # Role header chunk (empty content, sets role)
            yield "data: " + json.dumps({
                "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
            }) + "\n\n"
            # Emit one chunk per token (words + whitespace separators)
            tokens = re.split(r"(\s+)", text)
            for token in tokens:
                if not token:
                    continue
                yield "data: " + json.dumps({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                    "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]
                }) + "\n\n"
            # Final stop chunk
            yield "data: " + json.dumps({
                "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming (backward-compatible) response
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


@app.get("/v1/models")
async def list_models():
    data = []
    for key, cfg in MODEL_CONFIG.items():
        data.append({
            "id": key,
            "object": "model",
            "created": 1686935002,
            "owned_by": "zerobound"
        })
        model_names = cfg.get("model_name", [])
        if isinstance(model_names, str):
            model_names = [model_names]
        for name in model_names:
            if name != key:
                data.append({
                    "id": name,
                    "object": "model",
                    "created": 1686935002,
                    "owned_by": "zerobound"
                })
    return {
        "object": "list",
        "data": data
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


@app.get("/v1/inspect")
async def inspect_page():
    """Diagnostic: dump all visible input/editable elements on the current page."""
    try:
        page = contexts[DEFAULT_MODEL].pages[0]
        elements = await page.evaluate("""
            () => {
                const results = [];
                const tags = ['textarea', 'input', '[contenteditable]', 'div[role="textbox"]', 'button', 'div[role="button"]', '.ds-icon-button', '[aria-label]'];
                tags.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => {
                        const r = el.getBoundingClientRect();
                        const visible = r.width > 0 && r.height > 0 && window.getComputedStyle(el).display !== 'none';
                        if (!visible) return;

                        if (el.dataset.inspected) return;
                        el.dataset.inspected = "true";

                        results.push({
                            tag: el.tagName,
                            id: el.id || null,
                            classes: el.className || null,
                            placeholder: el.placeholder || el.getAttribute('data-placeholder') || null,
                            aria: el.getAttribute('aria-label') || null,
                            text: el.innerText ? el.innerText.substring(0, 20) : null,
                            contenteditable: el.getAttribute('contenteditable'),
                            visible: visible,
                            rect: {w: Math.round(r.width), h: Math.round(r.height)}
                        });
                    });
                });
                return results;
            }
        """)
        return {
            "url": page.url,
            "elements": elements
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
