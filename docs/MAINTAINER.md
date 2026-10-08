# Maintainer guide

The [README](../README.md) covers normal panel use. This document covers runtime
implementation, diagnostics and release verification.

## Runtime and persistence

The image runs as the unprivileged `container` user. `entrypoint.py` owns desktop
startup and process supervision; `fs25ctl.py` owns installation, configuration
and manually invoked desktop actions. Both select `INSTALL_SOURCE=giants` by
default and reject unknown installation sources.

The Wine prefix stays at `/home/container/.fs25server`. GIANTS installation
keeps its existing Program Files link. In Steam mode, a new `C:\Steam` points
at `/home/container/steam/library`, containing the normal Windows client,
session files and its default `steamapps` library. An already existing
`C:\Steam` directory is preserved, not migrated. Steam's native login data is
never extracted or copied into egg variables. Additional libraries are read
from the client's `libraryfolders.vdf` and must resolve inside `/home/container`.
Unmapped/external library entries are skipped with a console message so they
do not hide a finished installation in another persistent library.

Both sources link the Wine user's FS25 Documents directory to the existing
configuration folder. Switching source keeps game installations separate but
shares settings, mods and savegames. Back up the configuration before running
a different game version against an existing savegame.

Steam owns its manifests and depot/DLC layout. Installation readiness requires
`appmanifest_2300320.acf`, `StateFlags=4`, finished download/staging counters,
no remaining download files, the launcher, dedicated server, game engine and
Steam API DLL. This does not enumerate the account's purchases: operators must
check the desired DLC selection and completed downloads in Steam. DLCs are
never copied blindly into the GIANTS `pdlc` directory.

The official Windows Steam bootstrap is pinned in the Dockerfile. If Valve
replaces the installer at its CDN URL, review the official installer and update
its checksum. An installed client updates itself normally in its persistent
directory. No account, game files or remembered login is included in the image.

## Steam session and startup

Steam login and Steam Guard happen exclusively in the normal Steam UI through
noVNC. There is no SteamCMD login, credential forwarding, console-code parser,
stored Guard code or desktop click automation.

`steam_session.c` is a small Windows executable compiled in a separate build
stage with MinGW. The final image includes neither MinGW nor Steam SDK files.
Toolchain runtime notices are retained in `/opt/fs25/steam-probe-licenses`.
The probe loads the installed game's own `steam_api64.dll`, calls the documented
flat Steam API initialization/user interfaces and `BLoggedOn`, then shuts the
API down. It never calls `SteamAPI_RestartAppIfNecessary` or launches the game.
Supported exported SteamUser versions are selected from the DLL itself.

Probe results:

| Exit | Output | Meaning |
| --- | --- | --- |
| `0` | `STEAM_SESSION_READY` | Steam API initialized and the user is logged on |
| `1` | `STEAM_SESSION_PENDING` | Client/login/online session is pending |
| `2` | `STEAM_API_*` | Missing argument, DLL load failure or unsupported API |

The Python controller invokes the probe under the same Wine prefix/user as
Steam and FS25. Path conversion and probing share the remaining timeout budget.
It writes `steam_appid.txt` with
ASCII `2300320` plus a newline into the game root and engine directory, and
sets the working directory to the game root. Saved `loginusers.vdf` files are
not treated as proof of a running authenticated session.

On an uninstalled/partial first setup, only the desktop and Steam stay online.
Complete downloads and first-run setup, close the game and restart. On a later
start, autostart waits at most 180 seconds for the live Steam API check. Pending
login or errors leave noVNC available instead of repeatedly restarting the
container. `AUTOSTART_SERVER=false` intentionally skips the session probe.

The egg uses `/opt/fs25/fs25ctl.py start-webserver` as its source-neutral startup
command. Panel and desktop starts both invoke this controller; the original
fixed GIANTS startup command is normalized to it as well. Unrelated custom
startup commands remain unchanged and do not acquire the controller's lock.

The controller holds a nonblocking Linux `flock` on
`/home/container/.fs25-webserver.lock` throughout preparation and the Wine
server's lifetime. A competing start exits before it prepares the prefix or
rewrites configuration. The lock file stays in place and the kernel releases
ownership when the controller exits. Never delete this file while a controller
is running. Installation readiness is rechecked immediately before launch.

Pending Steam setup/session/update and competing managed starts return status
`75`. Other failures return `1`; server exit statuses are propagated. In Steam
mode, failed launches leave noVNC available and cancel the pending automatic
game-start helper. This also covers an update beginning after the first session
check. A finished normal server stop still stops the container.

Supervised server process groups are signalled even if their launcher has
already exited. Forced shutdowns also wait for the child to be reaped. Server
shutdown precedes Steam's normal `-shutdown` request and desktop shutdown. The desktop server action checks
for an already running Web Interface/game process before starting another.

