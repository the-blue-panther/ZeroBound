from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright
import asyncio
import base64
import json
import mimetypes
import re
from config import MODEL_CONFIG, DEFAULT_MODEL

from contextlib import asynccontextmanager

# Global browser and contexts
playwright_instance = None
browser_instance = None
contexts = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_instance, contexts
    from playwright.async_api import async_playwright
    import os
    
    playwright_instance = await async_playwright().start()
    browser_instance = await playwright_instance.chromium.launch(
        headless=False,
        channel="msedge",
        args=["--start-maximized"]
    )
    
    for key, cfg in MODEL_CONFIG.items():
        state_path = f"{cfg['profile_dir']}/state.json"
        if not os.path.exists(state_path):
            print(f"⚠️ Skipping {key}: No session found at {state_path}")
            continue
            
        print(f"Loading browser for {key}...")
        contexts[key] = await browser_instance.new_context(
            storage_state=state_path,
            no_viewport=True
        )
        page = await contexts[key].new_page()
        print(f"Navigating to {key} chat...")
        await page.goto(cfg["url"], timeout=0)
        print(f"{key} ready and visible!")
        
    yield  # Server runs here
    
    # Shutdown logic
    print("🛑 Shutting down browser...")
    for context in contexts.values():
        await context.close()
    if browser_instance:
        await browser_instance.close()
    if playwright_instance:
        await playwright_instance.stop()

app = FastAPI(title="ZeroBound-Router", lifespan=lifespan)

async def find_and_act(page, selectors, action="fill", data=None):
    """Tries multiple selectors for an action."""
    for selector in selectors:
        try:
            # Short timeout for each attempt
            await page.wait_for_selector(selector, timeout=3000)
            loc = page.locator(selector)
            if action == "fill":
                await loc.fill("")
                await loc.fill(data)
            elif action == "click":
                await loc.click()
            return True
        except:
            continue
    return False

def extract_content_parts(content):
    """Normalizes OpenAI-style content into plain text and image URL parts."""
    text_parts = []
    image_urls = []

    if isinstance(content, list):
        for part in content:
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text", "")
                if text:
                    text_parts.append(text)
            elif part_type == "image_url":
                image_url = (part.get("image_url") or {}).get("url")
                if image_url:
                    image_urls.append(image_url)
    elif content is not None:
        text_parts.append(str(content))

    return "\n".join(text_parts).strip(), image_urls

