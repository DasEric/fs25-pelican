# Maintainer guide

## Release status

The desktop/update changes have local regression coverage. A Docker build and
live-node acceptance are separate release gates, not implied by unit tests.
The prior live SteamCMD test downloaded Windows FS25 and five entitled DLCs,
but the real Steam API probe returned `STEAM_SESSION_PENDING`; the game did
not start successfully. Do not label a successful download or web page as a
working licensed game server. The normal Windows client also had an unresolved
login e20 in that test. This change does not claim that problem is fixed.

## Persistent profile binding

The controller queries `SHGetFolderPathW(CSIDL_PERSONAL, SHGFP_TYPE_CURRENT)`
through the existing native helper's `--documents` mode under the selected
Wine/Proton runtime. It never infers the Windows user from Linux `USER`.
Redirected Documents and native Proton's actual prefix are respected; failed,
ambiguous or non-persistent paths stop configuration instead of creating a
second, guessed profile. The Docker smoke tests exercise the helper in both
bundled Wine runtimes.

`Documents/My Games/FarmingSimulator2025` links to the persistent profile,
normally `/home/container/config/FarmingSimulator2025`. Windows Steam binds it
before client/game first run, without waiting for a Steam API login. Native
Steam waits for its real Proton first-run metadata; both backends also check
the binding at dedicated-server startup. A running game/web service prevents
early Steam configuration changes.

Persistent files win over old prefix files. The previous directory is retained
as `.before-fs25-link[-N]`; only missing files are imported. An existing
`savegameN` is never combined with files from a different farm. Copy/link failures
retain or restore the original Wine directory. Non-empty Egg fields override
their corresponding XML values; empty fields retain existing settings. XML
parse/root errors stop the operation and do not silently regenerate defaults.

A profile-root `dedicatedServerConfig.xml` is copied into `dedicated_server`
only when that destination is absent. Originals
remain untouched. Active `gameSettings.xml` mod-directory overrides are disabled
(with an XML backup) so uploaded PC drive letters do not hide the profile's mods.

Live acceptance after deploying the image: verify the logged persistent and
Windows profile paths, Egg override labels (never password values), and selected
slot. Confirm in GIANTS that the intended savegame and mods appear, then start
and save the farm, restart the container and verify persistence. Local tests do
not substitute for this licensed-game/node check.

## Service ownership

- `entrypoint.py` supervises Xvnc for the container lifetime. Web-service exit,
  including status 0 in GIANTS mode, leaves the desktop online. No automatic
  restart loop runs after web-service exit. The game-autostart helper is stopped.
- Panel startup uses `start-webserver`. Desktop startup uses `launch-webserver`,
  which detaches that same controller with `start_new_session=True` and opens
  `tail -F` as a disposable log viewer. It does not make XFCE or the terminal
  the server's parent session.
- `.fs25-webserver.lock` is a nonblocking lifetime flock. Its inode is never
  unlinked. A competing start cannot rewrite configuration before the check.
- `.fs25-webserver.json` identifies the controller using PID and `/proc` start
  time. Stop validates UID, command and identity; Linux pidfds prevent signalling
  a recycled PID. Unmanaged/legacy services require manual closure, not broad kills.
- The controller owns a separate Wine/Proton process group and forwards stop
  signals. Container cleanup includes detached desktop controllers.
- `.fs25-start-cancelled` cancels pending HTTP game-start requests. Only actual
  container boot resets this intent. Manual desktop starts do not rearm old helpers.

## Maintenance transaction

`.fs25-maintenance.lock` permits concurrent short shared startup checks, but
stop/update/install operations require an exclusive nonblocking lease.
Panel Steam launches and API checks use the same lease. Starts fail with status
75 during maintenance; they are not queued for an unexpected later launch.
Locks are held by the process, not inferred from stale PID files.

The update requires a terminal. Before changing game files it checks for a
running game/installer, cancels autostart, stops the verified web controller,
and requests normal Steam shutdown. Existing SteamCMD processes are not killed.
Process-inspection errors fail closed. VNC/XFCE are not update targets.

`.fs25-update-incomplete` is created before update work and survives cancellation,
process errors, missing DLCs, failed confirmation or post-update checks. Both
server-start paths refuse this marker. A successful retry checks files/depots,
repairs App-ID files, reapplies configuration/web patches, then removes it.
It does not automatically restart the service or game.

In normal Steam mode the updater opens `steam://validate/2300320`; an operator
finishes downloads, exits Steam and confirms `UPDATED`. The lease remains held.
GIANTS runs the original launcher, whose official updater is interactive; the
same explicit completion confirmation is required. No invented GIANTS update
flags or guessed version checks are used. Upload new DLC installers separately.

## Steam runtimes and downloads

`INSTALL_SOURCE=steam` defaults to Windows Steam under the checksum-pinned
official WineHQ Staging 11.19 trixie amd64/i386 packages. Client, game and probe
use `/opt/wine-staging`, prefix `/home/container/.fs25steam`, client directory
`/home/container/steam/library`, mapped as `C:\Steam`. The GIANTS prefix
`/home/container/.fs25server` and native Steam installation are retained.
CEF GPU rendering is disabled by default; the CEF sandbox is not disabled.

`FS25_STEAM_BACKEND=linux` is an explicit alternative using the full Proton
installation selected by Steam. It requires the node/container's user-namespace
support and first-run `compatdata/2300320/config_info`. Do not silently mix its
DLLs/prefix with standalone Wine. `STEAM_DOWNLOAD_MODE=steamcmd` is currently
limited to the Windows backend; no Proton metadata is fabricated.