References: [Steamworks initialization](https://partner.steamgames.com/doc/sdk/api),
[ISteamUser / BLoggedOn](https://partner.steamgames.com/doc/api/ISteamUser),
[DLC installation](https://partner.steamgames.com/doc/store/application/dlc),
[official Steam download](https://store.steampowered.com/about/).

## Wine selection and provenance

The default is Kron4ek's prebuilt `wine-proton-11.0-2-amd64-wow64`, based on
Valve Wine commit `dc26e61847081a1b5cb0733dc30feba6ee575482`. The Dockerfile pins
the binary SHA-256 and build recipe commit
`fe137c65b411ff3079d1b26573dcb70bf16814a5`. WineHQ 11 remains the `stable`
compatibility option. Wine itself is not compiled by this project.

This image runs the real Windows Steam client, not Linux Steam's Proton
integration. `PROTON_DISABLE_LSTEAMCLIENT=1` is set in the image and enforced
before runtime selection, including the already-selected desktop-child path.
In the pinned Wine source, leaving this unset enables Steam DLL interception
and redirects `tier0_s64.dll` / `vstdlib_s64.dll` imports from `steamclient64.dll`
to `ntdll.dll`. Those redirects are inappropriate for our Windows client.
See [the pinned loader source](https://github.com/ValveSoftware/wine/blob/dc26e61847081a1b5cb0733dc30feba6ee575482/dlls/ntdll/loader.c#L1151-L1211).

The build smoke test loads synthetic Steam-named DLLs and their dependency
in both Windows architectures. The 64-bit fixture specifically exercises the
redirected `tier0_s64.dll` import; the 32-bit fixture verifies WoW64 dependency
loading. These are loader tests, not a real Steam login or UI compatibility
test. They run as `container` in the isolated smoke prefix and are removed
afterwards. The C source also runs natively on Windows for regression tests.

For an existing client reporting an installation error or
`ClientAPI_InitGlobalInstance`, first deploy the rebuilt image and fully stop
and restart the container. Reopen Steam in noVNC with autostart disabled.
Keep the existing prefix, client, library and saved login. The log's generic
32-bit-dependency message alone does not identify a missing Linux package.
If the error persists, close Steam, then run the existing controller manually
in the noVNC terminal with import diagnostics and retain the resulting log:

```sh
WINEDEBUG=+timestamp,+pid,+loaddll,+module /opt/fs25/fs25ctl.py steam > /home/container/logs/steam-imports.log 2>&1
```

This manual command leaves the existing client/data intact. Inspect the first
missing module, failed export or failed load in that log before changing
packages. Font warnings alone do not establish the cause of client failure.

Both automatic and desktop Steam starts use the client directory as working
directory (the installer uses its own directory). They enable Wine errors by
default while preserving an explicit diagnostic `WINEDEBUG` value. The launch
log appends a UTC launch marker, arguments, working directory and relevant
runtime flags; it is not overwritten on restart. This separates successive
starts, but does not timestamp every line emitted by Steam itself.
First/partial FS25 setup and `AUTOSTART_SERVER=false` launch the client visibly;
`-silent` is reserved for installed game autostarts.

Xvnc commonly has no render node. Steam's CEF GUI uses `-cef-disable-gpu` by
default; `FS25_STEAM_GPU=true` restores its normal GPU path if desired. This
does not change the FS25 game's graphics/synchronization options. It is a
headless compatibility setting, not proof of a particular CEF crash.
See [Valve's GPU-disabled launch recommendation](https://github.com/ValveSoftware/steam-for-linux/issues/11610).
The image includes Liberation TrueType substitutes and refreshes Fontconfig.
The build additionally measures text through Win32 GDI and creates/joins a
Win32 worker thread in each architecture. Successful substitutes/text metrics
are not a test of Steam's full CEF/DirectWrite UI or an authenticated login.

For a missing Steam window, inspect `bootstrap_log.txt`, `webhelper.txt` and
`cef_log.txt` inside `C:\Steam\logs` (normally
`/home/container/steam/library/logs`). Host/launcher logs alone may not identify
the failing client phase. The Linux shell command `steam` is not installed;
use `/opt/fs25/fs25ctl.py steam` to open the Windows client. X connection loss
can follow a container stop; an untimestamped worker-thread error adjacent to
it does not by itself establish the initial startup cause.

FFmpeg 4 ABI compatibility libraries come from Ubuntu 22.04 and remain private
to Wine in `/opt/fs25/wine-compat`. Package versions and licenses are included.
ELF search paths are changed only for Wine and those libraries; there is no
global desktop library replacement. The build rejects unresolved dependencies.

Stop the container before changing `/home/container/config/wine-runtime.json`:

```json
{"runtime": "proton", "sync": "auto"}
```

- Runtime: `proton` or `stable`.
- Synchronization: `auto`, `fsync` or `server`.
- Non-empty `FS25_WINE_RUNTIME` / `FS25_WINE_SYNC` override the respective values.
- Selection occurs once per container lifetime and is inherited by Steam,
  desktop, installer and server processes.

The isolated FSYNC probe checks `futex_waitv` and usable shared memory before
Wine starts. Failed/blocked probes disable FSYNC rather than blocking startup.
Wine may use accessible NTSync in `auto` mode. Environment flags and old logs
are not proof of the backend actually in use. The open-file soft limit is raised
to at most 65,536 without exceeding the inherited hard limit.

Wine's LGPL source/notices, exact source archive and builder recipe are in
`/opt/fs25/wine-source`; `RUNTIME.txt` records provenance. Project sources,
including the Steam session probe, are MIT-licensed.

References: [Wine-Proton binary](https://github.com/Kron4ek/Wine-Builds/releases/tag/proton-11.0-2),
[build recipe](https://github.com/Kron4ek/Wine-Builds/tree/fe137c65b411ff3079d1b26573dcb70bf16814a5),
[Wine source](https://github.com/ValveSoftware/wine/tree/dc26e61847081a1b5cb0733dc30feba6ee575482),
[NTSync](https://docs.kernel.org/userspace-api/ntsync.html).

## Loading diagnostics

While the game loads a map, run this existing manual controller command in
the noVNC terminal:

```text
/opt/fs25/fs25ctl.py diagnose --seconds 10
```

It reads process/thread CPU, cgroup quota/throttling, disk I/O, file descriptors
and historical i3d timings from the last 256 KiB of the game log. It does not
prepare the prefix, rewrite settings, restart processes or alter mods/caches.
The game log is `/home/container/config/FarmingSimulator2025/log.txt`.

Compare at least three identical runs, separating first load from subsequent
loads. Record CPU/storage, game/DLC versions, mod ZIPs, map and a copy of the same
savegame. A busy single thread can indicate a sequential stage; cgroup throttling
shows quota exhaustion, not every kind of host contention. Zero reads can reflect
the page cache. Neither Wine-Proton nor FSYNC guarantees Windows-equivalent times.

Empty optional egg variables preserve GIANTS settings. Ports always follow
allocations. The old generated one-year Web API interval is migrated once to
60 seconds; later GIANTS changes remain preserved. Web API refresh and engine
disconnect timeout are separate. Set pause-when-empty in GIANTS and leave
`SERVER_PAUSE` empty when GIANTS should own it.

## Verification before release

Run `python -m unittest discover -s tests -v`. The Python tests use temporary
directories and mocked subprocesses, never real credentials or server data.
Linux CI additionally tests real lock contention/release; Windows tests use a
mock for `flock`, and the real Linux lock test is skipped there.
Optional native tests use `FS25_TEST_NATIVE_PROBE`, `FS25_TEST_NATIVE_STUB` and
`FS25_TEST_NATIVE_UNSUPPORTED` paths to compiled probe/test DLLs on Windows.
`FS25_TEST_NATIVE_IMPORTS` optionally points to a directory containing
`steam-import-32` and `steam-import-64`, each with the compiled import probe,
Steam-named fixture and dependency DLL. These native tests also cover missing
dependencies, Unicode paths and missing arguments.
`FS25_TEST_NATIVE_GUI` can point to the same layout with `steam-gui-test.exe`
in each architecture directory for native Windows text/thread tests.
The Docker build compiles the probe and test DLLs and exercises readiness,
offline and unsupported-API results under Wine, alongside both Windows command
interpreters. Test DLLs and the smoke prefix are removed from the final image.

These tests do not replace live FS25 acceptance. Before publishing, verify:

1. New/existing GIANTS installation, activation, desktop and sequential DLCs.
2. First Steam install, noVNC login/Guard, remembered login after recreation,
   expired session, client update and client closed unexpectedly.
3. Full/partial game downloads, resumed downloads, custom persistent library,
   disk exhaustion and game-file verification.
4. Desired DLC selection and a DLC-dependent savegame with join/rejoin.
5. All autostart modes; Web Interface and desktop start actions; no double game.
6. Game save/load, normal stop/restart, retained GIANTS settings and allocations.
7. Steam game/DLC update with autostart disabled, then normal restart.

The GitHub workflow publishes `latest` and a commit-specific tag when `main`
changes. Validate a test image/server before merging or pushing to `main`.
Do not present a stub test or successful image build as an authenticated live
Steam/FS25 test.

For rollback, record the old image digest and stop the container. Restore a
matching Wine-prefix, installation/library and configuration backup and select
the old image/egg. Reverting only the image does not revert Steam game updates.
Keep Steam and the game stopped while restoring the matching backups.
