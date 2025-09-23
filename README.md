# VAST - AI Database Architect & Operator

VAST is a persistent, conversational AI database agent that acts as your DBA/CTO. Unlike traditional AI assistants that forget everything after each session, VAST maintains complete context, can directly modify your database, and remembers all business rules and design decisions.

## 🚀 Key Features

- **Persistent Memory**: Remembers all conversations, decisions, and business rules across sessions
- **Direct Database Access**: Can execute DDL and DML operations directly (CREATE, ALTER, INSERT, UPDATE)
- **Natural Language Interface**: Talk to your database like you would to a senior DBA
- **Safety First**: Built-in guardrails, dry-run mode, and two-key write protection
- **Schema Awareness**: Automatically tracks and adapts to schema changes
- **Business Rule Enforcement**: Maintains and enforces your business rules consistently

## 🎯 The Problem VAST Solves

Traditional AI assistants (like ChatGPT) can help design databases but:
- ❌ Forget everything when the conversation ends
- ❌ Can't actually execute SQL
- ❌ Lose track of business rules and decisions
- ❌ Need the entire context re-explained every time

VAST solves this by being a **persistent database agent** that:
- ✅ Remembers everything permanently
- ✅ Directly modifies your database
- ✅ Enforces business rules consistently
- ✅ Maintains complete context across sessions

## 🛠️ Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/vast.git
cd vast
```

2. Create a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up your environment:
```bash
cp .env.example .env
# Edit .env with your database connection and OpenAI API key
# Set VAST_MASK_HOST_PORT=false to show real host:port in facts answers
```

## 💬 Usage

### Interactive Conversation Mode

Start a conversation with VAST:

```bash
python cli_chat.py
```

VAST will remember everything from previous sessions and can directly modify your database.

### One-Shot CLI Mode

For quick queries without conversation:

```bash
python cli.py ask "Show me the top 10 customers by revenue"
```

### REST API Service

Run the long-lived service that powers both the CLI and external frontends:

```bash
uvicorn src.vast.api:app --reload
```

The API exposes endpoints for health checks, schema inspection, agent-assisted planning/execution, and operational tasks like dumps/restores. Open <http://localhost:8000/docs> for interactive documentation once the server is running.

### Web Frontend Prototype

A lightweight browser UI is available under `frontend/`. Serve it with any static file server, then point it at the API base URL (defaults to `http://localhost:8000`).

```bash
python -m http.server --directory frontend 5173
# Visit http://localhost:5173 in your browser
```

### Programmatic Usage

```python
from src.vast.conversation import VastConversation

# Start or resume a session
vast = VastConversation("my_project")

# Natural language database operations
response = vast.process("Create a customer management system")

# Add persistent business rules
vast.add_business_rule("Never hard delete customer data")
vast.add_design_decision("Use UUID for all primary keys")
```

## 📚 Documentation

- [Full Documentation](VAST_README.md) - Detailed architecture and setup
- [Vision & Comparison](VAST_VISION_REALIZED.md) - How VAST differs from ChatGPT

## 🏗️ Architecture

```
User ↔️ Conversation Engine ↔️ Database
         ↓
    - Persistent Memory
    - Business Rules
    - Design Decisions
    - Schema Tracking
```

## 🔒 Safety Features

- **Read-only by default**: Writes require explicit flags
- **Two-key write system**: `--write` to allow, `--force-write` to execute
- **Dry-run mode**: Preview changes before execution
- **DDL protection**: Dangerous operations require confirmation
- **Parameterized queries**: Protection against SQL injection

## 🤝 Contributing

This is proprietary software owned by Sean Davey. All rights reserved. 
No contributions or modifications are permitted without express written authorization.

## 📄 License

**PROPRIETARY SOFTWARE - ALL RIGHTS RESERVED**

This software and all associated intellectual property is owned exclusively by Sean Davey. 
See [LICENSE](LICENSE) and [COPYRIGHT](COPYRIGHT) files for complete terms and conditions.

**NO LICENSE GRANTED** - This software is provided for personal use and evaluation only. 
Any reproduction, distribution, or commercial use is strictly prohibited without 
express written permission from Sean Davey.

## 🙏 Acknowledgments

Built to solve the problem of AI assistants with amnesia. Inspired by the need for a persistent, intelligent database layer that remembers your business context.

---

**VAST**: Your AI DBA that never forgets.
