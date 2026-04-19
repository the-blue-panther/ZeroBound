from playwright.async_api import async_playwright
import asyncio
import sys

async def login(model_key: str):
    from config import MODEL_CONFIG
    cfg = MODEL_CONFIG[model_key]
    print(f"🚀 Opening browser for {model_key} login...")
    
    import os
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="msedge",
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto(cfg["url"], timeout=0)
        
        print(f"✅ Browser opened. Log in manually to {model_key}.")
        print("Once logged in and you see the chat page, press ENTER here...")
        input()
        
        print(f"💾 Saving session to {cfg['profile_dir']}")
        os.makedirs(cfg["profile_dir"], exist_ok=True)
        await context.storage_state(path=f"{cfg['profile_dir']}/state.json")
        await browser.close()
        print("✅ Done! You can close the browser.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python manual_login.py deepseek")
        sys.exit(1)
    asyncio.run(login(sys.argv[1]))
