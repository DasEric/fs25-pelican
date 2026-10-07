"""Runtime regression tests; no Wine, Steam account or server data required."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock


SOURCE = pathlib.Path(os.environ.get("FS25_TEST_SOURCE", pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
import fs25ctl as ctl
import entrypoint as entry


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.prefix = self.home / ".fs25server"
        self.game = self.home / "game" / "Farming Simulator 2025"
        self.config = self.home / "config" / "FarmingSimulator2025"
        self.steam = self.prefix / "drive_c" / "Steam"
        self.library = self.home / "steam" / "library"
        paths = {
            "HOME": self.home,
            "PREFIX": self.prefix,
            "GAME_DIR": self.game,
            "CONFIG_DIR": self.config,
            "DEDICATED_DIR": self.config / "dedicated_server",
            "INSTALLER_DIR": self.home / "installer",
            "DLC_DIR": self.home / "dlc",
            "PDLC_DIR": self.config / "pdlc",
            "LOG_DIR": self.home / "logs",
            "DESKTOP_DIR": self.home / "Desktop",
            "SERVER_EXE": self.game / "dedicatedServer.exe",
            "GAME_EXE": self.game / "FarmingSimulator2025.exe",
            "WINE_GAME_DIR": self.prefix / "drive_c" / "Program Files (x86)" / "Farming Simulator 2025",
            "WINE_CONFIG_DIR": self.prefix / "drive_c" / "users" / "container" / "Documents" / "My Games" / "FarmingSimulator2025",
        }
        for name, value in {
            "STEAM_DIR": self.steam,
            "STEAM_LIBRARY_DIR": self.library,
        }.items():
            if hasattr(ctl, name):
                paths[name] = value
        patcher = mock.patch.multiple(ctl, **paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        entry_patcher = mock.patch.multiple(
            entry, HOME=self.home, LOG_DIR=self.home / "logs", INSTALLER_DIR=self.home / "installer",
            children=[], server_children=[], stopping=False, steam_started=False,
        ) if hasattr(entry, "steam_started") else mock.patch.multiple(entry, HOME=self.home, LOG_DIR=self.home / "logs", INSTALLER_DIR=self.home / "installer")
        entry_patcher.start()
        self.addCleanup(entry_patcher.stop)
        environment = mock.patch.dict(os.environ, {"USER": "container"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        if os.name == "nt" and hasattr(ctl, "server_start_lock"):
            lock = mock.patch.object(ctl, "server_start_lock", side_effect=contextlib.nullcontext)
            lock.start()
            self.addCleanup(lock.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def steam_install(self, *, library=None, flags="4", folder="Farming Simulator 25", extra=""):
        library = library or self.library
        directory = library / "steamapps" / "common" / folder
        (directory / "x64").mkdir(parents=True, exist_ok=True)
        for filename in ("dedicatedServer.exe", "FarmingSimulator2025.exe", "x64/FarmingSimulator2025Game.exe", "x64/steam_api64.dll"):
            (directory / filename).touch()
        manifest = library / "steamapps" / "appmanifest_2300320.acf"
        manifest.write_text(f'"AppState" {{ "appid" "2300320" "installdir" "{folder}" "StateFlags" "{flags}" {extra} }}', encoding="utf-8")
        return directory

    def vdf(self, text):
        path = self.home / "sample.vdf"
        path.write_text(text, encoding="utf-8")
        return ctl.read_vdf(path)

    def session_fixture(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        self.steam.mkdir(parents=True, exist_ok=True)
        (self.steam / "steam.exe").touch()
        helper = self.home / "steam-session.exe"
        helper.touch()
        patcher = mock.patch.object(ctl, "STEAM_SESSION_HELPER", helper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return directory


class ExistingBehaviourTests(RuntimeFixture):
    def test_boolean_values(self):
        for value in ("true", " YES ", "1", "on"):
            self.assertTrue(ctl.true_value(value))
        self.assertFalse(ctl.true_value("false"))

    def test_port_validation(self):
        self.assertEqual(ctl.validated_port("WEB_PORT", 7999), "7999")
        for value in ("abc", "1023", "65536"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"WEB_PORT": value}):
                with self.assertRaises(RuntimeError):
                    ctl.validated_port("WEB_PORT", 7999)

    def test_empty_variables_preserve_giants_settings(self):
        ctl.configure()
        path = ctl.DEDICATED_DIR / "dedicatedServerConfig.xml"
        root = ET.parse(path).getroot()
        root.find("./settings/game_name").text = "Existing farm"
        root.find("./settings/mapID").text = "MyModMap"
        root.find("./settings/mapFilename").text = "my-mod.zip"
        ctl.atomic_xml(path, root)
        ctl.configure()
        settings = ET.parse(path).getroot().find("settings")
        self.assertEqual(settings.findtext("game_name"), "Existing farm")
        self.assertEqual(settings.findtext("mapID"), "MyModMap")
        self.assertEqual(settings.findtext("mapFilename"), "my-mod.zip")

    def test_explicit_variables_override_giants_settings(self):
        with mock.patch.dict(os.environ, {"SERVER_NAME": "Panel farm", "WEB_PORT": "8001", "SERVER_PORT": "10824"}):
            ctl.configure()
        self.assertEqual(ET.parse(ctl.DEDICATED_DIR / "dedicatedServerConfig.xml").findtext("./settings/game_name"), "Panel farm")
        self.assertEqual(ET.parse(self.game / "dedicatedServer.xml").getroot().find("webserver").get("port"), "8001")

    def test_xml_writes_are_idempotent(self):
        ctl.configure()
        path = self.game / "dedicatedServer.xml"
        before = path.read_bytes()
        before_time = path.stat().st_mtime_ns
        ctl.configure()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, before_time)

    def test_saved_web_credentials(self):
        with mock.patch.dict(os.environ, {"WEB_USERNAME": "owner", "WEB_PASSWORD": "custom-password"}):
            ctl.configure()
        self.assertEqual(ctl.web_credentials(), ("owner", "custom-password"))

    def test_startup_expands_variables_without_a_shell(self):
        with mock.patch.dict(os.environ, {"STARTUP": 'wine "/path with spaces/server.exe" --port={{SERVER_PORT}}', "SERVER_PORT": "10823"}):
            self.assertEqual(entry.startup_command(), ["wine", "/path with spaces/server.exe", "--port=10823"])

    def test_diagnostics_duration_is_bounded(self):
        for seconds in (0, 31):
            with self.assertRaises(ValueError):
                ctl.diagnose(seconds)


class InstallationTests(RuntimeFixture):
    def test_giants_is_default(self):
        self.assertEqual(ctl.installation_source(), "giants")
        self.assertEqual(ctl.game_directory(), self.game)

    def test_source_is_validated(self):
        for source in ("other", "", "steamcmd"):
            with self.subTest(source=source), mock.patch.dict(os.environ, {"INSTALL_SOURCE": source}):
                with self.assertRaises(ValueError):
                    ctl.installation_source()

    def test_steam_does_not_create_a_giants_game_folder(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        ctl.ensure_directories()
        self.assertTrue(self.config.is_dir())
        self.assertFalse(self.game.exists())

    def test_steam_prepare_does_not_touch_giants_install_or_write_game_xml(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        self.game.mkdir(parents=True)
        sentinel = self.game / "existing-game.bin"
        sentinel.write_bytes(b"keep")
        with mock.patch.multiple(ctl, configure_runtime=mock.DEFAULT, ensure_prefix=mock.DEFAULT, configure_headless_wine=mock.DEFAULT, prepare_steam_layout=mock.DEFAULT, link_persistent=mock.DEFAULT, create_desktop_shortcuts=mock.DEFAULT, configure=mock.DEFAULT) as functions:
            ctl.prepare()
        functions["prepare_steam_layout"].assert_called_once()
        functions["link_persistent"].assert_called_once_with(self.config, ctl.WINE_CONFIG_DIR)
        functions["configure"].assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_steam_layout_keeps_installer_destination_empty(self):
        with mock.patch.object(ctl, "link_persistent") as link:
            ctl.prepare_steam_layout()
        link.assert_called_once_with(self.library, self.steam)
        self.assertFalse((self.steam / "steamapps").exists())

    def test_existing_steam_directory_is_not_migrated(self):
        self.steam.mkdir(parents=True)
        old = self.steam / "config.vdf"
        old.write_bytes(b"saved session")
        with mock.patch.object(ctl, "link_persistent") as link:
            ctl.prepare_steam_layout()
        link.assert_not_called()
        self.assertEqual(old.read_bytes(), b"saved session")

    def test_default_library_aliases_are_deduplicated(self):
        try:
            ctl.prepare_steam_layout()
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows host requires symbolic-link privilege; Linux CI runs this test")
            raise
        directory = self.steam_install()
        self.assertTrue(self.steam.is_symlink())
        self.assertEqual(ctl.steam_game_directory(), directory.resolve())

    def test_complete_steam_installation_is_resolved(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        self.assertEqual(ctl.game_directory(), directory.resolve())

    def test_files_without_a_manifest_are_not_an_installation(self):
        directory = self.steam_install()
        (self.library / "steamapps/appmanifest_2300320.acf").unlink()
        self.assertTrue((directory / "dedicatedServer.exe").exists())
        with self.assertRaises(ctl.SteamNotReady):
            ctl.steam_game_directory()

    def test_pending_installation_flags(self):
        for flags in ("0", "2", "6", "1028", "-1", "invalid"):
            with self.subTest(flags=flags):
                self.steam_install(flags=flags)
                with self.assertRaises(ctl.SteamNotReady):
                    ctl.steam_game_directory()

    def test_pending_download_and_staging_counters(self):
        for extra in ('"BytesToDownload" "100" "BytesDownloaded" "50"', '"BytesToStage" "100" "BytesStaged" "50"', '"BytesToDownload" { "invalid" "value" }', '"BytesToStage" "-1"'):
            with self.subTest(extra=extra):
                self.steam_install(extra=extra)
                with self.assertRaises(ctl.SteamNotReady):
                    ctl.steam_game_directory()

    def test_finished_download_counters(self):
        directory = self.steam_install(extra='"BytesToDownload" "100" "BytesDownloaded" "100" "BytesToStage" "100" "BytesStaged" "100"')
        self.assertEqual(ctl.steam_game_directory(), directory.resolve())

    def test_downloading_directory_blocks_start(self):
        self.steam_install()
        path = self.library / "steamapps/downloading/2300320/partial.bin"
        path.parent.mkdir(parents=True)
        path.touch()
        with self.assertRaises(ctl.SteamNotReady):
            ctl.steam_game_directory()
        path.unlink()
        self.assertTrue(ctl.steam_game_directory().is_dir())

    def test_all_required_executables_and_api_are_checked(self):
        for filename in ("dedicatedServer.exe", "FarmingSimulator2025.exe", "x64/FarmingSimulator2025Game.exe", "x64/steam_api64.dll"):
            with self.subTest(filename=filename):
                directory = self.steam_install()
                (directory / filename).unlink()
                with self.assertRaises(ctl.SteamNotReady):
                    ctl.steam_game_directory()

    def test_manifest_folder_cannot_escape_library(self):
        self.steam_install()
        manifest = self.library / "steamapps/appmanifest_2300320.acf"
        for folder in ("..", "../other", "C:/other", ""):
            with self.subTest(folder=folder):
                manifest.write_text(f'"AppState" {{ "appid" "2300320" "installdir" "{folder}" "StateFlags" "4" }}')
                with self.assertRaises(ctl.SteamNotReady):
                    ctl.steam_game_directory()

    def test_wrong_app_id_and_partial_manifest(self):
        self.steam_install()
        manifest = self.library / "steamapps/appmanifest_2300320.acf"
        for text in ('"AppState" { "appid" "480" }', '"AppState" { "appid" "2300320"'):
            manifest.write_text(text)
            with self.assertRaises(ctl.SteamNotReady):
                ctl.steam_game_directory()

    def test_custom_library_is_discovered(self):
        custom = self.prefix / "drive_c" / "Games"
        directory = self.steam_install(library=custom)
        metadata = self.steam / "steamapps/libraryfolders.vdf"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(r'"libraryfolders" { "0" { "path" "C:\\Games" "apps" { "2300320" "123" } } }')
        self.assertEqual(ctl.steam_game_directory(), directory.resolve())

    def test_duplicate_installations_are_explicit(self):
        self.steam_install(library=self.library)
        self.steam_install(library=self.steam)
        with self.assertRaisesRegex(ctl.SteamNotReady, "Multiple FS25"):
            ctl.steam_game_directory()

    def test_steam_install_and_dlc_commands_use_client_not_giants(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        with mock.patch.object(ctl, "open_steam") as client, mock.patch.object(ctl, "run_logged") as installer:
            ctl.install()
            ctl.install_dlcs()
        self.assertEqual(client.call_count, 2)
        installer.assert_not_called()
        self.assertFalse((self.home / "dlc/.extracted").exists())

    def test_shortcuts_follow_installation_source(self):
        ctl.create_desktop_shortcuts()
        self.assertIn("Install / activate", (ctl.DESKTOP_DIR / "fs25-install.desktop").read_text())
        os.environ["INSTALL_SOURCE"] = "steam"
        ctl.create_desktop_shortcuts()
        self.assertIn("Open Steam", (ctl.DESKTOP_DIR / "fs25-install.desktop").read_text())
        self.assertTrue((ctl.DESKTOP_DIR / "fs25-steam.desktop").exists())
        os.environ["INSTALL_SOURCE"] = "giants"
        ctl.create_desktop_shortcuts()
        self.assertFalse((ctl.DESKTOP_DIR / "fs25-steam.desktop").exists())

    def test_steam_configuration_preserves_shipped_game_attributes(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        (directory / "dedicatedServer.xml").write_text('<server><game exe="custom-engine.exe" name="FarmingSimulator2025"/></server>')
        ctl.configure()
        self.assertEqual(ET.parse(directory / "dedicatedServer.xml").find("game").get("exe"), "custom-engine.exe")
        self.assertFalse((self.game / "dedicatedServer.xml").exists())

    def test_web_patch_targets_steam_files_and_is_idempotent(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        frontend = directory / "web_data/js/frontend.js"
        frontend.parent.mkdir(parents=True)
        frontend.write_text("original frontend")
        ctl.patch_web()
        ctl.patch_web()
        self.assertEqual(frontend.read_text().count(ctl.WEB_PATCH_BEGIN), 1)
        self.assertFalse((self.game / "web_data").exists())


class SteamMetadataTests(RuntimeFixture):
    def test_vdf_nested_objects_comments_and_case(self):
        self.assertEqual(self.vdf('// comment\n"ROOT" { "MixedCase" "value" "nested" { "number" "4" } }'), {"root": {"mixedcase": "value", "nested": {"number": "4"}}})

    def test_vdf_escapes_and_quoted_braces(self):
        self.assertEqual(self.vdf(r'"path" "C:\\Steam" "value" "{hi} \"quoted\""'), {"path": "C:\\Steam", "value": '{hi} "quoted"'})

    def test_malformed_vdf_is_rejected(self):
        for text in ('"key"', '"key" {', '"key" "unterminated', '}', '{ "key" "value" }', '"key" "1" "KEY" "2"'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.vdf(text)

    def test_vdf_depth_is_bounded(self):
        with self.assertRaises(ValueError):
            self.vdf('"nested" {' * 18 + '"value" "1"' + '}' * 18)

    def test_vdf_size_is_bounded(self):
        path = self.home / "large.vdf"
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            ctl.read_vdf(path)

    def test_c_drive_maps_to_this_prefix(self):
        self.assertEqual(ctl.persistent_wine_path(r"C:\Steam"), (self.prefix / "drive_c/Steam").resolve())

    def test_relative_network_and_external_libraries_are_rejected(self):
        for value in ("relative", r"\\server\share", r"Z:\outside-container", r"D:\unmapped"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ctl.persistent_wine_path(value)


class SteamClientTests(RuntimeFixture):
    def test_first_run_uses_official_client_installer(self):
        installer = self.home / "SteamSetup.exe"
        installer.touch()
        with mock.patch.object(ctl, "STEAM_SETUP", installer):
            self.assertEqual(ctl.steam_command(), ["wine", str(installer), "/S", r"/D=C:\Steam"])

    def test_saved_client_launch_has_no_credentials(self):
        self.steam.mkdir(parents=True)
        executable = self.steam / "steam.exe"
        executable.touch()
        with mock.patch.dict(os.environ, {"STEAM_PASSWORD": "secret", "STEAM_GUARD_CODE": "ABCDE"}):
            self.assertEqual(ctl.steam_command(silent=True), ["wine", str(executable), "-silent"])
            self.assertEqual(ctl.steam_command(), ["wine", str(executable)])

    def test_client_filename_case(self):
        self.steam.mkdir(parents=True)
        (self.steam / "Steam.exe").touch()
        self.assertTrue(ctl.steam_executable().is_file())

    def test_missing_installer_is_reported(self):
        with mock.patch.object(ctl, "STEAM_SETUP", self.home / "missing.exe"):
            with self.assertRaisesRegex(RuntimeError, "installer is missing"):
                ctl.steam_command()

    def test_giants_mode_does_not_open_steam(self):
        with self.assertRaisesRegex(RuntimeError, "INSTALL_SOURCE=steam"):
            ctl.open_steam()

    def test_app_id_root_engine_and_idempotence(self):
        directory = self.steam_install()
        ctl.ensure_steam_appid(directory)
        for folder in (directory, directory / "x64"):
            path = folder / "steam_appid.txt"
            self.assertEqual(path.read_bytes(), b"2300320\n")
            timestamp = path.stat().st_mtime_ns
            ctl.ensure_steam_appid(directory)
            self.assertEqual(timestamp, path.stat().st_mtime_ns)

    def test_incorrect_app_id_is_backed_up_and_repaired(self):
        directory = self.steam_install()
        path = directory / "steam_appid.txt"
        path.write_bytes(b"wrong")
        ctl.ensure_steam_appid(directory)
        self.assertEqual(path.with_suffix(".txt.bak").read_bytes(), b"wrong")
        self.assertEqual(path.read_bytes(), b"2300320\n")

    def test_real_api_success_is_required_for_session_readiness(self):
        directory = self.session_fixture()
        results = [subprocess.CompletedProcess([], 0, r"C:\Steam\game\steam_api64.dll", ""), subprocess.CompletedProcess([], 0, "SDK message\nSTEAM_SESSION_READY\n", "")]
        with mock.patch.object(ctl.subprocess, "run", side_effect=results) as run:
            self.assertTrue(ctl.steam_session_ready(directory))
        self.assertEqual(run.call_args.kwargs["cwd"], directory)
        self.assertEqual(run.call_args.kwargs["env"]["SteamAppId"], "2300320")
        self.assertIn(str(ctl.STEAM_SESSION_HELPER), run.call_args.args[0])

    def test_pending_session_and_timeout_keep_desktop_available(self):
        directory = self.session_fixture()
        path = subprocess.CompletedProcess([], 0, r"C:\Steam\game\steam_api64.dll", "")
        with mock.patch.object(ctl.subprocess, "run", side_effect=[path, subprocess.CompletedProcess([], 1, "STEAM_SESSION_PENDING\n", "")]):
            self.assertFalse(ctl.steam_session_ready(directory))
        with mock.patch.object(ctl.subprocess, "run", side_effect=subprocess.TimeoutExpired("wine", 15)):
            self.assertFalse(ctl.steam_session_ready(directory))

    def test_unsupported_api_or_unexpected_success_output_is_not_a_login(self):
        directory = self.session_fixture()
        for code, output in ((2, "STEAM_API_UNSUPPORTED"), (0, "not ready")):
            with self.subTest(code=code, output=output), mock.patch.object(ctl.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "path", ""), subprocess.CompletedProcess([], code, output, "")]):
                with self.assertRaises(RuntimeError):
                    ctl.steam_session_ready(directory)

    def test_saved_login_files_alone_are_not_a_session(self):
        directory = self.steam_install()
        config = self.steam / "config/loginusers.vdf"
        config.parent.mkdir(parents=True)
        config.write_text('"users" { "123" { "RememberPassword" "1" } }')
        with mock.patch.object(ctl.subprocess, "run") as run:
            self.assertFalse(ctl.steam_session_ready(directory))
        run.assert_not_called()

    def test_shutdown_does_not_bootstrap_a_missing_client(self):
        with mock.patch.object(ctl.subprocess, "run") as run:
            ctl.shutdown_steam()
        run.assert_not_called()

    def test_existing_client_receives_shutdown(self):
        self.steam.mkdir(parents=True)
        (self.steam / "steam.exe").touch()
        with mock.patch.object(ctl.subprocess, "run") as run:
            ctl.shutdown_steam()
        self.assertEqual(run.call_args.args[0][-1], "-shutdown")
        self.assertEqual(run.call_args.kwargs["timeout"], 10)


class EntrypointTests(RuntimeFixture):
    def test_managed_startup_resolves_giants(self):
        self.assertEqual(entry.startup_command(), [entry.CONTROL, "start-webserver"])

    def test_managed_and_legacy_startup_resolve_steam(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        for startup in (f"{entry.CONTROL} start-webserver", f'wine "{self.game / "dedicatedServer.exe"}"'):
            with mock.patch.dict(os.environ, {"STARTUP": startup}):
                self.assertEqual(entry.startup_command(), [entry.CONTROL, "start-webserver"])

    def test_custom_startup_is_not_rewritten(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        with mock.patch.dict(os.environ, {"STARTUP": "custom-server --argument value"}):
            self.assertEqual(entry.startup_command(), ["custom-server", "--argument", "value"])

    def test_session_wait_succeeds_only_on_api_success(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        desktop = mock.Mock()
        desktop.poll.return_value = None
        with mock.patch.object(ctl, "steam_session_ready", side_effect=[False, True]), mock.patch.object(entry.time, "sleep"):
            self.assertTrue(entry.wait_for_steam_session(desktop))
        self.assertEqual((directory / "steam_appid.txt").read_bytes(), b"2300320\n")

    def test_session_wait_is_bounded(self):
        desktop = mock.Mock()
        desktop.poll.return_value = None
        with mock.patch.object(entry.time, "monotonic", side_effect=[0, 181]), mock.patch.object(ctl, "steam_session_ready") as check:
            self.assertFalse(entry.wait_for_steam_session(desktop))
        check.assert_not_called()

    def test_session_wait_ends_when_desktop_exits(self):
        desktop = mock.Mock()
        desktop.poll.return_value = 1
        self.assertFalse(entry.wait_for_steam_session(desktop))

    def test_session_checker_error_does_not_start_server(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        self.steam_install()
        desktop = mock.Mock()
        desktop.poll.return_value = None
        with mock.patch.object(ctl, "steam_session_ready", side_effect=RuntimeError("check failed")):
            self.assertFalse(entry.wait_for_steam_session(desktop))

    def main_fixture(self, source, *, installed=False, mode="web_only", ready=True):
        os.environ.update({"INSTALL_SOURCE": source, "AUTOSTART_SERVER": mode, "AUTO_INSTALL_DLC": "true", "AUTO_INSTALL": "true"})
        installer = self.home / "SteamSetup.exe"
        installer.touch()
        if installed:
            if source == "steam":
                self.steam_install()
            else:
                self.game.mkdir(parents=True)
                (self.game / "dedicatedServer.exe").touch()
        desktop = mock.Mock()
        desktop.wait.return_value = 0
        desktop.poll.return_value = None
        server = mock.Mock()
        server.wait.return_value = 0
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(ctl, "STEAM_SETUP", installer))
        stack.enter_context(mock.patch.object(ctl, "configure_runtime"))
        stack.enter_context(mock.patch.object(ctl, "configure_terminal"))
        stack.enter_context(mock.patch.object(ctl, "webserver_running", return_value=False))
        stack.enter_context(mock.patch.object(ctl, "game_server_running", return_value=False))
        stack.enter_context(mock.patch.object(entry, "start_xvnc", return_value=desktop))
        stack.enter_context(mock.patch.object(entry, "start_desktop"))
        stack.enter_context(mock.patch.object(entry, "find_novnc_webroot", return_value="/web"))
        stack.enter_context(mock.patch.object(entry, "wait_for_steam_session", return_value=ready))
        if not hasattr(entry.signal, "SIGHUP"):
            stack.enter_context(mock.patch.object(entry.signal, "SIGHUP", 1, create=True))
        stack.enter_context(mock.patch.object(entry.signal, "signal"))
        run = stack.enter_context(mock.patch.object(entry.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)))
        spawn = stack.enter_context(mock.patch.object(entry, "spawn", return_value=server))
        return run, spawn

    def test_first_steam_setup_keeps_desktop_and_skips_giants_installers(self):
        run, spawn = self.main_fixture("steam")
        self.assertEqual(entry.main(), 0)
        self.assertEqual(run.call_args_list, [mock.call([entry.CONTROL, "prepare"], check=True)])
        self.assertFalse(any(call.args[1] == "dedicated-server.log" for call in spawn.call_args_list))

    def test_pending_login_keeps_desktop_and_skips_server(self):
        run, spawn = self.main_fixture("steam", installed=True, ready=False)
        self.assertEqual(entry.main(), 0)
        self.assertFalse(any(call.args[1] == "dedicated-server.log" for call in spawn.call_args_list))

    def test_steam_autostart_modes(self):
        for mode in ("false", "web_only", "true"):
            with self.subTest(mode=mode):
                run, spawn = self.main_fixture("steam", installed=True, mode=mode)
                self.assertEqual(entry.main(), 0)
                logs = [call.args[1] for call in spawn.call_args_list]
                self.assertEqual("dedicated-server.log" in logs, mode != "false")
                self.assertEqual("autostart-game.log" in logs, mode == "true")
                self.assertNotIn(mock.call([entry.CONTROL, "install-dlcs"], check=True), run.call_args_list)
                if mode != "false":
                    call = next(call for call in spawn.call_args_list if call.args[1] == "dedicated-server.log")
                    self.assertEqual(call.args[0], [entry.CONTROL, "start-webserver"])
                    self.assertIsNone(call.kwargs["cwd"])

    def test_giants_start_does_not_launch_steam(self):
        run, spawn = self.main_fixture("giants", installed=True)
        self.assertEqual(entry.main(), 0)
        self.assertNotIn("steam-launch.log", [call.args[1] for call in spawn.call_args_list])
        self.assertIn(mock.call([entry.CONTROL, "install-dlcs"], check=True), run.call_args_list)

    def test_normal_steam_game_prevents_a_second_game_start(self):
        run, spawn = self.main_fixture("steam", installed=True, mode="true")
        with mock.patch.object(ctl, "game_server_running", return_value=True):
            self.assertEqual(entry.main(), 0)
        self.assertNotIn("dedicated-server.log", [call.args[1] for call in spawn.call_args_list])

    def test_cleanup_stops_server_before_steam_before_desktop(self):
        server, desktop = mock.Mock(), mock.Mock()
        entry.children[:] = [desktop, server]
        entry.server_children[:] = [server]
        entry.steam_started = True
        events = []
        with mock.patch.object(entry, "stop_processes", side_effect=lambda processes, sig: events.append(processes)), mock.patch.object(ctl, "shutdown_steam", side_effect=lambda: events.append("steam")):
            entry.stop_children()
            entry.stop_children()
        self.assertEqual(events, [[server], "steam", [desktop]])


class DesktopServerTests(RuntimeFixture):
    def start_fixture(self, *, session=True, web=False, game=False):
        os.environ["INSTALL_SOURCE"] = "steam"
        directory = self.steam_install()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(ctl, "prepare"))
        stack.enter_context(mock.patch.object(ctl, "steam_session_ready", return_value=session))
        stack.enter_context(mock.patch.object(ctl, "webserver_running", return_value=web))
        stack.enter_context(mock.patch.object(ctl, "game_server_running", return_value=game))
        chdir = stack.enter_context(mock.patch.object(ctl.os, "chdir"))
        execute = stack.enter_context(mock.patch.object(ctl.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)))
        return directory, chdir, execute

    def test_pending_session_prevents_desktop_server_start(self):
        directory, chdir, execute = self.start_fixture(session=False)
        with self.assertRaises(ctl.SteamNotReady):
            ctl.start_webserver()
        execute.assert_not_called()

    def test_existing_webserver_prevents_duplicate_start(self):
        directory, chdir, execute = self.start_fixture(web=True)
        with self.assertRaisesRegex(RuntimeError, "already running"):
            ctl.start_webserver()
        execute.assert_not_called()

    def test_existing_game_prevents_duplicate_start(self):
        directory, chdir, execute = self.start_fixture(game=True)
        with self.assertRaisesRegex(RuntimeError, "already running"):
            ctl.start_webserver()
        execute.assert_not_called()

    def test_desktop_start_uses_steam_directory_and_app_id(self):
        directory, chdir, execute = self.start_fixture()
        self.assertEqual(ctl.start_webserver(), 0)
        chdir.assert_not_called()
        execute.assert_called_once_with(["wine", str(directory / "dedicatedServer.exe")], cwd=directory.resolve())
        self.assertEqual((directory / "steam_appid.txt").read_bytes(), b"2300320\n")
        self.assertTrue((directory / "dedicatedServer.xml").is_file())


class ReviewRegressionTests(RuntimeFixture):
    def test_unrelated_unmapped_library_does_not_hide_installed_fs25(self):
        directory = self.steam_install()
        metadata = self.steam / "steamapps/libraryfolders.vdf"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(r'"libraryfolders" { "0" { "path" "D:\\OldLibrary" } }')
        self.assertEqual(ctl.steam_game_directory(), directory.resolve())

    def test_update_after_session_check_keeps_desktop_available(self):
        fixture = EntrypointTests.main_fixture(self, "steam", installed=True)
        run, spawn = fixture
        directory = self.library / "steamapps/common/Farming Simulator 25"
        with mock.patch.object(ctl, "game_directory", side_effect=[directory, ctl.SteamNotReady("Steam update started")]):
            self.assertEqual(entry.main(), 0)
        self.assertNotIn("dedicated-server.log", [call.args[1] for call in spawn.call_args_list])

    def test_duplicate_desktop_start_does_not_prepare_or_rewrite_settings(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        self.steam_install()
        with mock.patch.object(ctl, "prepare") as prepare, mock.patch.object(ctl, "webserver_running", return_value=True), mock.patch.object(ctl, "game_server_running", return_value=False), mock.patch.object(ctl, "steam_session_ready", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                ctl.start_webserver()
        prepare.assert_not_called()

    def test_cleanup_signals_group_even_when_launcher_has_exited(self):
        launcher = mock.Mock(pid=4321)
        launcher.poll.return_value = 0
        with mock.patch.object(entry.os, "killpg", create=True) as kill:
            entry.stop_processes([launcher], signal.SIGTERM)
        kill.assert_any_call(4321, signal.SIGTERM)

    def test_forced_cleanup_reaps_the_child(self):
        child = mock.Mock(pid=4321)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("server", 15), -9]
        with mock.patch.object(entry.os, "killpg", create=True) as kill, mock.patch.object(entry.signal, "SIGKILL", 9, create=True):
            entry.stop_processes([child], signal.SIGTERM)
        kill.assert_any_call(4321, 9)
        self.assertEqual(child.wait.call_count, 2)

    def test_session_wait_does_not_sleep_past_deadline(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        self.steam_install()
        desktop = mock.Mock()
        desktop.poll.return_value = None
        with mock.patch.object(entry.time, "monotonic", side_effect=[0, 178, 178, 181]), mock.patch.object(entry.time, "sleep") as sleep, mock.patch.object(ctl, "steam_session_ready", return_value=False):
            self.assertFalse(entry.wait_for_steam_session(desktop))
        sleep.assert_called_once_with(2)

    def test_late_probe_success_does_not_start_after_deadline(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        self.steam_install()
        desktop = mock.Mock()
        desktop.poll.return_value = None
        with mock.patch.object(entry.time, "monotonic", side_effect=[0, 179, 181, 181, 181]), mock.patch.object(entry.time, "sleep"), mock.patch.object(ctl, "steam_session_ready", return_value=True):
            self.assertFalse(entry.wait_for_steam_session(desktop))

    def test_failed_appid_replace_removes_temporary_file(self):
        directory = self.steam_install()
        before = set(directory.rglob("*"))
        with mock.patch.object(ctl.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                ctl.ensure_steam_appid(directory)
        self.assertEqual(set(directory.rglob("*")), before)


class EggTests(unittest.TestCase):
    def test_egg_is_valid_and_has_no_steam_secrets(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        egg = json.loads((root / "egg-farming-simulator-25.json").read_text(encoding="utf-8"))
        variables = {item["env_variable"]: item for item in egg["variables"]}
        self.assertEqual(variables["INSTALL_SOURCE"]["default_value"], "giants")
        self.assertEqual(egg["startup"], "/opt/fs25/fs25ctl.py start-webserver")
        self.assertFalse(any("STEAM" in name and any(secret in name for secret in ("PASSWORD", "GUARD", "TOKEN", "USERNAME")) for name in variables))
        self.assertEqual(len(variables), len(egg["variables"]))
        for name in ("files", "startup", "logs"):
            self.assertIsInstance(json.loads(egg["config"][name]), dict)


class StartupRecoveryTests(RuntimeFixture):
    def test_controller_preserves_server_exit_status(self):
        with mock.patch.object(sys, "argv", ["fs25ctl.py", "start-webserver"]), mock.patch.object(ctl, "start_webserver", return_value=12):
            self.assertEqual(ctl.main(), 12)

    def test_controller_reports_pending_start_as_temporary(self):
        for error in (ctl.SteamNotReady("update pending"), ctl.ServerAlreadyRunning("already starting")):
            with self.subTest(error=error), mock.patch.object(sys, "argv", ["fs25ctl.py", "start-webserver"]), mock.patch.object(ctl, "start_webserver", side_effect=error):
                self.assertEqual(ctl.main(), 75)

    def test_failed_steam_launch_preserves_desktop_and_stops_autostart_helper(self):
        run, spawn = EntrypointTests.main_fixture(self, "steam", installed=True, mode="true")
        server, helper, desktop_service = mock.Mock(), mock.Mock(), mock.Mock()
        server.wait.return_value = 75
        entry.server_children[:] = [server, helper]
        spawn.side_effect = [desktop_service, desktop_service, server, helper]
        with mock.patch.object(entry, "stop_processes") as stop:
            self.assertEqual(entry.main(), 0)
        stop.assert_called_once_with([helper], signal.SIGTERM)

    def test_custom_steam_configuration_failure_preserves_desktop(self):
        run, spawn = EntrypointTests.main_fixture(self, "steam", installed=True)
        os.environ["STARTUP"] = "custom-server"
        run.side_effect = [subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(75, "configure")]
        self.assertEqual(entry.main(), 0)
        self.assertNotIn("dedicated-server.log", [call.args[1] for call in spawn.call_args_list])

    def test_manifest_change_during_controller_preparation_prevents_launch(self):
        directory, chdir, execute = DesktopServerTests.start_fixture(self)
        with mock.patch.object(ctl, "patch_web", side_effect=ctl.SteamNotReady("update pending")):
            with self.assertRaises(ctl.SteamNotReady):
                ctl.start_webserver()
        execute.assert_not_called()

    def test_game_relocation_during_controller_preparation_prevents_launch(self):
        directory, chdir, execute = DesktopServerTests.start_fixture(self)
        with mock.patch.object(ctl, "configure"), mock.patch.object(ctl, "patch_web"), mock.patch.object(ctl, "game_directory", side_effect=[directory, directory / "new-location"]):
            with self.assertRaisesRegex(ctl.SteamNotReady, "changed"):
                ctl.start_webserver()
        execute.assert_not_called()

    def test_server_lock_stays_owned_while_wine_runs(self):
        directory, chdir, execute = DesktopServerTests.start_fixture(self)
        owned = []

        @contextlib.contextmanager
        def lock():
            owned.append(True)
            try:
                yield
            finally:
                owned.pop()

        def running(*args, **kwargs):
            self.assertEqual(owned, [True])
            return subprocess.CompletedProcess([], 0)

        execute.side_effect = running
        with mock.patch.object(ctl, "server_start_lock", side_effect=lock):
            self.assertEqual(ctl.start_webserver(), 0)
        self.assertEqual(owned, [])

    def test_server_signal_exit_status_is_normalized(self):
        directory, chdir, execute = DesktopServerTests.start_fixture(self)
        execute.return_value.returncode = -15
        self.assertEqual(ctl.start_webserver(), 143)

    def test_probe_timeouts_share_the_remaining_wait_budget(self):
        directory = self.session_fixture()
        with mock.patch.object(ctl.time, "monotonic", side_effect=[0, 0, 4]), mock.patch.object(ctl.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "path", ""), subprocess.CompletedProcess([], 1, "STEAM_SESSION_PENDING", "")]) as run:
            self.assertFalse(ctl.steam_session_ready(directory, timeout=5))
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [5, 1])

    def test_expired_budget_does_not_launch_probe(self):
        directory = self.session_fixture()
        with mock.patch.object(ctl.time, "monotonic", side_effect=[0, 0, 5]), mock.patch.object(ctl.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "path", "")) as run:
            self.assertFalse(ctl.steam_session_ready(directory, timeout=5))
        self.assertEqual(run.call_count, 1)

    def test_failed_appid_replace_preserves_existing_appid(self):
        directory = self.steam_install()
        for parent in (directory, directory / "x64"):
            (parent / "steam_appid.txt").write_bytes(b"old id")
        with mock.patch.object(ctl.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ctl.ensure_steam_appid(directory)
        for parent in (directory, directory / "x64"):
            self.assertEqual((parent / "steam_appid.txt").read_bytes(), b"old id")


class ServerLockTests(unittest.TestCase):
    def test_lock_is_nonblocking_and_released_on_exception(self):
        fcntl = mock.Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        with tempfile.TemporaryDirectory() as home, mock.patch.object(ctl, "HOME", pathlib.Path(home)), mock.patch.dict(sys.modules, {"fcntl": fcntl}):
            with self.assertRaisesRegex(ValueError, "launch error"):
                with ctl.server_start_lock():
                    raise ValueError("launch error")
            self.assertTrue((pathlib.Path(home) / ".fs25-webserver.lock").is_file())
        self.assertEqual([call.args[1] for call in fcntl.flock.call_args_list], [6, 8])

    def test_busy_lock_does_not_enter_startup_body(self):
        fcntl = mock.Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        fcntl.flock.side_effect = BlockingIOError("busy")
        with tempfile.TemporaryDirectory() as home, mock.patch.object(ctl, "HOME", pathlib.Path(home)), mock.patch.dict(sys.modules, {"fcntl": fcntl}):
            with self.assertRaisesRegex(ctl.ServerAlreadyRunning, "already running or starting"):
                with ctl.server_start_lock():
                    self.fail("A competing start entered the lock")
        self.assertEqual(fcntl.flock.call_count, 1)

    @unittest.skipUnless(sys.platform == "linux", "Real flock integration runs on Linux CI")
    def test_real_lock_blocks_second_caller_and_allows_restart(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(ctl, "HOME", pathlib.Path(home)):
            with ctl.server_start_lock():
                with self.assertRaises(ctl.ServerAlreadyRunning):
                    with ctl.server_start_lock():
                        self.fail("Concurrent server start obtained the lock")
            with ctl.server_start_lock():
                pass


@unittest.skipUnless(os.environ.get("FS25_TEST_NATIVE_PROBE"), "Native probe paths are optional; the Docker build also runs the probe against test DLLs")
class NativeSteamProbeTests(unittest.TestCase):
    def probe(self, mode="ready", *, dll=None, arguments=None):
        helper = os.environ["FS25_TEST_NATIVE_PROBE"]
        dll = dll or os.environ["FS25_TEST_NATIVE_STUB"]
        environment = os.environ.copy()
        environment["FS25_STEAM_STUB_MODE"] = mode
        return subprocess.run([helper, *(arguments if arguments is not None else [dll])], capture_output=True, text=True, env=environment, timeout=15)

    def test_ready_session(self):
        result = self.probe()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "STEAM_SESSION_READY"))

    def test_not_logged_on(self):
        result = self.probe("offline")
        self.assertEqual((result.returncode, result.stdout.strip()), (1, "STEAM_SESSION_PENDING"))

    def test_initialization_failure(self):
        result = self.probe("initfail")
        self.assertEqual((result.returncode, result.stdout.strip()), (1, "STEAM_SESSION_PENDING"))

    def test_missing_user_interface(self):
        result = self.probe("nouser")
        self.assertEqual((result.returncode, result.stdout.strip()), (1, "STEAM_SESSION_PENDING"))

    def test_unsupported_api(self):
        result = self.probe(dll=os.environ["FS25_TEST_NATIVE_UNSUPPORTED"])
        self.assertEqual((result.returncode, result.stdout.strip()), (2, "STEAM_API_UNSUPPORTED"))

    def test_missing_dll(self):
        result = self.probe(dll=str(pathlib.Path(os.environ["FS25_TEST_NATIVE_STUB"]).with_name("missing.dll")))
        self.assertEqual((result.returncode, result.stdout.strip()), (2, "STEAM_API_LOAD_FAILED"))

    def test_missing_argument(self):
        result = self.probe(arguments=[])
        self.assertEqual((result.returncode, result.stdout.strip()), (2, "STEAM_API_PATH_REQUIRED"))

    def test_unicode_dll_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "Steam API ü.dll"
            path.write_bytes(pathlib.Path(os.environ["FS25_TEST_NATIVE_STUB"]).read_bytes())
            result = self.probe(dll=str(path))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "STEAM_SESSION_READY"))


if __name__ == "__main__":
    unittest.main()
