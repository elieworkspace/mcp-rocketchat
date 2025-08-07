git clone git@github.com:elieworkspace/mcp-rocketchat.git
cd mcp-rocketchat

docker compose up -d
go to http://localhost:3000
save admin username and password
go to http://localhost:3000/admin/settings/Accounts#:rbb: > Disable 2FA > Save 

uv venv
.\venv\Scripts\activate
uv add mcp[cli] httpx