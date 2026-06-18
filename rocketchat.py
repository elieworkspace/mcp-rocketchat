import argparse
import asyncio
import logging
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from typing import Any
import httpx
from mcp.server.fastmcp import FastMCP

# Configure logging
def setup_logging():
    log_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(log_dir, 'rocketchat_mcp.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=1, encoding='utf-8'),
            logging.StreamHandler()  # stderr; the stdio protocol uses stdout and is unaffected
        ]
    )
    
    logger = logging.getLogger("RocketChatMCP")
    logger.info(f"Logging initialized. Log file: {log_file}")
    return logger

# Initialize logging
main_logger = setup_logging()

# Initialize FastMCP server
mcp = FastMCP("rocketchat")
main_logger.info("FastMCP server initialized with name 'rocketchat'")

# Global RocketChat client
rocket_client = None

class RocketChatAPI:
    def __init__(self, server_url, username=None, password=None, user_id=None, auth_token=None, verify_ssl=True):
        self.server_url = server_url.rstrip('/')
        self.logger = logging.getLogger("RocketChatAPI")
        self.auth_token = None
        self.user_id = None
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self._client = None  # persistent AsyncClient, lazily created, reused for connection pooling
        self._room_endpoint = {}  # roomId -> discovered *.messages endpoint, avoids re-probing on repeated reads
        self._room_id_cache = {}  # roomId or room name -> roomId (read/search endpoints only accept roomId)
        
        self.logger.info(f"Initializing RocketChat API client for server: {self.server_url}")
        
        if auth_token and user_id:
            self.auth_token = auth_token
            self.user_id = user_id
            self.logger.info("Initialized with auth token and user ID")
        elif username and password:
            self.logger.info(f"Will login with username: {username}")
            # Don't create task here, login will be called explicitly
        else:
            self.logger.error("No valid authentication method provided")
            raise ValueError("Provide either username/password or user_id/auth_token")

    async def login(self, username, password):
        url = f"{self.server_url}/api/v1/login"
        self.logger.info(f"Attempting login for user: {username}")
        
        async with httpx.AsyncClient(verify=self.verify_ssl) as client:
            try:
                response = await client.post(url, json={"user": username, "password": password})
                response.raise_for_status()
                data = response.json()
                
                if data.get('status') != 'success':
                    self.logger.error(f"Login failed: {data} (url={url})")
                    raise Exception("Login failed")
                
                self.auth_token = data['data']['authToken']
                self.user_id = data['data']['userId']
                self.logger.info(f"Login successful for user: {username}, user_id: {self.user_id}")
                
            except Exception as e:
                self.logger.error(f"Login error: {e} (url={url})")
                raise

    def _headers(self):
        return {
            'X-Auth-Token': self.auth_token,
            'X-User-Id': self.user_id,
            'Content-type': 'application/json'
        }

    def _auth_headers(self):
        return {
            'X-Auth-Token': self.auth_token,
            'X-User-Id': self.user_id,
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0)
        return self._client

    async def async_request(self, method: str, endpoint: str, json_data=None, params=None):
        """Make async HTTP request to RocketChat API"""
        url = f"{self.server_url}/api/v1/{endpoint}"
        self.logger.info(f"Making {method} request to {endpoint}")

        try:
            response = await self._get_client().request(
                method, url,
                headers=self._headers(),
                json=json_data,
                params=params,
            )
            response.raise_for_status()
            result = response.json()
            self.logger.info(f"Request {method} {endpoint} successful")
            return result
        except Exception as e:
            self.logger.error(f"{method} {endpoint} error: {e}")
            raise

    async def resolve_room_id(self, room: str) -> str:
        """Resolve a roomId or room name to a roomId (*.messages / chat.search only
        accept roomId, while chat.postMessage accepts both — rooms are often
        referred to by name)."""
        cached = self._room_id_cache.get(room)
        if cached:
            return cached
        for param_key in ("roomId", "roomName"):
            try:
                result = await self.async_request("GET", "rooms.info", params={param_key: room})
                rid = result.get("room", {}).get("_id")
                if rid:
                    self._room_id_cache[room] = rid
                    return rid
            except Exception:
                continue
        return room  # resolution failed: pass through and let the downstream endpoint report the actual error

    async def fetch_room_messages(self, room: str, params: dict):
        """Fetch messages by roomId or room name, probing the channels/groups/im
        endpoints and caching the working one to avoid wasted requests on
        repeated reads."""
        room_id = await self.resolve_room_id(room)
        cached = self._room_endpoint.get(room_id)
        endpoints = [cached] if cached else ["channels.messages", "groups.messages", "im.messages"]

        last_error = None
        for endpoint in endpoints:
            try:
                result = await self.async_request(
                    "GET", endpoint, params={"roomId": room_id, **params}
                )
                self._room_endpoint[room_id] = endpoint
                return result
            except httpx.HTTPStatusError as e:
                # 401/403 cannot be fixed by trying another endpoint (bad token /
                # no permission): fail fast instead of amplifying into a chain
                # of doomed requests
                if e.response.status_code in (401, 403):
                    raise
                last_error = e
                continue
            except httpx.TransportError:
                raise  # transport failures are unrelated to endpoint type; retrying others is pointless
            except Exception as e:
                last_error = e
                continue

        # cached endpoint went stale (e.g. room type changed): clear it and re-probe once
        if cached:
            self._room_endpoint.pop(room_id, None)
            return await self.fetch_room_messages(room_id, params)
        raise last_error if last_error else Exception(f"room {room} not found")

