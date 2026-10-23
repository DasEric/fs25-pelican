#!/usr/bin/env python3
"""Farming Simulator 25 runtime helpers shipped inside the Pelican image."""

from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import json
import os
import pathlib
import platform
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


HOME = pathlib.Path(os.environ.get("FS25_HOME", "/home/container"))
PREFIX = pathlib.Path(os.environ.get("WINEPREFIX", str(HOME / ".fs25server")))
GAME_DIR = HOME / "game" / "Farming Simulator 2025"
CONFIG_DIR = HOME / "config" / "FarmingSimulator2025"
DEDICATED_DIR = CONFIG_DIR / "dedicated_server"
INSTALLER_DIR = HOME / "installer"
DLC_DIR = HOME / "dlc"
PDLC_DIR = CONFIG_DIR / "pdlc"
LOG_DIR = HOME / "logs"
DESKTOP_DIR = HOME / "Desktop"
SERVER_EXE = GAME_DIR / "dedicatedServer.exe"
GAME_EXE = GAME_DIR / "FarmingSimulator2025.exe"
WINE_GAME_DIR = PREFIX / "drive_c" / "Program Files (x86)" / "Farming Simulator 2025"
WINE_CONFIG_DIR = (
    PREFIX / "drive_c" / "users" / os.environ.get("USER", "container")
    / "Documents" / "My Games" / "FarmingSimulator2025"
)
PROTON_DIR = pathlib.Path("/opt/fs25/wine")
STABLE_DIR = pathlib.Path("/opt/wine-stable")
STEAM_WINE_DIR = pathlib.Path("/opt/wine-staging")
STEAM_WINDOWS_PREFIX = HOME / ".fs25steam"
STEAM_WINDOWS_DIR = HOME / "steam" / "library"
STEAM_SETUP = pathlib.Path("/opt/fs25/SteamSetup.exe")
STEAM_APP_ID = "2300320"
STEAM_DIR = HOME / ".local" / "share" / "Steam"
STEAM_LIBRARY_DIR = STEAM_DIR
STEAM_LAUNCHER = pathlib.Path("/usr/bin/steam")
STEAM_SESSION_HELPER = pathlib.Path("/opt/fs25/steam-session.exe")
STEAMCMD_BOOTSTRAP = pathlib.Path("/opt/fs25/steamcmd")
PROC_DIR = pathlib.Path("/proc")


class SteamNotReady(RuntimeError):
    """The desktop stays available while Steam installation/login is pending."""


class ServerAlreadyRunning(RuntimeError):
    """Another controller or game already owns the server start."""


class MaintenanceBusy(RuntimeError):
    """A start/update conflicts with another operation or an unfinished update."""


