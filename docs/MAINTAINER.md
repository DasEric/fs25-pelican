# Maintainer guide

The [README](../README.md) covers normal panel use. This document describes
runtime selection, diagnostics and release verification.

## Two independent runtime paths

`INSTALL_SOURCE=giants` retains the standalone Wine installation and persistent
prefix `/home/container/.fs25server`. `INSTALL_SOURCE=steam` starts **native
Linux Steam**, then uses the **full Proton tool downloaded by Steam** for FS25.
The two Wine distributions and their prefixes must not be combined.

The official Valve `steam-launcher` Debian package is versioned and SHA-256
pinned in the Dockerfile. Its dependencies and the native 32-bit graphics/C
libraries are installed at image build time. Its self-update runs as `container`,
not root. The image contains no Steam account, remembered login or game files.
Linux Steam stores its client, logs, default library and full Proton downloads
in persistent `/home/container/.local/share/Steam`. Steam's other HOME data,
including `.steam`, also persists. Do not relocate its client behind its back.
The launcher's bundled apt sources are removed after installing the pinned
launcher and dependency metapackages. Otherwise, cleaned apt indexes cause
steamdeps to prompt for a privileged apt update at runtime. Portal backends
and bubblewrap are installed in the image as well.

Steam GUI startup does not run Wine, repair a prefix, or require FS25/Proton to
be installed. Desktop and panel launches share the same native command and
clean environment. Inherited GIANTS Wine binaries, DLL overrides, loader paths,
Steam App IDs and Proton variables are removed from the client environment;
DISPLAY, XDG runtime and the desktop session bus remain available. CEF uses
`-cef-disable-gpu` for Xvnc by default; `FS25_STEAM_GPU=true` opts into GPU use.
The CEF sandbox is not disabled by the controller.

