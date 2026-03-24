# mcp/protocol.py

from mcp.messages import MCPMessage, MCPResponse

class MCPAgentBase:
    def __init__(self, manifest):
        self.manifest = manifest
        self.agent_name = manifest.get("agent_name", "unknown")

    def handle_message(self, msg: MCPMessage):
        action = msg.action
        payload = msg.payload

        if action == "generate_method":
            return self.generate_method(payload)
        elif action == "generate_test":
            return self.generate_test(payload)
        elif action == "ping":
            return MCPResponse(True, f"{self.agent_name} alive")
        elif action == "generate_page_file":
            return self.generate_page_file(msg.payload)
        else:
            return MCPResponse(False, error=f"Action '{action}' not supported by {self.agent_name}")

    def generate_method(self, element_spec):
        raise NotImplementedError

    def generate_test(self, test_case_spec):
        raise NotImplementedError

    def generate_page_file(self, page_spec):
        raise NotImplementedError
