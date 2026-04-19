import subprocess
import os
import json

# Commands that are always allowed without asking
SAFE_COMMANDS = [
    'dir', 'ls', 'pwd', 'echo', 'git status', 'git log', 
    'python --version', 'pip --version', 'npm --version', 'node --version'
]

# The active workspace (defaults to current project root)
# Fallback to current directory if __file__ is not available
try:
    CURRENT_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
except:
    CURRENT_WORKSPACE = os.getcwd()

IGNORED_TREE_DIRS = {
    '.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'
}

# Keep common dependency/cache folders visible as folders, but don't recursively
# expand them into the initial websocket payload.
TRIMMED_TREE_DIRS = {
    'venv', '.venv', 'node_modules', 'dist', 'build', 'target'
}

MAX_TREE_NODES = 2500

def requires_approval(tool_name, args):
    """Determines if a tool call requires user permission."""
    if tool_name in ["run_command", "run_shell_command"]:
        cmd = args.get("command", "").lower()
        # If it's not in the safe list, it needs approval
        return not any(cmd.startswith(safe) for safe in SAFE_COMMANDS)
    
    if tool_name == "write_file":
        # We ALWAYS require approval for writing files now
        return True
        
    return False

def get_diff(path, new_content):
    """Generates a text-based diff between the existing file and new content."""
    import difflib
    
    if not path or not isinstance(path, str):
        return f"--- (Missing or invalid path for diff: {path})\n+++ new content\n" + (new_content or "")

    full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    old_content = ""
    if os.path.exists(full_path):
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                old_content = f.read()
        except:
            pass
            
    if new_content is None:
        new_content = ""
    if not isinstance(new_content, str):
        new_content = str(new_content)
        
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile='original',
        tofile='proposed'
    )
    return "".join(diff)

def set_workspace(path: str):
    """Changes the active working directory for the agent."""
    global CURRENT_WORKSPACE
    abs_path = os.path.abspath(path)
    if os.path.exists(abs_path) and os.path.isdir(abs_path):
        CURRENT_WORKSPACE = abs_path
        return {"status": "success", "workspace": CURRENT_WORKSPACE}
    else:
        return {"error": f"Path '{path}' does not exist or is not a directory."}

def run_command(command: str):
    """Executes a shell command in the CURRENT_WORKSPACE."""
    # Note: We skip the safe check here because requires_approval handles the interlock.
    # If it reached here, it's either safe or approved.
    try:
        # Run in the active workspace
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120, cwd=CURRENT_WORKSPACE)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "code": result.returncode,
            "cwd": CURRENT_WORKSPACE
        }
    except Exception as e:
        return {"error": str(e)}

