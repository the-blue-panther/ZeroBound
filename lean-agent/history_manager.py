import os
import json
import uuid
from datetime import datetime

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "History")
os.makedirs(HISTORY_DIR, exist_ok=True)

class HistoryManager:
    @staticmethod
    def save_session(session_id, workspace, deepseek_url=None, title=None):
        """
        Saves a lightweight session record.
        Stores ONLY: workspace path, deepseek chat URL, title, and timestamp.
        No messages are saved — the user views conversation history in DeepSeek directly.
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        # Auto-generate title from workspace folder name
        if not title:
            title = os.path.basename(workspace.rstrip("/\\")) or workspace

        file_path = os.path.join(HISTORY_DIR, f"{session_id}.json")
        data = {
            "session_id": session_id,
            "workspace": workspace,
            "deepseek_url": deepseek_url,
            "title": title,
            "updated_at": datetime.now().isoformat(),
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return session_id

    @staticmethod
    def load_session(session_id):
        """Loads a session record by ID."""
        file_path = os.path.join(HISTORY_DIR, f"{session_id}.json")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    @staticmethod
    def list_sessions():
        """Returns all saved sessions sorted by most recent first."""
        sessions = []
        for filename in os.listdir(HISTORY_DIR):
            if filename.endswith(".json"):
                path = os.path.join(HISTORY_DIR, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        # Support both new and legacy formats
                        if "session_id" in data:
                            sessions.append(data)
                        elif "metadata" in data:
                            # Legacy: extract what we can
                            m = data["metadata"]
                            sessions.append({
                                "session_id": m.get("session_id"),
                                "workspace": m.get("workspace", "Unknown"),
                                "deepseek_url": None,
                                "title": os.path.basename(m.get("workspace", "Unknown").rstrip("/\\")),
                                "updated_at": m.get("updated_at", ""),
                            })
                except Exception:
                    continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    @staticmethod
    def delete_session(session_id):
        """Deletes a session record."""
        file_path = os.path.join(HISTORY_DIR, f"{session_id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
        return False
