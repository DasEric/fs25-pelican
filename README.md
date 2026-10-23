# FS25 Pelican

Run a Farming Simulator 25 dedicated server on Pelican or Pterodactyl,
using your GIANTS installer or Steam installation. Settings, mods, savegames
and Steam account data stay in persistent storage.

## Setup

1. Import [the egg](egg-farming-simulator-25.json) and select
   `ghcr.io/daseric/fs25-pelican:latest` on a Linux AMD64 node.
2. Allocate three different ports:

   | Setting | Purpose | Default |
   | --- | --- | --- |
   | `SERVER_PORT` | Game, TCP/UDP | `10823` |
   | `WEB_PORT` | GIANTS Web Interface, TCP | `7999` |
   | `NOVNC_PORT` | Browser desktop, TCP | `6080` |

3. Set your own `VNC_PASSWORD`. Start the container and open the noVNC link
   printed in the panel console. Set `PUBLIC_IP` if those links need another address.

### GIANTS installation

Leave `INSTALL_SOURCE=giants`. Upload your official Windows installer and its
parts to `/home/container/installer`, then open **Install / activate FS25**.
Finish setup and activation in noVNC, close the game, then start the web server.
For DLCs, upload their installers to `/home/container/dlc` and use **Install FS25 DLCs**.

### Steam installation

Set `INSTALL_SOURCE=steam` and initially `AUTOSTART_SERVER=false`.
Open **Steam**, sign in, complete Steam Guard and enable **Remember me**.
Install FS25 and your selected DLCs, then launch the game once to finish setup.
Close the game, but keep Steam running when starting the server.

`STEAM_DOWNLOAD_MODE=client` keeps this normal Steam workflow. The optional
`steamcmd` mode downloads into a **separate** folder using the Install/Update
desktop buttons; password and Steam Guard prompts appear in the terminal.
It checks owned Windows DLC depots after downloading. **SteamCMD replaces the
downloader, not the Steam runtime: CLI-only game-server operation is not yet
verified.** Existing libraries are retained. See the [maintainer guide](docs/MAINTAINER.md).

## Desktop buttons

| Button | Action |
| --- | --- |
| **Start FS25 web server / view log** | Start once and display the log. Closing this window does not stop the server or VNC. |
| **Stop FS25 web server** | Stop the web service after you have saved/stopped the game in GIANTS. |
| **Update FS25** | Update/verify the installation while server starts are blocked. |
| **Open GIANTS Web Interface** | Open the server's web configuration. |

A new Web Interface starts with **admin / webpassword**. Change these credentials.
Leave optional egg settings empty to manage them in GIANTS; non-empty values
override the corresponding setting on startup.

`AUTOSTART_SERVER` applies **once per container start**: `false` starts only the
desktop, `web_only` starts the Web Interface, and `true` also requests game startup.
Closing a log window or reconnecting noVNC does not restart anything.
`FS25 image ready.` means the desktop is ready, not that the game is joinable.

## Updating

1. Back up your game installation, Wine prefix and configuration.
2. Save and **stop the game in GIANTS**, then click **Update FS25**.
3. The button stops the managed web service and, in Steam mode, closes Steam.
   Existing downloads/installers must finish first. It does not force-kill a running game.
4. SteamCMD runs in the terminal. Normal Steam mode opens file verification;
   finish all game/DLC downloads and use **Steam > Exit**, then confirm in the terminal.
   GIANTS mode opens its official launcher/updater; finish it, close the game and confirm.
5. After success, start Steam if applicable and click **Start FS25 web server**.
   The update never automatically starts the game server.

VNC and the desktop stay online throughout. If an update fails or is interrupted,
server startup stays blocked until **Update FS25** succeeds on retry.
This button updates the **game**, not the Docker image. To upgrade the image,
pull it on the node and fully stop/start the container.

## Your files

| Content | Persistent location |
| --- | --- |
| GIANTS game | `/home/container/game/Farming Simulator 2025` |
| Normal Windows Steam | `/home/container/steam/library` |
| SteamCMD game | `/home/container/steamcmd/game` |
| Settings, mods, savegames | `/home/container/config/FarmingSimulator2025` |
| Logs | `/home/container/logs` |

Upload mod ZIPs without extracting them. Stop the game before replacing savegames.
Troubleshooting and release checks: [maintainer guide](docs/MAINTAINER.md).

## License

Project code: [MIT](LICENSE). Bundled software retains its own licenses.
You supply the game and DLCs. Not affiliated with GIANTS Software or Valve.