def data_url_to_file_payload(data_url: str, index: int):
    """Converts a data URL into a Playwright file payload."""
    match = re.match(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", data_url, re.DOTALL)
    if not match:
        raise ValueError("Only base64 data URLs are supported for image upload.")

    mime_type = match.group("mime").lower()
    file_bytes = base64.b64decode(match.group("data"))
    ext = mimetypes.guess_extension(mime_type) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"

    return {
        "name": f"upload_{index}{ext}",
        "mimeType": mime_type,
        "buffer": file_bytes,
    }

async def upload_images(page, cfg, image_urls):
    """Uploads images into the active chat input before sending."""
    if not image_urls:
        return

    payloads = [data_url_to_file_payload(url, idx) for idx, url in enumerate(image_urls, start=1)]
    selectors = cfg.get("upload_selectors", ["input[type=\"file\"]"])
    last_error = None

    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception as e:
            last_error = e
            continue

        for idx in range(count):
            target = locator.nth(idx)
            try:
                accepts_multiple = await target.evaluate("(el) => !!el.multiple")
                if len(payloads) > 1 and not accepts_multiple:
                    last_error = RuntimeError(f"Selector '{selector}' does not accept multiple files.")
                    continue

                files = payloads if accepts_multiple else payloads[0]
                await target.set_input_files(files)
                await page.wait_for_timeout(cfg.get("upload_wait_ms", 2000))
                print(f"Uploaded {len(payloads)} image(s) using selector: {selector}")
                return
            except Exception as e:
                last_error = e

    if last_error:
        raise Exception(f"Could not upload image(s): {last_error}")
    raise Exception("Could not find any usable file input on the chat page.")

async def get_response(page, cfg, prompt: str, image_urls=None):
    print(f"🔍 Counting existing messages...")
    msg_count_before = await page.locator(cfg["response_container"]).count()

    if image_urls:
        print(f"🖼️ Uploading {len(image_urls)} image(s) before sending prompt...")
        await upload_images(page, cfg, image_urls)

    # Try all input selectors
    success = await find_and_act(page, cfg["input_selectors"], "fill", prompt)
    if not success:
        raise Exception("Could not find input box after trying all fallbacks.")

    # Try all send button selectors
    print("🚀 Sending message...")
    btn_success = await find_and_act(page, cfg["send_selectors"], "click")
    if not btn_success:
        await page.keyboard.press("Enter")

    print("⏳ Waiting for new response bubble...")
    # Wait for the message count to increase
    for _ in range(30):
        current_count = await page.locator(cfg["response_container"]).count()
        if current_count > msg_count_before:
            break
        await asyncio.sleep(1)
    
    # --- PHASE 2: Wait for Generation to START and FINISH ---
    print("⏳ Monitoring generation state...")
    
    # 1. Wait for "Typing" to start (max 5s)
    # Indicator: Attachment button becomes disabled
    if "attachment_selector" in cfg:
        started = False
        for _ in range(10): 
            is_disabled = await page.locator(cfg["attachment_selector"]).evaluate("(el) => el.classList.contains('ds-icon-button--disabled')")
            if is_disabled:
                started = True
                print("▶ Generation started.")
                break
            await asyncio.sleep(0.5)
        
        if started:
            # 2. Wait for "Typing" to finish (max 180s for huge files)
            print("⏳ Monitoring content for completion tags (</ACTION> or </REPORT>)...")
            for _ in range(360):
                is_disabled = await page.locator(cfg["attachment_selector"]).evaluate("(el) => el.classList.contains('ds-icon-button--disabled')")
                
                # Check for explicit termination tokens in the text
                try:
                    current_text = await page.locator(cfg["response_container"]).last.inner_text()
                    if "</ACTION>" in current_text or "</REPORT>" in current_text:
                        print("🎯 Detected completion tag. Generation looks complete.")
                        break
                except:
                    pass

                if not is_disabled:
                    print("✅ UI signal: Generation finished.")
                    break
                await asyncio.sleep(0.5)
        else:
            print("⚠️ Generation start signal not detected. Falling back to timer...")
    
    # --- PHASE 3: Stability Buffer ---
    print("⏳ Finalizing content...")
    last_text = ""
    stable_iterations = 0
    # Wait for up to 10 seconds of stability, checking every 0.5s
    # We require 4 consecutive stable checks (2.0s) to be sure DeepSeek finished its transition
    for _ in range(20): 
        await asyncio.sleep(0.5)
        try:
            current = await page.locator(cfg["response_container"]).last.inner_text()
            if current == last_text and current:
                stable_iterations += 1
            else:
                stable_iterations = 0
            
            if stable_iterations >= 4:
                # One final check: Is the attachment button still disabled?
                # If it's enabled, we are definitely done.
                if "attachment_selector" in cfg:
                    is_disabled = await page.locator(cfg["attachment_selector"]).evaluate("(el) => el.classList.contains('ds-icon-button--disabled')")
                    if not is_disabled:
                        print("✅ Generation stable and UI idle.")
                        break
                else:
                    print("✅ Content finalized.")
                    break
                    
            last_text = current
        except Exception as e:
            print(f"⚠️ Error during stabilization: {e}")
            break
            
    return last_text

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    data = await request.json()
    model_requested = data.get("model", DEFAULT_MODEL)
    print(f"📥 Received request for model: {model_requested}")
    
    backend_key = None
    for key, cfg in MODEL_CONFIG.items():
        if model_requested in cfg["model_name"] or model_requested == key:
            backend_key = key
            break
    if not backend_key:
        backend_key = DEFAULT_MODEL
    
    cfg = MODEL_CONFIG[backend_key]
    page = contexts[backend_key].pages[0]
    
    messages = data.get("messages", [])
    pending_images = []
    
    # Separate system prompt, history, and the latest user message
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    non_system = [m for m in messages if m.get("role") != "system"]
    
    # Count how many actual assistant turns have happened
    assistant_turns = sum(1 for m in non_system if m.get("role") == "assistant")
    
    if assistant_turns == 0:
        # === FIRST TURN: Inject system prompt inline so DeepSeek learns its role ===
        # Get the latest user message content
        latest_user = next((m for m in reversed(non_system) if m.get("role") == "user"), None)
        user_content = ""
        if latest_user:
            user_content, pending_images = extract_content_parts(latest_user.get("content", ""))

        system_content = system_msg.get("content", "") if system_msg else ""
        full_prompt = (
            f"[SYSTEM CONFIGURATION — read carefully before responding]\n"
            f"{system_content}\n"
            f"[END SYSTEM CONFIGURATION]\n\n"
            f"Now respond to this first user request:\n{user_content}"
        )
        print("🧠 First turn — injecting system prompt into message.")
    else:
        # === SUBSEQUENT TURNS: Send what happened since the last assistant message ===
        # DeepSeek already knows its role. We just need to give it the tool results 
        # or the new user Follow-Up message.
        last_assistant_idx = -1
        for i in range(len(messages)-1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                break
        
        new_messages = messages[last_assistant_idx+1:] if last_assistant_idx != -1 else messages
        
        prompt_parts = []
        for m in new_messages:
            role = m.get("role", "unknown").upper()
            content_str, content_images = extract_content_parts(m.get("content", ""))
            if role == "USER" and content_images:
                pending_images.extend(content_images)
                
            if role == "FUNCTION":
                tool_name = m.get("name", "unknown")
                prompt_parts.append(f"[TOOL RESULT for {tool_name}]:\n{content_str}\n\nWhat is your next step?")
            else:
                prompt_parts.append(f"[{role}]:\n{content_str}")
                
        full_prompt = "\n".join(prompt_parts)
        print(f"💬 Turn {assistant_turns + 1} — sending new updates to DeepSeek.")
    
    response_text = await get_response(page, cfg, full_prompt, image_urls=pending_images)

    
    return {
        "id": "chatcmpl-" + "web" + str(hash(response_text)),
        "object": "chat.completion",
        "created": int(asyncio.get_event_loop().time()),
        "model": model_requested,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": "stop"
        }]
    }

@app.get("/v1/current_url")
async def get_current_url():
    """Returns the current URL of the active DeepSeek browser page."""
    try:
        # Use the default model's context to get the current page URL
        backend_key = DEFAULT_MODEL
        page = contexts[backend_key].pages[0]
        url = page.url
        return {"url": url}
    except Exception as e:
        return {"url": None, "error": str(e)}

@app.post("/v1/navigate")
async def navigate_to_url(request: Request):
    """Navigates the DeepSeek browser page to a specific URL (to resume a previous chat)."""
    data = await request.json()
    target_url = data.get("url")
    if not target_url:
        return {"status": "error", "message": "No URL provided"}
    try:
        backend_key = DEFAULT_MODEL
        page = contexts[backend_key].pages[0]
        await page.goto(target_url, timeout=30000)
        print(f"🔗 Navigated browser to: {target_url}")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
