Version: V3

Added a bunch of features and switched from Ctk to PySide6.

Download:
[RoRejoin-Qt.zip](https://github.com/user-attachments/files/29282075/RoRejoin-Qt.zip)


**RoRejoin**
RoRejoin is the ultimate tool for autofarmers running multiple Roblox accounts at once. Launch and watch any number of clients side by side — if one crashes, gets kicked, or lands in the wrong game, RoRejoin instantly closes it and rejoins a fresh server, no babysitting required. Every account gets its own game and rejoin delay, with a live dashboard tracking uptime, crashes, and status. Tile all your windows into a clean grid and unlock their size to pack dozens onto one screen. Cookies are encrypted on disk with Windows DPAPI, auto-kill keeps sessions fresh, and the different-servers spreader scatters accounts apart.

**Features**
- Multi-account support with per-account place, rejoin delay, and kill-cooldown overrides
- Crash, kick, and wrong-game detection with automatic rejoin into a fresh server
- Auto-kill on a timer to cycle sessions — individual or synchronized across all accounts on one shared timer
- Join-server mode — paste a share link and every account joins the same private server
- Different-servers mode — spread each account across its own separate public server
- Detect open clients — adopt Roblox windows that are already running instead of launching new ones
- Window tiling and stacking layouts for multi-instance setups
- kick detection — detects when an account has been kicked from the game and relaunches it automatically
- Unlock window size — strips the client's border so tiled windows shrink past Roblox's ~800×600 minimum, packing dozens onto one screen
- Schedule — automatically make it start itself at a given time with the option to repeat it daily 
- Live monitor dashboard with uptime, crash count, and auto-kill countdowns
- Optional Discord webhook notifications (usernames and events only — never cookies)
- Settings persist between sessions with DPAPI-encrypted cookie storage

<img width="1250" height="825" alt="RoRejoin Setup Page" src="https://github.com/user-attachments/assets/73bea1ea-24d8-4f16-ab1b-a04c5a893116" />
<img width="1250" height="825" alt="RoRejoin Accounts Page" src="https://github.com/user-attachments/assets/34b84c73-22c6-48a6-bc90-60f42283fdd4" />
<img width="1250" height="825" alt="RoRejoin Monitor Page" src="https://github.com/user-attachments/assets/adbfc2ac-5393-43c1-8ea4-d31f54a20cdc" />


Built with Python, PySide6, Claude Opus 4.8 (main), gemini (only small stuff)

**⚠️ Antivirus false positive**

Some antivirus tools may flag RoRejoin.exe as suspicious. This is a known false positive caused by PyInstaller — the tool used to bundle Python scripts into a single executable. The detection is triggered by the bundler itself, not by anything RoRejoin does.
The full source code is included in RoRejoin.zip on this page. You can read every line and build the exe yourself by running build_exe.bat — it installs the dependencies and compiles everything in one step.


<img width="1402" height="827" alt="RoRejoinV2 Setup" src="https://github.com/user-attachments/assets/3898b024-bb0e-46f9-b0e6-1fb65b50ad96" />
<img width="1402" height="827" alt="RoRejoinV2 Accounts" src="https://github.com/user-attachments/assets/99c73ea3-c34e-4415-94d0-7e97f7e46aa8" />
<img width="1402" height="827" alt="RoRejoinV2 Monitor" src="https://github.com/user-attachments/assets/854ce9c6-746e-4445-b18b-6942d6f8cf05" />
