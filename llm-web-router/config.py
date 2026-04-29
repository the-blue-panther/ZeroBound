from typing import Dict

# Model registry - add new models here
MODEL_CONFIG: Dict[str, dict] = {
    "deepseek": {
        "url": "https://chat.deepseek.com/",
        "profile_dir": "profiles/deepseek",
        "input_selectors": [
            "textarea.ds-scroll-area",
            "textarea",
            "div[contenteditable='true']",
        ],
        "upload_selectors": [
            "input[type=\"file\"][accept*=\"image\"]",
            "input[type=\"file\"][accept*=\"png\"]",
            "input[type=\"file\"]",
        ],
        "send_selectors": ["button.ds-button--primary", "button[type=\"submit\"]"],
        "stop_selector": "div.ds-icon-button svg rect", # Square inside stop button
        "attachment_selector": "div.f02f0e25", # Paperclip button (gets disabled class)
        "response_container": "div.prose, div[class*=\"message\"]",
        "upload_wait_ms": 2000,
        "model_name": "deepseek-chat",
    },
    "claude": {
        "url": "https://claude.ai/",
        "profile_dir": "profiles/claude",
        "input_selectors": ["div[contenteditable=\"true\"]", "textarea"],
        "upload_selectors": [
            "input[type=\"file\"][accept*=\"image\"]",
            "input[type=\"file\"]",
        ],
        "send_selectors": ["button[aria-label*=\"Send\"]", "button.ds-button--primary"],
        "stop_selector": "button[aria-label*=\"Stop\"]",
        "response_container": "div.prose",
        "upload_wait_ms": 2000,
        "model_name": "claude-3-5-sonnet",
    },
}

DEFAULT_MODEL = "deepseek"
