"""
server_bridge.py – ZeroBound WebSocket bridge (v2.0)
=====================================================
- Manages agent lifecycle per WebSocket connection
- Handles approval, stop, session persistence
- Broadcasts workspace state
- Non‑blocking agent execution with proper task cancellation
"""

from __future__ import annotations
import asyncio
import json
import os
import sys
import subprocess
import socket
import re
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import httpx

# Force UTF-8 output on Windows
if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ROUTER_URL = "http://localhost:8000"

# ------------------------------------------------------------------------
# Path normalization helper for display
# ------------------------------------------------------------------------
def normalize_display_path(path: str) -> str:
    """Convert physical paths back to user-friendly Downloads paths for display."""
    if not path:
        return path
    
    # Handle D:\s\ style junction resolutions (case-insensitive)
    if "\\s\\" in path.lower() or "/s/" in path.lower():
        # Replace D:\s\ or D:/s/ with D:\Downloads\
        clean = re.sub(r'([A-Za-z]:)[/\\\\]s[/\\\\]', r'\1:\\Downloads\\', path, flags=re.IGNORECASE)
        # Also handle standalone s:\ or s:/ patterns
        clean = re.sub(r'[/\\\\]s[/\\\\]', r'\\Downloads\\', clean, flags=re.IGNORECASE)
        return clean
    return path

# ------------------------------------------------------------------------
# Helper to interact with the LLM‑web‑router
# ------------------------------------------------------------------------
async def fetch_deepseek_url() -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{ROUTER_URL}/v1/current_url")
            data = resp.json()
            return data.get("url")
    except Exception:
        return None

async def navigate_deepseek_to(url: str):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{ROUTER_URL}/v1/navigate", json={"url": url})
    except Exception:
        pass

