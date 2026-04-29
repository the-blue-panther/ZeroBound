"""
server_bridge.py – ZeroBound WebSocket bridge (v2.2)
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

from agent_brain import LeanAgent, normalize_path_for_display
import tool_registry
from history_manager import HistoryManager
from tool_registry import read_file  # Moved outside loop

# Force UTF-8 output on Windows
if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ROUTER_URL = os.getenv("LLM_ROUTER_URL", "http://localhost:8000")
ALLOWED_COMMANDS = {"ls", "dir", "pwd", "echo", "git status", "git log", "python --version", "pip list", "pip install", "cd"}

# ------------------------------------------------------------------------
# Workspace Restoration Helper
# ------------------------------------------------------------------------
def restore_workspace_from_history(workspace: str) -> Optional[str]:
    """Sanitize and restore workspace from history."""
    if not workspace:
        return None

    # Fix D:\s\ (Windows kernel path) → D:\Downloads\
    if r"\\s\\" in workspace.lower() or r"\s\\" in workspace.lower():
        cleaned = re.sub(r'([A-Za-z]:)[/\\\\]s([/\\\\]|$)', r'\1\\Downloads\\', workspace, flags=re.IGNORECASE)
        cleaned = re.sub(r'[/\\\\]s[/\\\\]', r'\\Downloads\\', cleaned, flags=re.IGNORECASE)
        if os.path.isdir(cleaned):
            return cleaned

    # Try original path
    if os.path.isdir(workspace):
        return workspace

    return None

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

    agent = LeanAgent()
    session_id: Optional[str] = None
    modified_files: set = set()
    agent_task: Optional[asyncio.Task] = None
    stop_event = asyncio.Event()

    # --------------------------------------------------------------------
    async def broadcast_state():
        display_path = normalize_path_for_display(tool_registry.CURRENT_WORKSPACE)
        await websocket.send_json({"type": "workspace_update", "path": display_path})
        tree = tool_registry.get_file_tree()
        await websocket.send_json({"type": "file_tree", "tree": tree})
        await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

    async def auto_save():
        nonlocal session_id
        url = await fetch_deepseek_url()
        display_workspace = normalize_path_for_display(tool_registry.CURRENT_WORKSPACE)
        session_id = HistoryManager.save_session(
            session_id=session_id,
            workspace=display_workspace,
            deepseek_url=url,
        )

    # Restore last workspace
    try:
        recent = HistoryManager.list_sessions()
        if recent:
            last = recent[0]
            ws = restore_workspace_from_history(last.get("workspace"))
            if ws:
                result = tool_registry.set_workspace(ws)
                if "status" in result and result["status"] == "success":
                    session_id = last.get("session_id")
    except Exception as e:
        print(f"Error restoring workspace: {e}")

    await broadcast_state()

    # --------------------------------------------------------------------
    async def agent_callback(data: Dict[str, Any]):
        if data.get("type") == "tool_result":
            result = data.get("result", {})
            for key in ["path", "from", "to", "file"]:
                if key in result and isinstance(result[key], str):
                    result[key] = normalize_path_for_display(result[key])

            if result.get("status") == "success" and "path" in result:
                try:
                    rel = os.path.relpath(result["path"], tool_registry.CURRENT_WORKSPACE)
                    modified_files.add(rel)
                except Exception:
                    pass
            data["result"] = result
            data["modified_files"] = list(modified_files)

        elif data.get("type") == "workspace_update" and "path" in data:
            data["path"] = normalize_path_for_display(data["path"])

        await websocket.send_json(data)

        if data.get("type") == "tool_result":
            tool = data.get("tool")
            if tool in {"set_workspace", "write_file", "create_folder", "run_command", "run_shell_command"}:
                await broadcast_state()
        if data.get("type") == "final_response":
            await auto_save()
            await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

    # --------------------------------------------------------------------
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "message":
                user_msg, images = data.get("content", ""), data.get("images", [])
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                    try:
                        await agent_task
                    except asyncio.CancelledError:
                        pass

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
                for pid, info in list(tool_registry.active_processes.items()):
                    if info["process"].poll() is None:
                        info["process"].kill()
                tool_registry.active_processes.clear()
                if tool_registry.browser_manager:
                    asyncio.create_task(tool_registry.browser_manager.close())
                if agent.pending_approval and not agent.pending_approval.done():
                    agent.pending_approval.set_result(False)
                agent.pending_approval = None
                await websocket.send_json({"type": "agent_stopped"})

            elif msg_type == "get_file":
                file_path = data.get("path")
                if file_path and (r"\\s\\" in file_path.lower() or r"\s\\" in file_path.lower()):
                    file_path = re.sub(r'([A-Za-z]:)[/\\\\]s([/\\\\]|$)', r'\1\\Downloads\\', file_path, flags=re.IGNORECASE)
                content = read_file(file_path)
                await websocket.send_json({"type": "file_content", "path": data.get("path"), "result": content})

            elif msg_type == "save_session":
                await auto_save()
                await websocket.send_json({"type": "session_saved", "workspace": normalize_path_for_display(tool_registry.CURRENT_WORKSPACE)})
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
                await navigate_deepseek_to("https://chat.deepseek.com/")
                await websocket.send_json({"type": "info", "content": "Session reset."})
                await broadcast_state()

            elif msg_type == "refresh_tree":
                await broadcast_state()

            elif msg_type == "pick_folder":
                await websocket.send_json({"type": "request_folder_picker"})

            elif msg_type == "folder_selected":
                folder = data.get("path")
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
                    await navigate_deepseek_to("https://chat.deepseek.com/")
                    await broadcast_state()

            elif msg_type == "load_history":
                sid = data.get("session_id")
                saved = HistoryManager.load_session(sid)
                if saved:
                    session_id = sid
                    ws, ds_url = saved.get("workspace"), saved.get("deepseek_url")
                    if ws:
                        ws_to_load = restore_workspace_from_history(ws)
                        if ws_to_load:
                            tool_registry.set_workspace(ws_to_load)
                        else:
                            await websocket.send_json({"type": "error", "content": f"Not found: {ws}"})
                            continue
                    modified_files.clear()
                    agent.reset()
                    if ds_url:
                        await navigate_deepseek_to(ds_url)
                    await websocket.send_json({"type": "info", "content": f"Restored: {normalize_path_for_display(tool_registry.CURRENT_WORKSPACE)}"})
                    await broadcast_state()

            elif msg_type == "delete_history":
                HistoryManager.delete_session(data.get("session_id"))
                await websocket.send_json({"type": "history_list", "sessions": HistoryManager.list_sessions()})

            elif msg_type == "direct_command":
                cmd = data.get("command", "").strip()
                if cmd.startswith("cd "):
                    target = cmd[3:].strip().strip('"').strip("'")
                    if r"\\s\\" in target.lower() or r"\s\\" in target.lower():
                        target = re.sub(r'([A-Za-z]:)[/\\\\]s([/\\\\]|$)', r'\1\\Downloads\\', target, flags=re.IGNORECASE)
                    new_path = os.path.normpath(os.path.join(tool_registry.CURRENT_WORKSPACE, target))
                    res = tool_registry.set_workspace(new_path)
                    if "error" in res:
                        await websocket.send_json({"type": "direct_terminal_result", "stderr": res["error"]})
                    else:
                        await websocket.send_json({"type": "direct_terminal_result", "stdout": f"Changed to {normalize_path_for_display(new_path)}\n"})
                else:
                    cmd_full = data.get("command", "").strip()
                    if not cmd_full:
                        continue
                    base_cmd = cmd_full.split()[0].lower() if cmd_full else ""
                    dangerous = ["rm -rf", "del /f", "format", ">", "|", "&"]
                    if base_cmd not in ALLOWED_COMMANDS or any(p in cmd_full.lower() for p in dangerous):
                        await websocket.send_json({"type": "direct_terminal_result", "stderr": f"Rejected: {cmd_full}"})
                        continue
                    try:
                        proc = subprocess.run(cmd_full, shell=True, capture_output=True, encoding="utf-8", errors="replace", timeout=120, cwd=tool_registry.CURRENT_WORKSPACE)
                        await websocket.send_json({"type": "direct_terminal_result", "stdout": proc.stdout, "stderr": proc.stderr, "code": proc.returncode})
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


def pick_available_port(host: str, candidates: list) -> int:
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No port: {candidates}")


if __name__ == "__main__":
    import uvicorn
    host, ports = "127.0.0.1", [8001, 8010, 8011]
    port = pick_available_port(host, ports)
    if port != ports[0]:
        print(f"Port {ports[0]} busy; using {port}")
    uvicorn.run(app, host=host, port=port, reload=False)