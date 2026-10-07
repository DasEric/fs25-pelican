# FS25 Pelican

Run a Farming Simulator 25 dedicated server on Pelican or Pterodactyl.
Install using a GIANTS Windows installer or the normal Windows Steam client.
Both use a browser-accessible noVNC desktop. Game files, settings, mods,
savegames and Steam's saved session persist between container restarts.

Maintained by [Eric (DasEric)](https://github.com/DasEric).

## Panel setup

1. Import [egg-farming-simulator-25.json](egg-farming-simulator-25.json).
2. Use the image `ghcr.io/daseric/fs25-pelican:latest` on a Linux AMD64 node.
3. Allocate three different ports and set them in the egg:

| Setting | Purpose | Default |
| --- | --- | --- |
| `SERVER_PORT` | Game connection, TCP and UDP | `10823` |
| `WEB_PORT` | GIANTS Web Interface, TCP | `7999` |
| `NOVNC_PORT` | Browser desktop, TCP | `6080` |

Set your own `VNC_PASSWORD`. Set `PUBLIC_IP` if the console links need a
different public address. Open the noVNC link printed after starting the server.

## Choose an installation method

### GIANTS installer

1. Leave `INSTALL_SOURCE=giants` (the default).
2. Upload your official FS25 Windows installer (`.exe`, `.img`, `.iso` or `.zip`)
   and any associated installer parts to `/home/container/installer`.
3. Open **Install / activate FS25** in noVNC. `AUTO_INSTALL=true` can also start
   the uploaded installer on container startup.
4. Complete activation in noVNC, then close the game and restart the container.
5. Delete uploaded installation media once installation succeeds to free space.

### Steam

1. Set `INSTALL_SOURCE=steam` and `AUTOSTART_SERVER=false` for the initial setup.
2. Start the container and open noVNC. The normal Windows Steam client is
   installed at `C:\Steam` and launched. Its first update can take a few minutes.
3. Sign in **inside Steam**, complete Steam Guard there and enable **Remember me**.
   The egg has no Steam username, password or Steam Guard fields.
   Use the **Steam** desktop shortcut to bring the client back if needed.
4. Install **Farming Simulator 25** in Steam's default library. In
   **Library → FS25 → Properties → DLC**, enable the game DLCs you want and
   wait until all downloads finish.
5. Launch FS25 once through Steam to complete first-run prerequisites, then
   close the game. Keep Steam itself running.
6. Set `AUTOSTART_SERVER=web_only` or `true` and restart the container.

The image prepares `steam_appid.txt` with FS25's App ID, `2300320`, before
starting the server. Steam remains running alongside it. Steam manages its
game and DLC updates; separate GIANTS activation/DLC installers are skipped.

Steam's saved login is reused on later starts. If Steam requests another login
or Steam Guard confirmation, complete it in noVNC. After a three-minute wait,
the desktop stays available; use **Start FS25 web server** or restart the
container after signing in.

Existing GIANTS installations are kept when selecting Steam; Steam downloads
its own copy. Both methods share your server settings, mods and savegames.

## Start and configure the server

| `AUTOSTART_SERVER` | Behaviour |
| --- | --- |
| `false` | Desktop only; Steam also opens in Steam mode |
| `web_only` | Start the GIANTS Web Interface; start the game there |
| `true` | Start the Web Interface and request game-server startup |

Open the GIANTS web link printed in the console. A new installation starts
with **admin / webpassword**; change these credentials in the Web Interface.

Leave optional server settings in the egg empty to manage them in GIANTS.
A non-empty egg value overrides the corresponding setting on each start.
Game and web ports always follow the egg settings.

`FS25 image ready.` means the desktop is ready, not that the game is joinable.
Large mod maps can take several minutes to load. Do not run a separate normal
game instance alongside the dedicated server.

## Mods, savegames and DLCs

| Data | Location |
| --- | --- |
| GIANTS game installation | `/home/container/game/Farming Simulator 2025` |
| Default Steam client/library | `/home/container/steam/library` (`C:\Steam`) |
| Wine prefix | `/home/container/.fs25server` |
| Settings | `/home/container/config/FarmingSimulator2025` |
| Mods | `/home/container/config/FarmingSimulator2025/mods` |
| Savegames | `/home/container/config/FarmingSimulator2025/savegameN` |
| Logs | `/home/container/logs` |

Upload mod ZIPs without extracting them. Stop the game before replacing a
savegame; copy the whole savegame into the slot selected in GIANTS.

For **GIANTS DLCs**, upload installers to `/home/container/dlc` and open
**Install FS25 DLCs** in noVNC. For **Steam DLCs**, use Steam's DLC settings.
Finish downloads before starting a savegame that needs those DLCs.

## Updates and backups

Before updating, stop the container and back up the Wine prefix, game/Steam
installation and configuration folder. Pull the new image and restart.
Existing GIANTS servers keep their installation method; update the egg to
expose the new Steam selection.

For a Steam game/DLC update, stop the game in GIANTS, set
`AUTOSTART_SERVER=false` and restart the container. Finish the update in
Steam, restore your preferred autostart setting and restart again.

If setup stalls, check noVNC and `/home/container/logs/steam-launch.log`
(Steam) or the installer logs (GIANTS). Steam downloads and first-run setup
must finish before the dedicated server starts.

Runtime details, diagnostics and the release test checklist are in
[the maintainer guide](docs/MAINTAINER.md).

## License

Project code and documentation: [MIT](LICENSE). Bundled software retains its
own licenses. You supply the FS25 game and DLCs. This project is not affiliated
with GIANTS Software or Valve.
