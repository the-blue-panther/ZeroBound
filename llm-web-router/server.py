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
import time
import uuid
import traceback
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
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


def _agent_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "../lean-agent"))


def _ensure_agent_path() -> None:
    import sys
    agent_path = _agent_path()
    if agent_path not in sys.path:
        sys.path.insert(0, agent_path)


def build_models_response() -> Dict[str, Any]:
    data = []
    seen = set()
    for key, cfg in MODEL_CONFIG.items():
        names = [key]
        model_names = cfg.get("model_name", [])
        if isinstance(model_names, str):
            model_names = [model_names]
        names.extend(model_names)
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            data.append({
                "id": name,
                "object": "model",
                "created": 1686935002,
                "owned_by": "zerobound",
                "capabilities": {
                    "chat": True,
                    "streaming": True,
                    "tool_calling": True,
                    "vision": True,
                },
                "metadata": {
                    "backend": key,
                    "tool_schema_url": "/v1/tools",
                    "tool_execution_url": "/v1/tools/{name}/execute",
                },
            })
    return {"object": "list", "data": data}


def chat_completion_response(
    model_name: str,
    message: Dict[str, Any],
    finish_reason: str = "stop",
    response_id: Optional[str] = None,
) -> Dict[str, Any]:
    content = message.get("content") or ""
    return {
        "id": response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": max(0, len(str(content).split())),
            "total_tokens": max(0, len(str(content).split())),
        },
    }


def _extract_tool_calls_from_call_syntax(
    response_text: str,
    allowed_tool_names: Optional[set] = None,
) -> List[Dict[str, Any]]:
    _ensure_agent_path()
    from agent_brain import _parse_tool_call

    parsed_tool_calls = []
    extracted = _parse_tool_call(response_text)
    logger.info("RAW EXTRACTED = %r", extracted)
    for i, call in enumerate(extracted):
        tool_name = call.get("tool", "")
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            logger.warning("Ignoring tool call for non-client tool: %s", tool_name)
            continue
        parsed_tool_calls.append({
            "index": i,
            "id": f"call_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(call.get("args", {})),
            },
        })
    return parsed_tool_calls


async def execute_registry_tool(tool_name: str, args: Dict[str, Any]) -> Any:
    _ensure_agent_path()
    from openai_adapter import validate_tool_call
    from tool_registry import handle_tool_call

    valid, error = validate_tool_call(tool_name, args or {})
    if not valid:
        return {"error": error}
    return await handle_tool_call(tool_name, args or {})

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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error while processing %s", request.url.path)
    detail = str(exc) or exc.__class__.__name__
    if os.getenv("ROUTER_DEBUG_ERRORS", "1") == "1":
        detail = f"{detail}\n{traceback.format_exc()}"
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": detail,
                "type": exc.__class__.__name__,
                "code": "internal_server_error",
            }
        },
    )


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