# ------------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    from agent_brain import LeanAgent
    import tool_registry
    from history_manager import HistoryManager

    agent = LeanAgent()
    session_id: Optional[str] = None
    modified_files: set = set()
    agent_task: Optional[asyncio.Task] = None
    stop_event = asyncio.Event()

    # ── Helper functions ────────────────────────────────────
    async def broadcast_state():
        # Normalize workspace path for display
        display_path = normalize_display_path(tool_registry.CURRENT_WORKSPACE)
        await websocket.send_json({"type": "workspace_update", "path": display_path})
        
        # Get file tree (already handles paths internally)
        tree = tool_registry.get_file_tree()
        await websocket.send_json({"type": "file_tree", "tree": tree})
        await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

    async def auto_save():
        nonlocal session_id
        url = await fetch_deepseek_url()
        # Save with the normalized path for user display
        display_workspace = normalize_display_path(tool_registry.CURRENT_WORKSPACE)
        session_id = HistoryManager.save_session(
            session_id=session_id,
            workspace=display_workspace,  # Save display path, not physical path
            deepseek_url=url,
        )

    # ── Restore last workspace ──────────────────────────────
    try:
        recent = HistoryManager.list_sessions()
        if recent:
            last = recent[0]
            ws = last.get("workspace")
            if ws:
                # Enhanced path sanitization for restoration
                # First, try to fix D:\s\ patterns to D:\Downloads\
                if "\\s\\" in ws.lower() or "/s/" in ws.lower():
                    # Attempt a heuristic fix if common mapping is known
                    ws_clean = re.sub(r'([A-Za-z]:)[/\\\\]s[/\\\\]', r'\1:\\Downloads\\', ws, flags=re.IGNORECASE)
                    ws_clean = re.sub(r'[/\\\\]s[/\\\\]', r'\\Downloads\\', ws_clean, flags=re.IGNORECASE)
                    
                    # Check if the cleaned path exists
                    if os.path.isdir(ws_clean):
                        ws = ws_clean
                    else:
                        # Try the original path
                        if os.path.isdir(ws):
                            pass  # keep original
                        else:
                            ws = None
                elif os.path.isdir(ws):
                    pass  # keep as is
                else:
                    ws = None
                
                if ws and os.path.isdir(ws):
                    # Set workspace with sanitization
                    result = tool_registry.set_workspace(ws)
                    if "status" in result and result["status"] == "success":
                        session_id = last.get("session_id")
    except Exception as e:
        print(f"Error restoring workspace: {e}")

    await broadcast_state()

    # ── Agent callback streamer ─────────────────────────────
    async def agent_callback(data: Dict[str, Any]):
        # Normalize any paths in tool results for display
        if data.get("type") == "tool_result":
            result = data.get("result", {})
            
            # Normalize path fields in results
            if "path" in result and isinstance(result["path"], str):
                result["path"] = normalize_display_path(result["path"])
            if "from" in result and isinstance(result["from"], str):
                result["from"] = normalize_display_path(result["from"])
            if "to" in result and isinstance(result["to"], str):
                result["to"] = normalize_display_path(result["to"])
            if "file" in result and isinstance(result["file"], str):
                result["file"] = normalize_display_path(result["file"])
            
            # Track modified files with normalized paths
            if result.get("status") == "success" and "path" in result:
                try:
                    rel = os.path.relpath(result["path"], tool_registry.CURRENT_WORKSPACE)
                    # Store relative path (doesn't need normalization)
                    modified_files.add(rel)
                except Exception:
                    pass
            data["result"] = result
            data["modified_files"] = list(modified_files)
        
        # Normalize workspace updates
        elif data.get("type") == "workspace_update":
            if "path" in data:
                data["path"] = normalize_display_path(data["path"])
        
        await websocket.send_json(data)

        if data.get("type") == "tool_result":
            tool = data.get("tool")
            if tool in {"set_workspace", "write_file", "create_folder", "run_command", "run_shell_command"}:
                await broadcast_state()
        if data.get("type") == "final_response":
            await auto_save()
            await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

    # ── Main message loop ───────────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "message":
                user_msg = data.get("content", "")
                images = data.get("images", [])
                # Cancel any previous still‑running task
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                    try:
                        await agent_task
                    except asyncio.CancelledError:
                        pass
                # Start new agent task
                async def run_agent():
                    try:
                        await agent.run(user_msg, callback=agent_callback, images=images)
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        await websocket.send_json({"type": "error", "content": str(e)})
                agent_task = asyncio.create_task(run_agent())

            elif msg_type == "stop_agent":
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                    try:
                        await agent_task
                    except asyncio.CancelledError:
                        pass
                # Kill subprocesses
                for pid, info in list(tool_registry.active_processes.items()):
                    proc = info["process"]
                    if proc.poll() is None:
                        proc.kill()
                tool_registry.active_processes.clear()
                if tool_registry.browser_manager:
                    asyncio.create_task(tool_registry.browser_manager.close())
                # Resolve pending approvals
                if agent.pending_approval and not agent.pending_approval.done():
                    agent.pending_approval.set_result(False)
                agent.pending_approval = None
                await websocket.send_json({"type": "agent_stopped"})

            elif msg_type == "get_file":
                from tool_registry import read_file
                # Normalize path before reading
                file_path = data.get("path")
                if file_path:
                    # Clean the path for internal use
                    if "\\s\\" in file_path.lower() or "/s/" in file_path.lower():
                        file_path = re.sub(r'([A-Za-z]:)[/\\\\]s[/\\\\]', r'\1:\\s\\', file_path, flags=re.IGNORECASE)
                content = read_file(file_path)
                await websocket.send_json({"type": "file_content", "path": data.get("path"), "result": content})

            elif msg_type == "save_session":
                await auto_save()
                await websocket.send_json({"type": "session_saved", "workspace": normalize_display_path(tool_registry.CURRENT_WORKSPACE)})
                await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

            elif msg_type == "reset":
                for pid, info in list(tool_registry.active_processes.items()):
                    if info["process"].poll() is None:
                        info["process"].kill()
                tool_registry.active_processes.clear()
                if tool_registry.browser_manager:
                    asyncio.create_task(tool_registry.browser_manager.close())
                agent.reset()
                session_id = None
                modified_files.clear()
                await websocket.send_json({"type": "info", "content": "Agent session reset."})
                await broadcast_state()

            elif msg_type == "refresh_tree":
                await broadcast_state()

            elif msg_type == "pick_folder":
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
                folder = filedialog.askdirectory()
                root.destroy()
                if folder:
                    for pid, info in list(tool_registry.active_processes.items()):
                        if info["process"].poll() is None:
                            info["process"].kill()
                    tool_registry.active_processes.clear()
                    if tool_registry.browser_manager:
                        asyncio.create_task(tool_registry.browser_manager.close())
                    tool_registry.set_workspace(folder)
                    modified_files.clear()
                    session_id = None
                    agent.reset()
                    await broadcast_state()

            elif msg_type == "load_history":
                sid = data.get("session_id")
                saved = HistoryManager.load_session(sid)
                if saved:
                    session_id = sid
                    ws = saved.get("workspace")
                    ds_url = saved.get("deepseek_url")
                    
                    # Normalize workspace path
                    if ws:
                        # Convert display path back to physical path if needed
                        physical_ws = ws
                        if "Downloads" in ws:
                            physical_ws = re.sub(r'Downloads', 's', physical_ws, flags=re.IGNORECASE)
                        
                        # Try physical path first, fall back to display path
                        if os.path.isdir(physical_ws):
                            tool_registry.set_workspace(physical_ws)
                        elif os.path.isdir(ws):
                            tool_registry.set_workspace(ws)
                        else:
                            await websocket.send_json({
                                "type": "error",
                                "content": f"Workspace not found: {ws}"
                            })
                            continue
                    
                    modified_files.clear()
                    agent.reset()
                    if ds_url:
                        await navigate_deepseek_to(ds_url)
                        await websocket.send_json({
                            "type": "info",
                            "content": f"✅ Workspace restored: {normalize_display_path(tool_registry.CURRENT_WORKSPACE)}\n🔗 DeepSeek chat reopened: {ds_url}"
                        })
                    else:
                        await websocket.send_json({
                            "type": "info",
                            "content": f"✅ Workspace restored: {normalize_display_path(tool_registry.CURRENT_WORKSPACE)}\n⚠️ No DeepSeek URL saved."
                        })
                    await broadcast_state()

            elif msg_type == "delete_history":
                HistoryManager.delete_session(data.get("session_id"))
                await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

            elif msg_type == "direct_command":
                cmd = data.get("command", "").strip()
                if cmd.startswith("cd "):
                    target = cmd[3:].strip().strip('"').strip("'")
                    # Normalize path for cd command
                    if "\\s\\" in target.lower() or "/s/" in target.lower():
                        target = re.sub(r'([A-Za-z]:)[/\\\\]s[/\\\\]', r'\1:\\Downloads\\', target, flags=re.IGNORECASE)
                    new_path = os.path.normpath(os.path.join(tool_registry.CURRENT_WORKSPACE, target))
                    res = tool_registry.set_workspace(new_path)
                    if "error" in res:
                        await websocket.send_json({"type": "direct_terminal_result", "stderr": res["error"]})
                    else:
                        await websocket.send_json({"type": "direct_terminal_result", "stdout": f"Changed to {normalize_display_path(new_path)}\n"})
                else:
                    try:
                        proc = subprocess.run(cmd, shell=True, capture_output=True, encoding="utf-8",
                                            errors="replace", timeout=120, cwd=tool_registry.CURRENT_WORKSPACE)
                        await websocket.send_json({
                            "type": "direct_terminal_result",
                            "stdout": proc.stdout,
                            "stderr": proc.stderr,
                            "code": proc.returncode
                        })
                    except Exception as e:
                        await websocket.send_json({"type": "direct_terminal_result", "stderr": str(e)})
                await broadcast_state()

            elif msg_type == "approval_decision":
                decision = data.get("decision", False)
                if agent.pending_approval and not agent.pending_approval.done():
                    agent.pending_approval.set_result(decision)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Cleanup on disconnect
        if agent_task and not agent_task.done():
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass
        for pid, info in list(tool_registry.active_processes.items()):
            if info["process"].poll() is None:
                info["process"].kill()
        tool_registry.active_processes.clear()


# ------------------------------------------------------------------------
# Port‑picker and startup
# ------------------------------------------------------------------------
def pick_available_port(host: str, candidates: list) -> int:
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in {candidates}")

if __name__ == "__main__":
    import uvicorn
    host = "127.0.0.1"
    ports = [8001, 8010, 8011]
    port = pick_available_port(host, ports)
    if port != ports[0]:
        print(f"Port {ports[0]} busy; using {port}")
    uvicorn.run(app, host=host, port=port, reload=False)