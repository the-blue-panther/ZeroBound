"""End-to-end test: send a message to DeepSeek, wait for response, 
find the copy button via the SVG clustering heuristic, click it, 
and verify that the clipboard interceptor captures raw markdown."""

import asyncio
from playwright.async_api import async_playwright

FIND_COPY_BTN_JS = """
() => {
    const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 &&
               window.getComputedStyle(el).display !== 'none' &&
               window.getComputedStyle(el).visibility !== 'hidden';
    };

    const editor = document.querySelector('#chat-input, textarea, [contenteditable="true"]');
    const maxY = editor ? editor.getBoundingClientRect().top : window.innerHeight - 150;

    const svgs = Array.from(document.querySelectorAll('svg'))
        .filter(visible)
        .filter(svg => svg.getBoundingClientRect().top < maxY - 10);
    
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
    
    rows.sort((a, b) => b.y - a.y);
    
    for (const row of rows) {
        if (row.items.length >= 2) {
            row.items.sort((a, b) => a.r.left - b.r.left);
            let targetSvg = row.items[0].svg;
            
            // Check for copy icon SVG path signature
            if (!targetSvg.innerHTML.includes('M9.672') && row.items.length > 1 && row.items[1].svg.innerHTML.includes('M9.672')) {
                targetSvg = row.items[1].svg;
            }

            return targetSvg.closest('button, div[role="button"], .ds-icon-button') || targetSvg;
        }
    }

    return null;
}
"""

INTERCEPT_CLIPBOARD_JS = """
() => {
    window.__zb_md = null;
    if (navigator.clipboard) {
        window.__zb_orig_write = navigator.clipboard.writeText;
        navigator.clipboard.writeText = async function(text) {
            window.__zb_md = text;
            return Promise.resolve();
        };
    }
    window.__zb_copy_listener = function(e) {
        if (e.clipboardData) {
            const text = e.clipboardData.getData('text/plain');
            if (text) window.__zb_md = text;
        }
    };
    document.addEventListener('copy', window.__zb_copy_listener, true);
}
"""

async def find_and_act(page, selectors, action, value=None):
    """Replicate server's find_and_act for the test."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                if action == "fill":
                    await loc.fill(value)
                elif action == "click":
                    await loc.click()
                return True
        except Exception:
            continue
    return False


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state='profiles/deepseek/state.json')
        page = await context.new_page()
        
        print("[1/6] Navigating to DeepSeek...")
        await page.goto('https://chat.deepseek.com/')
        await page.wait_for_load_state('networkidle')
        await asyncio.sleep(3)
        
        # Type a message using the same selectors as the real server
        input_selectors = ["textarea.ds-scroll-area", "textarea", "div[contenteditable='true']"]
        print("[2/6] Sending message...")
        if not await find_and_act(page, input_selectors, "fill", "Say hello in exactly 3 words"):
            print("FAIL: Could not find input box!")
            await browser.close()
            return
        
        # Send via Enter key
        await page.keyboard.press("Enter")
        
        # Wait for response to finish
        print("[3/6] Waiting for response to complete...")
        await asyncio.sleep(20)  # Give it time to generate
        
        # Debug: dump visible SVG row info
        svg_debug = await page.evaluate("""
            () => {
                const visible = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const editor = document.querySelector('#chat-input, textarea, [contenteditable="true"]');
                const maxY = editor ? editor.getBoundingClientRect().top : window.innerHeight - 150;
                
                const svgs = Array.from(document.querySelectorAll('svg'))
                    .filter(visible)
                    .filter(svg => svg.getBoundingClientRect().top < maxY - 10);
                
                const rows = [];
                for (const svg of svgs) {
                    const r = svg.getBoundingClientRect();
                    let added = false;
                    for (const row of rows) {
                        if (Math.abs(row.y - r.top) < 20) {
                            row.items.push({y: Math.round(r.top), x: Math.round(r.left), path: svg.innerHTML.substring(0, 80)});
                            added = true;
                            break;
                        }
                    }
                    if (!added) {
                        rows.push({y: Math.round(r.top), items: [{y: Math.round(r.top), x: Math.round(r.left), path: svg.innerHTML.substring(0, 80)}]});
                    }
                }
                
                rows.sort((a, b) => b.y - a.y);
                return "maxY=" + Math.round(maxY) + "\\n" + JSON.stringify(rows.slice(0, 5), null, 2);
            }
        """)
        print(f"[DEBUG] Bottom 5 SVG rows:\\n{svg_debug}")
        
        # Inject clipboard interceptor
        print("[4/6] Injecting clipboard interceptor...")
        await page.evaluate(INTERCEPT_CLIPBOARD_JS)
        
        # Find and click the copy button
        print("[5/6] Finding copy button...")
        btn_handle = await page.evaluate_handle(FIND_COPY_BTN_JS)
        btn_el = btn_handle.as_element() if btn_handle else None
        
        if not btn_el:
            print("FAIL: Could not find copy button!")
            
            # Save page for debugging
            html = await page.content()
            with open('test_page_dump.html', 'w', encoding='utf-8') as f:
                f.write(html)
            print("Saved page to test_page_dump.html")
            
            await browser.close()
            return
        
        # Debug: print what we found
        btn_html = await btn_el.evaluate("e => e.outerHTML.substring(0, 200)")
        print(f"  Found button: {btn_html}")
        
        print("  Clicking copy button...")
        await btn_el.scroll_into_view_if_needed()
        await btn_el.hover()
        await btn_el.click(delay=50)
        
        await asyncio.sleep(1.5)
        
        # Read captured markdown
        print("[6/6] Reading captured markdown...")
        markdown = await page.evaluate("window.__zb_md")
        
        if markdown:
            print(f"\\n=== SUCCESS: Captured {len(markdown)} chars of markdown ===")
            print(f"Content:\\n{markdown[:500]}")
        else:
            print("\\nFAIL: window.__zb_md is null/empty - clipboard intercept did not capture anything")
        
        await browser.close()

asyncio.run(main())