def write_file(path: str, content: str):
    """Writes content to a file relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str) or not isinstance(content, str):
        return {"error": "write_file requires both 'path' and 'content' to be strings."}
    try:
        # Resolve path
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(full_path)), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"status": "success", "path": full_path}
    except Exception as e:
        return {"error": str(e)}

def read_file(path: str):
    """Reads content from a file relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str):
        return {"error": "read_file requires 'path' to be a string."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        if not os.path.exists(full_path):
            return {"error": "File not found"}
        
        IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.svg'}
        ext = os.path.splitext(full_path)[1].lower()
        if ext in IMAGE_EXTS:
            import base64
            with open(full_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode('ascii')
            return {"content": b64, "is_image": True, "ext": ext.lstrip('.')}
        
        # Try encodings in order: utf-8-sig strips BOM, utf-16 handles Windows echo-created files
        for enc in ('utf-8-sig', 'utf-8', 'utf-16', 'latin-1'):
            try:
                with open(full_path, 'r', encoding=enc) as f:
                    content = f.read()
                return {"content": content}
            except (UnicodeDecodeError, UnicodeError):
                continue
        # Final fallback: read as binary and decode lossy
        with open(full_path, 'rb') as f:
            content = f.read().decode('utf-8', errors='replace')
        return {"content": content}
    except Exception as e:
        return {"error": str(e)}

def list_files(path: str = "."):
    """Lists files in a directory relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str):
        return {"error": "list_files requires 'path' to be a string."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        files = os.listdir(full_path)
        return {"files": files, "path": full_path}
    except Exception as e:
        return {"error": str(e)}

def get_file_tree(startpath: str = None):
    """Generates a recursive JSON tree of the workspace."""
    if startpath is None:
        startpath = CURRENT_WORKSPACE

    workspace_root = os.path.abspath(CURRENT_WORKSPACE)
    node_budget = {"remaining": MAX_TREE_NODES}
    
    def build_tree(path):
        if node_budget["remaining"] <= 0:
            return None

        name = os.path.basename(path)
        if not name: # Handle root case
            name = os.path.basename(path.rstrip(os.sep))

        if name in IGNORED_TREE_DIRS:
            return None

        abs_path = os.path.abspath(path)
        rel_path = "." if abs_path == workspace_root else os.path.relpath(abs_path, workspace_root)
        tree = {"name": name, "path": rel_path}
        node_budget["remaining"] -= 1
        
        if os.path.isdir(path):
            tree["type"] = "folder"
            if abs_path != workspace_root and name in TRIMMED_TREE_DIRS:
                tree["children"] = []
                tree["trimmed"] = True
                return tree

            children = []
            try:
                for entry in sorted(os.listdir(path)):
                    if node_budget["remaining"] <= 0:
                        tree["trimmed"] = True
                        break
                    child = build_tree(os.path.join(path, entry))
                    if child:
                        children.append(child)
            except (PermissionError, OSError):
                pass
            tree["children"] = children
        else:
            tree["type"] = "file"
            
        return tree

    return build_tree(startpath)

def reveal_in_os(path: str):
    """Opens the host OS file explorer and selects the file/folder."""
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        full_path = os.path.normpath(full_path)
        if os.path.exists(full_path):
            # Windows specific explorer /select
            subprocess.Popen(f'explorer /select,"{full_path}"')
            return {"status": "success", "path": full_path}
        else:
            return {"error": f"Path '{path}' does not exist."}
    except Exception as e:
        return {"error": str(e)}

def create_folder(path: str):
    """Creates a new directory relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str):
        return {"error": "create_folder requires 'path' to be a string."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        os.makedirs(full_path, exist_ok=True)
        return {"status": "success", "path": full_path}
    except Exception as e:
        return {"error": str(e)}

import asyncio
import subprocess
import threading

# Track the currently running subprocess so it can be killed on stop
current_process = None

async def run_command_async(command: str, callback=None):
    if callback:
        await callback({
            "type": "direct_terminal_result",
            "html": f"<div style='color:#3fb950;margin:10px 0 5px;'>▶ Executing: {command}</div>",
            "agent_controlled": True
        })
        
    loop = asyncio.get_running_loop()
    
    def run_proc():
        global current_process
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=CURRENT_WORKSPACE,
                text=False
            )
            current_process = process
            
            stdout_bytes = []
            stderr_bytes = []
            
            def stream_reader(stream, is_stderr):
                while True:
                    chunk = stream.read(1024)
                    if not chunk:
                        break
                    if is_stderr:
                        stderr_bytes.append(chunk)
                        if callback:
                            asyncio.run_coroutine_threadsafe(
                                callback({"type": "direct_terminal_result", "stderr": chunk.decode('utf-8', 'replace'), "agent_controlled": True}), 
                                loop
                            )
                    else:
                        stdout_bytes.append(chunk)
                        if callback:
                            asyncio.run_coroutine_threadsafe(
                                callback({"type": "direct_terminal_result", "stdout": chunk.decode('utf-8', 'replace'), "agent_controlled": True}), 
                                loop
                            )
            
            t1 = threading.Thread(target=stream_reader, args=(process.stdout, False), daemon=True)
            t2 = threading.Thread(target=stream_reader, args=(process.stderr, True), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            process.wait()
            
            return {
                "stdout": b"".join(stdout_bytes).decode('utf-8', 'replace'),
                "stderr": b"".join(stderr_bytes).decode('utf-8', 'replace'),
                "code": process.returncode,
                "cwd": CURRENT_WORKSPACE
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e) or repr(e)}

    result = await loop.run_in_executor(None, run_proc)
    
    if callback:
         await callback({
             "type": "direct_terminal_result",
             "html": f"<div style='color:#8a8a9a;margin-bottom:10px;'>Exit code: {result.get('code', 'N/A')}</div>",
             "agent_controlled": True
         })
         
    return result

async def handle_tool_call(tool_name, args, callback=None):
    """Main dispatcher for tool calls."""
    if tool_name == "set_workspace":
        return set_workspace(args.get("path"))
    elif tool_name == "create_folder":
        return create_folder(args.get("path"))
    elif tool_name in ["run_command", "run_shell_command"]:
        return await run_command_async(args.get("command"), callback)
    elif tool_name == "write_file":
        return write_file(args.get("path"), args.get("content"))
    elif tool_name == "append_file":
        path = args.get("path")
        content = args.get("content")
        if not path or content is None:
            return {"error": "Missing path or content"}
        
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            with open(full_path, "a", encoding="utf-8") as f:
                f.write(content)
            return {"status": "success", "message": f"Appended to {path}"}
        except Exception as e:
            return {"error": str(e)}
    elif tool_name == "read_file":
        return read_file(args.get("path"))
    elif tool_name == "list_files":
        return list_files(args.get("path", "."))
    elif tool_name == "reveal_in_os":
        return reveal_in_os(args.get("path"))
    else:
        return {"error": f"Tool {tool_name} not found"}

# Export tool definitions for LLM consumption
TOOLS = [
    {
        "name": "set_workspace",
        "description": "Change the active working directory for all subsequent tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the new workspace folder."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "run_shell_command",
        "description": "Run a shell command on Windows (cmd.exe). Use for installs, builds, git, etc. Do NOT use echo/redirect to write files — use write_file instead. One command per step.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."}
            },
            "required": ["command"]
        }
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file. Parent directories are created automatically — do NOT call create_folder first. When editing an existing file, read_file it first, then write_file with the COMPLETE corrected content. Use \\n for newlines in content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to workspace or absolute)."},
                "content": {"type": "string", "description": "Full text content to write. Use \\n for newlines."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "append_file",
        "description": "Append text to the end of an existing file. Use for assembling large files in chunks. Parent directories are created automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "Text to append. Use \\n for newlines."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file's content directly. Returns content or an error if missing — do NOT call list_files first to check existence. Just call this directly.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to workspace or absolute)."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "create_folder",
        "description": "Create a new directory. Path is relative to current workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Name or path of the folder to create."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory. Use ONLY for discovery when you don't know what files exist. Do NOT use as a prerequisite before read_file or write_file — those tools handle missing files gracefully.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (defaults to workspace root)."}
            }
        }
    }
]
