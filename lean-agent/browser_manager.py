"""
browser_manager.py – Async‑safe browser controller using a dedicated thread.
Version 2.0 – added proper timeouts, error propagation, and clean shutdown.
"""

from __future__ import annotations
import asyncio
import base64
import queue
import threading
import logging
from typing import Any, Dict

logger = logging.getLogger("browser_manager")


class BrowserManager:
    """Manages a Playwright browser instance in a dedicated thread with its own event loop."""

    def __init__(self):
        self._cmd_queue: queue.Queue = queue.Queue()
        self._res_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._shutdown = False

    def _start_worker_if_needed(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker_thread, daemon=True)
            self._thread.start()

    def _worker_thread(self):
        """Synchronous entry point – creates a fresh event loop for the thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_worker())
        except Exception as exc:
            logger.exception("Browser worker loop crashed: %s", exc)
        finally:
            loop.close()

    async def _async_worker(self):
        from playwright.async_api import async_playwright
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()
                self._inject_overlay(page)

                while not self._shutdown:
                    # Wait for a command with a timeout so we can check shutdown flag
                    try:
                        cmd, args = await asyncio.wait_for(
                            asyncio.to_thread(self._cmd_queue.get), timeout=0.5
                        )
                    except asyncio.TimeoutError:
                        continue

                    if cmd == "quit":
                        break

                    try:
                        result = await self._handle_command(page, cmd, args)
                        self._res_queue.put(result)
                    except Exception as e:
                        logger.error("Command '%s' failed: %s", cmd, e)
                        self._res_queue.put({"error": str(e)})
        except Exception as e:
            logger.exception("Browser engine crashed: %s", e)
            # Drain command queue to unblock any waiting callers
            while not self._cmd_queue.empty():
                self._cmd_queue.get_nowait()
            self._res_queue.put({"error": f"Browser engine crashed: {e}"})
        finally:
            self._res_queue.put({"status": "success", "message": "Browser closed"})

    async def _handle_command(self, page, cmd: str, args: list) -> Dict[str, Any]:
        if cmd == "goto":
            url = args[0]
            if not url.startswith("http") and not url.startswith("file://"):
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return await self._take_screenshot(page)

        elif cmd == "click":
            await page.click(args[0], timeout=5000, force=True)
            await page.wait_for_timeout(500)
            return await self._take_screenshot(page)

        elif cmd == "type":
            await page.fill(args[0], args[1], timeout=5000, force=True)
            await page.wait_for_timeout(500)
            return await self._take_screenshot(page)

        elif cmd == "scroll":
            direction, amount = args[0].lower(), args[1] if len(args) > 1 else 500
            delta = -amount if direction == "up" else amount
            await page.mouse.wheel(0, delta)
            await page.wait_for_timeout(500)
            return await self._take_screenshot(page)

        elif cmd == "screenshot":
            return await self._take_screenshot(page)

        else:
            return {"error": f"Unknown command: {cmd}"}

    async def _inject_overlay(self, page):
        """Ensure the controlling overlay is injected into the page."""
        try:
            await page.evaluate("""() => {
                if (document.getElementById('zb-agent-overlay')) return;
                const o = document.createElement('div');
                o.id = 'zb-agent-overlay';
                o.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.3);z-index:2147483647;display:none;justify-content:center;align-items:center;pointer-events:all;';
                const b = document.createElement('div');
                b.style.cssText = 'background:rgba(255,0,0,0.9);color:white;padding:15px 30px;border-radius:8px;font-family:sans-serif;font-size:24px;font-weight:bold;';
                b.innerText = '⚠️ Agent controlling page';
                o.appendChild(b);
                document.body.appendChild(o);
            }""")
        except Exception:
            pass

    async def _take_screenshot(self, page) -> Dict[str, Any]:
        """Capture screenshot with overlay shown temporarily."""
        await page.evaluate("document.getElementById('zb-agent-overlay').style.display = 'flex'")
        await page.wait_for_timeout(300)
        b = await page.screenshot(type="jpeg", quality=60)
        await page.evaluate("document.getElementById('zb-agent-overlay').style.display = 'none'")
        return {"status": "success", "base64_image": base64.b64encode(b).decode()}

    # Public async methods
    async def goto(self, url: str) -> Dict[str, Any]:
        self._start_worker_if_needed()
        self._cmd_queue.put(("goto", [url]))
        return await asyncio.to_thread(self._res_queue.get)

    async def click(self, selector: str) -> Dict[str, Any]:
        self._start_worker_if_needed()
        self._cmd_queue.put(("click", [selector]))
        return await asyncio.to_thread(self._res_queue.get)

    async def type(self, selector: str, text: str) -> Dict[str, Any]:
        self._start_worker_if_needed()
        self._cmd_queue.put(("type", [selector, text]))
        return await asyncio.to_thread(self._res_queue.get)

    async def scroll(self, direction: str, amount: int = 500) -> Dict[str, Any]:
        self._start_worker_if_needed()
        self._cmd_queue.put(("scroll", [direction, amount]))
        return await asyncio.to_thread(self._res_queue.get)

    async def screenshot(self) -> Dict[str, Any]:
        self._start_worker_if_needed()
        self._cmd_queue.put(("screenshot", []))
        return await asyncio.to_thread(self._res_queue.get)

    async def close(self) -> Dict[str, Any]:
        if self._thread and self._thread.is_alive():
            self._shutdown = True
            self._cmd_queue.put(("quit", []))
            return await asyncio.to_thread(self._res_queue.get)
        return {"status": "success", "message": "Browser was not open"}


# Global instance
browser_manager = BrowserManager()