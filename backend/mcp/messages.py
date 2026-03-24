# mcp/messages.py

class MCPMessage:
    def __init__(self, sender, recipient, action, payload):
        self.sender = sender
        self.recipient = recipient
        self.action = action
        self.payload = payload

class MCPResponse:
    def __init__(self, success, payload=None, error=None):
        self.success = success
        self.payload = payload
        self.error = error