@contextlib.contextmanager
def activity_lock(*, exclusive: bool = False):
    """Short shared startup leases; exclusive stop/update lease. Never unlink."""
    import fcntl

    HOME.mkdir(parents=True, exist_ok=True)
    with (HOME / ".fs25-maintenance.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusy("Another FS25 start, installation or maintenance operation is active; retry after it finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_update_complete() -> None:
    if (HOME / ".fs25-update-incomplete").exists():
        raise MaintenanceBusy("The previous update was interrupted or failed. Run Update FS25 again before starting the server")


def steam_download_mode() -> str:
    mode = env("STEAM_DOWNLOAD_MODE", "client").strip().lower()
    if mode not in {"client", "steamcmd"}:
        raise ValueError("STEAM_DOWNLOAD_MODE must be client or steamcmd")
    if mode == "steamcmd" and steam_backend() != "windows":
        raise ValueError("SteamCMD content currently requires the Windows Steam/Wine runtime; native Proton needs its own first-run metadata")
    return mode


def log(message: str) -> None:
    print(f"[FS25] {message}", flush=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def true_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def installation_source() -> str:
    source = env("INSTALL_SOURCE", "giants").strip().lower()
    if source not in {"giants", "steam"}:
        raise ValueError("INSTALL_SOURCE must be giants or steam")
    return source


def steam_backend() -> str:
    # Native Steam requires user namespaces that a normal Wings container does
    # not grant. Windows Steam/Wine is the image's namespace-free default.
    backend = env("FS25_STEAM_BACKEND", "windows").strip().lower()
    if backend not in {"windows", "linux"}:
        raise ValueError("FS25_STEAM_BACKEND must be windows or linux")
    return backend


def steam_client_directory() -> pathlib.Path:
    return STEAM_WINDOWS_DIR if steam_backend() == "windows" else STEAM_DIR


def read_vdf(path: pathlib.Path) -> dict:
    """Read Steam's text KeyValues files without executing directives or includes."""
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"Steam metadata is too large: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    return parse_vdf(text)


def parse_vdf(text: str) -> dict:
    """The same strict parser also handles public SteamCMD app information."""
    if len(text) > 2 * 1024 * 1024:
        raise ValueError("Steam metadata is too large")
    token = re.compile(r'\s+|//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
    tokens = []
    offset = 0
    while offset < len(text):
        match = token.match(text, offset)
        if match is None:
            raise ValueError("Malformed Steam metadata")
        value = match.group()
        offset = match.end()
        if value.isspace() or value.startswith("//"):
            continue
        if value.startswith('"'):
            value = re.sub(r'\\([\\"])', r'\1', value[1:-1])
            tokens.append((value, False))
        else:
            tokens.append((value, value in {"{", "}"}))
    position = 0

    def object_values(nested: bool = False, depth: int = 0) -> dict:
        nonlocal position
        if depth > 16:
            raise ValueError("Steam metadata is too deeply nested")
        result = {}
        while position < len(tokens):
            key, structural = tokens[position]
            position += 1
            if structural and key == "}" and nested:
                return result
            if structural or position >= len(tokens):
                raise ValueError("Malformed Steam metadata")
            value, structural = tokens[position]
            position += 1
            if structural:
                if value != "{":
                    raise ValueError("Malformed Steam metadata")
                value = object_values(True, depth + 1)
            key = key.casefold()
            if key in result:
                raise ValueError("Duplicate Steam metadata key")
            result[key] = value
        if nested:
            raise ValueError("Incomplete Steam metadata")
        return result

    return object_values()


def persistent_library_path(value: str) -> pathlib.Path:
    """Resolve native libraries without translating paths through a Wine prefix."""
    if steam_backend() == "windows" and not value.startswith("/"):
        windows = pathlib.PureWindowsPath(value)
        if not windows.is_absolute() or not re.fullmatch(r"[a-zA-Z]:", windows.drive):
            raise ValueError("Steam library must use an absolute local drive path")
        if ".." in windows.parts:
            raise ValueError("Steam library path must not contain parent traversal")
        drive = windows.drive.lower()
        if drive == "c:":
            path = STEAM_WINDOWS_PREFIX / "drive_c"
        elif drive == "z:":
            path = pathlib.Path("/")
        else:
            path = STEAM_WINDOWS_PREFIX / "dosdevices" / drive
            if not path.is_dir():
                raise ValueError("Steam library drive is not mapped in this Wine prefix")
        path = path.joinpath(*windows.parts[1:])
    else:
        path = pathlib.Path(value)
    if not path.is_absolute():
        raise ValueError("Linux Steam library path must be absolute")
    path = path.resolve()
    if not path.is_relative_to(HOME.resolve()):
        raise ValueError("Steam library must be inside /home/container to remain persistent")
    return path


def steam_libraries() -> list[pathlib.Path]:
    client = steam_client_directory()
    libraries = [client, STEAM_LIBRARY_DIR] if steam_backend() == "linux" else [client]
    for metadata in (client / "steamapps/libraryfolders.vdf", client / "config/libraryfolders.vdf"):
        if not metadata.is_file():
            continue
        folders = read_vdf(metadata).get("libraryfolders", {})
        if not isinstance(folders, dict):
            raise ValueError("Malformed Steam library list")
        for key, entry in folders.items():
            if not key.isdecimal():
                continue
            value = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(value, str):
                raise ValueError("Steam library path is missing")
            try:
                libraries.append(persistent_library_path(value))
            except ValueError as exc:
                # A removed drive or unrelated external library must not hide a
                # complete FS25 installation in another persistent library.
                log(f"Skipping Steam library {key}: {exc}")
    result = []
    seen = set()
    for library in libraries:
        # The client and its default library can name the same directory.
        identity = (library / "steamapps").resolve()
        if identity not in seen:
            result.append(library)
            seen.add(identity)
    return result


def steam_game_directory() -> pathlib.Path:
    """Require a finished Steam manifest, not just files left by a partial download."""
    try:
        cli = steam_download_mode() == "steamcmd"
        libraries = [HOME / "steamcmd/game"] if cli else steam_libraries()
        candidates = []
        pending = None
        for library in libraries:
            steamapps = library / "steamapps"
            manifest = steamapps / f"appmanifest_{STEAM_APP_ID}.acf"
            if not manifest.is_file():
                continue
            state = read_vdf(manifest).get("appstate", {})
            if not isinstance(state, dict) or state.get("appid") != STEAM_APP_ID:
                raise ValueError("FS25 Steam manifest has an unexpected app ID")
            folder = state.get("installdir", "")
            if not cli and (not isinstance(folder, str) or not folder or folder in {".", ".."} or any(char in folder for char in '/\\:')):
                raise ValueError("FS25 Steam installation directory is invalid")
            common = library.resolve() if cli else (steamapps / "common").resolve()
            directory = common if cli else (common / folder).resolve()
            if not directory.is_relative_to(common) or not directory.is_relative_to(HOME.resolve()):
                raise ValueError("FS25 Steam installation must remain inside /home/container")
            downloading = steamapps / "downloading" / STEAM_APP_ID

            def counter(name: str) -> int:
                value = state.get(name, "0")
                if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                    raise ValueError(f"Invalid Steam manifest counter: {name}")
                return int(value)

            incomplete = counter("stateflags") != 4
            for total, done in (("bytestodownload", "bytesdownloaded"), ("bytestostage", "bytesstaged")):
                incomplete |= counter(total) > counter(done)
            incomplete |= downloading.is_dir() and any(downloading.iterdir())
            engine = any((directory / name).is_file() for name in ("FarmingSimulator2025Game.exe", "x64/FarmingSimulator2025Game.exe"))
            api = any((directory / name).is_file() for name in ("steam_api64.dll", "x64/steam_api64.dll"))
            complete = (directory / "dedicatedServer.exe").is_file() and (directory / "FarmingSimulator2025.exe").is_file() and engine and api
            if incomplete or not complete:
                pending = "Finish the FS25 game/DLC downloads and file verification in Steam, then restart the container."
                continue
            candidates.append(directory)
        if len(candidates) > 1:
            raise ValueError("Multiple FS25 Steam installations found; keep one installation in Steam Storage settings")
        if candidates:
            return candidates[0]
        raise SteamNotReady(pending or "Install FS25 in Steam through noVNC, finish first-run setup, then restart the container.")
    except (OSError, ValueError) as exc:
        raise SteamNotReady(f"Steam installation is not ready ({exc}). Check Steam in noVNC, then restart the container.") from exc


def game_directory() -> pathlib.Path:
    return steam_game_directory() if installation_source() == "steam" else GAME_DIR


def prepare_steam_layout() -> None:
    if steam_backend() == "windows":
        target = STEAM_WINDOWS_PREFIX / "drive_c" / "Steam"
        target.parent.mkdir(parents=True, exist_ok=True)
        STEAM_WINDOWS_DIR.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() and target.resolve() == STEAM_WINDOWS_DIR.resolve():
            return
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"Unexpected Steam directory at {target}; existing data retained")
        target.symlink_to(STEAM_WINDOWS_DIR, target_is_directory=True)
        return
    # Valve's launcher uses this default path, wholly inside the persistent HOME.
    # Do not reuse Windows client files or rewrite Steam's own library metadata.
    STEAM_DIR.mkdir(parents=True, exist_ok=True)


def steam_executable() -> pathlib.Path | None:
    if steam_backend() == "windows":
        return next((path for name in ("steam.exe", "Steam.exe")
                     if (path := STEAM_WINDOWS_DIR / name).is_file()), None)
    return STEAM_LAUNCHER if STEAM_LAUNCHER.is_file() else None


def steam_command(*, silent: bool = False) -> list[str]:
    executable = steam_executable()
    if executable is None:
        raise RuntimeError("The Steam launcher is missing; pull the updated FS25 image")
    gui = [] if true_value(env("FS25_STEAM_GPU", "false")) else ["-cef-disable-gpu"]
    if steam_backend() == "windows":
        return [*steam_client_affinity(), str(STEAM_WINE_DIR / "bin/wine"), str(executable), *gui, *(["-silent"] if silent else [])]
    return [str(executable), *gui, *(["-silent"] if silent else [])]


def clean_steam_environment() -> dict[str, str]:
    """Keep the desktop session but isolate Linux Steam/Proton from GIANTS Wine."""
    result = os.environ.copy()
    for name in list(result):
        if name.startswith(("WINE", "PROTON_", "STEAM_COMPAT_", "FS25_SELECTED_WINE", "_FS25_RUNTIME")) or name in {
            "LD_LIBRARY_PATH", "LD_PRELOAD", "SteamAppId", "SteamGameId", "SteamOverlayGameId",
        }:
            result.pop(name, None)
    bins = {str(PROTON_DIR / "bin"), str(STABLE_DIR / "bin"), str(STEAM_WINE_DIR / "bin")}
    result["PATH"] = os.pathsep.join(part for part in result.get("PATH", os.defpath).split(os.pathsep) if part not in bins)
    result["HOME"] = str(HOME)
    result["XDG_DATA_HOME"] = str(HOME / ".local/share")
    return result


def steam_launch_context(*, silent: bool = False) -> tuple[list[str], pathlib.Path, dict[str, str]]:
    """Shared desktop/panel launch; the installed client never reruns its installer."""
    command = steam_command(silent=silent)
    windows = steam_backend() == "windows"
    cwd = STEAM_WINDOWS_DIR if windows else HOME
    launch_env = windows_steam_environment() if windows else clean_steam_environment()
    wine_index = command.index(str(STEAM_WINE_DIR / "bin/wine")) if windows else 0
    log(f"Starting {steam_backend()} Steam: {command[wine_index + 1] if windows else command[0]}")
    log(f"Steam working directory: {cwd}; UI: {'background' if silent else 'visible'}.")
    log(f"Steam client logs: {steam_client_directory() / 'logs'}.")
    log("An open terminal or updater is not proof that the Steam login/library window is ready.")
    return command, cwd, launch_env


def windows_steam_environment() -> dict[str, str]:
    result = clean_steam_environment()
    bins = str(STEAM_WINE_DIR / "bin")
    result.update({
        "PATH": bins + os.pathsep + result.get("PATH", os.defpath),
        "WINEPREFIX": str(STEAM_WINDOWS_PREFIX), "WINEARCH": "win64",
        "WINESERVER": str(STEAM_WINE_DIR / "bin/wineserver"),
        "WINEDLLOVERRIDES": "mscoree=d", "WINEDEBUG": "-all,err+all",
    })
    return result


def steam_client_affinity() -> list[str]:
    # Derive CPU IDs from the actual container allowance, never assume CPU 0
    # exists. This applies to the client/initializer, not the dedicated server.
    if not hasattr(os, "sched_getaffinity"):
        return []
    allowed = sorted(os.sched_getaffinity(0))[:2]
    if not allowed:
        raise RuntimeError("The container has no allowed CPU for Steam")
    return ["/usr/bin/taskset", "-c", ",".join(map(str, allowed))]


def prepare_windows_steam() -> None:
    """Bootstrap only this backend's prefix; retain GIANTS/native Steam data."""
    import fcntl

    ensure_directories()
    with (HOME / ".fs25-steam-setup.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        process_env = windows_steam_environment()
        wine = str(STEAM_WINE_DIR / "bin/wine")
        if not pathlib.Path(wine).is_file():
            raise RuntimeError("Wine 11.19 for Steam is missing; pull the updated FS25 image")
        try:
            initialized = prefix_files_exist(STEAM_WINDOWS_PREFIX)
        except OSError:
            initialized = False
        if not initialized:
            status = run_logged([*steam_client_affinity(), str(STEAM_WINE_DIR / "bin/wineboot"), "--init"],
                                LOG_DIR / "steam-wineboot.log", 240, process_env=process_env)
            try:
                initialized = prefix_files_exist(STEAM_WINDOWS_PREFIX)
            except OSError:
                initialized = False
            if status != 0 or not initialized:
                raise RuntimeError(f"Steam Wine prefix initialization exited with status {status}; data retained")
        prepare_steam_layout()
        if steam_executable() is None:
            if not STEAM_SETUP.is_file():
                raise RuntimeError("The bundled Steam installer is missing; pull the updated FS25 image")
            log("Installing the official Steam client into persistent storage.")
            # NSIS /D must be last. The normal client performs its own update.
            status = run_logged([*steam_client_affinity(), wine, str(STEAM_SETUP), "/S", r"/D=C:\Steam"],
                                LOG_DIR / "steam-install.log", 240, process_env=process_env)
            if status != 0 or steam_executable() is None:
                raise RuntimeError(f"Steam installation exited with status {status}; check logs/steam-install.log")


def steam_game_launch_context(directory: pathlib.Path, arguments: list[str]) -> tuple[list[str], dict[str, str]]:
    if steam_backend() == "linux":
        return proton_launch_context(directory, arguments)
    result = windows_steam_environment()
    result.update({"SteamAppId": STEAM_APP_ID, "SteamGameId": STEAM_APP_ID})
    return [str(STEAM_WINE_DIR / "bin/wine"), *arguments], result


def proton_launch_context(directory: pathlib.Path, arguments: list[str], *, verb: str = "run") -> tuple[list[str], dict[str, str]]:
    """Use the full Proton installation last used by Steam for this exact game."""
    compatdata = directory.parent.parent / "compatdata" / STEAM_APP_ID
    info = compatdata / "config_info"
    try:
        if info.stat().st_size > 64 * 1024:
            raise ValueError("Proton prefix metadata is too large")
        lines = info.read_text(encoding="utf-8").splitlines()
        # Valve Proton records: prefix version, fonts directory, lib directory,
        # Steam directory. Use that record instead of guessing an installed tool.
        libs = pathlib.Path(lines[2])
        if not libs.is_absolute() or libs.name != "lib" or libs.parent.name != "files":
            raise ValueError("Unexpected Proton library path")
        tool = libs.parent.parent.resolve()
        if not tool.is_relative_to(HOME.resolve()):
            raise ValueError("Proton must be installed in the persistent Steam library")
        for path in (tool / "proton", tool / "files/bin/wine", tool / "files/bin/wineserver", compatdata / "pfx/system.reg"):
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Proton first-run files are missing: {path.name}")
        bridge = libs / "wine" / "x86_64-unix" / "lsteamclient.so"
        if not bridge.is_file():
            raise ValueError("Full Proton's Linux Steam bridge is missing")
    except (OSError, ValueError, IndexError) as exc:
        raise SteamNotReady(
            f"Proton setup is pending ({exc}). In Linux Steam, select Proton 11 in FS25 Properties > Compatibility, "
            "start FS25 once, then close the game before starting the web server."
        ) from exc
    compatdata = compatdata.resolve()
    if not compatdata.is_relative_to(HOME.resolve()) or not (compatdata / "pfx").resolve().is_relative_to(HOME.resolve()):
        raise SteamNotReady("FS25's Proton prefix must remain inside the persistent Steam library")
    process_env = clean_steam_environment()
    process_env.update({
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(STEAM_DIR.resolve()),
        "STEAM_COMPAT_DATA_PATH": str(compatdata),
        "STEAM_COMPAT_INSTALL_PATH": str(directory),
        "STEAM_COMPAT_LIBRARY_PATHS": str(directory.parent.parent.parent),
        "SteamAppId": STEAM_APP_ID, "SteamGameId": STEAM_APP_ID,
        "PROTON_DISABLE_LSTEAMCLIENT": "0",
        "PROTON_LOG_DIR": str(LOG_DIR),
        # The server is already inside the Pelican container. Execute Proton's
        # own launcher; never mix its Wine DLLs with /opt/fs25/wine or WineHQ.
        "LD_LIBRARY_PATH": "/opt/fs25/wine-compat",
    })
    for name in ("PROTON_LOG", "PROTON_USE_WINED3D", "PROTON_NO_FSYNC", "PROTON_NO_NTSYNC"):
        if name in os.environ:
            process_env[name] = os.environ[name]
    trace = env("WINEDEBUG", "-all")
    process_env["WINEDEBUG"] = "-all,err+all" if trace in {"", "-all"} else trace
    return [sys.executable, str(tool / "proton"), verb, *arguments], process_env


def prepare_proton_config(process_env: dict[str, str]) -> None:
    if steam_backend() == "windows":
        target = pathlib.Path(process_env["WINEPREFIX"]) / "drive_c/users" / env("USER", "container") / "Documents/My Games/FarmingSimulator2025"
    else:
        target = pathlib.Path(process_env["STEAM_COMPAT_DATA_PATH"]) / "pfx/drive_c/users/steamuser/Documents/My Games/FarmingSimulator2025"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == CONFIG_DIR.resolve():
        return
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise SteamNotReady(f"Unexpected FS25 Documents link: {target}; retain it and check its destination")
    if target.exists():
        # First normal game launch may have created defaults. Preserve them in
        # the prefix; never replace existing server settings/savegames with them.
        backup = target.with_name(target.name + ".before-fs25-link")
        count = 1
        while backup.exists():
            backup = target.with_name(target.name + f".before-fs25-link-{count}")
            count += 1
        target.rename(backup)
        for source in backup.rglob("*"):
            destination = CONFIG_DIR / source.relative_to(backup)
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        log(f"First-run FS25 settings retained at {backup}; existing server data takes priority.")
    target.symlink_to(CONFIG_DIR, target_is_directory=True)


def open_steam() -> None:
    if installation_source() != "steam":
        raise RuntimeError("Select INSTALL_SOURCE=steam in the panel to use the Steam installation")
    with activity_lock():
        prepare()
        ensure_directories()
        log("Use Steam in noVNC to sign in, complete Steam Guard and install/manage FS25 and its DLCs.")
        command, cwd, launch_env = steam_launch_context()
        # The client is not owned by this terminal. Maintenance can shut it down
        # through Steam's normal -shutdown command, without killing the desktop.
        with (LOG_DIR / "steam-launch.log").open("a", encoding="utf-8") as output:
            subprocess.Popen(command, cwd=cwd, env=launch_env, stdin=subprocess.DEVNULL,
                             stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def ensure_steam_appid(directory: pathlib.Path) -> None:
    directories = {directory}
    for name in ("FarmingSimulator2025Game.exe", "x64/FarmingSimulator2025Game.exe"):
        executable = directory / name
        if executable.is_file():
            directories.add(executable.parent)
    for parent in directories:
        path = parent / "steam_appid.txt"
        contents = (STEAM_APP_ID + "\n").encode("ascii")
        if path.is_file() and path.read_bytes() == contents:
            continue
        if path.exists():
            shutil.copy2(path, path.with_suffix(".txt.bak"))
        temp = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=parent, delete=False) as output:
                temp = pathlib.Path(output.name)
                output.write(contents)
            os.replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


def steam_session_ready(directory: pathlib.Path, *, timeout: float = 25) -> bool:
    """Ask the installed game's Steam API; saved login files are not session proof."""
    deadline = time.monotonic() + timeout
    if steam_backend() == "windows" and steam_executable() is None:
        return False
    if steam_backend() == "linux" and not any((STEAM_DIR / name).is_file() for name in ("linux64/steamclient.so", "linux32/steamclient.so")):
        return False
    if not STEAM_SESSION_HELPER.is_file():
        raise RuntimeError("Steam session checker is missing; pull the updated FS25 image")
    api = next((directory / name for name in ("x64/steam_api64.dll", "steam_api64.dll") if (directory / name).is_file()), None)
    if api is None:
        raise SteamNotReady("FS25's Steam API is missing; verify the game files in Steam")
    if steam_backend() == "windows":
        path_command = [str(STEAM_WINE_DIR / "bin/winepath"), "-w", str(api)]
        probe_command, probe_env = steam_game_launch_context(directory, [str(STEAM_SESSION_HELPER)])
    else:
        path_command, probe_env = proton_launch_context(directory, [str(api)], verb="getcompatpath")
        probe_command = [*path_command[:2], "run", str(STEAM_SESSION_HELPER)]
    # Proton logging redirects the helper's stdout to a file. Readiness needs
    # its literal stdout marker; keep PROTON_LOG only for the real server.
    probe_env.pop("PROTON_LOG", None)

    def remaining(limit: float) -> float:
        budget = min(limit, deadline - time.monotonic())
        if budget <= 0:
            raise subprocess.TimeoutExpired("Steam session check", timeout)
        return budget

    try:
        windows_path = subprocess.run(path_command, cwd=directory, env=probe_env, capture_output=True, text=True, timeout=remaining(10), check=True).stdout.strip()
        if not windows_path:
            raise RuntimeError("Wine returned an empty Steam API path")
        probe_command = [*probe_command, windows_path]
        result = subprocess.run(
            probe_command, cwd=directory,
            capture_output=True, text=True, timeout=remaining(15), env=probe_env,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode == 0 and "STEAM_SESSION_READY" in result.stdout.splitlines():
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(f"Steam API session check failed (exit {result.returncode}; {result.stdout.strip()}; {result.stderr.strip()}); check FS25/runtime files and logs")


def shutdown_steam() -> None:
    executable = steam_executable()
    if executable is None:
        return
    try:
        if steam_backend() == "windows":
            command = [*steam_client_affinity(), str(STEAM_WINE_DIR / "bin/wine"), str(executable), "-shutdown"]
            process_env = windows_steam_environment()
        else:
            command, process_env = [str(executable), "-shutdown"], clean_steam_environment()
        subprocess.run(command, cwd=steam_client_directory(), env=process_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        log("Steam shutdown did not finish within 10 seconds; container cleanup continues.")


def ensure_directories() -> None:
    for path in (
        CONFIG_DIR,
        DEDICATED_DIR,
        INSTALLER_DIR,
        DLC_DIR,
        PDLC_DIR,
        LOG_DIR,
        DESKTOP_DIR,
        HOME / ".vnc",
    ):
        path.mkdir(parents=True, exist_ok=True)
    if installation_source() == "giants":
        GAME_DIR.mkdir(parents=True, exist_ok=True)


def run_checked(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    log("Executing: " + " ".join(args))
    return subprocess.run(args, text=True, timeout=timeout, check=True)


def prefix_files_exist(prefix: pathlib.Path = PREFIX) -> bool:
    """Check prefix state without assuming distro-provided DLL locations."""
    return (
        (prefix / "system.reg").stat().st_size > 0
        and (prefix / "user.reg").stat().st_size > 0
        and (prefix / "drive_c/windows/system32").is_dir()
    )


def prefix_runs() -> bool:
    try:
        files_exist = prefix_files_exist()
    except OSError:
        files_exist = False
    if not files_exist:
        return False
    try:
        result = subprocess.run(
            ["wine", "cmd", "/d", "/s", "/c", "ver"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=int(env("WINECHECK_TIMEOUT", "45")),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wait_for_prefix(seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if prefix_runs():
            return True
        time.sleep(2)
    return False


def move_prefix_aside(label: str) -> pathlib.Path | None:
    if not PREFIX.exists():
        return None
    subprocess.run(["wineserver", "-k"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    destination = PREFIX.with_name(f"{PREFIX.name}.{label}-{timestamp}")
    counter = 1
    while destination.exists():
        destination = PREFIX.with_name(f"{PREFIX.name}.{label}-{timestamp}-{counter}")
        counter += 1
    PREFIX.rename(destination)
    return destination


def wineboot(mode: str, timeout: int, attempt: int) -> int:
    boot_env = os.environ.copy()
    boot_env["WINEDEBUG"] = "err+all,warn+all"
    return run_logged(
        ["wineboot", mode],
        LOG_DIR / f"wineboot-{attempt}.log",
        timeout,
        process_env=boot_env,
        progress_interval=15,
    )


def ensure_prefix() -> None:
    ensure_directories()
    if wait_for_prefix(6):
        log("Wine prefix is complete and executable.")
        return

    timeout = int(env("WINEBOOT_TIMEOUT", "180"))
    try:
        layout_exists = prefix_files_exist()
    except OSError:
        layout_exists = False

    if layout_exists:
        log("Updating the existing Wine prefix for Wine 11.")
        status = wineboot("--update", timeout, 0)
        if status == 0 and wait_for_prefix(60):
            log("The existing Wine prefix was updated successfully.")
            return
        log(f"Updating the existing Wine prefix failed with status {status}.")

    # Older image revisions expected built-in Wine DLLs inside drive_c and
    # could therefore move a healthy prefix aside. Prefer restoring the newest
    # usable candidate so activation data is retained.
    candidates = sorted(HOME.glob(f"{PREFIX.name}.broken-*"), reverse=True)
    for candidate in candidates:
        try:
            usable_layout = prefix_files_exist(candidate)
        except OSError:
            usable_layout = False
        if not usable_layout:
            continue
        displaced = move_prefix_aside("failed-current")
        candidate.rename(PREFIX)
        log(f"Restored a previous Wine prefix from {candidate}.")
        status = wineboot("--update", timeout, 0)
        if status == 0 and wait_for_prefix(60):
            log("The restored Wine prefix is executable.")
            return
        failed = move_prefix_aside("failed-recovery")
        log(f"The restored prefix was not executable; it is stored at {failed}.")
        if displaced and displaced.exists():
            displaced.rename(PREFIX)

    if PREFIX.exists():
        backup = move_prefix_aside("broken")
        log(f"Moved the incomplete Wine prefix to {backup}.")

    for attempt in (1, 2):
        PREFIX.mkdir(parents=True, exist_ok=True)
        log(f"Creating a new Wine prefix (attempt {attempt}/2, timeout {timeout}s).")
        status = wineboot("--init", timeout, attempt)
        if status == 0 and wait_for_prefix(60):
            log("The Wine prefix was verified successfully.")
            return
        failed = move_prefix_aside(f"failed-init-{attempt}")
        log(
            f"Wine prefix attempt {attempt}/2 exited with status {status}; "
            f"see {LOG_DIR / f'wineboot-{attempt}.log'}. Data is stored at {failed}."
        )

    raise RuntimeError("The Wine prefix could not be created successfully after two attempts")


def probe_fsync() -> tuple[bool, str]:
    """Check the container, not the kernel version; isolate seccomp/SIGBUS failures."""
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        return False, "not Linux x86_64"
    # No Wine process is started. A disposable child contains SIGSYS/SIGBUS,
    # and the shared-memory test file is unlinked before it is mapped.
    check = r'''
import ctypes, errno, mmap, os, sys, tempfile
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except (ImportError, OSError, ValueError):
    pass
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
result = libc.syscall(ctypes.c_long(449), ctypes.c_void_p(), ctypes.c_uint(0),
                      ctypes.c_uint(0), ctypes.c_void_p(), ctypes.c_int(0))
error = ctypes.get_errno()
if result != -1 or error != errno.EINVAL:
    print("futex_waitv: " + os.strerror(error), flush=True)
    sys.exit(1)
try:
    available = os.statvfs("/dev/shm")
    if available.f_bavail * available.f_frsize < 4096:
        raise OSError("/dev/shm has no free page")
    with tempfile.TemporaryFile(dir="/dev/shm") as file:
        file.truncate(4096)
        with mmap.mmap(file.fileno(), 4096) as page:
            page[0] = 1
except OSError as exc:
    print("shared memory: " + str(exc), flush=True)
    sys.exit(1)
print("futex_waitv and shared memory are available", flush=True)
'''
    try:
        result = subprocess.run(
            [sys.executable, "-c", check], capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False, "probe timed out after 5 seconds"
    except OSError as exc:
        return False, f"probe did not complete: {exc}"
    detail = result.stdout.strip() or f"probe exited with status {result.returncode}"
    return result.returncode == 0, detail


def select_wine_runtime() -> None:
    """Select once in the parent; desktop and GIANTS children inherit the result."""
    # Standalone Wine is for GIANTS only. Steam's full Proton launch uses a
    # separate clean environment and explicitly enables the Linux Steam bridge.
    os.environ["PROTON_DISABLE_LSTEAMCLIENT"] = "1"
    if env("_FS25_RUNTIME_READY") == "1":
        return
    settings = {}
    path = HOME / "config" / "wine-runtime.json"
    try:
        if path.is_file():
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError("expected a JSON object")
    except (OSError, ValueError) as exc:
        log(f"Wine runtime settings ignored ({path}): {exc}")
        settings = {}
    runtime = str(env("FS25_WINE_RUNTIME").strip() or settings.get("runtime", "proton")).strip().lower()
    mode = str(env("FS25_WINE_SYNC").strip() or settings.get("sync", "auto")).strip().lower()
    if runtime not in {"proton", "stable"}:
        log(f"Unknown Wine runtime {runtime!r}; using proton.")
        runtime = "proton"
    if mode not in {"auto", "fsync", "server"}:
        log(f"Unknown Wine synchronization mode {mode!r}; using auto.")
        mode = "auto"
    directory = PROTON_DIR if runtime == "proton" else STABLE_DIR
    if not all((directory / "bin" / name).is_file() for name in ("wine", "wineserver")):
        if runtime == "proton":
            log("Bundled Wine runtime missing; trying WineHQ stable.")
            runtime, directory = "stable", STABLE_DIR
        if not all((directory / "bin" / name).is_file() for name in ("wine", "wineserver")):
            raise RuntimeError("Wine runtime is incomplete: wine or wineserver is missing")
    fsync, reason = False, "server-side synchronization requested"
    if runtime == "proton" and mode != "server":
        fsync, reason = probe_fsync()
    elif runtime == "stable":
        reason = "WineHQ stable compatibility runtime"
    bins = {str(PROTON_DIR / "bin"), str(STABLE_DIR / "bin")}
    inherited = [part for part in env("PATH", os.defpath).split(os.pathsep) if part not in bins]
    os.environ["PATH"] = os.pathsep.join([str(directory / "bin"), *inherited])
    os.environ["WINESERVER"] = str(directory / "bin" / "wineserver")
    # Never combine libraries/loaders belonging to different Wine builds.
    for name in ("WINELOADER", "WINEDLLPATH"):
        os.environ.pop(name, None)
    os.environ["WINEFSYNC"] = "1" if fsync else "0"
    os.environ["PROTON_NO_NTSYNC"] = "0" if runtime == "proton" and mode == "auto" else "1"
    os.environ["FS25_SELECTED_WINE_RUNTIME"] = runtime
    os.environ["_FS25_RUNTIME_READY"] = "1"
    log(f"Wine runtime: {runtime}; synchronization mode: {mode}.")
    log(f"FSYNC {'eligible' if fsync else 'disabled'}: {reason}.")
    if runtime == "proton" and mode == "auto":
        log("Wine may use NTSync if already accessible; otherwise FSYNC or server-side synchronization.")
    log("Confirm the active backend with diagnose while the game server is loading.")


def observed_wine_sync(pid: str, proc: pathlib.Path = pathlib.Path("/proc")) -> str:
    """Inspect the current process descriptors, not an old log or a requested flag."""
    try:
        for descriptor in (proc / pid / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target == "/dev/ntsync":
                return "NTSync device descriptor observed"
            if re.fullmatch(r"/dev/shm/wine-[0-9a-f]+-fsync(?: \(deleted\))?", target):
                return "FSYNC shared-memory descriptor observed"
    except OSError:
        return "synchronization descriptors not accessible"
    return "no accelerated synchronization descriptor observed (not conclusive)"


def configure_runtime() -> None:
    """Set inherited Wine options and use the permitted open-file allowance."""
    if installation_source() == "giants":
        select_wine_runtime()
    if env("WINE_AUDIO_MODE", "disabled").lower() == "disabled":
        overrides = [part.strip() for part in env("WINEDLLOVERRIDES", "mscoree=d").split(";") if part.strip()]
        for library in ("winealsa.drv", "winepulse.drv", "winedbg.exe"):
            disabled = f"{library}=d"
            if disabled not in overrides:
                overrides.append(disabled)
        os.environ["WINEDLLOVERRIDES"] = ";".join(overrides)

    try:
        import resource
    except ImportError:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 65536 if hard == resource.RLIM_INFINITY else min(65536, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log(f"Open-file soft limit raised from {soft} to {target}; hard limit unchanged.")
    except (OSError, ValueError) as exc:
        log(f"Open-file limit unchanged: {exc}")


def configure_headless_wine() -> None:
    if env("WINE_AUDIO_MODE", "disabled").lower() != "disabled":
        log(f"Wine audio remains enabled ({env('WINE_AUDIO_MODE')}).")
        return
    marker = PREFIX / ".fs25-headless-audio-disabled"
    if marker.exists():
        return
    try:
        run_checked(
            ["wine", "reg", "add", r"HKCU\Software\Wine\Drivers", "/v", "Audio", "/t", "REG_SZ", "/d", "disabled", "/f"],
            timeout=60,
        )
        marker.touch()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        log("Wine audio could not be disabled permanently; startup will continue.")


def link_persistent(source: pathlib.Path, target: pathlib.Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == source.resolve():
        return
    if target.exists() and target.is_dir() and not target.is_symlink():
        shutil.copytree(target, source, dirs_exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.symlink_to(source, target_is_directory=True)


def atomic_xml(path: pathlib.Path, root: ET.Element) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="    ")
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        temp = pathlib.Path(handle.name)
        ET.ElementTree(root).write(handle, encoding="utf-8", xml_declaration=True)
    ET.parse(temp)
    if path.is_file() and path.read_bytes() == temp.read_bytes():
        temp.unlink()
        return False
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    os.replace(temp, path)
    return True


def validated_port(name: str, default: int) -> str:
    raw = env(name, str(default))
    try:
        number = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number: {raw}") from exc
    if number < 1024 or number > 65535:
        raise RuntimeError(f"{name} must be between 1024 and 65535: {number}")
    return str(number)


def load_or_create(path: pathlib.Path, root_name: str) -> ET.Element:
    if path.is_file():
        try:
            root = ET.parse(path).getroot()
            if root.tag == root_name:
                return root
        except (ET.ParseError, OSError):
            pass
    return ET.Element(root_name)


def child(parent: ET.Element, name: str, attributes: dict[str, str] | None = None) -> ET.Element:
    found = parent.find(name)
    if found is None:
        found = ET.SubElement(parent, name, attributes or {})
    elif attributes:
        found.attrib.update(attributes)
    return found


def optional_value(
    parent: ET.Element,
    element_name: str,
    variable_name: str,
    initial_default: str,
) -> bool:
    """Apply a non-empty panel override, otherwise preserve the XML value."""
    current = parent.find(element_name)
    configured = env(variable_name)
    if configured.strip():
        child(parent, element_name).text = configured
        return True
    if current is None:
        child(parent, element_name).text = initial_default
    return False


def web_credentials() -> tuple[str, str]:
    """Resolve credentials without requiring persistent panel overrides."""
    configured_username = env("WEB_USERNAME")
    configured_password = env("WEB_PASSWORD")
    username = configured_username if configured_username.strip() else ""
    password = configured_password if configured_password.strip() else ""
    if username and password:
        return username, password

    server_path = game_directory() / "dedicatedServer.xml"
    if server_path.is_file():
        try:
            server = ET.parse(server_path).getroot()
            username = username or (server.findtext("./webserver/initial_admin/username") or "").strip()
            password = password or (server.findtext("./webserver/initial_admin/passphrase") or "")
        except (ET.ParseError, OSError):
            pass
    return username or "admin", password or "webpassword"


def configure() -> None:
    ensure_directories()
    directory = game_directory()
    web_port = validated_port("WEB_PORT", 7999)
    game_port = validated_port("SERVER_PORT", 10823)
    if web_port == game_port:
        raise RuntimeError("WEB_PORT and SERVER_PORT must be different")

    server_path = directory / "dedicatedServer.xml"
    server = load_or_create(server_path, "server")
    web = child(server, "webserver", {"port": web_port})
    admin = child(web, "initial_admin")
    managed = []
    if optional_value(admin, "username", "WEB_USERNAME", "admin"):
        managed.append("web username")
    if optional_value(admin, "passphrase", "WEB_PASSWORD", "webpassword"):
        managed.append("web password")
    game = child(server, "game")
    for name, value in {
        "description": "Farming Simulator 25",
        "name": "FarmingSimulator2025",
        "exe": "FarmingSimulator2025Game.exe",
    }.items():
        game.attrib.setdefault(name, value)
    atomic_xml(server_path, server)

    config_path = DEDICATED_DIR / "dedicatedServerConfig.xml"
    gameserver = load_or_create(config_path, "gameserver")
    settings = child(gameserver, "settings")
    requested_map = env("SERVER_MAP").strip()
    existing_map = (settings.findtext("mapID") or "").strip()
    existing_filename = (settings.findtext("mapFilename") or "").strip()
    optional_settings = {
        "game_name": ("SERVER_NAME", "FS25 Server", "server name"),
        "admin_password": ("SERVER_ADMIN", "adminpassword", "admin password"),
        "game_password": ("SERVER_PASSWORD", "", "game password"),
        "savegame_index": ("SAVEGAME_INDEX", "1", "savegame slot"),
        "max_player": ("SERVER_PLAYERS", "16", "player limit"),
        "language": ("SERVER_REGION", "en", "language"),
        "auto_save_interval": ("SERVER_SAVE_INTERVAL", "180.000000", "autosave interval"),
        "stats_interval": ("SERVER_STATS_INTERVAL", "60.000000", "Web API interval"),
        "crossplay_allowed": ("SERVER_CROSSPLAY", "true", "crossplay"),
        "pause_game_if_empty": ("SERVER_PAUSE", "2", "pause-when-empty"),
    }
    for key, (variable, default, label) in optional_settings.items():
        if optional_value(settings, key, variable, default):
            managed.append(label)

    # Older image revisions forced a one-year Web API update interval. Migrate
    # that generated value once so connected-player data refreshes promptly.
    migration_marker = CONFIG_DIR / ".fs25-settings-v2"
    stats = (settings.findtext("stats_interval") or "").strip()
    if (
        not migration_marker.exists()
        and not env("SERVER_STATS_INTERVAL").strip()
        and stats in {"31536000", "31536000.000000"}
    ):
        child(settings, "stats_interval").text = "60.000000"
        log("Migrated the legacy Web API interval from one year to 60 seconds.")

    child(settings, "port").text = game_port
    if requested_map:
        child(settings, "mapID").text = requested_map
        if requested_map != existing_map:
            child(settings, "mapFilename").text = "default"
        managed.append("map")
    else:
        if not existing_map:
            child(settings, "mapID").text = "MapUS"
        if not existing_filename:
            child(settings, "mapFilename").text = "default"
    atomic_xml(config_path, gameserver)
    migration_marker.touch(exist_ok=True)
    selected_map = settings.findtext("mapID") or "MapUS"
    overrides = ", ".join(managed) if managed else "none"
    log(f"Configuration ready: web={web_port} game={game_port} map={selected_map}; panel overrides: {overrides}")
    log("Empty optional panel variables preserve settings saved in the GIANTS Web Interface.")


def configure_terminal() -> None:
    """Repair the desktop terminal before XFCE loads persistent preferences."""
    helpers = HOME / ".config" / "xfce4" / "helpers.rc"
    original = helpers.read_text(encoding="utf-8") if helpers.is_file() else ""
    lines = original.splitlines(keepends=True)
    # XFCE stores preferred helpers in the global group, not in a named section.
    group_start = next(
        (index for index, line in enumerate(lines) if line.strip().startswith("[")),
        len(lines),
    )
    global_lines = [
        line for line in lines[:group_start]
        if not re.match(r"^\s*TerminalEmulator\s*=", line)
    ]
    if global_lines and not global_lines[-1].endswith("\n"):
        global_lines[-1] += "\n"
    helper_config = "".join(global_lines) + "TerminalEmulator=xterm\n" + "".join(lines[group_start:])
    terminal = (
        "[Desktop Entry]\nType=Application\nName=Terminal\n"
        'Exec=/usr/bin/xterm -fa "DejaVu Sans Mono" -fs 11 -title "Terminal" -e /bin/bash\n'
        "TryExec=/usr/bin/xterm\nTerminal=false\nStartupNotify=false\n"
        f"Path={HOME}\n"
        "Icon=utilities-terminal\nCategories=System;TerminalEmulator;\n"
    )
    # Override the existing XFCE menu item as well as its preferred helper.
    # XTerm starts its own window and does not reuse a D-Bus terminal server.
    for path, content, executable in (
        (helpers, helper_config, False),
        (DESKTOP_DIR / "fs25-terminal.desktop", terminal, True),
        (HOME / ".local" / "share" / "applications" / "xfce4-terminal.desktop", terminal, True),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text(encoding="utf-8") if path.is_file() else None
        if previous != content:
            backup = path.with_name(path.name + ".bak")
            if previous is not None and not backup.exists():
                shutil.copy2(path, backup)
            path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)


def write_desktop_file(name: str, title: str, command: str, icon: str) -> None:
    path = DESKTOP_DIR / name
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={title}\n"
        f'Exec=/usr/bin/xterm -fa "DejaVu Sans Mono" -fs 11 -hold -e {command}\n'
        "TryExec=/usr/bin/xterm\nStartupNotify=false\n"
        f"Path={HOME}\n"
        "Terminal=false\n"
        f"Icon={icon}\n"
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def create_desktop_shortcuts() -> None:
    ensure_directories()
    configure_terminal()
    steam = installation_source() == "steam"
    cli = steam and steam_download_mode() == "steamcmd"
    title = "Install FS25 with SteamCMD" if cli else "Open Steam / install FS25" if steam else "Install / activate FS25"
    write_desktop_file("fs25-install.desktop", title, "/opt/fs25/fs25ctl.py install", "system-software-install")
    write_desktop_file("fs25-server.desktop", "Start FS25 web server / view log", "/opt/fs25/fs25ctl.py launch-webserver", "applications-games")
    write_desktop_file("fs25-stop.desktop", "Stop FS25 web server", "/opt/fs25/fs25ctl.py stop-server", "process-stop")
    write_desktop_file("fs25-update.desktop", "Update FS25", "/opt/fs25/fs25ctl.py update", "system-software-update")
    title = "Update owned DLCs with SteamCMD" if cli else "Manage FS25 DLCs in Steam" if steam else "Install FS25 DLCs"
    write_desktop_file("fs25-dlcs.desktop", title, "/opt/fs25/fs25ctl.py install-dlcs", "system-software-install")
    steam_shortcut = DESKTOP_DIR / "fs25-steam.desktop"
    if steam:
        write_desktop_file(steam_shortcut.name, "Steam", "/opt/fs25/fs25ctl.py steam", "applications-games")
    else:
        steam_shortcut.unlink(missing_ok=True)
    path = DESKTOP_DIR / "fs25-web.desktop"
    path.write_text(
        "[Desktop Entry]\nType=Application\nName=Open GIANTS Web Interface\n"
        f"Exec=firefox-esr http://127.0.0.1:{validated_port('WEB_PORT', 7999)}/\n"
        "Terminal=false\nIcon=web-browser\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def prepare() -> None:
    configure_runtime()
    if installation_source() == "steam":
        create_desktop_shortcuts()
        if steam_backend() == "windows":
            prepare_windows_steam()
        else:
            prepare_steam_layout()
        # Neither backend creates/repairs the separate GIANTS prefix.
        return
    ensure_prefix()
    configure_headless_wine()
    link_persistent(GAME_DIR, WINE_GAME_DIR)
    link_persistent(CONFIG_DIR, WINE_CONFIG_DIR)
    create_desktop_shortcuts()
    configure()


def archive_command(archive: pathlib.Path, output: pathlib.Path) -> list[str]:
    output.mkdir(parents=True, exist_ok=True)
    for executable in ("7z", "7zz", "7za"):
        path = shutil.which(executable)
        if path:
            return [path, "x", "-y", f"-o{output}", str(archive)]
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        return [bsdtar, "-xf", str(archive), "-C", str(output)]
    raise RuntimeError("No archive tool is available (7z/7zz/7za/bsdtar)")


def find_installer() -> pathlib.Path | None:
    names = {"setup.exe", "farmingsimulator2025.exe"}
    candidates = sorted(
        path for path in INSTALLER_DIR.rglob("*")
        if path.is_file() and path.name.lower() in names
    )
    return candidates[0] if candidates else None


def run_logged(
    args: list[str],
    log_path: pathlib.Path,
    timeout: int,
    cwd: pathlib.Path | None = None,
    progress_interval: int = 30,
    process_env: dict[str, str] | None = None,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log("Executing: " + " ".join(args))
    with log_path.open("w", encoding="utf-8", errors="replace") as output:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=process_env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        while process.poll() is None:
            elapsed = int(time.monotonic() - started)
            if elapsed >= timeout:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return 124
            try:
                process.wait(timeout=min(progress_interval, timeout - elapsed))
            except subprocess.TimeoutExpired:
                log(f"Process has been running for {int(time.monotonic() - started)}s; log: {log_path}")
        return process.returncode


def create_slice_aliases(installer: pathlib.Path) -> None:
    stem = installer.stem
    if stem.lower() == "setup":
        stem = "Setup"
    elif stem.lower() == "farmingsimulator2025":
        stem = "FarmingSimulator2025"
    count = 0
    for item in installer.parent.iterdir():
        match = re.match(r"^[^_]+_([0-9]+[A-Za-z])\.[Bb][Ii][Nn]$", item.name)
        if not item.is_file() or not match:
            continue
        alias = installer.parent / f"{stem}-{match.group(1).lower()}.bin"
        if not alias.exists():
            try:
                os.link(item, alias)
            except OSError:
                alias.symlink_to(item.name)
        count += 1
    if count:
        log(f"{count} installer slices are available under the expected names.")


def install() -> None:
    if installation_source() == "steam":
        update() if steam_download_mode() == "steamcmd" else open_steam()
        return
    prepare()
    required_gib = int(env("REQUIRED_SPACE_GB", "50"))
    available_gib = shutil.disk_usage(HOME).free // (1024 ** 3)
    if not (GAME_EXE.is_file() and SERVER_EXE.is_file()) and available_gib < required_gib:
        raise RuntimeError(
            f"Not enough free disk space: {required_gib} GiB required, {available_gib} GiB available"
        )
    installer = find_installer()
    if not (GAME_EXE.is_file() and SERVER_EXE.is_file()) and installer is None:
        archives = sorted(
            path for path in INSTALLER_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".img", ".iso", ".zip"}
        )
        if archives:
            extracted = INSTALLER_DIR / "extracted"
            if extracted.exists():
                shutil.rmtree(extracted)
            log(f"Extracting {archives[0].name} ...")
            run_checked(archive_command(archives[0], extracted))
            installer = find_installer()

    if not (GAME_EXE.is_file() and SERVER_EXE.is_file()):
        if installer is None:
            raise RuntimeError(f"No setup executable was found in {INSTALLER_DIR}")
        create_slice_aliases(installer)
        args = ["wine", str(installer)]
        if env("INSTALL_MODE", "silent") == "silent":
            args.extend(["/SILENT", "/NOCANCEL", "/NOICONS"])
        status = run_logged(
            args,
            LOG_DIR / "fs25-installer.log",
            int(env("INSTALL_TIMEOUT", "7200")),
            installer.parent,
            int(env("INSTALL_PROGRESS_INTERVAL", "30")),
        )
        if status != 0:
            raise RuntimeError(f"The FS25 installer exited with status {status}")

    if not (GAME_EXE.is_file() and SERVER_EXE.is_file()):
        raise RuntimeError("The installation is incomplete: the game or server executable is missing")
    log("Installation verified.")

    if true_value(env("ACTIVATE_AFTER_INSTALL", "true")):
        status = run_logged(
            ["wine", str(GAME_EXE)],
            LOG_DIR / "fs25-activation.log",
            int(env("ACTIVATION_TIMEOUT", "7200")),
            GAME_DIR,
        )
        if status not in {0, 124}:
            raise RuntimeError(f"Activation exited with status {status}")
        if status == 124:
            log("The activation timeout was reached; the process was stopped.")

    configure()
    if true_value(env("AUTO_INSTALL_DLC", "false")):
        install_dlcs()
    log("Setup completed.")
    log("Installation files in /home/container/installer can now be deleted to free disk space.")


def dlc_name(path: pathlib.Path) -> str:
    raw = path.stem
    prefix = "FarmingSimulator25_"
    if raw.lower().startswith(prefix.lower()):
        raw = raw[len(prefix):]
    return raw.split("_", 1)[0]


def install_dlcs() -> None:
    if installation_source() == "steam":
        if steam_download_mode() == "steamcmd":
            update()
            return
        log("Manage owned FS25 DLCs in Steam: Library > FS25 > Properties > DLC. Finish downloads before restarting the server.")
        open_steam()
        return
    prepare()
    extracted_root = DLC_DIR / ".extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    for archive in sorted(DLC_DIR.iterdir()):
        if not archive.is_file() or archive.suffix.lower() not in {".img", ".iso", ".zip"}:
            continue
        target = extracted_root / archive.stem
        if not target.exists():
            log(f"Extracting DLC archive {archive.name} ...")
            run_checked(archive_command(archive, target))

    installers = sorted(
        {path.resolve() for root in (DLC_DIR, extracted_root) for path in root.rglob("*.exe") if path.is_file()},
        key=lambda path: path.name.lower(),
    )
    if not installers:
        log(f"No DLC installers were found in {DLC_DIR}.")
        return

    failures = 0
    log(f"Processing {len(installers)} DLC installers sequentially.")
    for installer in installers:
        name = dlc_name(installer)
        if (PDLC_DIR / f"{name}.dlc").is_file():
            log(f"{name} is already installed; skipping {installer.name}.")
            continue
        status = run_logged(
            ["wine", str(installer)],
            LOG_DIR / f"dlc-{name}.log",
            int(env("DLC_INSTALL_TIMEOUT", "7200")),
            installer.parent,
        )
        if status != 0:
            failures += 1
            log(f"DLC {name} exited with status {status}.")
    if failures:
        raise RuntimeError(f"{failures} DLC installation(s) failed")
    log("All detected DLC installers have been processed.")


WEB_PATCH_BEGIN = "/* === FS25 PELICAN HOST PATCH BEGIN === */"
WEB_PATCH_END = "/* === FS25 PELICAN HOST PATCH END === */"


def patch_web() -> None:
    directory = game_directory()
    frontend = directory / "web_data" / "js" / "frontend.js"
    if frontend.is_file():
        content = frontend.read_text(encoding="utf-8", errors="replace")
        content = re.sub(
            re.escape(WEB_PATCH_BEGIN) + r".*?" + re.escape(WEB_PATCH_END),
            "",
            content,
            flags=re.DOTALL,
        ).rstrip()
        patch = r'''
/* === FS25 PELICAN HOST PATCH BEGIN === */
(function () {
  if (window.__fs25PelicanHostPatch) return;
  window.__fs25PelicanHostPatch = true;
  function internal(host) {
    return host === "localhost" || host === "127.0.0.1" ||
      /^10\./.test(host) || /^192\.168\./.test(host) ||
      /^172\.(1[6-9]|2[0-9]|3[01])\./.test(host);
  }
  function rewrite() {
    document.querySelectorAll("a[href]").forEach(function (node) {
      try {
        var url = new URL(node.href, window.location.href);
        if (internal(url.hostname)) {
          url.hostname = window.location.hostname;
          url.port = window.location.port;
          node.href = url.toString();
        }
      } catch (_) {}
    });
  }
  new MutationObserver(rewrite).observe(document.documentElement, {childList:true, subtree:true});
  rewrite();
})();
/* === FS25 PELICAN HOST PATCH END === */
'''
        frontend.write_text(content + "\n" + patch, encoding="utf-8")
        log("The Web Interface host correction is active.")

    imports = {
        directory / "web_data/css/grid.css": 'https://cdn.jsdelivr.net/gh/yellowfromseegg/FS25-Webinterface-DarkMode@main/dark-theme-grid.css',
        directory / "web_data/css/main.css": 'https://cdn.jsdelivr.net/gh/yellowfromseegg/FS25-Webinterface-DarkMode@main/dark-theme-main.css',
    }
    begin = "/* WEB_DARKMODE_BEGIN */"
    end = "/* WEB_DARKMODE_END */"
    enabled = true_value(env("WEB_DARKMODE", "false"))
    for path, url in imports.items():
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        content = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\s*", "", content, flags=re.DOTALL)
        if enabled:
            content = f'{begin}\n@import url("{url}");\n{end}\n' + content
        path.write_text(content, encoding="utf-8")


def matching_processes(pattern: str) -> list[int]:
    result = subprocess.run(["pgrep", "-u", str(os.getuid()), "-i", "-f", pattern],
                            capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise MaintenanceBusy("Process inspection failed; the update has not been started")
    return [int(pid) for pid in result.stdout.split()] if result.returncode == 0 else []


STEAM_PROCESS_PATTERN = r"(^|[/\\ ])(steam(\.exe)?|steamwebhelper(\.exe)?|steamcmd(\.sh)?)(\s|$)"
INSTALLER_PROCESS_PATTERN = r"(^|[/\\ ])(setup|steamsetup|FarmingSimulator2025Setup)\.exe(\s|$)"


def require_update_idle() -> None:
    if game_server_running() or webserver_running() or server_owner() is not None:
        raise MaintenanceBusy("An FS25 game/web process is still running; no update will be started")
    if matching_processes(INSTALLER_PROCESS_PATTERN):
        raise MaintenanceBusy("An installer is still running; finish or close it before updating")


def stop_update_clients() -> None:
    # Never kill an existing SteamCMD download or an installer. The caller holds
    # the exclusive lease, so no managed start/installation can race these checks.
    if matching_processes(r"(^|[/\\ ])steamcmd(\.sh)?(\s|$)"):
        raise MaintenanceBusy("SteamCMD is already running; close its session before updating")
    shutdown_steam()
    deadline = time.monotonic() + 20
    while matching_processes(STEAM_PROCESS_PATTERN):
        if time.monotonic() >= deadline:
            raise MaintenanceBusy("Steam has not exited. Close it using Steam > Exit, then retry; no processes were force-killed")
        time.sleep(0.5)


def interactive_process(command: list[str], *, cwd: pathlib.Path,
                        process_env: dict[str, str] | None = None, capture: bool = False) -> str:
    """A real controlling PTY preserves SteamCMD's password/Guard echo rules.

    Forward input without retaining/logging it. Only the child's output is
    captured, in bounded memory, including prompts without a newline.
    """
    import errno
    import fcntl
    import pty
    import termios
    import tty

    process = None
    previous = {}
    data = bytearray()
    master = slave = stdin = attributes = None

    def child_terminal():
        # This CLI is single-threaded. Give the child its own terminal/session
        # rather than relying on /dev/tty after start_new_session has detached it.
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def cancel(_signum, _frame):
        raise KeyboardInterrupt

    try:
        stdin = sys.stdin.fileno()
        attributes = termios.tcgetattr(stdin)
        master, slave = pty.openpty()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, cancel)
        size = fcntl.ioctl(stdin, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        tty.setraw(stdin)
        process = subprocess.Popen(command, cwd=cwd, env=process_env, stdin=slave,
                                   stdout=slave, stderr=slave, preexec_fn=child_terminal, close_fds=True)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 21600
        input_open = True
        while True:
            ready, _, _ = select.select([master, stdin] if input_open else [master], [], [], 0.5)
            if time.monotonic() >= deadline:
                raise RuntimeError("Update session exceeded six hours; partial files are retained for retry")
            if stdin in ready:
                incoming = os.read(stdin, 4096)
                os.write(master, incoming if incoming else b"\x04")
                input_open = bool(incoming)
            if master in ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break  # Linux PTY EOF once its slave closes
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                if capture:
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise RuntimeError("SteamCMD metadata output exceeded the allowed size")
        status = process.wait(timeout=10)
        if status != 0:
            raise RuntimeError(f"Update process exited with status {status}; installation remains blocked until a successful retry")
        return data.decode("utf-8", errors="replace")
    finally:
        try:
            if process is not None and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        finally:
            for descriptor in (master, slave):
                if descriptor is not None:
                    os.close(descriptor)
            if attributes is not None:
                with contextlib.suppress(OSError):
                    termios.tcsetattr(stdin, termios.TCSADRAIN, attributes)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def steamcmd_environment() -> dict[str, str]:
    result = clean_steam_environment()
    result["HOME"] = str(HOME / "steamcmd/home")
    result["LANG"] = result["LC_ALL"] = "C.UTF-8"
    pathlib.Path(result["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return result


def prepare_steamcmd() -> pathlib.Path:
    target = HOME / "steamcmd/client"
    for directory in (target, HOME / "steamcmd/game", HOME / "steamcmd/home"):
        if not directory.resolve().is_relative_to(HOME.resolve()):
            raise RuntimeError("SteamCMD data must stay inside persistent HOME; existing data was retained")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    (HOME / "steamcmd").chmod(0o700)
    # Valve updates its bootstrap in writable persistent storage. The image's
    # original is immutable; never reuse Windows Steam files or GUI manifests.
    for source in STEAMCMD_BOOTSTRAP.rglob("*"):
        relative = source.relative_to(STEAMCMD_BOOTSTRAP)
        destination = target / relative
        if source.is_file() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    launcher = target / "steamcmd.sh"
    if not launcher.is_file():
        raise RuntimeError("The SteamCMD bootstrap is missing; load the rebuilt image")
    return launcher


def steamcmd_app_info(output: str) -> dict:
    # Ignore SteamCMD banners/login messages outside the one quoted app object.
    match = re.search(r'(?m)^\s*"' + STEAM_APP_ID + r'"\s*\{', output)
    if match is None:
        raise RuntimeError("SteamCMD did not return FS25 metadata")
    depth, quoted, escaped = 0, False, False
    for index in range(output.index("{", match.start()), len(output)):
        char = output[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return parse_vdf(output[match.start():index + 1])[STEAM_APP_ID]
    raise RuntimeError("SteamCMD returned incomplete FS25 metadata")


def steamcmd_dlc_catalog(info: dict) -> tuple[set[str], dict[str, str]]:
    ids = {value.strip() for value in info.get("extended", {}).get("listofdlc", "").split(",") if value.strip()}
    depots = {}
    for depot, details in info.get("depots", {}).items():
        if not isinstance(details, dict) or "dlcappid" not in details:
            continue
        appid = details["dlcappid"]
        ids.add(appid)
        config = details.get("config", {})
        systems = (config.get("oslist") or "windows").split(",")
        language = config.get("language") or "english"
        public = details.get("manifests", {}).get("public", {})
        if "windows" in systems and (config.get("osarch") or "64") == "64" and language == "english" and public:
            depots[depot] = appid
    if any(not re.fullmatch(r"[0-9]{1,10}", appid) for appid in ids):
        raise RuntimeError("Steam returned an invalid DLC App ID")
    return ids, depots


def steamcmd_owned_dlcs(output: str, requested: set[str]) -> set[str]:
    absent = set(re.findall(r"No active license found for appID (\d+)\.", output))
    owned = set()
    for package in re.split(r"License packageID\s+\d+:", output)[1:]:
        if not re.search(r"State\s*:\s*Active\b", package):
            continue
        for apps in re.findall(r"Apps\s*:\s*(.*?)\(\d+ in total\)", package, re.S):
            owned.update(re.findall(r"\b\d+\b", apps))
    if requested - (owned | absent) or requested & owned & absent:
        raise RuntimeError("SteamCMD did not confirm every DLC entitlement; the game download was not started")
    return requested & owned


def update_steamcmd() -> None:
    launcher = prepare_steamcmd()
    account = input("Steam account name (password and Steam Guard are requested by SteamCMD): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", account):
        raise ValueError("Enter a valid Steam account name, not a password or command")
    environment = steamcmd_environment()
    directory = HOME / "steamcmd/game"
    directory.mkdir(parents=True, exist_ok=True)
    base = [str(launcher), "+@sSteamCmdForcePlatformType", "windows", "+force_install_dir", str(directory), "+login", account]

    def session(arguments):
        return interactive_process([*base, *arguments, "+quit"], cwd=launcher.parent,
                                   process_env=environment, capture=True)

    info = steamcmd_app_info(session(["+app_info_update", "1", "+app_info_print", STEAM_APP_ID]))
    ids, depots = steamcmd_dlc_catalog(info)
    questions = [item for appid in sorted(ids) for item in ("+licenses_for_app", appid)]
    owned = steamcmd_owned_dlcs(session(questions), ids) if ids else set()
    # Entitlements are collected from live stdout, never console_log.txt: that
    # file omits license responses on the tested client. No hard-coded DLC list.
    session(["+app_update", STEAM_APP_ID, "validate"])
    if game_directory() != directory.resolve():
        raise RuntimeError("SteamCMD updated an unexpected installation")
    state = read_vdf(directory / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf")["appstate"]
    installed = state.get("installeddepots", {})
    missing = {depot for depot, appid in depots.items() if appid in owned and depot not in installed}
    if missing:
        raise RuntimeError("Owned Windows DLC depots are missing: " + ", ".join(sorted(missing)) + ". Finish optional DLC installation before retrying")
    for depot, appid in depots.items():
        if appid not in owned:
            continue
        public = info["depots"][depot]["manifests"]["public"]
        expected = public.get("gid") if isinstance(public, dict) else public
        if not expected or installed[depot].get("manifest") != expected:
            raise RuntimeError(f"DLC depot {depot} does not match the current public manifest; retry the update")
    receipt = {"appid": STEAM_APP_ID, "buildid": state.get("buildid"), "owned_dlcs": sorted(owned),
               "installed_dlcs": sorted({value.get("dlcappid") for value in installed.values() if value.get("dlcappid")})}
    (HOME / "steamcmd/verified-dlcs.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    log(f"SteamCMD game validation and owned Windows DLC depot checks passed ({len(owned)} DLC entitlements).")


def update() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("Open Update FS25 on the noVNC desktop; interactive login/update confirmation needs a terminal")
    with activity_lock(exclusive=True):
        # Refuse before stopping anything if an active game needs saving.
        if game_server_running():
            raise MaintenanceBusy("Save and stop the game in GIANTS first, then click Update FS25 again")
        if matching_processes(INSTALLER_PROCESS_PATTERN):
            raise MaintenanceBusy("Finish or close the running installer before updating")
        stop_owned_webserver()
        if installation_source() == "steam":
            stop_update_clients()
        require_update_idle()
        marker = HOME / ".fs25-update-incomplete"
        marker.touch()
        if installation_source() == "steam" and steam_download_mode() == "steamcmd":
            update_steamcmd()
        else:
            if installation_source() == "steam":
                prepare()
                command, cwd, environment = steam_launch_context()
                with (LOG_DIR / "steam-launch.log").open("a", encoding="utf-8") as output:
                    subprocess.Popen([*command, f"steam://validate/{STEAM_APP_ID}"], cwd=cwd, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True)
                log("Finish FS25/DLC downloads in Steam, then use Steam > Exit. The update lease stays held while you work.")
            else:
                directory = game_directory()
                executable = directory / "FarmingSimulator2025.exe"
                if not executable.is_file():
                    raise RuntimeError("Install FS25 before using its official updater")
                log("The official GIANTS launcher checks for updates. Complete its updater, then close the game.")
                interactive_process(["wine", str(executable)], cwd=directory)
            if input("After the official updater finishes successfully and you have closed it, type UPDATED: ").strip() != "UPDATED":
                raise RuntimeError("Update was not confirmed; server startup remains blocked until you retry Update FS25")
        # Do not shut down a newly opened Steam session before acknowledging
        # the operator's completed downloads. Never force-kill pending work.
        if installation_source() == "steam" and matching_processes(STEAM_PROCESS_PATTERN):
            raise MaintenanceBusy("Close Steam/SteamCMD completely, then retry Update FS25; installation remains blocked")
        require_update_idle()
        directory = game_directory()
        if installation_source() == "steam":
            ensure_steam_appid(directory)
        elif not all((directory / name).is_file() for name in ("FarmingSimulator2025.exe", "dedicatedServer.exe")):
            raise RuntimeError("The updated GIANTS installation is incomplete")
        configure()
        patch_web()
        marker.unlink()
        log("Update session completed and installation checks passed. The server stays stopped. Start Steam (Steam mode) and the FS25 web server when ready.")


@contextlib.contextmanager
def server_start_lock():
    """Serialize panel/desktop starts and hold ownership for the server lifetime."""
    import fcntl

    HOME.mkdir(parents=True, exist_ok=True)
    # Keep this inode in place: unlinking it would let a second caller lock a
    # replacement file while the first caller still owns the original inode.
    with (HOME / ".fs25-webserver.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServerAlreadyRunning("The FS25 web server is already running or starting") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_server_stopped() -> None:
    if webserver_running():
        raise ServerAlreadyRunning("The FS25 web server is already running; use the existing GIANTS Web Interface")
    if game_server_running():
        raise ServerAlreadyRunning("An FS25 game process is already running; close it before starting another server")


def process_start_time(pid: int) -> str:
    # /proc stat's comm can itself contain spaces and parentheses.
    return (PROC_DIR / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]


def server_owner() -> int | None:
    path = HOME / ".fs25-webserver.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
        pid = state["pid"]
        if type(pid) is not int or pid <= 1 or pid == os.getpid():
            raise ValueError("Invalid web server owner")
        proc = PROC_DIR / str(pid)
        if not proc.exists():
            return None
        if proc.stat().st_uid != os.getuid() or process_start_time(pid) != state["start_time"]:
            return None  # stale PID file; never signal a reused PID
        arguments = (proc / "cmdline").read_bytes().decode().split("\0")
        if "start-webserver" not in arguments or not any(pathlib.PurePosixPath(arg).name == "fs25ctl.py" for arg in arguments):
            raise ValueError("Web server owner is not the managed controller")
        return pid
    except FileNotFoundError:
        return None  # owner exited during the /proc read
    except (KeyError, ValueError, OSError, TypeError, IndexError) as exc:
        raise MaintenanceBusy(f"Could not verify the web server owner: {exc}") from exc


def stop_owned_webserver(*, allow_game: bool = False) -> None:
    if not allow_game and game_server_running():
        raise MaintenanceBusy("Save and stop the game in the GIANTS Web Interface first; no game process was killed")
    (HOME / ".fs25-start-cancelled").touch()
    pid = server_owner()
    if pid is not None:
        # pidfd ties delivery to this exact process even if it exits/reuses its
        # numeric PID between validation and signal delivery (Linux >= 5.3).
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                descriptor = None
            if descriptor is not None:
                try:
                    if server_owner() == pid:
                        try:
                            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                finally:
                    os.close(descriptor)
        else:
            if server_owner() == pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    deadline = time.monotonic() + 20
    while webserver_running() or server_owner() is not None:
        if pid is None or time.monotonic() >= deadline:
            raise MaintenanceBusy("The web server has not stopped; close its original controller before updating")
        time.sleep(0.2)


def stop_server() -> None:
    with activity_lock(exclusive=True):
        stop_owned_webserver()
        log("FS25 web server stopped. The desktop stays online; nothing will restart until you request it.")


def run_server_process(command: list[str], cwd: pathlib.Path, process_env: dict[str, str] | None,
                       release_startup) -> int:
    state_path = HOME / ".fs25-webserver.json"
    process = None
    previous = {}

    def stop(_signum, _frame):
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            raise SystemExit(143)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
        process = subprocess.Popen(command, cwd=cwd, env=process_env,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        state_path.write_text(json.dumps({"pid": os.getpid(), "start_time": process_start_time(os.getpid())}), encoding="utf-8")
        release_startup()
        status = process.wait()
        return status if status >= 0 else 128 - status
    finally:
        try:
            if process is not None and process.poll() is None:
                stop(signal.SIGTERM, None)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        finally:
            state_path.unlink(missing_ok=True)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def launch_webserver() -> None:
    """A desktop terminal is a log viewer, never the service's parent session."""
    with activity_lock():
        require_update_complete()
        ensure_directories()
        if not webserver_running() and server_owner() is None:
            with (LOG_DIR / "dedicated-server.log").open("a", encoding="utf-8") as output:
                subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), "start-webserver"],
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True)
    log("Closing this log window does not stop FS25 or noVNC. Use Stop FS25 web server to stop the service.")
    subprocess.run(["tail", "-n", "60", "-F", str(LOG_DIR / "dedicated-server.log")], check=False)


def start_webserver() -> int:
    with server_start_lock(), contextlib.ExitStack() as startup:
        startup.enter_context(activity_lock())
        require_update_complete()
        # Check before prepare() as it can rewrite GIANTS configuration and links.
        require_server_stopped()
        prepare()
        require_server_stopped()
        directory = game_directory()
        executable = directory / "dedicatedServer.exe"
        if not executable.is_file():
            raise RuntimeError("dedicatedServer.exe is missing; FS25 must be installed first")
        if installation_source() == "steam":
            ensure_steam_appid(directory)
            if not steam_session_ready(directory):
                raise SteamNotReady("Sign in to Steam in noVNC and keep the client running, then start the FS25 web server again")
            # Readiness probing must not relink Documents while a user starts
            # the normal game. Link data only during the locked server start.
            require_server_stopped()
            _, process_env = steam_game_launch_context(directory, [str(executable)])
            prepare_proton_config(process_env)
        configure()
        patch_web()
        # Steam can change its manifest during a login/update. Revalidate right
        # before launch, rather than starting stale/partially updated binaries.
        if game_directory() != directory:
            raise SteamNotReady("The FS25 installation changed during startup; finish the update in Steam and retry")
        require_server_stopped()
        # The controller retains the lock; an exec'ed Wine loader may close
        # inherited descriptors and accidentally release it before the game exits.
        if installation_source() == "steam":
            command, process_env = steam_game_launch_context(directory, [str(executable)])
            prepare_proton_config(process_env)
            log(f"Starting FS25 dedicated server with {steam_backend()} Steam runtime.")
        else:
            command, process_env = ["wine", str(executable)], None
        return run_server_process(command, cwd=directory, process_env=process_env, release_startup=startup.close)


def webserver_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-i", "-f", r"(^|[/\\ ])dedicatedServer\.exe(\s|$)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError("Web server process inspection failed")
        return result.returncode == 0
    except OSError as exc:
        raise RuntimeError("Web server process inspection failed") from exc


def game_server_running() -> bool:
    """Return whether the GIANTS game process is already active."""
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", r"FarmingSimulator2025(Game)?\.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError("Game process inspection failed")
        return result.returncode == 0
    except OSError as exc:
        raise RuntimeError("Game process inspection failed") from exc


def autostart_game() -> None:
    if (HOME / ".fs25-start-cancelled").exists():
        log("Automatic game startup was cancelled by stop/update.")
        return
    if game_server_running():
        log("The game server is already running; the automatic start request was skipped.")
        return

    port = int(validated_port("WEB_PORT", 7999))
    hosts = ["127.0.0.1"]
    try:
        import socket
        hosts.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    base = ""
    for _ in range(60):
        if (HOME / ".fs25-start-cancelled").exists():
            return
        for host in hosts:
            candidate = f"http://{host}:{port}/index.html?lang=en"
            try:
                with urllib.request.urlopen(candidate, timeout=2) as response:
                    if response.status < 400:
                        base = candidate
                        break
            except Exception:
                continue
        if base:
            break
        time.sleep(2)
    if not base:
        raise RuntimeError("The GIANTS Web Interface did not become reachable in time")

    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    username, password = web_credentials()
    login_data = urllib.parse.urlencode(
        {"username": username, "password": password, "login": "Login"}
    ).encode()
    with opener.open(base, login_data, timeout=15) as response:
        response.read()
    with opener.open(base, timeout=15) as response:
        html = response.read().decode("utf-8", errors="replace")

    expected = (
        "game_name", "admin_password", "game_password", "savegame", "server_port",
        "max_player", "mp_language", "auto_save_interval", "stats_interval", "pause_game_if_empty",
    )
    params: dict[str, str] = {}
    for name, value in re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html, re.I):
        if name in expected:
            params[name] = value
    for name in expected:
        if name not in params:
            select = re.search(rf'<select[^>]+name="{re.escape(name)}".*?<option[^>]+value="([^"]*)"[^>]*selected', html, re.I | re.S)
            if select:
                params[name] = select.group(1)
    missing = [name for name in expected if name not in params]
    if missing:
        raise RuntimeError("The start form is incomplete: " + ", ".join(missing))
    if re.search(r'name="crossplay_allowed"[^>]+checked', html, re.I):
        params["crossplay_allowed"] = "on"
    if game_server_running():
        log("The game server started while the Web Interface was loading; no second start request was sent.")
        return
    with activity_lock():
        require_update_complete()
        if (HOME / ".fs25-start-cancelled").exists():
            return
        params["start_server"] = "Start"
        with opener.open(base, urllib.parse.urlencode(params).encode(), timeout=30) as response:
            response.read()
    log("The game server was started through the GIANTS Web Interface.")


def cpu_cgroup(proc: pathlib.Path = pathlib.Path("/proc")) -> tuple[pathlib.Path | None, bool]:
    """Locate this process's CPU cgroup, accounting for container namespaces."""
    try:
        memberships = (proc / "self/cgroup").read_text().splitlines()
        mounts = (proc / "self/mountinfo").read_text().splitlines()
    except OSError:
        return None, False

    def decode_mount_path(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)

    for mount in mounts:
        left, separator, right = mount.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 5 or len(filesystem) < 3:
            continue
        unified = filesystem[0] == "cgroup2"
        if not unified and (filesystem[0] != "cgroup" or "cpu" not in filesystem[2].split(",")):
            continue
        mount_root = pathlib.PurePosixPath(decode_mount_path(fields[3]))
        mount_path = pathlib.Path(decode_mount_path(fields[4]))
        for membership in memberships:
            parts = membership.split(":", 2)
            if len(parts) != 3:
                continue
            if unified and (parts[0] != "0" or parts[1]):
                continue
            if not unified and "cpu" not in parts[1].split(","):
                continue
            group = pathlib.PurePosixPath(parts[2])
            try:
                relative = group.relative_to(mount_root)
            except ValueError:
                if group != pathlib.PurePosixPath("/"):
                    continue
                relative = pathlib.PurePosixPath(".")
            if ".." in relative.parts:
                continue
            candidate = mount_path.joinpath(*relative.parts)
            if (candidate / "cpu.stat").is_file():
                return candidate, unified
    return None, False


def load_timings(path: pathlib.Path) -> list[tuple[float, str]]:
    """Read a bounded log tail without rescanning or changing any mod files."""
    try:
        with path.open("rb") as source:
            size = source.seek(0, os.SEEK_END)
            source.seek(max(0, size - 262144))
            lines = source.read(262144).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    timings = []
    for line in lines:
        match = re.search(r"\(([0-9]+(?:\.[0-9]+)?) ms\)", line)
        if match and ".i3d" in line.lower():
            timings.append((float(match.group(1)), line))
    return sorted(timings, key=lambda item: item[0], reverse=True)[:5]


def diagnose(seconds: int = 5) -> None:
    """Manually sample an ongoing game load; never prepare or reconfigure it."""
    if not 1 <= seconds <= 30:
        raise ValueError("Diagnostic sample duration must be between 1 and 30 seconds")
    log("GAME-LOAD DIAGNOSTICS (read-only; run while the game server loads its map)")
    log(f"Wine executable: {shutil.which('wine') or 'not found'}")
    log(f"Inherited synchronization request: WINEFSYNC={env('WINEFSYNC', 'unset')} "
        f"PROTON_NO_NTSYNC={env('PROTON_NO_NTSYNC', 'unset')} (not proof of active use)")
    try:
        version = subprocess.run(["wine", "--version"], capture_output=True, text=True, timeout=5)
        log(f"Wine version: {version.stdout.strip() or 'not reported'}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"Wine version not available: {exc}")
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        log(f"Diagnostic process open-file limit: soft={soft} hard={hard}; see pidstat PIDs for game processes.")
    except (ImportError, OSError, ValueError):
        log("Open-file limit not available.")
    if hasattr(os, "sched_getaffinity"):
        try:
            log("Allowed logical CPUs: " + ",".join(str(cpu) for cpu in sorted(os.sched_getaffinity(0))))
        except OSError:
            log("CPU affinity not available.")
    try:
        descriptor = os.open("/dev/ntsync", os.O_RDONLY | os.O_CLOEXEC)
        os.close(descriptor)
        log("NTSync device is accessible. This alone does not verify Wine build support or active use.")
    except (OSError, AttributeError) as exc:
        log(f"NTSync device not accessible: {exc}")

    group, unified = cpu_cgroup()
    def cpu_snapshot(label: str) -> None:
        if group is None:
            log(f"{label}: CPU cgroup not available; verify limits on the Wings node.")
            return
        files = ("cpu.max", "cpu.stat", "cpu.pressure", "cpuset.cpus.effective") if unified else (
            "cpu.cfs_quota_us", "cpu.cfs_period_us", "cpu.stat", "cpuset.cpus",
        )
        for name in files:
            try:
                value = (group / name).read_text().strip().replace("\n", "; ")
                log(f"{label} {name}: {value}")
            except OSError:
                continue

    cpu_snapshot("BEFORE SAMPLE")
    pattern = r"FarmingSimulator2025(Game)?\.exe|dedicatedServer\.exe|(^|/)wineserver(64)?(\s|$)"
    try:
        processes = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
        pids = [pid for pid in processes.stdout.split() if pid.isdigit()] if processes.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"Process lookup not available: {exc}")
        pids = []
    if not pids:
        log("No FS25/Wine server process found. Start the game in GIANTS, then repeat this command.")
    for pid in pids:
        log(f"PID {pid}: {observed_wine_sync(pid)}")
    if pids and not (pidstat := shutil.which("pidstat")):
        log("pidstat is missing. Pull the updated FS25 image to enable thread/CPU/I/O sampling.")
    elif pids:
        for pid in pids:
            try:
                limits = (pathlib.Path("/proc") / pid / "limits").read_text().splitlines()
                for line in limits:
                    if line.startswith("Max open files"):
                        log(f"PID {pid}: {line.strip()}")
                        break
                count = sum(1 for _ in (pathlib.Path("/proc") / pid / "fd").iterdir())
                log(f"PID {pid}: {count} open file descriptors at sample start.")
            except OSError:
                continue
        log(f"Sampling process and thread CPU, memory and disk I/O for {seconds}s; PIDs: {','.join(pids)}")
        try:
            sample = subprocess.run(
                [pidstat, "-h", "-t", "-u", "-r", "-d", "-p", ",".join(pids), "1", str(seconds)],
                env={**os.environ, "LC_ALL": "C", "S_COLORS": "never"},
                timeout=seconds + 10,
            )
            if sample.returncode:
                log(f"pidstat exited with status {sample.returncode}; a sampled process may have stopped.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"Process sample did not complete: {exc}")
    cpu_snapshot("AFTER SAMPLE")
    log("Largest i3d timings in the last 256 KiB of the game log (historical, not necessarily this sample):")
    timings = load_timings(CONFIG_DIR / "log.txt")
    for milliseconds, line in timings:
        log(f"{milliseconds:.2f} ms: {line}")
    if not timings:
        log("No i3d timings found in the game-log tail.")
    log("Diagnostics finished. No settings, mods, savegames, caches or Wine prefix were changed.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("prepare", "configure", "install", "install-dlcs", "steam", "patch-web", "start-webserver", "launch-webserver", "stop-server", "update", "autostart-game", "diagnose"),
    )
    parser.add_argument("--seconds", type=int, default=5, help="Read-only diagnose sample duration (1-30 seconds).")
    args = parser.parse_args()
    try:
        if args.command == "diagnose":
            diagnose(args.seconds)
            return 0
        actions = {
            "prepare": prepare,
            "configure": configure,
            "install": install,
            "install-dlcs": install_dlcs,
            "steam": open_steam,
            "patch-web": patch_web,
            "start-webserver": start_webserver,
            "launch-webserver": launch_webserver,
            "stop-server": stop_server,
            "update": update,
            "autostart-game": autostart_game,
        }
        if args.command in {"prepare", "configure", "patch-web"} or (
            args.command in {"install", "install-dlcs"} and installation_source() == "giants"
        ):
            # Legacy commands remain available, but cannot mutate installation
            # files while an update holds the exclusive lease.
            with activity_lock(exclusive=True):
                if args.command in {"install", "install-dlcs"}:
                    require_server_stopped()
                result = actions[args.command]()
        else:
            result = actions[args.command]()
        return result if isinstance(result, int) else 0
    except (SteamNotReady, ServerAlreadyRunning, MaintenanceBusy) as exc:
        log(f"PENDING: {exc}")
        return 75
    except (KeyboardInterrupt, EOFError):
        log("Operation cancelled. An unfinished update must be retried before starting the server.")
        return 130
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
