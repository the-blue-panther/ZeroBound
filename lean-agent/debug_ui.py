import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        # Log all console messages
        page.on("console", lambda msg: print(f"CONSOLE [{msg.type}]: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        
        url = "file:///" + os.path.abspath(r"d:\Downloads\Projects\My Coding Agent\lean-agent\ui\index.html").replace('\\', '/')
        print(f"Navigating to {url}")
        
        try:
            await page.goto(url)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print("Failed to navigate:", e)
            
        await browser.close()

asyncio.run(run())