def format_message_line(msg: dict, indent: str = "") -> str:
    """Uniform single-message rendering: timestamp + id + user + text +
    attachment captions / file hints. Fields may be null (not just absent), so
    use `or` fallbacks to keep one bad message from breaking the whole page."""
    ts = msg.get('ts', 'N/A')
    user = (msg.get('u') or {}).get('username', 'Unknown')
    text = msg.get('msg', '')
    msg_id = msg.get('_id', 'N/A')
    line = f"{indent}[{ts}] (id: {msg_id}) {user}: {text}"
    for att in msg.get('attachments') or []:
        desc = (att.get('description') or '').strip()
        if desc:
            line += f"\n{indent}  💬 {desc}"
    files = msg.get('files') or []
    if not files and msg.get('file'):
        files = [msg['file']]
    for f in files:
        fname = f.get('name', 'unknown')
        ftype = f.get('type', '')
        line += f"\n{indent}  📎 {fname} ({ftype}) [use download_attachment with id: {msg_id}]"
    return line


@mcp.tool()
async def list_users() -> str:
    """List all users available to the user."""
    main_logger.info("list_users called")
    
    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"
    
    try:
        result = await rocket_client.async_request("GET", "users.list")
        if result.get('success') and 'users' in result:
            users = result['users']
            main_logger.info(f"Retrieved {len(users)} users")
            
            if not users:
                return "No users found"
            
            # return username, emails, name
            users_list = ""
            for user in users:
                user_info = f"Username: {user.get('username', 'N/A')}\nEmail: {user.get('emails', [{}])[0].get('address', 'N/A') if user.get('emails') else 'N/A'}\nName: {user.get('name', 'N/A')}\n"
                users_list += user_info
            return "Available users:\n" + users_list
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to list users: {error_msg}")
            return f"Failed to list users: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error listing users: {str(e)}")
        return f"Error listing users: {str(e)}"


@mcp.tool()
async def send_message_in_channel(channel: str, text: str) -> str:
    """Send a message to a RocketChat channel.

    Args:
        channel: Channel name (e.g., 'general') or channel ID
        text: Message text to send
    """
    main_logger.info(f"send_message_in_channel called - channel: {channel}, text length: {len(text)}")
    
    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"
    
    try:
        result = await rocket_client.async_request(
            "POST", "chat.postMessage",
            json_data={"channel": channel, "text": text}
        )
        if result.get('success'):
            msg = result.get('message', {})
            msg_id = msg.get('_id', 'N/A')
            rid = msg.get('rid', 'N/A')
            main_logger.info(f"Message sent successfully to channel: {channel}")
            return f"Message sent to {channel} (msgId: {msg_id}, roomId: {rid})"
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to send message to {channel}: {error_msg}")
            return f"Failed to send message: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error sending message to {channel}: {str(e)}")
        return f"Error sending message: {str(e)}"

