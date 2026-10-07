#!/usr/bin/env python3
"""Pelican entrypoint for the Farming Simulator 25 image."""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time


HOME = pathlib.Path(os.environ.get("FS25_HOME", "/home/container"))
LOG_DIR = HOME / "logs"
INSTALLER_DIR = HOME / "installer"
CONTROL = "/opt/fs25/fs25ctl.py"
children: list[subprocess.Popen] = []
server_children: list[subprocess.Popen] = []
stopping = False
steam_started = False


def log(message: str) -> None:
    print(f"[FS25] {message}", flush=True)


def log_access_addresses(public_host: str, novnc_port: str) -> None:
    novnc_url = f"http://{public_host}:{novnc_port}/vnc.html?resize=remote&autoconnect=1"
    web_url = f"http://{public_host}:{os.environ.get('WEB_PORT', '7999')}"
    game_port = os.environ.get("SERVER_PORT", "10823")

    log("======================================================================")
    log("                     ACCESS ADDRESSES")
    log("----------------------------------------------------------------------")
    log("  noVNC (Desktop / Installation):")
    log(f"  >>> {novnc_url}")
    log("----------------------------------------------------------------------")
    log("  GIANTS Web Interface:")
    log(f"  >>> {web_url}")
    log("----------------------------------------------------------------------")
    log(f"  Game port: {game_port}/tcp+udp")
    log("======================================================================")
    log("GAME-START NOTE: The Web Interface can be ready while the game server is still loading.")
    log("Large mod maps can take several minutes on any start, not only the first one.")
    log("For loading diagnostics, run /opt/fs25/fs25ctl.py diagnose in the noVNC terminal.")


def find_installation_media() -> list[pathlib.Path]:
    if not INSTALLER_DIR.is_dir():
        return []
    executable_names = {"setup.exe", "farmingsimulator2025.exe"}
    supported_archives = {".img", ".iso", ".zip"}
    media = [
        path
        for path in INSTALLER_DIR.rglob("*")
        if path.is_file()
        and (path.suffix.lower() in supported_archives or path.name.lower() in executable_names)
    ]
    return sorted(
        media,
        key=lambda path: (len(path.relative_to(INSTALLER_DIR).parts), path.as_posix().lower()),
    )


def handle_incomplete_installation() -> None:
    media = find_installation_media()
    auto_install = os.environ.get("AUTO_INSTALL", "false").lower() == "true"
    if media:
        media_path = "/home/container/" + media[0].relative_to(HOME).as_posix()
        log(f"Installation media detected: {media_path}")
        log("The FS25 installation has not completed yet.")
    else:
        log("No installation media detected. Upload a file to /home/container/installer.")

    if auto_install and media:
        log("Starting automatic installation. Progress: /home/container/logs/automatic-install.log")
        spawn([CONTROL, "install"], "automatic-install.log")
    elif media:
        log('Start or monitor the installation in noVNC with "Install / activate FS25".')
    elif auto_install:
        log("AUTO_INSTALL is enabled. Restart the server after uploading the installer.")


def spawn(
    args: list[str],
    log_name: str | None = None,
    *,
    cwd: pathlib.Path | None = None,
) -> subprocess.Popen:
    target = None
    if log_name:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = (LOG_DIR / log_name).open("a", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            args, cwd=cwd, stdout=target, stderr=subprocess.STDOUT, start_new_session=True,
        )
    finally:
        if target is not None:
            target.close()
    children.append(process)
    if log_name in {"dedicated-server.log", "autostart-game.log"}:
        server_children.append(process)
    return process


def stop_processes(processes: list[subprocess.Popen], signum: int) -> None:
    for process in reversed(processes):
        # Wine/XFCE launchers can exit while their process group still lives.
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log(f"Process {process.pid} is still exiting after SIGKILL.")


def stop_children(signum: int = signal.SIGTERM) -> None:
    global stopping
    if stopping:
        return
    stopping = True
    # Keep Steam and the desktop available while the supervised FS25 processes stop.
    stop_processes(server_children, signum)
    if steam_started:
        from fs25ctl import shutdown_steam

        shutdown_steam()
    stop_processes([process for process in children if process not in server_children], signum)


def signal_handler(signum: int, _frame) -> None:
    stop_children(signum)
    raise SystemExit(128 + signum)


