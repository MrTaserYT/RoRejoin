Version: V3.1

Added "Roblox Settings" tab, added ability to change Roblox installation RoRejoin uses, added per account settings and more (I forgot)

Download:
[RoRejoin-Qt.zip](https://github.com/user-attachments/files/29324860/RoRejoin-Qt.zip)


**RoRejoin**
RoRejoin is the ultimate tool for autofarmers running multiple Roblox accounts at once. Launch and watch any number of clients side by side — if one crashes, gets kicked, or lands in the wrong game, RoRejoin instantly closes it and rejoins a fresh server, no babysitting required. Every account gets its own game and rejoin delay, with a live dashboard tracking uptime, crashes, and status. Tile all your windows into a clean grid and unlock their size to pack dozens onto one screen. Cookies are encrypted on disk with Windows DPAPI, auto-kill keeps sessions fresh, and the different-servers spreader scatters accounts apart.

**Requirements:**
PySide6 (latest version)

**Features**
- Multi-account support with per-account place, rejoin delay, and kill-cooldown overrides
- Crash, kick, and wrong-game detection with automatic rejoin into a fresh server
- Auto-kill on a timer to cycle sessions — individual or synchronized across all accounts on one shared timer
- Join-server mode — paste a share link and every account joins the same private server
- Different-servers mode — spread each account across its own separate public server
- Detect open clients — adopt Roblox windows that are already running instead of launching new ones
- Window tiling and stacking layouts for multi-instance setups
- Individual account settings — alloows user to change certain settings per account
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

Some antivirus tools may flag RoRejoin.exe as suspicious. I don't know why.
The full source code is included in RoRejoin-Qt.zip on this page. You can read every line and build the exe yourself by running build_qt.bat — it installs the dependencies and compiles everything in one step.