@mcp.tool()
async def send_direct_message(username: str, text: str) -> str:
    """Send a direct message to a user.

    Args:
        username: Username of the recipient
        text: Message text to send
    """
    main_logger.info(f"send_direct_message called - to: {username}, text length: {len(text)}")

    if not rocket_client:
        return "RocketChat client not initialized"

    try:
        result = await rocket_client.async_request(
            "POST", "chat.postMessage",
            json_data={"channel": f"@{username}", "text": text}
        )
        if result.get('success'):
            msg = result.get('message', {})
            msg_id = msg.get('_id', 'N/A')
            rid = msg.get('rid', 'N/A')
            main_logger.info(f"Direct message sent to {username}")
            return f"Direct message sent to {username} (msgId: {msg_id}, roomId: {rid})"
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to send DM to {username}: {error_msg}")
            return f"Failed to send direct message: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error sending DM to {username}: {str(e)}")
        return f"Error sending direct message: {str(e)}"

@mcp.tool()
async def delete_message(room_id: str, msg_id: str) -> str:
    """Delete a message.

    Args:
        room_id: ID of the room containing the message
        msg_id: ID of the message to delete
    """
    main_logger.info(f"delete_message called - room_id: {room_id}, msg_id: {msg_id}")

    if not rocket_client:
        return "RocketChat client not initialized"

    try:
        result = await rocket_client.async_request(
            "POST", "chat.delete",
            json_data={"roomId": room_id, "msgId": msg_id}
        )
        if result.get('success'):
            main_logger.info(f"Message {msg_id} deleted")
            return f"Message {msg_id} deleted successfully"
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to delete message {msg_id}: {error_msg}")
            return f"Failed to delete message: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error deleting message {msg_id}: {str(e)}")
        return f"Error deleting message: {str(e)}"

@mcp.tool()
async def get_unread() -> str:
    """Get all rooms with unread messages and their latest unread content."""
    main_logger.info("get_unread called")

    if not rocket_client:
        return "RocketChat client not initialized"

    try:
        result = await rocket_client.async_request("GET", "subscriptions.get")
        if not result.get('success'):
            return f"Failed to get subscriptions: {result.get('error', 'Unknown error')}"

        unread_rooms = []
        for sub in result.get('update', []):
            unread = sub.get('unread', 0)
            if unread > 0:
                room_name = sub.get('name', sub.get('fname', 'N/A'))
                room_id = sub.get('rid', 'N/A')
                room_type = {'d': 'DM', 'c': 'Channel', 'p': 'Group'}.get(sub.get('t', ''), 'Unknown')
                unread_rooms.append({
                    'name': room_name, 'id': room_id,
                    'type': room_type, 'unread': unread,
                })

        if not unread_rooms:
            return "No unread messages"

        # fetch the latest messages for each unread room
        lines = []
        for room in unread_rooms:
            lines.append(f"\n[{room['type']}] {room['name']} ({room['unread']} unread, ID: {room['id']})")
            try:
                msgs = await rocket_client.fetch_room_messages(
                    room['id'], {"count": room['unread']}
                )
                if msgs.get('success'):
                    for msg in msgs.get('messages', []):
                        lines.append(format_message_line(msg, indent="  "))
                else:
                    lines.append(f"  (failed to read messages: {msgs.get('error', 'unknown')})")
            except Exception as e:
                lines.append(f"  (failed to read messages: {e})")

        return "Unread messages:" + "\n".join(lines)
    except Exception as e:
        main_logger.error(f"Error getting unread: {str(e)}")
        return f"Error getting unread: {str(e)}"

@mcp.tool()
async def send_file(channel: str, file_path: str, message: str = "") -> str:
    """Send a file (image, document, etc.) to a channel or user.

    Args:
        channel: Channel name, channel ID, or @username for DM
        file_path: Absolute path to the file to upload
        message: Optional text message to send with the file
    """
    main_logger.info(f"send_file called - channel: {channel}, file: {file_path}")

    if not rocket_client:
        return "RocketChat client not initialized"

    if not os.path.exists(file_path):
        return f"File not found: {file_path}"

    try:
        # @username -> resolve to a room ID via im.create
        room_id = channel
        if channel.startswith("@"):
            username = channel[1:]
            im_result = await rocket_client.async_request(
                "POST", "im.create", json_data={"username": username}
            )
            if im_result.get('success') and 'room' in im_result:
                room_id = im_result['room']['_id']
            else:
                return f"Failed to resolve DM room for {channel}"

        url = f"{rocket_client.server_url}/api/v1/rooms.upload/{room_id}"
        filename = os.path.basename(file_path)

        client = rocket_client._get_client()
        with open(file_path, "rb") as f:
            files = {"file": (filename, f)}
            data = {}
            if message:
                data["msg"] = message
            response = await client.post(
                url,
                headers=rocket_client._auth_headers(),
                files=files,
                data=data,
                timeout=60.0,
            )
        response.raise_for_status()
        result = response.json()

        if result.get('success'):
            main_logger.info(f"File sent to {channel}")
            return f"File '{filename}' sent to {channel}"
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to send file to {channel}: {error_msg}")
            return f"Failed to send file: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error sending file to {channel}: {str(e)}")
        return f"Error sending file: {str(e)}"

