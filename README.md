# MCP Rocket.Chat Setup Guide

Follow these steps to set up and configure MCP Rocket.Chat on your local machine.

## 1. Clone the Repository

```bash
git clone git@github.com:elieworkspace/mcp-rocketchat.git
cd mcp-rocketchat
```

## 2. Start Rocket.Chat with Docker

```bash
docker compose up -d
```

Access Rocket.Chat at [http://localhost:3000](http://localhost:3000).

## 3. Initial Configuration

- Save your admin username and password.
- Navigate to [http://localhost:3000/admin/settings/Accounts#:rbb:](http://localhost:3000/admin/settings/Accounts#:rbb:)  
    Disable 2FA and click **Save**.

## 4. Set Up Python Environment

```bash
uv venv
.venv\Scripts\activate
uv add mcp[cli] httpx
```

## 5. Client Configuration Example

Add the following configuration to your client, replacing `-username-` and `-password-` with your credentials:

```json
{
    "mcpServers": {
        "mcp-rocketchat": {
            "command": "uv",
            "args": [
                "--directory",
                "C:/Users/-username-/Desktop/mcp-rocketchat",
                "run",
                "rocketchat.py",
                "--server-url", "http://localhost:3000",
                "--username", "-username-",
                "--password", "-password-"
            ]
        }
    }
}
```

---

**Tip:**  
Make sure Docker and Python are installed on your system before starting.

For more details, refer to the [Rocket.Chat documentation](https://docs.rocket.chat/).