def strip_router_protocol(text: str) -> str:
    """Remove router-injected protocol blocks before replaying client history."""
    if not text:
        return text
    text = re.sub(
        r'\[SYSTEM (?:CONFIGURATION|REINFORCEMENT)\].*?\[END\]\s*',
        '',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r'--- IDENTITY \(v[\s\S]*?--- AVAILABLE TOOLS ---[\s\S]*?(?=\n\[(?:USER|ASSISTANT|TOOL RESULT)|\Z)',
        '',
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


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
        await page.bring_to_front()
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

        # Step 2 – Scroll to absolute bottom and LOCK scroll to prevent DeepSeek auto-scroll
        # DeepSeek has scroll restoration that scrolls back up - we counteract it
        await page.evaluate("""
        () => {
            // Multiple scrolls to fight DeepSeek's scroll restoration
            window.scrollTo(0, document.body.scrollHeight);
            window.scrollTo(0, document.body.scrollHeight);
            
            // Lock scroll position by disabling scroll until we click copy button
            window.__zb_scroll_lock = true;
            window.__zb_locked_scroll_pos = document.body.scrollHeight;
            
            // Monitor and restore scroll position
            window.__zb_scroll_restore = () => {
                if (!window.__zb_scroll_lock) return;
                const current = window.scrollY;
                const target = window.__zb_locked_scroll_pos;
                // Only restore if we drifted more than 50px
                if (Math.abs(current - target) > 50) {
                    window.scrollTo(0, target);
                }
            };
            
            window.addEventListener('scroll', window.__zb_scroll_restore);
        }
        """)

        await page.wait_for_timeout(500)

        # Step 3 – Find the copy button with RETRY LOGIC (5 seconds max)
        # Handles: async action bar rendering, delayed button appearance
        btn_el = None
        last_diagnostics = {}
        
        await page.evaluate("window.__zb_prev_button = null;")

        for attempt in range(10):
            # Scroll the last message action bar into view before every scan attempt.
            # Long responses push the action bar below the fold; this keeps it measurable.
            try:
                await page.evaluate("""
                    () => {
                        // Find last action bar row (_965abe9 class pattern from diagnostics)
                        const bars = document.querySelectorAll('[class*="_965abe9"], [class*="_54866f7"]');
                        if (bars.length > 0) {
                            const last = bars[bars.length - 1];
                            last.scrollIntoView({ block: 'center', behavior: 'instant' });
                        } else {
                            // Fallback: scroll to bottom
                            window.scrollTo(0, document.body.scrollHeight);
                        }
                    }
                """)
                await page.wait_for_timeout(150)
            except Exception:
                pass

            # Find button using 4-tier selection strategy (most stable → most fragile)
            # IMPORTANT: We search fresh each attempt - button might become enabled on later attempts
            btn_handle = await page.evaluate_handle("""
                () => {
                    const diag = { candidates: [], rows: [] };
                    window.__zb_button_diag = diag;

                    // ─── HELPER: Is element genuinely visible AND ENABLED? ───
                    const isVisible = el => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const cs = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                               cs.display !== 'none' &&
                               cs.visibility !== 'hidden' &&
                               parseFloat(cs.opacity) > 0.01;
                    };

                    const isEnabled = el => {
                        // Check disabled attribute
                        if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') {
                            return false;
                        }
                        // For divs with role="button", check if pointer-events is disabled
                        const cs = window.getComputedStyle(el);
                        if (cs.pointerEvents === 'none') {
                            return false;
                        }
                        return true;
                    };

                    // Collect all possible buttons for diagnostics
                    const diagBtns = Array.from(document.querySelectorAll('button, div[role="button"], [role="button"], .ds-icon-button, [aria-label]'));
                    diagBtns.forEach((el, i) => {
                        const r = el.getBoundingClientRect();
                        diag.candidates.push({
                            index: i + 1,
                            x: Math.round(r.left),
                            y: Math.round(r.top),
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                            aria: el.getAttribute('aria-label') || '',
                            title: el.getAttribute('title') || '',
                            cls: el.className || '',
                            innerText: (el.innerText || '').slice(0, 50).replace(/\\n/g, ' '),
                            enabled: isEnabled(el),
                            visible: isVisible(el),
                            parentCls: (el.parentElement && el.parentElement.className) || ''
                        });
                    });

                    // ─── HELPER: Does element have copy icon? ───
                    const hasCopyIcon = el => {
                        const svg = el.querySelector('svg');
                        if (!svg) return false;
                        const inner = svg.innerHTML;
                        // Look for copy icon patterns
                        if (inner.includes('M9.672') || inner.includes('M16 1') || inner.includes('clipboard')) {
                            return true;
                        }
                        // Also check for "copy" in aria-label/title of parent
                        const label = (el.getAttribute('aria-label') || el.getAttribute('title') || '').toLowerCase();
                        return label.includes('copy');
                    };

                    // ─── TIER 1: Explicit aria-label containing "copy" + ENABLED ───
                    const byExplicitLabel = Array.from(document.querySelectorAll(
                        'button[aria-label*="copy"], button[title*="copy"], [aria-label*="copy"], [title*="copy"]'
                    )).filter(el => {
                        const r = el.getBoundingClientRect();
                        if (!isVisible(el) || !isEnabled(el)) {
                            return false;
                        }
                        if (r.top < window.innerHeight * 0.1) {
                            return false;
                        }
                        return true;
                    });

                    let selected_btn = null;

                    if (byExplicitLabel.length > 0) {
                        selected_btn = byExplicitLabel[byExplicitLabel.length - 1];
                    }

                    // ─── TIER 2: Row scan + COPY ICON MATCHING + ENABLED ───
                    if (!selected_btn) {
                        const allBtns = Array.from(document.querySelectorAll(
                            'button, div[role="button"], [role="button"]'
                        )).filter(el => {
                            const r = el.getBoundingClientRect();
                     // CRITICAL: Only consider ENABLED buttons
                            if (!isVisible(el) || !isEnabled(el)) return false;
                            if (!el.querySelector('svg')) return false;
                            if (r.top < window.innerHeight * 0.1) return false;
                            // Exclude buttons with visible label text (code block Copy/Download)
                            const txt = (el.innerText || '').toLowerCase().trim();
                            if (txt === 'copy' || txt === 'download' || txt === 'run' || txt === 'insert') return false;
                            const cls = el.className.toLowerCase();
                            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                            if (cls.includes('menu') || cls.includes('nav') || aria.includes('menu')) return false;
                            return true;
                        });

                        // Group into rows
                        const rows = [];
                        for (const btn of allBtns) {
                            const r = btn.getBoundingClientRect();
                            let row = rows.find(x => Math.abs(x.y - r.top) < 25);
                            if (!row) {
                                row = { y: r.top, items: [] };
                                rows.push(row);
                            }
                            row.items.push(btn);
                        }

                        rows.sort((a, b) => b.y - a.y);
                        
                        diag.rows = rows.map(r => ({
                            count: r.items.length,
                            positions: r.items.map(b => Math.round(b.getBoundingClientRect().left))
                        }));

                        // Filter out input bar (DeepThink/Search) AND code-block toolbars (Copy/Download text)
                        const isInputBarRow = row => {
                            return row.items.some(btn => {
                                const text = (btn.innerText || '').toLowerCase();
                                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                                return text.includes('deepthink') || text.includes('search') || aria.includes('deepthink');
                            });
                        };
                        const isCodeBlockToolbar = row => {
                            return row.items.some(btn => {
                                const text = (btn.innerText || '').toLowerCase().trim();
                                return text === 'copy' || text === 'download' || text === 'run' || text === 'insert';
                            });
                        };
                        const validRows = rows.filter(r => !isInputBarRow(r) && !isCodeBlockToolbar(r) && r.items.length >= 2);
                        
                        for (const row of validRows) {
                            row.items.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                        }

                        // PASS 1: Explicit "copy" label across valid rows
                        for (const row of validRows) {
                            for (const btn of row.items) {
                                const label = (btn.getAttribute('aria-label') || btn.getAttribute('title') || '').toLowerCase();
                                if (label.includes('copy')) {
                                    selected_btn = btn;
                                    break;
                                }
                            }
                            if (selected_btn) break;
                        }

                        // PASS 2: Copy icon in SVG across valid rows
                        if (!selected_btn) {
                            for (const row of validRows) {
                                for (const btn of row.items) {
                                    if (hasCopyIcon(btn)) {
                                        selected_btn = btn;
                                        break;
                                    }
                                }
                                if (selected_btn) break;
                            }
                        }

                        // PASS 3: Leftmost button of the bottom-most action bar row
                        if (!selected_btn && validRows.length > 0) {
                            const bottomRow = validRows[0]; // validRows are sorted bottom-to-top (b.y - a.y)
                            selected_btn = bottomRow.items[0];
                        }
                    }

                    // ─── TIER 3: Any enabled button with copy icon ───
                    if (!selected_btn) {
                        const byIcon = Array.from(document.querySelectorAll('button, [role="button"]'))
                            .filter(el => {
                                if (!isVisible(el) || !isEnabled(el)) return false;
                                const r = el.getBoundingClientRect();
                                if (r.top < window.innerHeight * 0.15) return false;
                                const txt = (el.innerText || '').toLowerCase().trim();
                                if (txt === 'copy' || txt === 'download' || txt === 'run' || txt === 'insert') return false;
                                if (!hasCopyIcon(el)) return false;
                                return true;
                            });

                        if (byIcon.length > 0) {
                            const sorted = byIcon.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
                            selected_btn = sorted[0];
                        }
                    }

                    // ─── TIER 4: Last resort - any enabled icon button ───
                    if (!selected_btn) {
                        const allEnabled = Array.from(document.querySelectorAll('button, [role="button"]'))
                            .filter(el => {
                                if (!isVisible(el) || !isEnabled(el)) return false;
                                if (!el.querySelector('svg')) return false;
                                const txt = (el.innerText || '').toLowerCase().trim();
                                if (txt === 'copy' || txt === 'download' || txt === 'run' || txt === 'insert') return false;
                                return true;
                            })
                            .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);

                        if (allEnabled.length > 0) {
                            selected_btn = allEnabled[0];
                        }
                    }
                    
                    if (selected_btn) {
                        diag.selectedHtml = selected_btn.outerHTML.slice(0, 500);
                        diag.parentHtml = selected_btn.parentElement ? selected_btn.parentElement.outerHTML.slice(0, 500) : '';
                        
                        if (window.__zb_prev_button && window.__zb_prev_button !== selected_btn) {
                            diag.replaced = true;
                        } else if (window.__zb_prev_button === selected_btn) {
                            diag.replaced = false;
                        } else {
                            diag.replaced = null;
                        }
                        window.__zb_prev_button = selected_btn;
                    }

                    return selected_btn;
                }
            """)

            btn_el = btn_handle.as_element() if btn_handle else None

            # Extract diagnostics before next retry
            try:
                diag = await page.evaluate("window.__zb_button_diag")
                logger.info("--- DIAGNOSTICS ATTEMPT %d ---", attempt + 1)
                for c in diag.get("candidates", []):
                    logger.info("candidate #%d\\nx: %s\\ny: %s\\nwidth: %s\\nheight: %s\\naria-label: %s\\ntitle: %s\\nclass: %s\\ninnerText: %s\\nenabled: %s\\nvisible: %s\\nparent class: %s", 
                        c.get('index'), c.get('x'), c.get('y'), c.get('width'), c.get('height'), c.get('aria'), c.get('title'), c.get('cls'), c.get('innerText'), c.get('enabled'), c.get('visible'), c.get('parentCls'))
                
                for i, r in enumerate(diag.get("rows", [])):
                    logger.info("ROW %d\\nbutton count: %d\\nbutton positions: %s", i + 1, r.get('count'), r.get('positions'))
                
                if diag.get("replaced") is True:
                    logger.info("Selected button disappeared. New button appeared.")
                elif diag.get("replaced") is False:
                    logger.info("Selected button remained the same DOM element.")
                    
                if diag.get("selectedHtml"):
                    logger.info("SELECTED HTML: %s", diag.get("selectedHtml"))
                    logger.info("PARENT HTML: %s", diag.get("parentHtml"))
            except Exception as e:
                logger.debug("Diagnostic dump failed: %s", e)

            if btn_el:
                logger.info(
                    "Copy button found on attempt %d/%d",
                    attempt + 1, 10
                )
                break

            # Wait before retry (gives async rendering time)
            await page.wait_for_timeout(500)

        if not btn_el:
            logger.debug(
                "get_markdown_via_copy_button: no copy button found after %d attempts. %s",
                10, 
                f"Last diagnostics: {last_diagnostics}" if last_diagnostics else "No diagnostics captured"
            )
            return None

        # Step 4 – Removed redundant scroll that causes mouseleave hover-loss

        # Step 4b – Extract detailed button info for diagnostics
        try:
            btn_info = await btn_el.evaluate("""
                el => {
                    const svg = el.querySelector('svg');
                    const svgInner = svg ? svg.innerHTML : '';
                    return {
                        tag: el.tagName,
                        cls: el.className?.slice(0, 80),
                        aria: el.getAttribute('aria-label'),
                        title: el.getAttribute('title'),
                        innerText: el.innerText?.slice(0, 50),
                        y: Math.round(el.getBoundingClientRect().top),
                        x: Math.round(el.getBoundingClientRect().left),
                        width: Math.round(el.getBoundingClientRect().width),
                        height: Math.round(el.getBoundingClientRect().height),
                        hasSvg: !!svg,
                        svgText: svgInner.slice(0, 100),
                        parent: el.parentElement?.tagName,
                        parentCls: el.parentElement?.className?.slice(0, 60)
                    };
                }
            """)
            logger.info("Copy button candidate → %s", btn_info)
            
            # Check what we're about to click
            aria_label = btn_info.get('aria')
            title = btn_info.get('title')
            innerText = btn_info.get('innerText', '')
            full_text = f"{aria_label} {title} {innerText}".lower()
            
            if 'deepthink' in full_text or 'search' in full_text or 'download' in full_text:
                logger.error("⚠️  WRONG BUTTON SELECTED! Button is: %s", btn_info)
                # Try to find a better button - skip this one and keep searching
                logger.warning("Skipping this button, searching for better match...")
                return None
            
            if 'copy' not in full_text and not btn_info.get('hasSvg'):
                logger.warning("⚠️  Button label unclear and no SVG - risky selection: %s", btn_info)
            elif 'copy' in full_text or btn_info.get('hasSvg'):
                logger.debug("✓ Button looks safe to click")

            visible = await btn_el.is_visible()
            enabled = await btn_el.is_enabled()
            box = await btn_el.bounding_box()
            logger.info("COPY BTN VISIBLE=%s ENABLED=%s BOX=%s", visible, enabled, box)
            
            if not enabled:
                logger.warning("Button is not enabled yet, waiting for it to become enabled...")

        except Exception as e:
            logger.debug("Button diagnostic failed: %s", e)

        # Step 4c – Diagnostic logging before click

        # Step 5 – Ensure button is enabled before clicking (wait for response to fully render)
        # The response might still be rendering, so button could be disabled temporarily
        max_wait_for_enabled = 10
        start_wait_time = time.time()
        for wait_attempt in range(max_wait_for_enabled):
            try:
                # Actively hover the button to prevent hover-loss (mouseleave) timeouts
                try:
                    await btn_el.hover(timeout=1000)
                except Exception:
                    pass
                is_enabled = await btn_el.is_enabled()
                elapsed_time = time.time() - start_wait_time
                logger.info("t=%.1fs enabled=%s", elapsed_time, is_enabled)
                
                if is_enabled:
                    logger.debug("Button is enabled on wait attempt %d/%d", wait_attempt + 1, max_wait_for_enabled)
                    break
                else:
                    logger.debug("Button still disabled (attempt %d/%d), waiting...", wait_attempt + 1, max_wait_for_enabled)
            except Exception as e:
                logger.debug("Could not check enabled state: %s", e)
                break
            
            await page.wait_for_timeout(500)

        # Step 5b – Click the copy button
        try:
            await btn_el.hover()
            await btn_el.click(delay=50)
            logger.debug("Copy button clicked successfully")
        except Exception as e:
            logger.warning("Failed to click copy button: %s", e)
            # Clean up scroll lock before returning
            await page.evaluate("""
                () => {
                    window.__zb_scroll_lock = false;
                    if (window.__zb_scroll_restore) {
                        window.removeEventListener('scroll', window.__zb_scroll_restore);
                    }
                }
            """)
            return None

        # Step 5b – Disable scroll lock (copy button was clicked)
        await page.evaluate("""
            () => {
                window.__zb_scroll_lock = false;
                if (window.__zb_scroll_restore) {
                    window.removeEventListener('scroll', window.__zb_scroll_restore);
                }
            }
        """)

        # Wait for async clipboard handlers to fire
        await asyncio.sleep(1.0)

        # Step 6 – Read the intercepted markdown from clipboard
        markdown = await page.evaluate("window.__zb_md")

        # Step 7 – Cleanup clipboard interception hooks and scroll lock
        await page.evaluate("""
            () => {
                if (navigator.clipboard && window.__zb_orig_write) {
                    navigator.clipboard.writeText = window.__zb_orig_write;
                }
                if (window.__zb_copy_listener) {
                    document.removeEventListener('copy', window.__zb_copy_listener, true);
                }
                if (window.__zb_scroll_restore) {
                    window.removeEventListener('scroll', window.__zb_scroll_restore);
                }
                delete window.__zb_md;
                delete window.__zb_orig_write;
                delete window.__zb_copy_listener;
                delete window.__zb_button_diag;
                delete window.__zb_scroll_lock;
                delete window.__zb_locked_scroll_pos;
                delete window.__zb_scroll_restore;
            }
        """)

        if markdown:
            logger.info("Raw markdown captured via clipboard hook (%d chars)", len(markdown))
            return markdown
        else:
            logger.debug("No markdown captured from clipboard (button click may have failed)")
            return None

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
        if request is not None and await request.is_disconnected():
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
            # Check for a "Continue" button and click it if present
            try:
                continue_box = await page.evaluate("""
                    () => {
                        const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                        // Reverse array to find the LAST (most recent) continue button
                        for (let i = btns.length - 1; i >= 0; i--) {
                            const b = btns[i];
                            if (b.offsetWidth > 0 && b.offsetHeight > 0 && window.getComputedStyle(b).display !== 'none') {
                                const txt = (b.innerText || '').trim().toLowerCase();
                                if (txt === 'continue') {
                                    const r = b.getBoundingClientRect();
                                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                                }
                            }
                        }
                        return null;
                    }
                """)
                if continue_box:
                    logger.info("Clicked 'Continue' button to resume generation (via mouse)")
                    await page.mouse.click(continue_box['x'], continue_box['y'])
                    stable_since = 0.0
                    await asyncio.sleep(1)  # Give it a moment to resume
                    continue
            except Exception as e:
                logger.warning(f"Error clicking continue button: {e}")
                pass
                
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
        # Scroll to the bottom so the last message is fully in view
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(300)

        # Hover the outermost message wrapper (not just the content div) to
        # reveal the action-bar; .ds-markdown or equivalent is a child of the
        # wrapper – we need to walk up to the real hover-target.
        hovered = await page.evaluate_handle("""
            () => {
                // Find the last visible response container
                const containers = Array.from(document.querySelectorAll(
                    'div.ds-markdown, div[class*="message-content"], div[class*="markdown-body"], div[class*="response"]'
                )).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (!containers.length) return null;
                const last = containers[containers.length - 1];
                // Walk up to find the closest ancestor that has > 1 children
                // (i.e. the wrapper that holds both content and action bar)
                let candidate = last.parentElement;
                for (let i = 0; i < 6 && candidate; i++) {
                    if (candidate.children.length >= 2) return candidate;
                    candidate = candidate.parentElement;
                }
                return last;
            }
        """)
        hover_el = hovered.as_element() if hovered else None
        if hover_el:
            await hover_el.hover(timeout=2000)
            logger.info("Hovered message wrapper for action-bar reveal")
        else:
            # Fallback: move mouse to bottom-centre of viewport
            vp = page.viewport_size or {"width": 1280, "height": 800}
            await page.mouse.move(vp["width"] // 2, vp["height"] - 180)

        # Wait generously for the CSS fade-in transition on the action bar
        await page.wait_for_timeout(1500)
    except Exception as e:
        logger.warning(f"Hover before copy button failed: {e}")

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


@app.get("/v1/tools")
async def list_tools():
    _ensure_agent_path()
    from openai_adapter import get_openai_tools_schema
    return {
        "object": "list",
        "data": get_openai_tools_schema(),
    }


@app.post("/v1/tools/{tool_name}/execute")
async def execute_tool_by_name(tool_name: str, request: Request):
    data = await request.json()
    args = data.get("arguments", data.get("args", data))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {"error": "arguments must be a JSON object or JSON-encoded object string"}
    result = await execute_registry_tool(tool_name, args or {})
    return {
        "tool": tool_name,
        "result": result,
    }


@app.post("/v1/tool-calls")
async def execute_tool_calls(request: Request):
    data = await request.json()
    tool_calls = data.get("tool_calls", [])
    tool_messages = []
    results = {}
    for tc in tool_calls:
        call_id = tc.get("id", f"call_{uuid.uuid4().hex[:10]}")
        function = tc.get("function", {})
        tool_name = function.get("name")
        raw_args = function.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        result = await execute_registry_tool(tool_name, args or {})
        results[call_id] = {"tool": tool_name, "result": result}
        tool_messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, default=str) if not isinstance(result, str) else result,
        })
    return {
        "object": "tool_call.execution",
        "tool_results": tool_messages,
        "results": results,
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
        
        current_url = page.url
        base_url = cfg["url"]
        is_fresh_page = (current_url == base_url or current_url.rstrip("/") == base_url.rstrip("/") or "/chat" not in current_url)

        if turns == 0 and not is_fresh_page:
            # For stateless API calls starting a new thread, navigate to base URL
            logger.info("Stateless query detected; resetting page to base URL: %s", base_url)
            try:
                await page.goto(base_url, timeout=15000)
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(1)
                is_fresh_page = True
            except Exception as e:
                logger.warning("Could not reset page to base URL: %s", e)

        tools = data.get("tools", [])
        client_tool_names = {
            t.get("function", {}).get("name")
            for t in tools
            if t.get("type") == "function" and t.get("function", {}).get("name")
        }
        tool_choice = data.get("tool_choice", "auto")

        # Build master prompt based on selected mode. Native tool clients such
        # as VS Code must not see ZeroBound's internal registry prompt, because
        # they can only execute the tools they supplied in this request.
        mode = os.environ.get("AGENT_MODE")
        master_prompt = ""
        if mode in ("zerobound", "marimo") and not tools:
            try:
                _ensure_agent_path()
                from agent_brain import build_system_prompt
                master_prompt = build_system_prompt(os.getcwd(), agent_mode=mode)
            except Exception as e:
                logger.error("Failed to load master prompt: %s", e)

        native_tools_prompt = ""
        if tools:
            logger.info("Intercepted %d native tools from API request", len(tools))
            native_tools_prompt = "\n[CLIENT PROVIDED TOOLS]\nYou may call ONLY the following client-provided tools:\n"
            for t in tools:
                func = t.get("function", {})
                name = func.get("name", "unknown")
                desc = func.get("description", "")
                params = json.dumps(func.get("parameters", {}), indent=2)
                native_tools_prompt += f"- {name}: {desc}\nParameters schema: {params}\n\n"
            native_tools_prompt += (
                "To call one of these tools, write exactly this on a new line (do not wrap in action tags):\n"
                "```json\nCALL: tool_name({\"arg\": \"val\"})\n```\n"
                "The tool_name MUST exactly match one of the client-provided names above, and the JSON arguments MUST match that tool's parameters schema exactly. "
                "Do not use ZeroBound internal tool names such as read_file, write_file, list_files, get_file_tree, edit_file, run_command, or git_diff unless the client explicitly listed that exact tool name above. "
                "Wait for the system to provide the [TOOL RESULT] before continuing."
            )
            if tool_choice == "none":
                native_tools_prompt += "\nThe client set tool_choice=none, so answer without calling tools."
            elif isinstance(tool_choice, dict):
                chosen = tool_choice.get("function", {}).get("name")
                if chosen:
                    native_tools_prompt += f"\nThe client explicitly selected tool `{chosen}`; call that tool if possible."
            elif tool_choice == "required":
                native_tools_prompt += "\nThe client set tool_choice=required; you must call at least one tool."

        sys_text = system_msg["content"] if system_msg else ""
        if master_prompt and not tools:
            # Only use internal lean-agent master_prompt if we are NOT given native tools
            sys_text = master_prompt
        
        if native_tools_prompt:
            # Always append native tools schema if provided
            sys_text += "\n" + native_tools_prompt

        if is_fresh_page:
            # We are on a fresh chat page, so we MUST send the entire history + system prompt
            if turns > 0:
                logger.info("Stateless replay on fresh page with %d turns", turns)
            
            parts = [f"[SYSTEM CONFIGURATION]\n{sys_text}\n[END]\n\nNow respond to this request:"]
            for m in non_system:
                role = m["role"].upper()
                txt, imgs = extract_content_parts(m.get("content", ""))
                if tools:
                    txt = strip_router_protocol(txt)
                if imgs:
                    pending_images.extend(imgs)
                if m.get("role") in ("function", "tool"):
                    t_name = m.get('name', m.get('tool_call_id', 'unknown'))
                    parts.append(f"[TOOL RESULT for {t_name}]:\n{txt}")
                else:
                    parts.append(f"[{role}]:\n{txt}")
            prompt = "\n\n".join(parts)
        else:
            # We are continuing an existing chat on the current page, send only delta
            last_ai = -1
            for i, m in enumerate(messages):
                if m["role"] == "assistant":
                    last_ai = i
            new_msgs = messages[last_ai+1:] if last_ai >= 0 else messages
            parts = []
            for m in new_msgs:
                role = m["role"].upper()
                txt, imgs = extract_content_parts(m.get("content", ""))
                if tools:
                    txt = strip_router_protocol(txt)
                if imgs:
                    pending_images.extend(imgs)
                if m.get("role") in ("function", "tool"):
                    t_name = m.get('name', m.get('tool_call_id', 'unknown'))
                    parts.append(f"[TOOL RESULT for {t_name}]:\n{txt}\nWhat next?")
                else:
                    parts.append(f"[{role}]:\n{txt}")
            prompt = "\n".join(parts)
            
            # If they just switched mode mid-conversation, prepend it as a reminder
            if master_prompt and not tools and len(parts) > 0 and "SYSTEM CONFIGURATION" not in prompt:
                prompt = f"[SYSTEM REINFORCEMENT]\n{master_prompt}\n[END]\n\n" + prompt

        # ── Orchestrator loop (Marimo mode only) ─────────────────────────────
        # For Marimo mode we run a tool-call loop inside the router:
        #   1. Send prompt to DeepSeek via the browser.
        #   2. Parse response for MODE: TOOL_CALLS or MODE: REPORT tag.
        #   3. If TOOL_CALLS → execute tools, build a [TOOL RESULT] follow-up
        #      and loop back to step 1.  Placeholder chunks are streamed live.
        #   4. If REPORT (or no tag found after MAX_TOOL_ITERATIONS) → done.
        # For zerobound / passthrough modes we keep the original single-shot path.
        # ─────────────────────────────────────────────────────────────────────

        MAX_TOOL_ITERATIONS = 10  # safety cap

        if mode == "marimo" and stream and not tools:
            # Import helpers once; errors fall through to passthrough.
            try:
                _ensure_agent_path()
                from agent_brain import _parse_tool_call as _ptc
                from tool_registry import handle_tool_call as _htc
                _orchestration_available = True
            except Exception as _oe:
                logger.warning("Orchestration helpers unavailable (%s); falling back to passthrough", _oe)
                _orchestration_available = False

            async def marimo_orchestrated_generate(
                initial_prompt=prompt,
                pg=page,
                backend_cfg=cfg,
                mdl=model_name,
                imgs=pending_images,
                orchestrate=_orchestration_available,
            ):
                chunk_id = f"chatcmpl-{abs(hash(initial_prompt))}"
                ts = int(asyncio.get_event_loop().time())

                # ── SSE helpers ──────────────────────────────────────────────
                def _sse(content: str, finish: str = None) -> str:
                    payload = {
                        "id": chunk_id, "object": "chat.completion.chunk",
                        "created": ts, "model": mdl,
                        "choices": [{"index": 0,
                                     "delta": {"content": content} if content else {},
                                     "finish_reason": finish}],
                    }
                    return "data: " + json.dumps(payload) + "\n\n"

                def _placeholder(text: str) -> str:
                    """Emit a dim italic status line visible in Marimo."""
                    return _sse(f"\n\n*{text}*\n\n")

                # Role header
                yield "data: " + json.dumps({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                }) + "\n\n"

                current_prompt = initial_prompt
                iteration = 0
                final_text = ""

                while iteration < MAX_TOOL_ITERATIONS:
                    iteration += 1
                    logger.info("[Orchestrator] Iteration %d – sending prompt to DeepSeek", iteration)

                    try:
                        raw = await get_response(pg, backend_cfg, current_prompt, None,
                                                 image_urls=imgs if iteration == 1 else None)
                    except RuntimeError as e:
                        yield _sse(f"\n\n[Router Error] {e}\n\n", "stop")
                        yield "data: [DONE]\n\n"
                        return

                    # ── Parse mode tag ────────────────────────────────────────
                    first_line = raw.split("\n", 1)[0].strip().upper()
                    rest = raw.split("\n", 1)[1].strip() if "\n" in raw else raw

                    # Tolerate responses where the LLM forgot the tag (treat as REPORT)
                    is_tool_call_mode = "MODE: TOOL_CALLS" in first_line or "MODE:TOOL_CALLS" in first_line
                    is_report_mode = "MODE: REPORT" in first_line or "MODE:REPORT" in first_line

                    if not orchestrate:
                        # Orchestration unavailable: stream raw text and stop
                        final_text = raw
                        break

                    if is_tool_call_mode:
                        # ── Execute tools ─────────────────────────────────────
                        body = rest if rest else raw
                        tool_calls = _ptc(body)

                        if not tool_calls:
                            # Model said TOOL_CALLS but we couldn't parse any — treat as REPORT
                            final_text = body
                            break

                        tool_results_parts = []
                        for tc in tool_calls:
                            tool_name = tc.get("tool", "unknown")
                            tool_args = tc.get("args", {})
                            logger.info("[Orchestrator] Executing tool: %s(%s)", tool_name, tool_args)

                            # Stream placeholder to Marimo so user sees progress
                            yield _placeholder(f"⚙️ Executing tool: `{tool_name}`…")
                            await asyncio.sleep(0)  # flush

                            try:
                                result = await _htc(tool_name, tool_args)
                                result_str = json.dumps(result, default=str) if not isinstance(result, str) else result
                            except Exception as te:
                                result_str = f"ERROR: {te}"
                                logger.warning("[Orchestrator] Tool %s failed: %s", tool_name, te)

                            yield _placeholder(f"✅ `{tool_name}` done — {len(result_str)} chars returned.")
                            await asyncio.sleep(0)

                            tool_results_parts.append(
                                f"[TOOL RESULT for {tool_name}]:\n{result_str}"
                            )

                        # Build next prompt (tool results only — DeepSeek already has history on the page)
                        current_prompt = "\n\n".join(tool_results_parts) + "\n\nBased on these results, continue. Remember to begin with MODE: TOOL_CALLS or MODE: REPORT."
                        imgs = None  # don't re-send images on follow-up turns

                    elif is_report_mode:
                        # ── Final answer ──────────────────────────────────────
                        final_text = rest if rest else raw
                        break

                    else:
                        # No recognized tag — treat entire response as final report
                        logger.warning("[Orchestrator] No MODE tag found in iteration %d; treating as REPORT", iteration)
                        final_text = raw
                        break

                else:
                    # Exceeded max iterations — return whatever we have
                    logger.warning("[Orchestrator] Hit max iterations (%d); returning last response", MAX_TOOL_ITERATIONS)
                    final_text = raw if 'raw' in dir() else "(max iterations exceeded)"

                # ── Stream final text to Marimo ───────────────────────────────
                if final_text:
                    # Drop any leftover MODE: REPORT / MODE: TOOL_CALLS prefix artefacts
                    cleaned = re.sub(r'^MODE:\s*(REPORT|TOOL_CALLS)\s*\n?', '', final_text, flags=re.IGNORECASE).strip()
                    tokens = re.split(r"(\s+)", cleaned)
                    for token in tokens:
                        if token:
                            yield _sse(token)

                yield _sse("", "stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                marimo_orchestrated_generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # ── Non-orchestrated path (zerobound / passthrough / non-streaming) ──
        try:
            response_text = await get_response(page, cfg, prompt, None, image_urls=pending_images if pending_images else None)
        except Exception as e:
            logger.exception("get_response failed")
            error_msg = f"[Router Error] {e}"
            if stream:
                async def error_stream(msg=error_msg):
                    chunk_id = "chatcmpl-error"
                    ts = int(asyncio.get_event_loop().time())
                    yield "data: " + json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": model_name, "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg}, "finish_reason": None}]}) + "\n\n"
                    yield "data: " + json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(error_stream(), media_type="text/event-stream")
            return chat_completion_response(
                model_name,
                {"role": "assistant", "content": error_msg},
                "stop",
                response_id="chatcmpl-error",
            )

    # ── Parse OpenAI Native Tool Calls ──
    parsed_tool_calls = []
    if tools:
        try:
            parsed_tool_calls = _extract_tool_calls_from_call_syntax(
                response_text,
                allowed_tool_names=client_tool_names,
            )
        except Exception as e:
            logger.error("Failed to parse native tool calls: %s", e)

    if stream:
        # Standard streaming for zerobound / passthrough modes
        async def generate(text=response_text, mdl=model_name, tcs=parsed_tool_calls):
            chunk_id = f"chatcmpl-{abs(hash(text))}"
            ts = int(asyncio.get_event_loop().time())
            yield "data: " + json.dumps({
                "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
            }) + "\n\n"
            
            if tcs:
                yield "data: " + json.dumps({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                    "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]
                }) + "\n\n"
                yield "data: " + json.dumps({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
                }) + "\n\n"
                yield "data: [DONE]\n\n"
                return

            tokens = re.split(r"(\s+)", text)
            for token in tokens:
                if not token:
                    continue
                yield "data: " + json.dumps({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": ts, "model": mdl,
                    "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]
                }) + "\n\n"
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
    msg = {"role": "assistant", "content": response_text}
    finish_reason = "stop"
    
    if parsed_tool_calls:
        # Non-streaming format shouldn't have 'index' in the tool_calls array elements
        for tc in parsed_tool_calls:
            tc.pop("index", None)
        msg["content"] = None
        msg["tool_calls"] = parsed_tool_calls
        finish_reason = "tool_calls"

    return chat_completion_response(model_name, msg, finish_reason)



