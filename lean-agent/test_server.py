import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import asyncio
import uvicorn
from server_bridge import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8005)
