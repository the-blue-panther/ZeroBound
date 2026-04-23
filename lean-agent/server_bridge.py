from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from agent_brain import LeanAgent
from history_manager import HistoryManager
import json
import asyncio
import os
import socket
import httpx  # For talking to llm-web-router

import io
import sys

# Windows Terminal UTF-8 Emoji Support Patch
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Agent will be initialized on-demand within the WebSocket connection
# to prevent heavy Litellm imports from slowing down uvicorn reloads.
agent = None 

# URL of the llm-web-router server
ROUTER_URL = "http://localhost:8000"


async def fetch_deepseek_url() -> str | None:
    """Fetches the current DeepSeek page URL from the llm-web-router."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{ROUTER_URL}/v1/current_url")
            data = resp.json()
            url = data.get("url")
            # Only return actual chat URLs (not the base landing page)
            if url and "/chat/" in url:
                return url
            return url
    except Exception as e:
        print(f"⚠️ Could not fetch DeepSeek URL: {e}")
        return None


async def navigate_deepseek_to(url: str):
    """Tells the llm-web-router to navigate its browser to the given URL."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{ROUTER_URL}/v1/navigate", json={"url": url})
        print(f"[INFO] Navigated DeepSeek browser to: {url}")
    except Exception as e:
        print(f"[ERROR] Could not navigate DeepSeek browser: {e}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    import tool_registry
    from agent_brain import LeanAgent
    
    print(f"[INFO] WebSocket client connected. Initializing agent...")
    agent = LeanAgent() # Initialize a fresh agent for each connection
    
    session_id = None
    modified_files = set()
    current_deepseek_url = None  # Tracks the live DeepSeek chat URL
    agent_task = None            # Tracks the running agent asyncio.Task

    # ── Broadcast workspace state + history list ──────────────────────────
    async def broadcast_state():
        tree = tool_registry.get_file_tree()
        await websocket.send_json({"type": "workspace_update", "path": tool_registry.CURRENT_WORKSPACE})
        await websocket.send_json({"type": "file_tree", "tree": tree})
        sessions = HistoryManager.list_sessions()
        await websocket.send_json({"type": "history_list", "sessions": sessions})

    # ── Save session (workspace + DeepSeek URL only, no messages) ─────────
    async def auto_save():
        nonlocal session_id, current_deepseek_url
        # Always grab the freshest URL from the router
        url = await fetch_deepseek_url()
        if url:
            current_deepseek_url = url

        session_id = HistoryManager.save_session(
            session_id=session_id,
            workspace=tool_registry.CURRENT_WORKSPACE,
            deepseek_url=current_deepseek_url,
        )
        print(f"[SAVE] Session saved: {session_id} | URL: {current_deepseek_url}")

    # ── Auto-restore last workspace on connect ───────────────────────────
    try:
        recent_sessions = HistoryManager.list_sessions()
        if recent_sessions:
            last = recent_sessions[0]  # Most recently updated
            last_ws = last.get("workspace", "")
            if last_ws and os.path.isdir(last_ws):
                tool_registry.CURRENT_WORKSPACE = last_ws
                session_id = last.get("session_id")  # Resume same session ID
                print(f"[AUTO-RESTORE] Workspace: {last_ws}")
    except Exception as e:
        print(f"[ERROR] Could not auto-restore workspace: {e}")

    # ── Initial sync ──────────────────────────────────────────────────────
    await broadcast_state()

    # ── Agent callback: streams events to UI ─────────────────────────────
    async def agent_callback(data):
        if data.get("type") == "tool_result":
            result = data.get("result") or {}
            if result.get("status") == "success" and "path" in result:
                try:
                    rel = os.path.relpath(result["path"], tool_registry.CURRENT_WORKSPACE)
                    modified_files.add(rel)
                except Exception:
                    pass
            data["modified_files"] = list(modified_files)

        await websocket.send_json(data)

        tool = data.get("tool")
        if data.get("type") == "tool_result" and tool in [
            "set_workspace", "write_file", "create_folder",
            "run_command", "run_shell_command"
        ]:
            await broadcast_state()

        # Save after every final agent response
        if data.get("type") == "final_response":
            await auto_save()
            sessions = HistoryManager.list_sessions()
            await websocket.send_json({"type": "history_list", "sessions": sessions})

    # ── Main message loop ─────────────────────────────────────────────────
    while True:
        try:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # ── User sends a chat message ─────────────────────────────────
            if data.get("type") == "message":
                user_msg = data.get("content", "")
                images = data.get("images", [])
                
                async def run_agent_task():
                    nonlocal agent_task
                    try:
                        await agent.run(user_msg, callback=agent_callback, images=images)
                        await auto_save()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        print(f"Agent Task Error: {e}")
                        
                agent_task = asyncio.create_task(run_agent_task())

            # ── Stop the running agent + subprocess ───────────────────────
            elif data.get("type") == "stop_agent":
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                agent_task = None
                
                # Kill all running subprocesses
                import tool_registry as _tr
                for pid, info in list(_tr.active_processes.items()):
                    process = info["process"]
                    if process.poll() is None:
                        try:
                            process.kill()
                        except Exception:
                            pass
                _tr.active_processes.clear()
                
                if _tr.browser_manager:
                    asyncio.create_task(_tr.browser_manager.close())
                
                # Resolve any pending approval so the agent doesn't ghost
                if agent.pending_approval and not agent.pending_approval.done():
                    agent.pending_approval.set_result(False)
                agent.pending_approval = None
                
                # Tell the UI we stopped cleanly
                await websocket.send_json({"type": "agent_stopped"})
                print("[STOP] Agent stopped by user.")

            # ── Open a file in the editor ─────────────────────────────────
            elif data.get("type") == "get_file":
                from tool_registry import read_file
                path = data.get("path")
                content = read_file(path)
                await websocket.send_json({"type": "file_content", "path": path, "result": content})

            # ── Manual session save ───────────────────────────────────────
            elif data.get("type") == "save_session":
                await auto_save()
                sessions = HistoryManager.list_sessions()
                await websocket.send_json({"type": "session_saved", "workspace": tool_registry.CURRENT_WORKSPACE})
                await websocket.send_json({"type": "history_list", "sessions": sessions})

            # ── Reset agent session ───────────────────────────────────────
            elif data.get("type") == "reset":
                import tool_registry as _tr
                for pid, info in list(_tr.active_processes.items()):
                    if info["process"].poll() is None:
                        try: info["process"].kill()
                        except: pass
                _tr.active_processes.clear()
                if _tr.browser_manager:
                    asyncio.create_task(_tr.browser_manager.close())
                agent.reset()
                session_id = None
                current_deepseek_url = None
                modified_files.clear()
                await websocket.send_json({"type": "info", "content": "Agent session reset."})
                await broadcast_state()

            # ── Refresh Explorer Tree ─────────────────────────────────────
            elif data.get("type") == "refresh_tree":
                await broadcast_state()

            # ── Pick a workspace folder via dialog ────────────────────────
            elif data.get("type") == "pick_folder":
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
                folder_path = filedialog.askdirectory()
                root.destroy()
                if folder_path:
                    import tool_registry as _tr
                    for pid, info in list(_tr.active_processes.items()):
                        if info["process"].poll() is None:
                            try: info["process"].kill()
                            except: pass
                    _tr.active_processes.clear()
                    if _tr.browser_manager:
                        asyncio.create_task(_tr.browser_manager.close())
                    tool_registry.set_workspace(folder_path)
                    modified_files.clear()
                    session_id = None          # Fresh session for new workspace
                    current_deepseek_url = None
                    agent.reset()
                    await broadcast_state()

            # ── Load a saved history session ──────────────────────────────
            elif data.get("type") == "load_history":
                sid = data.get("session_id")
                saved = HistoryManager.load_session(sid)
                if saved:
                    session_id = sid
                    ws_path = saved.get("workspace")
                    deepseek_url = saved.get("deepseek_url")

                    # Restore workspace
                    tool_registry.set_workspace(ws_path)
                    modified_files.clear()

                    # Reset agent (fresh context — conversation lives in DeepSeek)
                    agent.reset()

                    # Navigate the DeepSeek browser to the saved chat URL
                    if deepseek_url:
                        current_deepseek_url = deepseek_url
                        await navigate_deepseek_to(deepseek_url)
                        await websocket.send_json({
                            "type": "info",
                            "content": f"✅ Workspace restored: {ws_path}\n🔗 DeepSeek chat reopened: {deepseek_url}"
                        })
                    else:
                        await websocket.send_json({
                            "type": "info",
                            "content": f"✅ Workspace restored: {ws_path}\n⚠️ No DeepSeek URL saved for this session."
                        })

                    await broadcast_state()

            # ── Delete a history session ──────────────────────────────────
            elif data.get("type") == "delete_history":
                sid = data.get("session_id")
                HistoryManager.delete_session(sid)
                sessions = HistoryManager.list_sessions()
                await websocket.send_json({"type": "history_list", "sessions": sessions})

            # ── Direct terminal command ───────────────────────────────────
            elif data.get("type") == "direct_command":
                cmd = data.get("command", "").strip()
                if cmd.startswith("cd "):
                    target = cmd[3:].strip().strip('"').strip("'")
                    new_path = os.path.normpath(os.path.join(tool_registry.CURRENT_WORKSPACE, target))
                    res = tool_registry.set_workspace(new_path)
                    if "error" in res:
                        await websocket.send_json({"type": "direct_terminal_result", "stderr": res["error"]})
                    else:
                        await websocket.send_json({"type": "direct_terminal_result", "stdout": f"Changed directory to {new_path}\n"})
                else:
                    try:
                        import subprocess
                        result = subprocess.run(
                            cmd, shell=True, capture_output=True, encoding='utf-8', errors='replace',
                            timeout=120, cwd=tool_registry.CURRENT_WORKSPACE
                        )
                        await websocket.send_json({
                            "type": "direct_terminal_result",
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "code": result.returncode
                        })
                    except Exception as e:
                        await websocket.send_json({"type": "direct_terminal_result", "stderr": str(e)})
                await broadcast_state()

            # ── Approval decision for write_file review ───────────────────
            elif data.get("type") == "approval_decision":
                decision = data.get("decision", False)
                if agent.pending_approval and not agent.pending_approval.done():
                    agent.pending_approval.set_result(decision)

        except Exception as e:
            print(f"WS Error: {e}")
            break


if __name__ == "__main__":
    import uvicorn

    def pick_available_port(host: str, candidates: list[int]) -> int:
        for port in candidates:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind((host, port))
                    return port
                except OSError:
                    continue
        raise RuntimeError(f"No free port found in {candidates}")

    # Run without the auto-reloader in normal use. On Windows, reload mode can
    # interrupt websocket clients by bouncing the worker process.
    host = "127.0.0.1"
    preferred_ports = [8001, 8010, 8011]
    port = pick_available_port(host, preferred_ports)
    if port != preferred_ports[0]:
        print(f"[INFO] Port {preferred_ports[0]} is busy; starting bridge on http://{host}:{port} instead.")
    uvicorn.run(app, host=host, port=port, reload=False)