The native client updates itself normally. A versioned installer URL avoids
unrelated upstream launcher changes breaking a reproducible build.
References: [Valve's launcher repository](https://repo.steampowered.com/steam/),
[Valve's CEF GPU recommendation](https://github.com/ValveSoftware/steam-for-linux/issues/11610).

## FS25's Proton installation and server launch

Users select Proton 11 in **FS25 Properties > Compatibility**, complete all
game/DLC/tool downloads, and start FS25 once in Steam before closing the game.
This obtains the Windows game depots and creates Steam's app-specific prefix.
No client, manifest, compatibility mapping or depot is fabricated by the controller.

Library discovery reads native `libraryfolders.vdf`. Libraries, games, tools
and compatdata must remain under persistent HOME. Readiness requires
`appmanifest_2300320.acf`, `StateFlags=4`, finished download/staging counters,
no remaining download files, and the Windows launcher, dedicated server,
game engine and Steam API DLL. Desired DLCs are selected in Steam; they are
not copied into the GIANTS `pdlc` directory.

The exact previously used Proton tool is resolved from Steam's first-run
`steamapps/compatdata/2300320/config_info` record. Valve records the Wine prefix
version, fonts directory, library directory and Steam directory there.
The controller validates its full `proton` launcher, Wine/wineserver binaries,
64-bit `lsteamclient.so` bridge and initialized prefix. Missing/partial tools
produce pending status, never a fallback to standalone Wine. If the user
changes the compatibility tool, launch FS25 through Steam once again first.

Both the Steam API probe and dedicated server run via that Proton launcher
with the same `STEAM_COMPAT_DATA_PATH`, `STEAM_COMPAT_CLIENT_INSTALL_PATH`,
`STEAM_COMPAT_INSTALL_PATH`, Steam App ID and game working directory.
`PROTON_DISABLE_LSTEAMCLIENT=0` enables the bridge to the native Linux client.
Inherited Wine loaders, prefixes and DLL paths are cleared. Proton manages its
own libraries; `/opt/fs25/wine-compat` is an additional private ABI lookup path
for server subprocesses, not a global desktop library override.

The dedicated-server path invokes `proton run` inside the existing Pelican
container, rather than nesting Steam's pressure-vessel game container. This
host-library path needs live validation for the selected Proton release.
Steam's normal first game launch and native webhelper can use Steam Runtime;
their namespace/runtime requirements must also be verified on the actual node.
A mock test or the GIANTS Wine build smoke is not proof that those paths work.

References: [Valve Proton](https://github.com/ValveSoftware/Proton),
[Proton prefix/environment implementation](https://github.com/ValveSoftware/Proton/blob/proton_11.0/proton),
[Linux Steam bridge](https://github.com/ValveSoftware/Proton/blob/proton_11.0/lsteamclient/Makefile.in).

## Settings, migration and session readiness

Server settings/mods/savegames remain in
`/home/container/config/FarmingSimulator2025`. Steam's Windows user is
`steamuser`: the corresponding Proton Documents folder is linked there, not
to a guessed Linux-user Wine folder. Existing server data takes priority.
First-game-launch defaults are retained beside that Documents folder as
`FarmingSimulator2025.before-fs25-link`; only missing data is imported.

Older Windows Steam clients, libraries, account files and GIANTS prefixes are
not deleted or moved. Users sign into native Linux Steam once. Existing game
downloads can be added through **Steam Settings > Storage** and verified by
Steam; unregistered Windows libraries are not assumed to belong to Linux Steam.
Do not copy Windows client/session files into the native client directory.

Login and Steam Guard happen in noVNC, exclusively in Steam's own UI. There
are no egg credentials, SteamCMD login or desktop click-automation scripts.
Saved login files are not proof of a running authenticated session.

`steam_session.c` loads the game's own `steam_api64.dll`, calls SteamAPI_Init
and the exported SteamUser interface/BLoggedOn, then shuts the API down. It
never calls SteamAPI_RestartAppIfNecessary or launches FS25. Proton converts
the API DLL path using `getcompatpath`; conversion and probe share a timeout.

| Exit | Output | Meaning |
| --- | --- | --- |
| 0 | `STEAM_SESSION_READY` | API initialized and user logged on |
| 1 | `STEAM_SESSION_PENDING` | Client/login/online session pending |
| 2 | `STEAM_API_*` | DLL load/export/argument failure |

The controller writes ASCII `2300320` plus a newline in `steam_appid.txt` in
the game root and engine directory before startup. Existing wrong values
are backed up. MinGW builds the helper in a separate image stage; toolchain
runtime notices remain in `/opt/fs25/steam-probe-licenses`.

Initial/partial setup and `AUTOSTART_SERVER=false` open Steam visibly. Installed
autostarts can use `-silent`; a desktop action opens it visibly again. Autostart
waits at most 180 seconds for the API. Errors/pending setup leave noVNC online.
Both panel and desktop starts use the controller's nonblocking Linux flock
for the server's entire lifetime; competing starts exit before rewriting data.
Pending setup/session/update or a competing managed start returns 75, other
failures return 1, and normal server exit statuses are propagated.
Server shutdown precedes native Steam `-shutdown` and desktop shutdown.

## Diagnostics

The appended `/home/container/logs/steam-launch.log` includes a UTC launch
marker, native backend, command, HOME, DISPLAY and working directory. Inspect
Steam's own `bootstrap_log.txt`, `webhelper.txt` and `cef_log.txt` under
`/home/container/.local/share/Steam/logs` if the window remains missing.
Neither an updater nor an open terminal is proof that the UI is ready.

Open the actual native client from the noVNC terminal:

```sh
/opt/fs25/fs25ctl.py steam > /home/container/logs/steam-native.log 2>&1
```

To inspect the server/Proton path after first-run setup and with the normal
game closed, run:

```sh
PROTON_LOG=1 /opt/fs25/fs25ctl.py start-webserver
```

The old `lsteamclient disabled` line was an intentional diagnostic from the
Windows-client Wine path, not proof of the cause of the missing GUI. It is not
part of native Linux Steam startup. Font warnings and X connection loss near
container shutdown also do not establish the original failure.

For map-loading analysis, `/opt/fs25/fs25ctl.py diagnose --seconds 10` reads CPU,
cgroup throttling, I/O and Wine sync descriptors. Use identical maps/mods,
savegames, DLC versions and resource limits for comparisons.

## GIANTS Wine and build verification

GIANTS keeps Kron4ek `wine-proton-11.0-2-amd64-wow64`, source commit
`dc26e61847081a1b5cb0733dc30feba6ee575482`, recipe commit
`fe137c65b411ff3079d1b26573dcb70bf16814a5`, and pinned SHA-256. WineHQ 11 is
the stable option. Source/notices are in `/opt/fs25/wine-source`. FFmpeg 4 ABI
libraries and notices remain private in `/opt/fs25/wine-compat`.

`config/wine-runtime.json` (`{"runtime":"proton","sync":"auto"}`) and optional
`FS25_WINE_RUNTIME`/`FS25_WINE_SYNC` affect GIANTS, not Steam's full Proton.
The standalone loader keeps its Linux Steam hook disabled. Its build smoke
tests exercise Wine x86/x64, synthetic Steam-named imports, text metrics,
worker threads and API probe statuses; they do not run native Linux Steam.

Run `python -m unittest discover -s tests -v`. Tests use temporary directories
and mocked subprocesses, no credentials. Linux CI also tests real flock.
Optional native Windows fixtures use `FS25_TEST_NATIVE_PROBE`,
`FS25_TEST_NATIVE_STUB`, `FS25_TEST_NATIVE_UNSUPPORTED`,
`FS25_TEST_NATIVE_IMPORTS` and `FS25_TEST_NATIVE_GUI`.

Before release, verify on a real Linux AMD64 Pelican node:

1. Native first-run Steam UI, login/Guard, client update and remembered login.
2. Steam Runtime/webhelper operation with the node's actual container settings.
3. Proton first-run setup, matching game/tool/library/prefix and Windows depots.
4. Live API check and dedicated-server launch; record missing host libraries.
5. Old-library reuse, interrupted downloads, native custom libraries and DLCs.
6. All autostart modes, no duplicate game/server, clean stop/restart.
7. Settings/mod/savegame persistence, DLC-dependent join/rejoin and GIANTS regression.

GitHub publishes latest and a commit-specific image on main. Test a candidate
image/server before release. For rollback, stop the container and select the
previous image/egg with matching prefix, game and configuration backups. Old
Windows Steam data remains intact, but reverting the image does not undo game
updates or changes to shared savegames.