def find_novnc_webroot() -> str:
    for candidate in ("/usr/share/novnc", "/usr/share/webapps/novnc"):
        if pathlib.Path(candidate).is_dir():
            return candidate
    raise RuntimeError("noVNC web directory was not found")


def start_xvnc() -> subprocess.Popen:
    pathlib.Path("/tmp/.X0-lock").unlink(missing_ok=True)
    pathlib.Path("/tmp/.X11-unix/X0").unlink(missing_ok=True)
    vnc_dir = HOME / ".vnc"
    vnc_dir.mkdir(parents=True, exist_ok=True)
    vnc_dir.chmod(0o700)
    geometry = os.environ.get("VNC_GEOMETRY", "1280x720")
    command = ["Xvnc", ":0", "-depth", "24", "-geometry", geometry, "-rfbport", "5900"]
    password = os.environ.get("VNC_PASSWORD", "")
    if len(password) >= 6:
        result = subprocess.run(
            ["vncpasswd", "-f"],
            input=(password + "\n").encode(),
            capture_output=True,
            check=True,
        )
        passwd = vnc_dir / "passwd"
        passwd.write_bytes(result.stdout)
        passwd.chmod(0o600)
        command.append(f"-PasswordFile={passwd}")
    else:
        command.append("-SecurityTypes=None")
    process = spawn(command, "xvnc.log")
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError("Xvnc exited early; see logs/xvnc.log")
        if subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return process
        time.sleep(1)
    raise RuntimeError("Xvnc did not become ready in time")