`STEAM_DOWNLOAD_MODE=client` remains the default. `steamcmd` is opt-in and uses:

| Data | Path |
| --- | --- |
| Immutable official bootstrap | `/opt/fs25/steamcmd` |
| Writable self-updating client | `/home/container/steamcmd/client` |
| Private client HOME / remembered login | `/home/container/steamcmd/home` |
| Windows game / force_install_dir | `/home/container/steamcmd/game` |
| Own manifest | `/home/container/steamcmd/game/steamapps/appmanifest_2300320.acf` |
| Nonsecret entitlement/depot receipt | `/home/container/steamcmd/verified-dlcs.json` |

The bootstrap archive is pinned by SHA-256 and its ELF dependencies are checked
in Docker. Persistent client/home data is writable and private. No GUI manifests,
libraries, game files or account data are copied between the two download modes.
Switching modes selects another installation; it does not migrate or delete the old one.

The account name is requested interactively and passed as SteamCMD's login
identifier. **Passwords and Guard codes are entered into SteamCMD's inherited
terminal, never command arguments, environment variables, egg fields or log files.**
SteamCMD itself manages its remembered session. The controller forwards prompts
immediately, captures metadata only in bounded memory, and restores signal handlers
and reaps children on cancellation. No account identifier is saved in the receipt.

Three authenticated phases use Valve's remembered login: current app information,
live entitlement queries for dynamically discovered DLC IDs, then `app_update
2300320 validate`. Authentication can be requested again by Steam in any phase.
License answers are parsed from live stdout, not `console_log.txt`, which omitted
them in the prior live test. Active package app lists and explicit no-license
answers must cover every requested ID before a game download is allowed.

Relevant public Windows/64-bit/English DLC depots are selected from live metadata.
Unowned, Mac-only, other-language and unpublished depots are not blindly required.
After downloading, the completed manifest, required binaries, owned DLC depot
presence and public manifest IDs are checked. Missing optional content fails
explicitly rather than silently reporting success. The controller does not
fabricate ownership or assume a successful base-game download includes every DLC.

The CLI force-install root is resolved separately from normal Steam's
`steamapps/common/<installdir>`. Paths must remain under persistent HOME.
Both download modes still require the real game's `SteamAPI_Init`/`BLoggedOn`
readiness check before web-server launch. A CLI login is not substituted for it.
`steam_appid.txt` is maintained as `2300320\n` at root and the engine directory.

## GIANTS runtime

The independent GIANTS Wine-Proton 11.0-2 and WineHQ stable option are unchanged.
Sources/notices live in `/opt/fs25/wine-source`; private FFmpeg ABI libraries and
notices live in `/opt/fs25/wine-compat`. `config/wine-runtime.json` and
`FS25_WINE_RUNTIME`/`FS25_WINE_SYNC` apply to GIANTS, not Steam's full Proton.
The standalone Linux Steam hook stays disabled. No host security settings are changed.

## Verification and publication

Run `python -m unittest discover -s tests -v`. Temporary fixtures/mock processes
test lifecycle, shutdown ordering, lock conflicts, cancellation, manifest/DLC
failures and preservation of settings/savegames. Linux CI additionally exercises
real flock. Native probe fixtures are optional locally and exercised by Docker's
Wine smoke tests. Mocks are not live client/game evidence.

Before merging to `main`, test the candidate image on a Linux AMD64 node:

1. Fresh and existing GIANTS/Steam installations; all three autostart modes.
2. Close the log window, reconnect noVNC, stop/restart the web service, induce
   web-service exit 0/nonzero: desktop stays online and no duplicate/restart occurs.
3. Update during a running game/installer, simultaneous double clicks, start
   during maintenance, expired login, Guard, cancelled/failed/partial downloads.
4. Normal client update confirmation and SteamCMD's three terminal-auth phases;
   owned/unowned/optional DLCs, current public depot IDs and interrupted-update retry.
5. Real Steam API readiness, dedicated game-server launch, map loading, and a
   successful player join with DLCs. Check process limits and persisted login.
6. Full settings/mod/savegame persistence and GIANTS regression.

`codex/**` pushes publish only commit-specific `sha-<commit>` candidate images;
they never replace `latest`. `main` publishes `latest` and the SHA tag after tests
and a successful Docker build. Live-node acceptance is a manual gate before merge.
To revert a deployed update, stop the container and restore the matching game,
prefix/configuration backup and previous image/egg. Reverting source alone does
not roll back downloaded game updates or shared savegames.

## References

- [Valve: SteamCMD / SteamPipe](https://partner.steamgames.com/doc/sdk/uploading)
- [Valve: DLC installation](https://partner.steamgames.com/doc/store/application/dlc)
- [Valve: depot filtering](https://partner.steamgames.com/doc/store/application/depots)
- [Valve: Steam API initialization](https://partner.steamgames.com/doc/sdk/api#initialization_and_shutdown)
- [GIANTS: official update workflow](https://farming-simulator.com/updates.php?country=us&lang=en)

Diagnostics: `/opt/fs25/fs25ctl.py diagnose --seconds 10`; client logs are in
`logs/steam-launch.log`, game/service output in `logs/dedicated-server.log`.

To request the normal client from a noVNC terminal (its own output remains in
`steam-launch.log`):

```sh
/opt/fs25/fs25ctl.py steam > /home/container/logs/steam-command.log 2>&1
```
