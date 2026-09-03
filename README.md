# RecoverAI

Multi-agent payment-recovery system for Razorpay — automatically diagnoses
failed transactions, selects the optimal recovery channel, and sends
compliant recovery nudges with bounded discount offers.

---

## Agent-Extensibility (Q4)

### What is MCP?

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) is an open
standard that lets AI agents **discover and call tools** exposed by external
servers — without needing to know the server's internal code, language, or
architecture. Think of it as a USB-C port for AI: any MCP-compatible agent
(Claude, custom LangGraph orchestrators, or third-party systems) can plug
into any MCP server and immediately see what tools are available, what
inputs they expect, and whether they're safe to call.

### Why RecoverAI uses MCP

RecoverAI's core recovery actions (eligibility checks, retry-link
generation, message dispatch, and human escalation) are exposed as an MCP
server in [`action_mcp/server.py`](action_mcp/server.py). This means:

- **Any MCP-compatible agent can use them** — not just our built-in
  LangGraph pipeline. A developer can point Claude Desktop, or their own
  agent, at this server and immediately call recovery actions.
- **Tools are self-describing** — each tool has a Pydantic input schema
  with field-level descriptions, a docstring explaining when and why to
  call it, and MCP annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`) that tell calling agents which tools are safe to
  explore vs. which ones have real-world side effects.
- **Compliance is enforced server-side** — stopping rules (max attempts,
  cooldown, opt-out) and discount caps are checked inside the MCP tools,
  not in the calling agent. This means an external agent **cannot bypass
  safety rules** even if it tries to.

### MCP Tool Inventory

| Tool | Read/Write | Description |
|------|-----------|-------------|
| `recovery_check_eligibility` | Read-only | Check stopping rules (max attempts, cooldown, opt-out) before generating any recovery action. |
| `recovery_generate_retry_link` | Write | Generate a Razorpay-style payment retry link with an optional discount (hard-capped at 20%). |
| `recovery_send_message` | Write (destructive) | Send a recovery nudge via WhatsApp, SMS, email, or push notification. Enforces compliance internally. |
| `recovery_escalate_to_human` | Write (destructive, idempotent) | Mark a transaction for manual human follow-up when the agent can't or shouldn't resolve it autonomously. |

### Inspecting the MCP Server

You can interactively inspect all tools, schemas, and annotations using the
MCP Inspector:

```bash
cd action_mcp
npx @modelcontextprotocol/inspector python server.py
```

This opens a web UI where you can browse the tool list, view input schemas,
and make live test calls.

### Connecting to Claude Desktop

Add this to your `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "recoverai": {
      "command": "python",
      "args": ["<full-path-to>/action_mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop — the RecoverAI tools will appear in the chat, and
Claude can call them directly in conversation.

### Honest Scope Note

This MCP integration proves that RecoverAI's recovery actions are
**discoverable and callable** by any MCP-compatible agent — an external
agent can inspect the tool list, understand input schemas from the
descriptions alone, and execute recovery workflows without reading our
source code. It does **not** mean the system can autonomously rewrite its
own architecture or generate arbitrary new tools at runtime. The
extensibility is at the *tool-consumption* level: a developer (or an
AI coding assistant) can add new tools by following the existing pattern,
and any connected agent will immediately discover them.