@mcp.tool()
async def list_all_rooms() -> str:
    """List all rooms (channels, groups and DMs) available to the user."""
    main_logger.info("list_all_rooms called")
    
    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"
    
    try:
        # Get channels
        channels_result = await rocket_client.async_request("GET", "channels.list")
        # Get groups
        groups_result = await rocket_client.async_request("GET", "groups.list")
        
        all_rooms = []
        
        if channels_result.get('success') and 'channels' in channels_result:
            channels_count = len(channels_result['channels'])
            main_logger.info(f"Retrieved {channels_count} channels")
            for channel in channels_result['channels']:
                all_rooms.append(f"[Channel] {channel.get('name', 'N/A')} (ID: {channel.get('_id', 'N/A')})")
        
        if groups_result.get('success') and 'groups' in groups_result:
            groups_count = len(groups_result['groups'])
            main_logger.info(f"Retrieved {groups_count} groups")
            for group in groups_result['groups']:
                all_rooms.append(f"[Group] {group.get('name', 'N/A')} (ID: {group.get('_id', 'N/A')})")

        # Get DMs
        im_result = await rocket_client.async_request("GET", "im.list")
        if im_result.get('success') and 'ims' in im_result:
            for im in im_result['ims']:
                usernames = ', '.join(im.get('usernames', []))
                all_rooms.append(f"[DM] {usernames} (ID: {im.get('_id', 'N/A')})")

        main_logger.info(f"Total rooms found: {len(all_rooms)}")
        
        if not all_rooms:
            return "No rooms found"
        
        return "Available rooms:\n" + "\n".join(all_rooms)
    except Exception as e:
        main_logger.error(f"Error listing rooms: {str(e)}")
        return f"Error listing rooms: {str(e)}"

@mcp.tool()
async def get_user_info(username: str) -> str:
    """Get information about a specific user.

    Args:
        username: Username to get information about
    """
    main_logger.info(f"get_user_info called for username: {username}")
    
    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"
    
    try:
        result = await rocket_client.async_request("GET", "users.info", params={"username": username})
        if result.get('success') and 'user' in result:
            user = result['user']
            main_logger.info(f"Retrieved user info for: {username}")
            info = f"""User Information:
Name: {user.get('name', 'N/A')}
Username: {user.get('username', 'N/A')}
Email: {user.get('emails', [{}])[0].get('address', 'N/A') if user.get('emails') else 'N/A'}
Status: {user.get('status', 'N/A')}
Active: {user.get('active', 'N/A')}
Roles: {', '.join(user.get('roles', []))}"""
            return info
        else:
            error_msg = result.get('error', 'User not found')
            main_logger.warning(f"Failed to get user info for {username}: {error_msg}")
            return f"Failed to get user info: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error getting user info for {username}: {str(e)}")
        return f"Error getting user info: {str(e)}"

@mcp.tool()
async def create_channel(name: str) -> str:
    """Create a new channel.

    Args:
        name: Name of the channel to create
    """
    main_logger.info(f"create_channel called for channel: {name}")

    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"

    try:
        result = await rocket_client.async_request(
            "POST", "channels.create",
            json_data={"name": name}
        )
        if result.get('success'):
            channel = result.get('channel', {})
            channel_id = channel.get('_id', 'N/A')
            main_logger.info(f"Channel '{name}' created successfully with ID: {channel_id}")
            return f"Channel '{name}' created successfully with ID: {channel_id}"
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to create channel '{name}': {error_msg}")
            return f"Failed to create channel: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error creating channel '{name}': {str(e)}")
        return f"Error creating channel: {str(e)}"

