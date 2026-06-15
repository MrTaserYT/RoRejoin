
**RoRejoin** is a Windows desktop app for managing multiple Roblox accounts simultaneously. It watches your running Roblox instances in the background and automatically relaunches any that crash or get disconnected — so you never have to babysit your sessions.

**Features**
- Multi-account support with per-account place and timing overrides
- Crash detection and automatic rejoin with configurable delay
- Auto-kill on a timer to cycle sessions (individual or synchronized across all accounts)
- Join-server mode — paste a share link and every account joins the same private server
- Window tiling and stacking layout for multi-instance setups
- Live monitor dashboard with uptime, crash count, and auto-kill countdowns
- Discord bot for remote control and webhook notifications
- Settings persist between sessions with DPAPI-encrypted cookie storage

Built with Python and CustomTkinter. This tool is made with mostly Claude Opus 4.8 and Fabel 5

!Quick warning, Virustotal detects this as a trojan, it is not. That's why I always give the source code too.!

To make the source code (.py) into an exe, type "powershell" in the top bar of the folder where you saved it, then paste this command:

python -m PyInstaller --onefile --windowed --collect-all customtkinter --name RoRejoin rorejoin.py

and hit enter

MASSIVE UI UPDATE: Completely redid the entire ui, added "join server from share link" and fixed some bugs.



<img width="1402" height="827" alt="RoRejoinV2 Setup" src="https://github.com/user-attachments/assets/41399372-0b56-4bbe-ba0d-b106c2a849fc" />
<img width="1402" height="827" alt="RoRejoinV2 Accounts" src="https://github.com/user-attachments/assets/e3d75fd0-cc15-4458-b8af-ea80b58c4bb2" />
<img width="1402" height="827" alt="RoRejoinV2 Monitor" src="https://github.com/user-attachments/assets/c7160190-61d2-4998-87bb-34bd6a73be60" />