def start_desktop() -> None:
    runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    sessions = HOME / ".cache" / "sessions"
    if sessions.exists():
        shutil.rmtree(sessions)
    config = HOME / ".config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "user-dirs.dirs").write_text(f'XDG_DESKTOP_DIR="{HOME / "Desktop"}"\n', encoding="utf-8")

    dbus = subprocess.run(
        ["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [line.strip() for line in dbus.stdout.splitlines() if line.strip()]
    if lines:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = lines[0]
    spawn(["startxfce4"], "xfce.log")
    time.sleep(4)
    fallbacks = (
        ("xfsettingsd", ["xfsettingsd"]),
        ("xfwm4", ["xfwm4", "--replace", "--compositor=off"]),
        ("xfdesktop", ["xfdesktop"]),
        ("xfce4-panel", ["xfce4-panel"]),
    )
    for name, command in fallbacks:
        running = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", name], stdout=subprocess.DEVNULL).returncode == 0
        if not running:
            spawn(command, "xfce.log")


def startup_command() -> list[str]:
    startup = os.environ.get("STARTUP", f"{CONTROL} start-webserver")
    startup = re.sub(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", lambda match: os.environ.get(match.group(1), ""), startup)
    startup = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: os.environ.get(match.group(1), ""), startup)
    command = shlex.split(startup)
    if not command:
        raise RuntimeError("STARTUP is empty")
    from fs25ctl import GAME_DIR

    managed = command == [CONTROL, "start-webserver"]
    legacy = command == ["wine", str(GAME_DIR / "dedicatedServer.exe")]
    if managed or legacy:
        # Panel and desktop must use the same locking/readiness checks. Bypassing
        # the controller here used to allow concurrent starts during Steam login.
        command = [CONTROL, "start-webserver"]
    return command


def wait_for_steam_session(xvnc: subprocess.Popen, timeout: int = 180) -> bool:
    from fs25ctl import SteamNotReady, ensure_steam_appid, game_directory, steam_session_ready

    log("Waiting for Steam. Complete login / Steam Guard in noVNC if Steam asks for it.")
    deadline = time.monotonic() + timeout
    previous = ""
    while xvnc.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            directory = game_directory()
            ensure_steam_appid(directory)
            if steam_session_ready(directory, timeout=remaining) and time.monotonic() < deadline:
                log("Steam session ready (confirmed by FS25's Steam API).")
                return True
            message = "Steam login is pending. Open Steam in noVNC; the client must stay running."
        except SteamNotReady as exc:
            message = str(exc)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            log(f"Steam startup check: {exc}")
            return False
        if message != previous:
            log(message)
            previous = message
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(5, remaining))
    log("Steam startup is still pending. Finish setup in noVNC, then use Start FS25 web server or restart the container.")
    return False


def main() -> int:
    global steam_started
    os.environ.update(
        {
            "HOME": str(HOME),
            "USER": "container",
            "LOGNAME": "container",
            "SHELL": "/bin/bash",
            "WINEPREFIX": str(HOME / ".fs25server"),
            "WINEARCH": "win64",
            "WINEDEBUG": os.environ.get("WINEDEBUG", "-all"),
            "DISPLAY": ":0",
            "XDG_RUNTIME_DIR": "/tmp/xdg-runtime-fs25",
        }
    )
    # Apply these in the parent before Wine starts so the GIANTS game child
    # inherits them, including when an existing prefix skips registry setup.
    from fs25ctl import (
        SteamNotReady, configure_runtime, configure_terminal, game_directory, game_server_running,
        installation_source, steam_command, webserver_running,
    )

    source = installation_source()
    mode = os.environ.get("AUTOSTART_SERVER", "web_only").lower()
    if mode not in {"false", "true", "web_only"}:
        raise RuntimeError(f"Invalid AUTOSTART_SERVER value: {mode}")
    configure_runtime()
    configure_terminal()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal_handler)

    xvnc = start_xvnc()
    novnc_port = os.environ.get("NOVNC_PORT", "6080")
    spawn(["websockify", "--web", find_novnc_webroot(), novnc_port, "localhost:5900"], "novnc.log")
    start_desktop()
    subprocess.run([CONTROL, "prepare"], check=True)

    public_host = os.environ.get("PUBLIC_IP") or os.environ.get("SERVER_IP") or "SERVER-IP"
    log_access_addresses(public_host, novnc_port)
    log("FS25 image ready.")
    log("This is container readiness, not confirmation that the game map has finished loading.")

    if source == "steam":
        log("Steam mode: sign in and complete Steam Guard in the Steam client through noVNC.")
        log("Keep Remember me enabled. Steam's client/session and game library are persistent.")
        steam_started = True
        spawn(steam_command(silent=True), "steam-launch.log")
        try:
            directory = game_directory()
        except SteamNotReady as exc:
            log(str(exc))
            log("Use the Steam desktop shortcut if the client needs to be opened again.")
            return xvnc.wait()
    else:
        directory = game_directory()
        if not (directory / "dedicatedServer.exe").is_file():
            handle_incomplete_installation()
            return xvnc.wait()

    if source == "giants" and find_installation_media():
        log("Installation is complete. Files in /home/container/installer can be deleted to free disk space.")

    if source == "giants" and os.environ.get("AUTO_INSTALL_DLC", "false").lower() == "true":
        subprocess.run([CONTROL, "install-dlcs"], check=True)

    if mode == "false":
        log("Installation detected. The web server can be started from the desktop.")
        return xvnc.wait()
    if source == "steam" and not wait_for_steam_session(xvnc):
        return xvnc.wait()
    # A user may have started the web server from the desktop during Steam setup.
    if webserver_running():
        log("The FS25 web server is already running; use the existing GIANTS Web Interface.")
        return xvnc.wait()
    if source == "steam" and game_server_running():
        log("An FS25 game process is already running. Close the normal game in noVNC, then restart the container.")
        return xvnc.wait()
    try:
        directory = game_directory()
        command = startup_command()
        if command != [CONTROL, "start-webserver"]:
            subprocess.run([CONTROL, "configure"], check=True)
            subprocess.run([CONTROL, "patch-web"], check=True)
    except (SteamNotReady, subprocess.CalledProcessError) as exc:
        if source != "steam":
            raise
        log(f"Steam startup is pending: {exc}. Finish setup in noVNC and retry.")
        return xvnc.wait()
    log("Startup command: " + " ".join(command))
    # Match the desktop launch for GIANTS without changing the working
    # directory of unrelated custom startup commands.
    game_command = any(
        arg.replace("\\", "/").rsplit("/", 1)[-1].lower() == "dedicatedserver.exe"
        for arg in command
    )
    server = spawn(command, "dedicated-server.log", cwd=directory if game_command else None)
    if mode == "true":
        spawn([CONTROL, "autostart-game"], "autostart-game.log")
    status = server.wait()
    if source == "steam" and status != 0:
        # A download, expired login or a competing desktop start can happen
        # after the first check. Keep noVNC instead of tearing down the container.
        log(f"FS25 startup exited with status {status}. Check noVNC and logs/dedicated-server.log, then retry.")
        stop_processes([process for process in server_children if process is not server], signal.SIGTERM)
        return xvnc.wait()
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"ERROR: {exc}")
        stop_children()
        raise SystemExit(1)
    finally:
        stop_children()
