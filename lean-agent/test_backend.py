import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    import agent_brain
    print("agent_brain imported successfully.")
    agent = agent_brain.LeanAgent()
    print("LeanAgent initialized successfully.")
except Exception as e:
    import traceback
    traceback.print_exc()