@mcp.tool()
async def get_channel_messages(room_id: str, count: int = 20, offset: int = 0) -> str:
    """Get messages from a channel, group or DM (newest first).

    Args:
        room_id: Room ID or room name of the channel/group/DM
        count: Number of messages to retrieve (default: 20, max: 100)
        offset: Skip this many newest messages — use to page back through history
                instead of re-reading with a larger count (e.g. offset=20 to get
                the 20 messages older than the first page)
    """
    main_logger.info(f"get_channel_messages called for room_id: {room_id}, count: {count}, offset: {offset}")

    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"

    try:
        count = min(count, 100)
        params = {"count": count}
        if offset:
            params["offset"] = offset
        try:
            result = await rocket_client.fetch_room_messages(room_id, params)
        except Exception as e:
            # preserve the real error category (401 / network failure != room
            # not found) to avoid misleading the caller
            return f"Failed to get messages from room {room_id}: {e}"

        if result.get('success') and 'messages' in result:
            messages = result['messages']
            main_logger.info(f"Retrieved {len(messages)} messages from room {room_id}")
            
            if not messages:
                return "No messages found in this channel"

            formatted_messages = [format_message_line(msg) for msg in messages]
            header = f"Messages from channel (last {len(messages)}"
            if offset:
                header += f", offset {offset}"
            return header + "):\n" + "\n".join(formatted_messages)
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to get messages from room {room_id}: {error_msg}")
            return f"Failed to get messages: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error getting messages from room {room_id}: {str(e)}")
        return f"Error getting messages: {str(e)}"

@mcp.tool()
async def search_messages(room_id: str, query: str, count: int = 20) -> str:
    """Search messages in a room by keyword. Much cheaper than paging through
    history with get_channel_messages when looking for a specific message.

    Args:
        room_id: Room ID or room name of the channel/group/DM to search in
        query: Search keyword(s)
        count: Max results to return (default: 20, max: 100)
    """
    main_logger.info(f"search_messages called - room_id: {room_id}, query: {query}, count: {count}")

    if not rocket_client:
        return "RocketChat client not initialized"

    try:
        count = min(count, 100)
        rid = await rocket_client.resolve_room_id(room_id)
        result = await rocket_client.async_request(
            "GET", "chat.search",
            params={"roomId": rid, "searchText": query, "count": count}
        )
        if result.get('success'):
            messages = result.get('messages', [])
            if not messages:
                return f"No messages matching '{query}' in room {room_id}"
            formatted = [format_message_line(msg) for msg in messages]
            return f"Found {len(messages)} message(s) matching '{query}':\n" + "\n".join(formatted)
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Search failed in room {room_id}: {error_msg}")
            return f"Search failed: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error searching room {room_id}: {str(e)}")
        return f"Error searching messages: {str(e)}"


@mcp.tool()
async def download_attachment(message_id: str) -> str:
    """Download attachments from a message. Returns local file paths that can be viewed with the Read tool.

    Args:
        message_id: The message ID (shown as 'id: xxx' in message listings)
    """
    main_logger.info(f"download_attachment called for message_id: {message_id}")

    if not rocket_client:
        return "RocketChat client not initialized"

    try:
        # fetch the message details
        result = await rocket_client.async_request(
            "GET", "chat.getMessage", params={"msgId": message_id}
        )
        if not result.get('success'):
            return f"Failed to get message: {result.get('error', 'Unknown error')}"

        msg = result.get('message', {})
        files = msg.get('files', [])
        file_obj = msg.get('file')

        # some messages use a files array, others a single file object
        if not files and file_obj:
            files = [file_obj]
        if not files:
            return f"Message {message_id} has no attachments"

        downloaded = []
        for f in files:
            file_id = f.get('_id')
            file_name = f.get('name', 'unknown')
            if not file_id:
                continue

            # RocketChat file download URL
            download_url = f"{rocket_client.server_url}/file-upload/{file_id}/{file_name}"
            main_logger.info(f"Downloading: {download_url}")

            resp = await rocket_client._get_client().get(
                download_url,
                headers=rocket_client._auth_headers(),
                timeout=60.0,
                follow_redirects=True,
            )
            resp.raise_for_status()

            # save to the temp dir
            save_path = os.path.join(tempfile.gettempdir(), f"rc_{file_id}_{file_name}")
            with open(save_path, "wb") as out:
                out.write(resp.content)

            downloaded.append(save_path)
            main_logger.info(f"Saved attachment to: {save_path}")

        if not downloaded:
            return f"Message {message_id} has file metadata but no downloadable files"

        paths_str = "\n".join(downloaded)
        return f"Downloaded {len(downloaded)} attachment(s):\n{paths_str}\n\nUse the Read tool to view these files."

    except Exception as e:
        main_logger.error(f"Error downloading attachment: {str(e)}")
        return f"Error downloading attachment: {str(e)}"


