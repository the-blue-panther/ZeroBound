import asyncio
import threading
import queue
import base64
import time

class BrowserManager:
    def __init__(self):
        self.cmd_queue = queue.Queue()
        self.res_queue = queue.Queue()
        self.thread = None
        
    def _log(self, msg):
        try:
            with open("browser.log", "a") as f:
                f.write(msg + "\n")
        except:
            pass

    def _start_worker_if_needed(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()

    def _worker(self):
        self._log("[WORKER] Starting async thread")
        # Create a dedicated event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_worker())
        except Exception as e:
            self._log(f"[WORKER] Loop crashed: {str(e)}")
        finally:
            loop.close()

    async def _async_worker(self):
        from playwright.async_api import async_playwright
        self._log("[WORKER] Entering async_playwright")
        try:
            async with async_playwright() as p:
                self._log("[WORKER] Playwright async initialized")
                browser = await p.chromium.launch(headless=False)
                self._log("[WORKER] Browser launched")
                context = await browser.new_context(viewport={'width': 1280, 'height': 800})
                page = await context.new_page()
                self._log("[WORKER] Page ready")
                
                overlay_injected = False
                
                async def inject_overlay():
                    nonlocal overlay_injected
                    try:
                        await page.evaluate("""() => {
                            if (document.getElementById('zb-agent-overlay')) return;
                            document.body.style.boxShadow = 'inset 0 0 0 5px rgba(0, 120, 255, 0.7)';
                            const overlay = document.createElement('div');
                            overlay.id = 'zb-agent-overlay';
                            overlay.style.position = 'fixed'; overlay.style.top = '0'; overlay.style.left = '0';
                            overlay.style.width = '100vw'; overlay.style.height = '100vh';
                            overlay.style.backgroundColor = 'rgba(0, 0, 0, 0.3)';
                            overlay.style.zIndex = '2147483647'; overlay.style.display = 'none';
                            overlay.style.justifyContent = 'center'; overlay.style.alignItems = 'center';
                            overlay.style.pointerEvents = 'all';
                            const badge = document.createElement('div');
                            badge.style.backgroundColor = 'rgba(255,0,0,0.9)'; badge.style.color = 'white';
                            badge.style.padding = '15px 30px'; badge.style.borderRadius = '8px';
                            badge.style.fontFamily = 'sans-serif'; badge.style.fontSize = '24px'; badge.style.fontWeight = 'bold';
                            badge.innerText = '⚠️ Agent is controlling this page';
                            overlay.appendChild(badge); document.body.appendChild(overlay);
                        }""")
                        overlay_injected = True
                    except Exception:
                        pass

                async def set_blocking(is_blocked):
                    if not overlay_injected: await inject_overlay()
                    display = 'flex' if is_blocked else 'none'
                    try:
                        await page.evaluate(f"() => {{ const el = document.getElementById('zb-agent-overlay'); if(el) el.style.display = '{display}'; }}")
                    except Exception: pass
                    
                async def take_screenshot():
                    await inject_overlay()
                    await set_blocking(True)
                    await page.wait_for_timeout(500)
                    b = await page.screenshot(type='jpeg', quality=60)
                    await set_blocking(False)
                    return base64.b64encode(b).decode('utf-8')

                while True:
                    self._log("[WORKER] Waiting for command (asyncio.to_thread)...")
                    # Use a non-blocking poll or asyncio.to_thread so we don't block the event loop!
                    cmd, args = await asyncio.to_thread(self.cmd_queue.get)
                    self._log(f"[WORKER] Received cmd: {cmd}")
                    
                    if cmd == "quit":
                        break
                        
                    try:
                        if cmd == "goto":
                            url = args[0]
                            if not url.startswith("http") and not url.startswith("file://"): url = "https://" + url
                            self._log(f"[WORKER] Navigating to {url}")
                            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            self._log("[WORKER] Navigation complete, taking screenshot")
                            overlay_injected = False
                            b64 = await take_screenshot()
                            self._log("[WORKER] Screenshot complete, putting to queue")
                            self.res_queue.put({"status": "success", "url": page.url, "base64_image": b64})
                            
                        elif cmd == "click":
                            await set_blocking(True)
                            await page.click(args[0], timeout=5000, force=True)
                            await page.wait_for_timeout(500)
                            await set_blocking(False)
                            self.res_queue.put({"status": "success", "message": f"Clicked {args[0]}", "base64_image": await take_screenshot()})
                            
                        elif cmd == "type":
                            await set_blocking(True)
                            await page.fill(args[0], args[1], timeout=5000, force=True)
                            await page.wait_for_timeout(500)
                            await set_blocking(False)
                            self.res_queue.put({"status": "success", "message": f"Filled {args[0]} with '{args[1]}'", "base64_image": await take_screenshot()})
                            
                        elif cmd == "scroll":
                            await set_blocking(True)
                            amt = args[1]
                            if args[0].lower() == "up": amt = -amt
                            await page.mouse.wheel(0, amt)
                            await page.wait_for_timeout(500)
                            await set_blocking(False)
                            self.res_queue.put({"status": "success", "message": f"Scrolled {args[0]} by {args[1]}px", "base64_image": await take_screenshot()})
                            
                        elif cmd == "screenshot":
                            self.res_queue.put({"status": "success", "base64_image": await take_screenshot()})
                            
                    except Exception as e:
                        self._log(f"[WORKER] Command error: {str(e)}")
                        await set_blocking(False)
                        self.res_queue.put({"error": str(e)})

                self._log("[WORKER] Exiting naturally")
                self.res_queue.put({"status": "success", "message": "Browser closed"})
                
        except Exception as e:
            self._log(f"[WORKER] CRASHED: {str(e)}")
            while not self.cmd_queue.empty():
                self.cmd_queue.get()
            self.res_queue.put({"error": f"Browser engine crashed: {str(e)}"})

    async def goto(self, url: str):
        self._start_worker_if_needed()
        self.cmd_queue.put(("goto", [url]))
        return await asyncio.to_thread(self.res_queue.get)

    async def click(self, selector: str):
        self._start_worker_if_needed()
        self.cmd_queue.put(("click", [selector]))
        return await asyncio.to_thread(self.res_queue.get)

    async def type(self, selector: str, text: str):
        self._start_worker_if_needed()
        self.cmd_queue.put(("type", [selector, text]))
        return await asyncio.to_thread(self.res_queue.get)

    async def scroll(self, direction: str, amount: int = 500):
        self._start_worker_if_needed()
        self.cmd_queue.put(("scroll", [direction, amount]))
        return await asyncio.to_thread(self.res_queue.get)

    async def screenshot(self):
        self._start_worker_if_needed()
        self.cmd_queue.put(("screenshot", []))
        return await asyncio.to_thread(self.res_queue.get)

    async def close(self):
        if self.thread and self.thread.is_alive():
            self.cmd_queue.put(("quit", []))
            return await asyncio.to_thread(self.res_queue.get)
        return {"status": "success", "message": "Browser was not open"}

# Global instance
browser_manager = BrowserManager()
