import argparse
import asyncio
import logging
import os
from datetime import datetime

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
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()  # Also log to console
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
    def __init__(self, server_url, username=None, password=None, user_id=None, auth_token=None):
        self.server_url = server_url.rstrip('/')
        self.logger = logging.getLogger("RocketChatAPI")
        self.auth_token = None
        self.user_id = None
        self.username = username
        self.password = password
        
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
        
        async with httpx.AsyncClient() as client:
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

    async def async_request(self, method: str, endpoint: str, json_data=None, params=None):
        """Make async HTTP request to RocketChat API"""
        url = f"{self.server_url}/api/v1/{endpoint}"
        self.logger.info(f"Making {method} request to {endpoint}")
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method, url, 
                    headers=self._headers(), 
                    json=json_data, 
                    params=params,
                    timeout=30.0
                )
                response.raise_for_status()
                result = response.json()
                self.logger.info(f"Request {method} {endpoint} successful")
                return result
            except Exception as e:
                self.logger.error(f"{method} {endpoint} error: {e}")
                raise

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
            
            # Renvoyer le username, emails, name
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
            main_logger.info(f"Message sent successfully to channel: {channel}")
            return f"Message sent successfully to {channel}"
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to send message to {channel}: {error_msg}")
            return f"Failed to send message: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error sending message to {channel}: {str(e)}")
        return f"Error sending message: {str(e)}"

@mcp.tool()
async def list_channels() -> str:
    """List all channels available to the user."""
    main_logger.info("list_channels called")
    
    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"
    
    try:
        result = await rocket_client.async_request("GET", "channels.list")
        if result.get('success') and 'channels' in result:
            channels = result['channels']
            main_logger.info(f"Retrieved {len(channels)} channels")
            
            if not channels:
                return "No channels found"
            
            channel_list = []
            for channel in channels:
                channel_list.append(f"- {channel.get('name', 'N/A')} (ID: {channel.get('_id', 'N/A')})")
            
            return "Available channels:\n" + "\n".join(channel_list)
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to list channels: {error_msg}")
            return f"Failed to list channels: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error listing channels: {str(e)}")
        return f"Error listing channels: {str(e)}"

@mcp.tool()
async def list_all_rooms() -> str:
    """List all rooms (channels and groups) available to the user."""
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
async def get_channel_messages(room_id: str, count: int = 20) -> str:
    """Get messages from a specific channel.

    Args:
        room_id: ID of the room/channel
        count: Number of messages to retrieve (default: 20, max: 100)
    """
    main_logger.info(f"get_channel_messages called for room_id: {room_id}, count: {count}")
    
    if not rocket_client:
        main_logger.error("RocketChat client not initialized")
        return "RocketChat client not initialized"
    
    try:
        count = min(count, 100)  # Limit to 100 messages
        result = await rocket_client.async_request(
            "GET", "channels.messages",
            params={"roomId": room_id, "count": count}
        )
        
        if result.get('success') and 'messages' in result:
            messages = result['messages']
            main_logger.info(f"Retrieved {len(messages)} messages from room {room_id}")
            
            if not messages:
                return "No messages found in this channel"
            
            formatted_messages = []
            for msg in messages:
                timestamp = msg.get('ts', {}).get('$date', 'N/A')
                user = msg.get('u', {}).get('username', 'Unknown')
                text = msg.get('msg', 'No content')
                formatted_messages.append(f"[{timestamp}] {user}: {text}")
            
            return f"Messages from channel (last {len(messages)}):\n" + "\n".join(formatted_messages)
        else:
            error_msg = result.get('error', 'Unknown error')
            main_logger.error(f"Failed to get messages from room {room_id}: {error_msg}")
            return f"Failed to get messages: {error_msg}"
    except Exception as e:
        main_logger.error(f"Error getting messages from room {room_id}: {str(e)}")
        return f"Error getting messages: {str(e)}"

async def initialize_client(server_url: str, username: str, password: str):
    """Initialize the RocketChat client"""
    global rocket_client
    main_logger.info(f"Initializing RocketChat client for server: {server_url}, user: {username}")
    
    try:
        rocket_client = RocketChatAPI(server_url, username, password)
        await rocket_client.login(username, password)
        main_logger.info("RocketChat client initialized successfully")
        return True
    except Exception as e:
        main_logger.error(f"Failed to initialize RocketChat client: {e}")
        return False

if __name__ == "__main__":
    main_logger.info("Starting RocketChat MCP Server")
    
    parser = argparse.ArgumentParser(description="RocketChat MCP Server")
    parser.add_argument("--server-url", required=True, help="RocketChat server URL")
    parser.add_argument("--username", required=True, help="RocketChat username")
    parser.add_argument("--password", required=True, help="RocketChat password")
    
    args = parser.parse_args()
    main_logger.info(f"Arguments parsed - server: {args.server_url}, username: {args.username}")
    
    # Initialize the client
    async def setup():
        main_logger.info("Starting client setup")
        success = await initialize_client(args.server_url, args.username, args.password)
        if not success:
            main_logger.error("Failed to initialize RocketChat client, exiting")
            print("Failed to initialize RocketChat client")
            exit(1)
        main_logger.info("Client setup completed successfully")
    
    # Run setup before starting the server
    try:
        asyncio.run(setup())
    except Exception as e:
        main_logger.error(f"Setup failed: {e}")
        exit(1)
    
    # Initialize and run the server
    main_logger.info("Starting MCP server with stdio transport")
    mcp.run(transport='stdio')