async def initialize_client(server_url: str, username: str, password: str, verify_ssl: bool = True):
    """Initialize the RocketChat client"""
    global rocket_client
    main_logger.info(f"Initializing RocketChat client for server: {server_url}, user: {username}")

    try:
        rocket_client = RocketChatAPI(server_url, username, password, verify_ssl=verify_ssl)
        await rocket_client.login(username, password)
        main_logger.info("RocketChat client initialized successfully")
        return True
    except Exception as e:
        main_logger.error(f"Failed to initialize RocketChat client: {e}")
        return False

def start_orphan_watchdog(interval: float):
    """When the MCP client dies abnormally, the stdio server may linger and
    such processes pile up over time. Periodically check (POSIX only):
    - parent became pid 1: this process is orphaned -> exit
    - grandparent became pid 1: the parent (e.g. a uv wrapper) is orphaned,
      so the client is gone -> exit
    """
    def check():
        while True:
            time.sleep(interval)
            ppid = os.getppid()
            if ppid == 1:
                main_logger.info("Orphaned (ppid=1), shutting down")
                os._exit(0)
            try:
                out = subprocess.run(
                    ["ps", "-o", "ppid=", "-p", str(ppid)],
                    capture_output=True, text=True, timeout=5,
                )
                if out.stdout.strip() == "1":
                    main_logger.info(f"Parent {ppid} orphaned (grandparent=1), shutting down")
                    os._exit(0)
            except Exception:
                pass  # ps failure is not fatal; if the parent really died, ppid becomes 1 on a later check

    threading.Thread(target=check, daemon=True, name="orphan-watchdog").start()


def main():
    global rocket_client
    main_logger.info("Starting RocketChat MCP Server")

    parser = argparse.ArgumentParser(description="RocketChat MCP Server")
    parser.add_argument("--server-url", default=os.getenv("ROCKETCHAT_SERVER_URL"),
                        help="RocketChat server URL (env: ROCKETCHAT_SERVER_URL)")
    parser.add_argument("--username", help="RocketChat username")
    parser.add_argument("--password", help="RocketChat password")
    parser.add_argument("--auth-token", default=os.getenv("ROCKETCHAT_AUTH_TOKEN"),
                        help="RocketChat personal access token (env: ROCKETCHAT_AUTH_TOKEN; prefer the env var to keep the token out of process arguments)")
    parser.add_argument("--user-id", default=os.getenv("ROCKETCHAT_USER_ID"),
                        help="RocketChat user ID (env: ROCKETCHAT_USER_ID)")
    parser.add_argument("--no-verify-ssl", action="store_true", help="Disable SSL certificate verification")

    args = parser.parse_args()
    if not args.server_url:
        parser.error("--server-url is required (or set ROCKETCHAT_SERVER_URL)")
    verify_ssl = not args.no_verify_ssl

    if args.auth_token and args.user_id:
        main_logger.info(f"Using token auth for user_id: {args.user_id}")
        rocket_client = RocketChatAPI(
            args.server_url, user_id=args.user_id, auth_token=args.auth_token,
            verify_ssl=verify_ssl,
        )
    elif args.username and args.password:
        main_logger.info(f"Using password auth for username: {args.username}")
        try:
            ok = asyncio.run(initialize_client(args.server_url, args.username, args.password, verify_ssl=verify_ssl))
        except Exception as e:
            main_logger.error(f"Setup failed: {e}")
            exit(1)
        if not ok:
            # initialize_client swallows exceptions and returns False; without
            # this check the server would start with a None token and every
            # request would fail with a header type error instead of an auth error
            main_logger.error("Login failed, refusing to start with invalid credentials")
            exit(1)
    else:
        parser.error("Provide either --auth-token/--user-id or --username/--password")

    watchdog_interval = float(os.getenv("ROCKETCHAT_WATCHDOG_INTERVAL", "60"))
    start_orphan_watchdog(watchdog_interval)

    main_logger.info("Starting MCP server with stdio transport")
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()