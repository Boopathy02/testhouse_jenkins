# orchestrator/orchestrator.py

from mcp.messages import MCPMessage
from agents.python_agent import PlaywrightPythonAgent
from agents.typescript_agent import PlaywrightTypescriptAgent
import json

with open("agents/agent_manifest_python.json") as f:
    python_manifest = json.load(f)
with open("agents/agent_manifest_typescript.json") as f:
    ts_manifest = json.load(f)

AGENTS = {
    "python": PlaywrightPythonAgent(python_manifest),
    "typescript": PlaywrightTypescriptAgent(ts_manifest)
}

def send_message(language, action, payload):
    agent = AGENTS[language]
    msg = MCPMessage(sender="orchestrator", recipient=agent.agent_name, action=action, payload=payload)
    resp = agent.handle_message(msg)
    return resp