@app.get("/v1/models")
async def list_models():
    return build_models_response()


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
    print("\n" + "="*50)
    print("[ZeroBound] Web Router Initialization")
    print("="*50)

    mode = os.environ.get("AGENT_MODE")
    if mode:
        mode = mode.strip().lower()
        if mode not in {"zerobound", "marimo", "none"}:
            print(f"Unknown AGENT_MODE={mode!r}; falling back to zerobound.")
            mode = "zerobound"
        os.environ["AGENT_MODE"] = mode
    else:
        print("Which mode would you like to run in?")
        print("  1. ZeroBound (General Engineering Agent)")
        print("  2. Marimo (Specialized Notebook Agent)")
        print("  3. Pass-through (No prompt injection)")
        print("="*50)

        choice = input("Enter choice (1/2/3) [1]: ").strip()
        if choice == "2":
            mode = "marimo"
        elif choice == "3":
            mode = "none"
        else:
            mode = "zerobound"
        os.environ["AGENT_MODE"] = mode

    labels = {"zerobound": "ZEROBOUND", "marimo": "MARIMO", "none": "PASS-THROUGH"}
    print(f"-> Mode set to: {labels.get(os.environ['AGENT_MODE'], os.environ['AGENT_MODE'])}")
    print("="*50 + "\n")

    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
