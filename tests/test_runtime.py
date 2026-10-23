"""Runtime regression tests; no Wine, Steam account or server data required."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shlex
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
        self.steam = self.home / ".local" / "share" / "Steam"
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
            "STEAM_LAUNCHER": self.home / "bin/steam",
        }.items():
            if hasattr(ctl, name):
                paths[name] = value
        patcher = mock.patch.multiple(ctl, **paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        if hasattr(ctl, "STEAM_LAUNCHER"):
            ctl.STEAM_LAUNCHER.parent.mkdir()
            ctl.STEAM_LAUNCHER.write_bytes(b"native launcher fixture")
        entry_patcher = mock.patch.multiple(
            entry, HOME=self.home, LOG_DIR=self.home / "logs", INSTALLER_DIR=self.home / "installer",
            children=[], server_children=[], stopping=False, steam_started=False,
        ) if hasattr(entry, "steam_started") else mock.patch.multiple(entry, HOME=self.home, LOG_DIR=self.home / "logs", INSTALLER_DIR=self.home / "installer")
        entry_patcher.start()
        self.addCleanup(entry_patcher.stop)
        # Existing native-backend regressions remain explicit. Production uses
        # Windows Steam by default; WindowsSteamIntegrationTests cover that path.
        environment = mock.patch.dict(os.environ, {"USER": "container", "FS25_STEAM_BACKEND": "linux"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        if os.name == "nt" and hasattr(ctl, "server_start_lock"):
            lock = mock.patch.object(ctl, "server_start_lock", side_effect=contextlib.nullcontext)
            lock.start()
            self.addCleanup(lock.stop)
            maintenance = mock.patch.object(ctl, "activity_lock", side_effect=lambda **kwargs: contextlib.nullcontext())
            maintenance.start()
            self.addCleanup(maintenance.stop)
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
        (self.steam / "linux64").mkdir()
        (self.steam / "linux64/steamclient.so").touch()
        self.proton_fixture(directory)
        helper = self.home / "steam-session.exe"
        helper.touch()
        patcher = mock.patch.object(ctl, "STEAM_SESSION_HELPER", helper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return directory

    def proton_fixture(self, directory):
        tool = self.home / "tools/Proton 11.0"
        for filename in ("proton", "files/bin/wine", "files/bin/wineserver", "files/lib/wine/x86_64-unix/lsteamclient.so"):
            path = tool / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"full Proton fixture")
        data = directory.parent.parent / "compatdata/2300320"
        (data / "pfx").mkdir(parents=True, exist_ok=True)
        (data / "pfx/system.reg").write_bytes(b"prefix fixture")
        (data / "config_info").write_text(f"11.1000\n{tool / 'files/share/fonts'}\n{tool / 'files/lib'}\n{self.steam}\n", encoding="utf-8")
        return tool, data


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
        functions["link_persistent"].assert_not_called()
        functions["ensure_prefix"].assert_not_called()
        functions["configure_headless_wine"].assert_not_called()
        functions["configure"].assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_steam_layout_creates_native_client_directory_without_wine_link(self):
        with mock.patch.object(ctl, "link_persistent") as link:
            ctl.prepare_steam_layout()
        link.assert_not_called()
        self.assertTrue(self.steam.is_dir())
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
        self.assertFalse(self.steam.is_symlink())
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
        custom = self.home / "SteamGames"
        directory = self.steam_install(library=custom)
        metadata = self.steam / "steamapps/libraryfolders.vdf"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(f'"libraryfolders" {{ "0" {{ "path" "{custom.as_posix()}" "apps" {{ "2300320" "123" }} }} }}')
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

    def test_native_library_resolves_without_wine_prefix_mapping(self):
        self.assertEqual(ctl.persistent_library_path(str(self.home / "Steam")), (self.home / "Steam").resolve())

    def test_relative_network_and_external_libraries_are_rejected(self):
        for value in ("relative", r"\\server\share", r"Z:\outside-container", r"D:\unmapped"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ctl.persistent_library_path(value)


class SteamClientTests(RuntimeFixture):
    def test_first_run_uses_native_linux_launcher(self):
        self.assertEqual(ctl.steam_command(), [str(ctl.STEAM_LAUNCHER), "-cef-disable-gpu"])

    def test_saved_client_launch_has_no_credentials(self):
        self.steam.mkdir(parents=True)
        executable = self.steam / "steam.exe"
        executable.touch()
        with mock.patch.dict(os.environ, {"STEAM_PASSWORD": "secret", "STEAM_GUARD_CODE": "ABCDE"}):
            self.assertEqual(ctl.steam_command(silent=True), [str(ctl.STEAM_LAUNCHER), "-cef-disable-gpu", "-silent"])
            self.assertEqual(ctl.steam_command(), [str(ctl.STEAM_LAUNCHER), "-cef-disable-gpu"])

    def test_windows_client_is_never_selected(self):
        self.steam.mkdir(parents=True)
        (self.steam / "Steam.exe").touch()
        self.assertEqual(ctl.steam_executable(), ctl.STEAM_LAUNCHER)

    def test_missing_installer_is_reported(self):
        with mock.patch.object(ctl, "STEAM_LAUNCHER", self.home / "missing"):
            with self.assertRaisesRegex(RuntimeError, "launcher is missing"):
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

    def test_shutdown_does_not_bootstrap_a_missing_launcher(self):
        with mock.patch.object(ctl, "STEAM_LAUNCHER", self.home / "missing"), mock.patch.object(ctl.subprocess, "run") as run:
            ctl.shutdown_steam()
        run.assert_not_called()

    def test_existing_client_receives_shutdown(self):
        self.steam.mkdir(parents=True)
        (self.steam / "steam.exe").touch()
        with mock.patch.object(ctl.subprocess, "run") as run:
            ctl.shutdown_steam()
        self.assertEqual(run.call_args.args[0][-1], "-shutdown")
        self.assertEqual(run.call_args.kwargs["timeout"], 10)


class SteamGuiLaunchTests(RuntimeFixture):
    def client_fixture(self):
        self.steam.mkdir(parents=True)
        client = ctl.STEAM_LAUNCHER
        os.environ["INSTALL_SOURCE"] = "steam"
        return client

    def test_both_client_modes_disable_cef_gpu_by_default(self):
        self.client_fixture()
        for silent in (False, True):
            with self.subTest(silent=silent):
                command = ctl.steam_command(silent=silent)
                self.assertIn("-cef-disable-gpu", command)
                self.assertEqual("-silent" in command, silent)

    def test_gpu_opt_in_changes_only_client_arguments(self):
        self.client_fixture()
        os.environ["FS25_STEAM_GPU"] = "true"
        self.assertNotIn("-cef-disable-gpu", ctl.steam_command())
        self.assertNotIn("-cef-disable-sandbox", ctl.steam_command())

    def test_context_selects_native_client_without_mutating_parent(self):
        client = self.client_fixture()
        os.environ["WINEDEBUG"] = "-all"
        command, cwd, environment = ctl.steam_launch_context()
        self.assertEqual(command[0], str(client))
        self.assertEqual(cwd, self.home)
        self.assertNotIn("WINEDEBUG", environment)
        self.assertNotIn("PROTON_DISABLE_LSTEAMCLIENT", environment)
        self.assertEqual(os.environ["WINEDEBUG"], "-all")

    def test_native_client_does_not_inherit_wine_import_diagnostics(self):
        self.client_fixture()
        trace = "+timestamp,+pid,+module,+loaddll"
        os.environ["WINEDEBUG"] = trace
        self.assertNotIn("WINEDEBUG", ctl.steam_launch_context()[2])

    def test_first_native_launch_uses_cef_flags_not_windows_installer(self):
        command, cwd, environment = ctl.steam_launch_context()
        self.assertEqual(cwd, self.home)
        self.assertEqual(command, [str(ctl.STEAM_LAUNCHER), "-cef-disable-gpu"])
        self.assertNotIn("WINEDEBUG", environment)

    def test_desktop_launch_passes_same_directory_and_environment(self):
        self.client_fixture()
        with mock.patch.object(ctl, "prepare"), mock.patch.object(ctl.os, "chdir") as chdir, \
                mock.patch.object(ctl.subprocess, "Popen") as execute:
            ctl.open_steam()
        chdir.assert_not_called()
        self.assertEqual(execute.call_args.kwargs["cwd"], self.home)
        self.assertIn("-cef-disable-gpu", execute.call_args.args[0])
        self.assertNotIn("WINEDEBUG", execute.call_args.kwargs["env"])
        self.assertTrue(execute.call_args.kwargs["start_new_session"])

    def test_launch_context_uses_the_selected_command_even_if_installation_changes(self):
        with mock.patch.object(ctl, "steam_executable", side_effect=[ctl.STEAM_LAUNCHER, None]) as check:
            command, cwd, environment = ctl.steam_launch_context()
        self.assertEqual(command[0], str(ctl.STEAM_LAUNCHER))
        self.assertEqual(cwd, self.home)
        self.assertEqual(check.call_count, 1)

    def test_automatic_launch_uses_same_headless_client_context(self):
        self.client_fixture()
        run, spawn = EntrypointTests.main_fixture(self, "steam", installed=True, mode="false")
        self.assertEqual(entry.main(), 0)
        call = next(call for call in spawn.call_args_list if call.args[1] == "steam-launch.log")
        self.assertEqual(call.kwargs["cwd"], self.home)
        self.assertNotIn("WINEDEBUG", call.kwargs["process_env"])
        self.assertIn("-cef-disable-gpu", call.args[0])
        self.assertNotIn("-silent", call.args[0])

    def test_initial_setup_is_visible_and_installed_autostart_is_background(self):
        self.client_fixture()
        run, spawn = EntrypointTests.main_fixture(self, "steam", installed=False, mode="web_only")
        self.assertEqual(entry.main(), 0)
        call = next(call for call in spawn.call_args_list if call.args[1] == "steam-launch.log")
        self.assertNotIn("-silent", call.args[0])
        self.steam_install()
        spawn.reset_mock()
        self.assertEqual(entry.main(), 0)
        call = next(call for call in spawn.call_args_list if call.args[1] == "steam-launch.log")
        self.assertIn("-silent", call.args[0])

    def test_append_log_has_separate_launch_markers_and_runtime_flags(self):
        self.client_fixture()
        environment = {"WINEDEBUG": "-all,err+all", "PROTON_DISABLE_LSTEAMCLIENT": "1", "WINEFSYNC": "0", "PROTON_NO_NTSYNC": "1"}
        path = self.home / "logs/steam-launch.log"
        path.parent.mkdir(parents=True)
        path.write_text("previous output\n", encoding="utf-8")
        with mock.patch.object(entry.subprocess, "Popen", return_value=mock.Mock()) as popen:
            entry.spawn(["wine", "Steam.exe", "-cef-disable-gpu"], "steam-launch.log", cwd=self.steam, process_env=environment)
            entry.spawn(["wine", "Steam.exe", "-cef-disable-gpu"], "steam-launch.log", cwd=self.steam, process_env=environment)
        contents = path.read_text(encoding="utf-8")
        self.assertTrue(contents.startswith("previous output\n"))
        self.assertEqual(contents.count("Launch:"), 2)
        self.assertRegex(contents, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z Launch:")
        self.assertIn("WINEFSYNC=0", contents)
        self.assertEqual(popen.call_args.kwargs["env"], environment)

    def test_documented_diagnostic_command_is_valid(self):
        docs = (SOURCE / "docs/MAINTAINER.md").read_text(encoding="utf-8")
        self.assertIn("/opt/fs25/fs25ctl.py steam >", docs)
        self.assertNotIn("/opt/fs25/fs25ctl.py open-steam >", docs)

    def test_log_write_failure_closes_file_and_does_not_spawn(self):
        target = mock.Mock()
        target.write.side_effect = OSError("disk full")
        with mock.patch.object(pathlib.Path, "open", return_value=target), \
                mock.patch.object(entry.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(OSError, "disk full"):
                entry.spawn(["wine", "Steam.exe"], "steam-launch.log")
        target.close.assert_called_once_with()
        popen.assert_not_called()

    def test_image_has_true_type_fonts_and_both_gui_smokes(self):
        docker = (SOURCE / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("fonts-liberation", docker)
        self.assertIn("fc-cache -f", docker)
        self.assertIn("wine /tmp/steam-import-64/steam-gui-test.exe", docker)
        self.assertIn("wine /tmp/steam-import-32/steam-gui-test.exe", docker)
        self.assertIn("!tests/steam_gui_probe.c", (SOURCE / ".dockerignore").read_text(encoding="utf-8"))


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
        if installed:
            if source == "steam":
                self.steam_install()
            else:
                self.game.mkdir(parents=True, exist_ok=True)
                (self.game / "dedicatedServer.exe").touch()
        desktop = mock.Mock()
        desktop.wait.return_value = 0
        desktop.poll.return_value = None
        server = mock.Mock()
        server.wait.return_value = 0
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
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
        self.proton_fixture(directory)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(ctl, "prepare"))
        stack.enter_context(mock.patch.object(ctl, "steam_session_ready", return_value=session))
        stack.enter_context(mock.patch.object(ctl, "prepare_proton_config"))
        stack.enter_context(mock.patch.object(ctl, "webserver_running", return_value=web))
        stack.enter_context(mock.patch.object(ctl, "game_server_running", return_value=game))
        chdir = stack.enter_context(mock.patch.object(ctl.os, "chdir"))
        execute = stack.enter_context(mock.patch.object(ctl, "run_server_process", return_value=0))
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
        self.assertEqual(execute.call_args.args[0][2:], ["run", str(directory.resolve() / "dedicatedServer.exe")])
        self.assertTrue(execute.call_args.args[0][1].endswith("proton"))
        self.assertEqual(execute.call_args.kwargs["cwd"], directory.resolve())
        self.assertEqual(execute.call_args.kwargs["process_env"]["PROTON_DISABLE_LSTEAMCLIENT"], "0")
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


class StandaloneWineRuntimeTests(RuntimeFixture):
    def test_cached_selection_still_disables_linux_steam_bridge(self):
        os.environ["_FS25_RUNTIME_READY"] = "1"
        ctl.select_wine_runtime()
        self.assertEqual(os.environ.get("PROTON_DISABLE_LSTEAMCLIENT"), "1")

    def test_inherited_zero_cannot_reenable_linux_steam_bridge(self):
        os.environ.update({"_FS25_RUNTIME_READY": "1", "PROTON_DISABLE_LSTEAMCLIENT": "0"})
        ctl.configure_runtime()
        self.assertEqual(os.environ["PROTON_DISABLE_LSTEAMCLIENT"], "1")

    def test_first_runtime_selection_disables_linux_steam_bridge(self):
        runtime = self.home / "wine"
        (runtime / "bin").mkdir(parents=True)
        for name in ("wine", "wineserver"):
            (runtime / "bin" / name).touch()
        with mock.patch.object(ctl, "PROTON_DIR", runtime), mock.patch.object(ctl, "probe_fsync", return_value=(False, "test")):
            ctl.select_wine_runtime()
        self.assertEqual(os.environ.get("PROTON_DISABLE_LSTEAMCLIENT"), "1")
        self.assertEqual(os.environ["WINESERVER"], str(runtime / "bin/wineserver"))

    def test_bridge_is_disabled_before_prefix_boot_and_preserves_data(self):
        os.environ.update({"INSTALL_SOURCE": "giants", "_FS25_RUNTIME_READY": "1"})
        self.steam.mkdir(parents=True)
        saved = self.steam / "loginusers.vdf"
        saved.write_bytes(b"saved session fixture")

        def check_environment():
            self.assertEqual(os.environ.get("PROTON_DISABLE_LSTEAMCLIENT"), "1")

        with mock.patch.object(ctl, "ensure_prefix", side_effect=check_environment), \
                mock.patch.object(ctl, "configure_headless_wine"), \
                mock.patch.object(ctl, "link_persistent"), \
                mock.patch.object(ctl, "create_desktop_shortcuts"):
            ctl.prepare()
        self.assertEqual(saved.read_bytes(), b"saved session fixture")

    def test_game_api_probe_reenables_linux_steam_bridge(self):
        directory = self.session_fixture()
        os.environ["_FS25_RUNTIME_READY"] = "1"
        ctl.configure_runtime()
        results = [subprocess.CompletedProcess([], 0, "path", ""),
                   subprocess.CompletedProcess([], 0, "STEAM_SESSION_READY\n", "")]
        with mock.patch.object(ctl.subprocess, "run", side_effect=results) as run:
            self.assertTrue(ctl.steam_session_ready(directory))
        self.assertEqual(run.call_args.kwargs["env"].get("PROTON_DISABLE_LSTEAMCLIENT"), "0")


class LinuxSteamIntegrationTests(RuntimeFixture):
    def test_steam_runtime_configuration_skips_standalone_wine(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        with mock.patch.object(ctl, "select_wine_runtime") as wine:
            ctl.configure_runtime()
        wine.assert_not_called()

    def test_first_gui_prepare_does_not_create_or_boot_wine_prefix(self):
        os.environ["INSTALL_SOURCE"] = "steam"
        with mock.patch.object(ctl, "ensure_prefix") as boot, mock.patch.object(ctl, "select_wine_runtime") as wine, mock.patch.object(ctl, "configure_terminal"):
            ctl.prepare()
        boot.assert_not_called()
        wine.assert_not_called()
        self.assertFalse(self.prefix.exists())
        self.assertTrue(self.steam.is_dir())
        self.assertIn("fs25ctl.py steam", (ctl.DESKTOP_DIR / "fs25-steam.desktop").read_text())

    def test_native_gui_environment_retains_desktop_not_wine(self):
        os.environ.update({"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/session", "WINEPREFIX": "old-prefix", "WINESERVER": "old-wineserver", "WINEDLLPATH": "old-dlls", "PROTON_DISABLE_LSTEAMCLIENT": "1", "LD_LIBRARY_PATH": "old-libs", "SteamAppId": "480", "PATH": str(ctl.PROTON_DIR / "bin") + os.pathsep + "/usr/bin"})
        command, cwd, environment = ctl.steam_launch_context()
        self.assertEqual(command[0], str(ctl.STEAM_LAUNCHER))
        self.assertEqual(cwd, self.home)
        self.assertEqual(environment["DISPLAY"], ":0")
        self.assertEqual(environment["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/tmp/session")
        self.assertEqual(environment["PATH"], "/usr/bin")
        for key in ("WINEPREFIX", "WINESERVER", "WINEDLLPATH", "PROTON_DISABLE_LSTEAMCLIENT", "LD_LIBRARY_PATH", "SteamAppId"):
            self.assertNotIn(key, environment)
        self.assertEqual(os.environ["PROTON_DISABLE_LSTEAMCLIENT"], "1")

    def test_failed_native_launcher_preserves_novnc(self):
        run, spawn = EntrypointTests.main_fixture(self, "steam", mode="true")
        spawn.side_effect = [mock.Mock(), OSError("launcher missing")]
        self.assertEqual(entry.main(), 0)
        self.assertFalse(entry.steam_started)
        self.assertFalse(any(call.args[1] == "dedicated-server.log" for call in spawn.call_args_list))

    def test_proton_uses_tool_and_prefix_recorded_by_first_game_launch(self):
        directory = self.steam_install()
        tool, data = self.proton_fixture(directory)
        os.environ.update({"WINESERVER": "old-server", "WINEPREFIX": "old-prefix", "WINEDLLPATH": "old-dlls", "PROTON_DISABLE_LSTEAMCLIENT": "1"})
        command, environment = ctl.proton_launch_context(directory, ["dedicatedServer.exe"])
        self.assertEqual(command, [sys.executable, str(tool.resolve() / "proton"), "run", "dedicatedServer.exe"])
        self.assertEqual(environment["STEAM_COMPAT_DATA_PATH"], str(data.resolve()))
        self.assertEqual(environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"], str(self.steam.resolve()))
        self.assertEqual(environment["PROTON_DISABLE_LSTEAMCLIENT"], "0")
        self.assertEqual(environment["SteamAppId"], "2300320")
        for key in ("WINESERVER", "WINEPREFIX", "WINEDLLPATH"):
            self.assertNotIn(key, environment)

    def test_missing_first_run_metadata_stops_server_without_creating_prefix(self):
        directory = self.steam_install()
        with self.assertRaisesRegex(ctl.SteamNotReady, "start FS25 once"):
            ctl.proton_launch_context(directory, ["dedicatedServer.exe"])
        self.assertFalse((directory.parent.parent / "compatdata").exists())

    def test_partial_or_wine_only_tool_is_not_full_proton(self):
        directory = self.steam_install()
        tool, data = self.proton_fixture(directory)
        for filename in ("proton", "files/bin/wine", "files/bin/wineserver", "files/lib/wine/x86_64-unix/lsteamclient.so"):
            with self.subTest(filename=filename):
                path = tool / filename
                contents = path.read_bytes()
                path.unlink()
                with self.assertRaises(ctl.SteamNotReady):
                    ctl.proton_launch_context(directory, ["server.exe"])
                path.write_bytes(contents)

    def test_malformed_or_external_proton_record_is_pending(self):
        directory = self.steam_install()
        tool, data = self.proton_fixture(directory)
        for contents in ("", "one-line", "version\nfonts\nrelative/files/lib\n", "version\nfonts\n/outside/files/lib\n", "x" * 65537):
            with self.subTest(contents=contents[:30]):
                (data / "config_info").write_text(contents)
                with self.assertRaises(ctl.SteamNotReady):
                    ctl.proton_launch_context(directory, ["server.exe"])

    def test_custom_native_library_keeps_its_own_compatdata(self):
        directory = self.steam_install(library=self.home / "Games")
        tool, data = self.proton_fixture(directory)
        command, environment = ctl.proton_launch_context(directory, ["server.exe"])
        self.assertEqual(pathlib.Path(environment["STEAM_COMPAT_DATA_PATH"]), data.resolve())
        self.assertEqual(environment["STEAM_COMPAT_LIBRARY_PATHS"], str(directory.parent.parent.parent))

    def test_proton_diagnostics_are_preserved_only_for_game(self):
        directory = self.steam_install()
        self.proton_fixture(directory)
        os.environ.update({"PROTON_LOG": "1", "WINEDEBUG": "+module", "PROTON_USE_WINED3D": "1"})
        _, environment = ctl.proton_launch_context(directory, ["server.exe"])
        self.assertEqual(environment["PROTON_LOG"], "1")
        self.assertEqual(environment["WINEDEBUG"], "+module")
        self.assertEqual(environment["PROTON_USE_WINED3D"], "1")
        self.assertNotIn("PROTON_LOG", ctl.steam_launch_context()[2])

    def test_game_defaults_are_backed_up_without_overwriting_server_data(self):
        directory = self.steam_install()
        tool, data = self.proton_fixture(directory)
        _, environment = ctl.proton_launch_context(directory, ["server.exe"])
        target = data / "pfx/drive_c/users/steamuser/Documents/My Games/FarmingSimulator2025"
        target.mkdir(parents=True)
        (target / "game.xml").write_bytes(b"first-run defaults")
        (target / "savegame1").mkdir()
        (target / "savegame1/careerSavegame.xml").write_bytes(b"savegame")
        self.config.mkdir(parents=True)
        (self.config / "game.xml").write_bytes(b"server settings")
        with mock.patch.object(pathlib.Path, "symlink_to") as link:
            ctl.prepare_proton_config(environment)
        link.assert_called_once_with(self.config, target_is_directory=True)
        self.assertEqual((self.config / "game.xml").read_bytes(), b"server settings")
        self.assertEqual((self.config / "savegame1/careerSavegame.xml").read_bytes(), b"savegame")
        self.assertEqual((target.with_name(target.name + ".before-fs25-link") / "game.xml").read_bytes(), b"first-run defaults")

    def test_old_windows_client_and_login_are_never_modified(self):
        old = self.prefix / "drive_c/Steam/config/loginusers.vdf"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old login")
        old_game = self.home / "steam/library/steamapps/old-download"
        old_game.parent.mkdir(parents=True)
        old_game.write_bytes(b"download")
        ctl.prepare_steam_layout()
        ctl.steam_launch_context()
        self.assertEqual(old.read_bytes(), b"old login")
        self.assertEqual(old_game.read_bytes(), b"download")

    def test_shutdown_is_native_and_has_clean_environment(self):
        os.environ["PROTON_DISABLE_LSTEAMCLIENT"] = "1"
        with mock.patch.object(ctl.subprocess, "run") as execute:
            ctl.shutdown_steam()
        self.assertEqual(execute.call_args.args[0], [str(ctl.STEAM_LAUNCHER), "-shutdown"])
        self.assertNotIn("PROTON_DISABLE_LSTEAMCLIENT", execute.call_args.kwargs["env"])

    def test_api_conversion_and_probe_use_identical_full_proton_environment(self):
        directory = self.session_fixture()
        os.environ["PROTON_LOG"] = "1"
        results = [subprocess.CompletedProcess([], 0, "Z:\\game\\steam_api64.dll", ""), subprocess.CompletedProcess([], 0, "STEAM_SESSION_READY\n", "")]
        with mock.patch.object(ctl, "prepare_proton_config") as config, mock.patch.object(ctl.subprocess, "run", side_effect=results) as execute:
            self.assertTrue(ctl.steam_session_ready(directory))
        config.assert_not_called()
        conversion, probe = execute.call_args_list
        self.assertEqual(conversion.args[0][2], "getcompatpath")
        self.assertEqual(probe.args[0][2], "run")
        self.assertEqual(conversion.args[0][:2], probe.args[0][:2])
        self.assertEqual(conversion.kwargs["env"], probe.kwargs["env"])
        self.assertEqual(conversion.kwargs["env"]["PROTON_DISABLE_LSTEAMCLIENT"], "0")
        self.assertNotIn("PROTON_LOG", probe.kwargs["env"])

    def test_image_installs_pinned_native_launcher_and_32bit_libraries(self):
        source = (SOURCE / "Dockerfile").read_text()
        self.assertIn("steam-launcher_1.0.0.87_amd64.deb", source)
        self.assertIn("765aba9a0ed339a50226ceb614fcc9879a991ba184098bc8de920efb12c714a4", source)
        self.assertIn("libc6:i386", source)
        self.assertIn("test -x /usr/bin/steam", source)
        self.assertIn("/tmp/steam-libs-amd64.deb /tmp/steam-libs-i386.deb", source)
        self.assertIn("rm -f /etc/apt/sources.list.d/steam-stable.list", source)
        self.assertIn("SteamSetup.exe", source)  # Windows is the namespace-free default.


class WindowsSteamIntegrationTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        os.environ.pop("FS25_STEAM_BACKEND")
        os.environ["INSTALL_SOURCE"] = "steam"
        self.steam_prefix = self.home / ".fs25steam"
        self.steam_wine = self.home / "wine-staging"
        paths = mock.patch.multiple(ctl, STEAM_WINDOWS_PREFIX=self.steam_prefix,
                                    STEAM_WINDOWS_DIR=self.library, STEAM_WINE_DIR=self.steam_wine,
                                    STEAM_SETUP=self.home / "SteamSetup.exe")
        paths.start()
        self.addCleanup(paths.stop)
        for name in ("wine", "wineserver", "winepath", "wineboot"):
            path = self.steam_wine / "bin" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"runtime fixture")
        self.library.mkdir(parents=True)
        (self.library / "steam.exe").write_bytes(b"client fixture")

    def test_default_does_not_require_native_namespaces(self):
        self.assertEqual(ctl.steam_backend(), "windows")
        self.assertEqual(ctl.steam_executable(), self.library / "steam.exe")
        with mock.patch.dict(os.environ, {"FS25_STEAM_BACKEND": "invalid"}):
            with self.assertRaises(ValueError):
                ctl.steam_backend()

    def test_prefix_runtime_and_client_are_separate_from_giants_and_native(self):
        os.environ.update({"WINEPREFIX": str(self.prefix), "WINESERVER": "/old/wineserver",
                           "WINEDLLOVERRIDES": "dwrite=d", "LD_LIBRARY_PATH": "/old/lib",
                           "PROTON_DISABLE_LSTEAMCLIENT": "1", "SteamAppId": "wrong",
                           "DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "desktop-bus"})
        before = dict(os.environ)
        command, cwd, environment = ctl.steam_launch_context()
        self.assertIn(str(self.steam_wine / "bin/wine"), command)
        self.assertIn(str(self.library / "steam.exe"), command)
        self.assertEqual(cwd, self.library)
        self.assertEqual(environment["WINEPREFIX"], str(self.steam_prefix))
        self.assertEqual(environment["WINESERVER"], str(self.steam_wine / "bin/wineserver"))
        self.assertEqual(environment["WINEDLLOVERRIDES"], "mscoree=d")
        self.assertEqual(environment["DBUS_SESSION_BUS_ADDRESS"], "desktop-bus")
        for name in ("LD_LIBRARY_PATH", "PROTON_DISABLE_LSTEAMCLIENT", "SteamAppId"):
            self.assertNotIn(name, environment)
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn("-no-cef-sandbox", command)

    def test_client_affinity_uses_actual_allowed_cpu_ids(self):
        with mock.patch.object(ctl.os, "sched_getaffinity", return_value={7, 9, 11}, create=True):
            self.assertEqual(ctl.steam_client_affinity(), ["/usr/bin/taskset", "-c", "7,9"])
        with mock.patch.object(ctl.os, "sched_getaffinity", return_value={12}, create=True):
            self.assertEqual(ctl.steam_client_affinity()[-1], "12")

    def test_api_and_game_share_runtime_prefix_and_app_id(self):
        directory = self.steam_install()
        helper = self.home / "probe.exe"
        helper.touch()
        results = [subprocess.CompletedProcess([], 0, r"C:\Steam\game\steam_api64.dll", ""),
                   subprocess.CompletedProcess([], 0, "STEAM_SESSION_READY\n", "")]
        with mock.patch.object(ctl, "STEAM_SESSION_HELPER", helper), \
                mock.patch.object(ctl.subprocess, "run", side_effect=results) as run:
            self.assertTrue(ctl.steam_session_ready(directory))
        conversion, probe = run.call_args_list
        self.assertEqual(conversion.args[0][0], str(self.steam_wine / "bin/winepath"))
        self.assertEqual(probe.args[0][0], str(self.steam_wine / "bin/wine"))
        self.assertEqual(conversion.kwargs["env"], probe.kwargs["env"])
        game_command, game_env = ctl.steam_game_launch_context(directory, ["server.exe"])
        self.assertEqual(game_command, [str(self.steam_wine / "bin/wine"), "server.exe"])
        self.assertEqual(game_env, probe.kwargs["env"])
        self.assertEqual(game_env["SteamAppId"], "2300320")
        self.assertNotIn("taskset", game_command)  # Server retains its CPU allowance.

    def test_windows_library_translation_and_external_path_rejection(self):
        with mock.patch.object(pathlib.Path, "resolve", autospec=True, side_effect=lambda path: path):
            self.assertEqual(ctl.persistent_library_path(r"C:\Steam"), self.steam_prefix / "drive_c/Steam")
        if os.name != "nt":
            # The production Wine Z: mapping is a POSIX root, not the Windows
            # host drive used to execute this repository's unit tests.
            with mock.patch.object(ctl, "HOME", pathlib.Path("/home/container")):
                self.assertEqual(ctl.persistent_library_path(r"Z:\home\container\SteamLibrary"),
                                 pathlib.Path("/home/container/SteamLibrary").resolve())
        for value in (r"C:\..\..\outside", r"\\host\share", "relative", "Z:\\outside"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ctl.persistent_library_path(value)

    def test_windows_documents_preserve_existing_server_settings(self):
        _, environment = ctl.steam_game_launch_context(self.library, [])
        target = self.steam_prefix / "drive_c/users/container/Documents/My Games/FarmingSimulator2025"
        target.mkdir(parents=True)
        (target / "game.xml").write_bytes(b"game defaults")
        self.config.mkdir(parents=True)
        (self.config / "game.xml").write_bytes(b"server settings")
        with mock.patch.object(pathlib.Path, "symlink_to") as link:
            ctl.prepare_proton_config(environment)
        link.assert_called_once_with(self.config, target_is_directory=True)
        self.assertEqual((self.config / "game.xml").read_bytes(), b"server settings")
        self.assertEqual((target.with_name(target.name + ".before-fs25-link") / "game.xml").read_bytes(), b"game defaults")

    def test_existing_client_shutdown_uses_the_same_wine_prefix(self):
        with mock.patch.object(ctl.subprocess, "run") as run:
            ctl.shutdown_steam()
        self.assertEqual(run.call_args.args[0][-1], "-shutdown")
        self.assertEqual(run.call_args.kwargs["env"]["WINEPREFIX"], str(self.steam_prefix))
        self.assertEqual(run.call_args.kwargs["cwd"], self.library)

    def test_prepare_dispatches_to_windows_bootstrap_not_giants(self):
        with mock.patch.multiple(ctl, prepare_windows_steam=mock.DEFAULT, ensure_prefix=mock.DEFAULT,
                                 configure_runtime=mock.DEFAULT, create_desktop_shortcuts=mock.DEFAULT) as functions:
            ctl.prepare()
        functions["prepare_windows_steam"].assert_called_once_with()
        functions["ensure_prefix"].assert_not_called()

    def test_image_pins_the_tested_wine_and_keeps_cef_sandbox(self):
        docker = (SOURCE / "Dockerfile").read_text()
        self.assertIn("wine-staging-amd64_11.19~trixie-1_amd64.deb", docker)
        self.assertIn("wine-staging-i386_11.19~trixie-1_i386.deb", docker)
        self.assertIn("a09a949df1b1e297639aa14fb10c43e3c4e66e933f45617e7b9c1ae1e2ae0ded", docker)
        self.assertIn("6996249688cbb0e52bf62171eadb1bd42f90cd036f22208ee22c675d43de3d2a", docker)
        self.assertIn("FS25_STEAM_BACKEND=windows", docker)
        self.assertIn("fs25-steam-wine-smoke", docker)
        self.assertNotIn("-no-cef-sandbox", docker)


class DockerSmokeOwnershipTests(unittest.TestCase):
    """Build contracts; the actual Wine smoke test runs during the image build."""

    def docker_instructions(self):
        source = (SOURCE / "Dockerfile").read_text(encoding="utf-8")
        return source.replace("\\\n", " ").splitlines()

    def test_smoke_prefix_is_created_by_container_before_wineboot(self):
        instructions = self.docker_instructions()
        smokes = [line for line in instructions if line.startswith("RUN ") and "wineboot --init" in line]
        self.assertEqual(len(smokes), 2)
        for smoke in smokes:
            tokens = shlex.split(smoke)
            prefix = tokens[4]
            self.assertIn(prefix, {"/tmp/fs25-wine-smoke", "/tmp/fs25-steam-wine-smoke"})
            self.assertEqual(tokens[:6], ["RUN", "mkdir", "-m", "0700", prefix, "&&"])
            self.assertIn(f"WINEPREFIX={prefix}", tokens[6:])
            position = instructions.index(smoke)
            user = next(line for line in reversed(instructions[:position]) if line.startswith("USER "))
            self.assertEqual(user.split(), ["USER", "container"])

    def test_smoke_dlls_are_owned_by_container_for_tmp_cleanup(self):
        instructions = self.docker_instructions()
        copied = next(line for line in instructions if line.startswith("COPY ") and "/steam-api-test.dll" in line)
        self.assertIn("--chown=container", copied.split())
        self.assertIn("/steam-api-unsupported-test.dll", copied.split())
        self.assertEqual(copied.split()[-1], "/tmp/")

    def test_windows_steam_environment_precedes_wineboot(self):
        instructions = self.docker_instructions()
        environment = next(line for line in instructions if line.startswith("ENV "))
        self.assertIn("PROTON_DISABLE_LSTEAMCLIENT=1", shlex.split(environment))
        smoke = next(line for line in instructions if line.startswith("RUN ") and "wineboot --init" in line)
        self.assertLess(instructions.index(environment), instructions.index(smoke))

    def test_client_dependency_loading_is_smoked_for_both_architectures(self):
        instructions = self.docker_instructions()
        smoke = next(line for line in instructions if line.startswith("RUN ") and "wineboot --init" in line)
        for architecture, name in (("64", "steamclient64.dll"), ("32", "steamclient.dll")):
            self.assertIn(f"wine /tmp/steam-import-{architecture}/steam-import-test.exe", smoke)
            self.assertIn(name, smoke)
        source = (SOURCE / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("gcc-mingw-w64-i686", source)
        self.assertIn("!tests/steam_import_fixture.c", (SOURCE / ".dockerignore").read_text(encoding="utf-8"))


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
            return 0

        execute.side_effect = running
        with mock.patch.object(ctl, "server_start_lock", side_effect=lock):
            self.assertEqual(ctl.start_webserver(), 0)
        self.assertEqual(owned, [])

    def test_server_signal_exit_status_is_normalized(self):
        process = mock.Mock()
        process.wait.return_value = process.poll.return_value = -15
        if not hasattr(signal, "SIGHUP"):
            self.enterContext(mock.patch.object(signal, "SIGHUP", 1, create=True))
        with mock.patch.object(ctl.subprocess, "Popen", return_value=process), \
                mock.patch.object(ctl.signal, "signal"), mock.patch.object(ctl, "process_start_time", return_value="123"):
            self.assertEqual(ctl.run_server_process(["server"], self.home, None, lambda: None), 143)

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


@unittest.skipUnless(os.environ.get("FS25_TEST_NATIVE_IMPORTS"), "Optional native 32/64-bit DLL import fixtures; also run in the Docker build")
class NativeSteamImportTests(unittest.TestCase):
    def probe(self, architecture, *, directory=None, arguments=None):
        root = pathlib.Path(os.environ["FS25_TEST_NATIVE_IMPORTS"]) / f"steam-import-{architecture}"
        client = (directory or root) / ("steamclient64.dll" if architecture == "64" else "steamclient.dll")
        return subprocess.run([str(root / "steam-import-test.exe"),
                               *(arguments if arguments is not None else [str(client)])],
                              capture_output=True, text=True, timeout=15)

    def test_both_clients_load_their_real_dependency(self):
        for architecture in ("32", "64"):
            with self.subTest(architecture=architecture):
                result = self.probe(architecture)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, f"STEAM_IMPORT_READY_{architecture}"))

    def test_missing_dependency_is_detected(self):
        for architecture in ("32", "64"):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as temp:
                source = pathlib.Path(os.environ["FS25_TEST_NATIVE_IMPORTS"]) / f"steam-import-{architecture}"
                client = "steamclient64.dll" if architecture == "64" else "steamclient.dll"
                (pathlib.Path(temp) / client).write_bytes((source / client).read_bytes())
                result = self.probe(architecture, directory=pathlib.Path(temp))
                self.assertEqual(result.returncode, 2)
                self.assertIn("STEAM_IMPORT_LOAD_FAILED", result.stderr)

    def test_unicode_path_preserves_dependency_search(self):
        for architecture in ("32", "64"):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as temp:
                directory = pathlib.Path(temp) / "Steam ü"
                directory.mkdir()
                source = pathlib.Path(os.environ["FS25_TEST_NATIVE_IMPORTS"]) / f"steam-import-{architecture}"
                for dll in source.glob("*.dll"):
                    (directory / dll.name).write_bytes(dll.read_bytes())
                result = self.probe(architecture, directory=directory)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, f"STEAM_IMPORT_READY_{architecture}"))

    def test_missing_path_is_reported(self):
        for architecture in ("32", "64"):
            with self.subTest(architecture=architecture):
                result = self.probe(architecture, arguments=[])
                self.assertEqual((result.returncode, result.stderr.strip()), (2, "STEAM_IMPORT_PATH_REQUIRED"))


@unittest.skipUnless(os.environ.get("FS25_TEST_NATIVE_GUI"), "Optional native Win32 font/thread probes; also run in the Docker build")
class NativeSteamGuiTests(unittest.TestCase):
    def test_font_measurement_and_worker_thread_in_both_architectures(self):
        root = pathlib.Path(os.environ["FS25_TEST_NATIVE_GUI"])
        for architecture in ("32", "64"):
            with self.subTest(architecture=architecture):
                result = subprocess.run([str(root / f"steam-import-{architecture}/steam-gui-test.exe")], capture_output=True, text=True, timeout=15)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, f"STEAM_GUI_READY_{architecture}"))

    def test_probe_pe_machine_types(self):
        root = pathlib.Path(os.environ["FS25_TEST_NATIVE_GUI"])
        for architecture, expected in (("32", 0x14c), ("64", 0x8664)):
            with self.subTest(architecture=architecture):
                data = (root / f"steam-import-{architecture}/steam-gui-test.exe").read_bytes()
                header = int.from_bytes(data[0x3c:0x40], "little")
                self.assertEqual(data[header:header + 4], b"PE\0\0")
                self.assertEqual(int.from_bytes(data[header + 4:header + 6], "little"), expected)


class DesktopMaintenanceTests(RuntimeFixture):
    def test_interrupted_update_suppresses_boot_autostart_but_keeps_vnc(self):
        run, spawn = EntrypointTests.main_fixture(self, "steam", installed=True)
        (self.home / ".fs25-update-incomplete").touch()
        self.assertEqual(entry.main(), 0)
        logs = [call.args[1] for call in spawn.call_args_list]
        self.assertNotIn("dedicated-server.log", logs)
        self.assertNotIn("steam-launch.log", logs)

    def test_manual_start_does_not_rearm_a_cancelled_autostart_helper(self):
        DesktopServerTests.start_fixture(self)
        (self.home / ".fs25-start-cancelled").touch()
        self.assertEqual(ctl.start_webserver(), 0)
        self.assertTrue((self.home / ".fs25-start-cancelled").exists())

    def test_controller_owns_detached_child_and_records_owner_before_releasing_startup(self):
        child = mock.Mock(pid=4242)
        child.poll.return_value = child.wait.return_value = 0
        if not hasattr(signal, "SIGHUP"):
            self.enterContext(mock.patch.object(signal, "SIGHUP", 1, create=True))
        def release():
            state = json.loads((self.home / ".fs25-webserver.json").read_text())
            self.assertEqual(state, {"pid": os.getpid(), "start_time": "123"})
        with mock.patch.object(ctl.subprocess, "Popen", return_value=child) as spawn, \
                mock.patch.object(ctl.signal, "signal"), mock.patch.object(ctl, "process_start_time", return_value="123"):
            self.assertEqual(ctl.run_server_process(["wine", "server.exe"], self.home, {}, release), 0)
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse((self.home / ".fs25-webserver.json").exists())

    def test_controller_signal_targets_only_its_owned_group(self):
        child = mock.Mock(pid=4242)
        child.poll.return_value = -15
        handlers = {}
        if not hasattr(signal, "SIGHUP"):
            self.enterContext(mock.patch.object(signal, "SIGHUP", 1, create=True))
        def handler(sig, value):
            old = handlers.get(sig)
            handlers[sig] = value
            return old
        def wait():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return -15
        child.wait.side_effect = wait
        with mock.patch.object(ctl.subprocess, "Popen", return_value=child), \
                mock.patch.object(ctl.signal, "signal", side_effect=handler), \
                mock.patch.object(ctl.os, "killpg", create=True) as kill, \
                mock.patch.object(ctl, "process_start_time", return_value="123"):
            self.assertEqual(ctl.run_server_process(["server"], self.home, None, lambda: None), 143)
        kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_web_exit_never_ends_desktop_in_either_source(self):
        for source in ("giants", "steam"):
            for status in (0, 12, 75, 143):
                with self.subTest(source=source, status=status):
                    run, spawn = EntrypointTests.main_fixture(self, source, installed=True)
                    desktop, server = mock.Mock(), mock.Mock()
                    desktop.wait.return_value = 37
                    desktop.poll.return_value = None
                    server.wait.return_value = status
                    with mock.patch.object(entry, "start_xvnc", return_value=desktop), \
                            mock.patch.object(entry, "stop_processes"):
                        spawn.return_value = server
                        self.assertEqual(entry.main(), 37)
                    desktop.wait.assert_called_once_with()
                    self.assertEqual(sum(call.args[1] == "dedicated-server.log" for call in spawn.call_args_list), 1)

    def test_desktop_buttons_use_managed_actions_not_wine_or_shell(self):
        ctl.create_desktop_shortcuts()
        for name, action in (("server", "launch-webserver"), ("stop", "stop-server"), ("update", "update")):
            text = (ctl.DESKTOP_DIR / f"fs25-{name}.desktop").read_text()
            self.assertIn(f"/opt/fs25/fs25ctl.py {action}\n", text)
            self.assertNotIn("bash -c", text)
            self.assertNotIn("wine ", text)

    def test_launch_detaches_service_and_log_viewer_does_not_own_it(self):
        with mock.patch.object(ctl, "webserver_running", return_value=False), \
                mock.patch.object(ctl, "server_owner", return_value=None), \
                mock.patch.object(ctl.subprocess, "Popen") as spawn, mock.patch.object(ctl.subprocess, "run") as view:
            ctl.launch_webserver()
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.args[0][-1], "start-webserver")
        self.assertEqual(view.call_args.args[0][:4], ["tail", "-n", "60", "-F"])

    def test_launch_when_running_only_opens_log(self):
        with mock.patch.object(ctl, "webserver_running", return_value=True), \
                mock.patch.object(ctl.subprocess, "Popen") as spawn, mock.patch.object(ctl.subprocess, "run"):
            ctl.launch_webserver()
        spawn.assert_not_called()

    def test_unfinished_update_blocks_both_start_paths(self):
        (self.home / ".fs25-update-incomplete").touch()
        for method in (ctl.launch_webserver, ctl.start_webserver):
            with self.subTest(method=method.__name__), mock.patch.object(ctl, "prepare") as prepare, \
                    mock.patch.object(ctl.subprocess, "Popen") as spawn:
                with self.assertRaises(ctl.MaintenanceBusy):
                    method()
                prepare.assert_not_called()
                spawn.assert_not_called()

    def test_stop_refuses_a_running_game_without_signalling(self):
        with mock.patch.object(ctl, "game_server_running", return_value=True), \
                mock.patch.object(ctl.os, "kill") as kill:
            with self.assertRaises(ctl.MaintenanceBusy):
                ctl.stop_server()
        kill.assert_not_called()
        self.assertFalse((self.home / ".fs25-start-cancelled").exists())

    def test_unmanaged_web_server_is_not_killed(self):
        with mock.patch.object(ctl, "game_server_running", return_value=False), \
                mock.patch.object(ctl, "webserver_running", return_value=True), \
                mock.patch.object(ctl, "server_owner", return_value=None), mock.patch.object(ctl.os, "kill") as kill:
            with self.assertRaises(ctl.MaintenanceBusy):
                ctl.stop_server()
        kill.assert_not_called()

    def test_stop_cancels_pending_autostart(self):
        with mock.patch.object(ctl, "game_server_running", return_value=False), \
                mock.patch.object(ctl, "webserver_running", return_value=False):
            ctl.stop_server()
        with mock.patch.object(ctl.urllib.request, "urlopen") as request:
            ctl.autostart_game()
        request.assert_not_called()

    def test_process_check_failures_are_not_treated_as_stopped(self):
        with mock.patch.object(ctl.os, "getuid", return_value=999, create=True), \
                mock.patch.object(ctl.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "failure")):
            for method in (ctl.webserver_running, ctl.game_server_running, lambda: ctl.matching_processes("steam")):
                with self.assertRaises(RuntimeError):
                    method()

    def update_fixture(self, source="steam", mode="steamcmd"):
        os.environ.update({"INSTALL_SOURCE": source, "STEAM_DOWNLOAD_MODE": mode, "FS25_STEAM_BACKEND": "windows"})
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name in ("stop_owned_webserver", "stop_update_clients", "require_update_idle", "configure", "patch_web", "ensure_steam_appid"):
            stack.enter_context(mock.patch.object(ctl, name))
        stack.enter_context(mock.patch.object(ctl.sys.stdin, "isatty", return_value=True))
        stack.enter_context(mock.patch.object(ctl, "game_server_running", return_value=False))
        stack.enter_context(mock.patch.object(ctl, "matching_processes", return_value=[]))
        stack.enter_context(mock.patch.object(ctl, "game_directory", return_value=self.game))
        return stack

    def test_update_refuses_game_before_stopping_anything(self):
        self.update_fixture()
        with mock.patch.object(ctl, "game_server_running", return_value=True), \
                mock.patch.object(ctl, "stop_owned_webserver") as stop, mock.patch.object(ctl, "update_steamcmd") as download:
            with self.assertRaises(ctl.MaintenanceBusy):
                ctl.update()
        stop.assert_not_called()
        download.assert_not_called()
        self.assertFalse((self.home / ".fs25-update-incomplete").exists())

    def test_update_refuses_nonterminal_without_changing_state(self):
        with mock.patch.object(ctl.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "terminal"):
                ctl.update()
        self.assertFalse((self.home / ".fs25-update-incomplete").exists())

    def test_update_refuses_installer_before_stopping_server(self):
        self.update_fixture()
        with mock.patch.object(ctl, "matching_processes", return_value=[123]), \
                mock.patch.object(ctl, "stop_owned_webserver") as stop:
            with self.assertRaises(ctl.MaintenanceBusy):
                ctl.update()
        stop.assert_not_called()

    def test_update_orders_shutdown_download_checks_and_never_autostarts(self):
        self.update_fixture()
        events = []
        def download():
            self.assertTrue((self.home / ".fs25-update-incomplete").exists())
            events.append("download")
        with mock.patch.object(ctl, "stop_owned_webserver", side_effect=lambda: events.append("stop-web")), \
                mock.patch.object(ctl, "stop_update_clients", side_effect=lambda: events.append("stop-steam")), \
                mock.patch.object(ctl, "update_steamcmd", side_effect=download), \
                mock.patch.object(ctl, "configure", side_effect=lambda: events.append("configure")), \
                mock.patch.object(ctl, "start_webserver") as start:
            ctl.update()
        self.assertEqual(events, ["stop-web", "stop-steam", "download", "configure"])
        start.assert_not_called()
        self.assertFalse((self.home / ".fs25-update-incomplete").exists())

    def test_failed_update_retains_blocker_and_existing_savegame(self):
        self.update_fixture()
        save = self.config / "savegame1/careerSavegame.xml"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"original save")
        with mock.patch.object(ctl, "update_steamcmd", side_effect=RuntimeError("download failed")):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                ctl.update()
        self.assertTrue((self.home / ".fs25-update-incomplete").exists())
        self.assertEqual(save.read_bytes(), b"original save")

    def test_postupdate_process_prevents_success(self):
        self.update_fixture()
        with mock.patch.object(ctl, "update_steamcmd"), \
                mock.patch.object(ctl, "require_update_idle", side_effect=[None, ctl.MaintenanceBusy("late game")]):
            with self.assertRaisesRegex(ctl.MaintenanceBusy, "late game"):
                ctl.update()
        self.assertTrue((self.home / ".fs25-update-incomplete").exists())

    def test_client_update_keeps_lease_until_explicit_confirmation(self):
        self.update_fixture(mode="client")
        ctl.ensure_directories()
        with mock.patch.object(ctl, "prepare"), mock.patch.object(ctl, "steam_launch_context", return_value=(["steam"], self.home, {})), \
                mock.patch.object(ctl.subprocess, "Popen") as launch, mock.patch("builtins.input", return_value="UPDATED"):
            ctl.update()
        self.assertEqual(launch.call_args.args[0], ["steam", "steam://validate/2300320"])
        self.assertFalse((self.home / ".fs25-update-incomplete").exists())

    def test_declined_giants_update_keeps_blocker(self):
        self.update_fixture(source="giants")
        self.game.mkdir(parents=True)
        (self.game / "FarmingSimulator2025.exe").touch()
        with mock.patch.object(ctl, "interactive_process") as run, mock.patch("builtins.input", return_value="no"):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                ctl.update()
        self.assertEqual(run.call_args.args[0], ["wine", str(self.game / "FarmingSimulator2025.exe")])
        self.assertTrue((self.home / ".fs25-update-incomplete").exists())

    def test_existing_cli_download_is_not_killed(self):
        with mock.patch.object(ctl, "matching_processes", return_value=[99]), mock.patch.object(ctl, "shutdown_steam") as shutdown:
            with self.assertRaises(ctl.MaintenanceBusy):
                ctl.stop_update_clients()
        shutdown.assert_not_called()

    def test_failed_client_shutdown_does_not_start_update(self):
        with mock.patch.object(ctl, "matching_processes", side_effect=[[], [77]]), \
                mock.patch.object(ctl, "shutdown_steam"), mock.patch.object(ctl.time, "monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(ctl.MaintenanceBusy, "not exited"):
                ctl.stop_update_clients()


class SteamCMDTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        os.environ.update({"INSTALL_SOURCE": "steam", "STEAM_DOWNLOAD_MODE": "steamcmd", "FS25_STEAM_BACKEND": "windows"})

    def catalog(self):
        return {"extended": {"listofdlc": "777,888,999"}, "depots": {
            "780": {"dlcappid": "777", "manifests": {"public": {"gid": "123"}}},
            "781": {"dlcappid": "888", "config": {"oslist": "macos"}, "manifests": {"public": {"gid": "456"}}},
            "782": {"dlcappid": "999", "config": {"oslist": "windows"}},
        }}

    def cli_install(self):
        directory = self.home / "steamcmd/game"
        (directory / "x64").mkdir(parents=True)
        (directory / "steamapps").mkdir()
        for name in ("FarmingSimulator2025.exe", "dedicatedServer.exe", "x64/FarmingSimulator2025Game.exe", "x64/steam_api64.dll"):
            (directory / name).touch()
        (directory / "steamapps/appmanifest_2300320.acf").write_text(
            '"AppState" { "appid" "2300320" "StateFlags" "4" "installdir" "/custom/force/path" "InstalledDepots" { "780" { "dlcappid" "777" "manifest" "123" } } }')
        return directory

    def test_cli_manifest_resolves_fixed_root_not_gui_common_directory(self):
        directory = self.cli_install()
        self.assertEqual(ctl.steam_game_directory(), directory.resolve())
        self.assertFalse(self.library.exists())

    def test_cli_mode_does_not_silently_reuse_gui_manifest(self):
        self.steam_install()
        with self.assertRaises(ctl.SteamNotReady):
            ctl.steam_game_directory()

    def test_cli_partial_download_blocks_start(self):
        directory = self.cli_install()
        (directory / "steamapps/downloading/2300320").mkdir(parents=True)
        (directory / "steamapps/downloading/2300320/part").touch()
        with self.assertRaises(ctl.SteamNotReady):
            ctl.steam_game_directory()

    def test_cli_mode_requires_compatible_runtime(self):
        os.environ["FS25_STEAM_BACKEND"] = "linux"
        with self.assertRaisesRegex(ValueError, "native Proton"):
            ctl.steam_download_mode()

    def test_catalog_excludes_mac_and_unpublished_depots_not_entitlements(self):
        ids, depots = ctl.steamcmd_dlc_catalog(self.catalog())
        self.assertEqual(ids, {"777", "888", "999"})
        self.assertEqual(depots, {"780": "777"})

    def test_catalog_keeps_empty_os_and_language_as_universal(self):
        info = self.catalog()
        info["depots"]["780"]["config"] = {"oslist": "", "language": "", "osarch": ""}
        self.assertEqual(ctl.steamcmd_dlc_catalog(info)[1], {"780": "777"})

    def test_untrusted_app_id_is_not_used_as_console_command(self):
        info = {"extended": {"listofdlc": "777,+quit"}}
        with self.assertRaises(RuntimeError):
            ctl.steamcmd_dlc_catalog(info)

    def test_appinfo_ignores_banner_and_trailer_but_handles_quoted_braces(self):
        value = ctl.steamcmd_app_info('banner\n"2300320" { "common" { "name" "a { farm }" } }\nUnloading Steam API...OK')
        self.assertEqual(value["common"]["name"], "a { farm }")
        for text in ('"2300320" {', '"123" {}'):
            with self.assertRaises(RuntimeError):
                ctl.steamcmd_app_info(text)

    def test_license_stdout_supports_wrapped_apps_and_missing_licenses(self):
        output = 'License packageID 12:\n - State : Active (flags 0x0)\n - Apps : 2300320,\n 777, (2 in total)\n - Depots : (0 in total)\nNo active license found for appID 888.\n'
        self.assertEqual(ctl.steamcmd_owned_dlcs(output, {"777", "888"}), {"777"})
        with self.assertRaisesRegex(RuntimeError, "every DLC"):
            ctl.steamcmd_owned_dlcs(output, {"777", "999"})

    def test_inactive_and_contradictory_licenses_are_not_entitlements(self):
        for text in ("License packageID 12:\nState : Expired\nApps : 777, (1 in total)",
                     "License packageID 12:\nState : Active\nApps : 777, (1 in total)\nNo active license found for appID 777."):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                ctl.steamcmd_owned_dlcs(text, {"777"})

    def test_bootstrap_preserves_self_updated_client_files(self):
        bootstrap = self.home / "bootstrap"
        bootstrap.mkdir()
        (bootstrap / "steamcmd.sh").write_bytes(b"vendor bootstrap")
        with mock.patch.object(ctl, "STEAMCMD_BOOTSTRAP", bootstrap):
            launcher = ctl.prepare_steamcmd()
            launcher.write_bytes(b"self updated")
            self.assertEqual(ctl.prepare_steamcmd().read_bytes(), b"self updated")

    def update_fixture(self):
        directory = self.cli_install()
        launcher = self.home / "steamcmd/client/steamcmd.sh"
        launcher.parent.mkdir()
        launcher.touch()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(ctl, "prepare_steamcmd", return_value=launcher))
        stack.enter_context(mock.patch.object(ctl, "steamcmd_app_info", return_value=self.catalog()))
        stack.enter_context(mock.patch("builtins.input", return_value="account_fixture"))
        run = stack.enter_context(mock.patch.object(ctl, "interactive_process", side_effect=["metadata", "License packageID 12:\nState : Active (flags 0x0)\nApps : 777, (1 in total)\nNo active license found for appID 888.\nNo active license found for appID 999.\n", "Success!"]))
        return directory, run

    def test_cli_download_validates_live_entitlements_and_writes_nonsecret_receipt(self):
        directory, run = self.update_fixture()
        ctl.update_steamcmd()
        self.assertEqual(len(run.call_args_list), 3)
        self.assertEqual(run.call_args.args[0][-4:], ["+app_update", "2300320", "validate", "+quit"])
        self.assertEqual(run.call_args.kwargs["process_env"]["HOME"], str(self.home / "steamcmd/home"))
        receipt = json.loads((self.home / "steamcmd/verified-dlcs.json").read_text())
        self.assertEqual(receipt["owned_dlcs"], ["777"])
        self.assertNotIn("account_fixture", json.dumps(receipt))
        self.assertNotIn("WINEPREFIX", run.call_args.kwargs["process_env"])

    def test_missing_entitlement_response_prevents_game_download(self):
        directory, run = self.update_fixture()
        run.side_effect = ["metadata", "No active license found for appID 777."]
        with self.assertRaises(RuntimeError):
            ctl.update_steamcmd()
        self.assertEqual(run.call_count, 2)

    def test_missing_owned_depot_is_not_reported_as_success(self):
        directory, run = self.update_fixture()
        manifest = directory / "steamapps/appmanifest_2300320.acf"
        manifest.write_text(manifest.read_text().replace('"780"', '"790"'))
        with self.assertRaisesRegex(RuntimeError, "missing"):
            ctl.update_steamcmd()
        self.assertFalse((self.home / "steamcmd/verified-dlcs.json").exists())

    def test_stale_dlc_manifest_is_not_reported_as_success(self):
        directory, run = self.update_fixture()
        manifest = directory / "steamapps/appmanifest_2300320.acf"
        manifest.write_text(manifest.read_text().replace('"123"', '"100"'))
        with self.assertRaisesRegex(RuntimeError, "current public manifest"):
            ctl.update_steamcmd()

    def test_account_prompt_rejects_injected_console_arguments(self):
        directory, run = self.update_fixture()
        with mock.patch("builtins.input", return_value="name +quit"):
            with self.assertRaises(ValueError):
                ctl.update_steamcmd()
        run.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Symlink containment integration requires Linux")
    def test_cli_path_symlink_outside_home_is_rejected_before_bootstrap_copy(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.home / "steamcmd").mkdir()
            (self.home / "steamcmd/game").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "inside persistent HOME"):
                ctl.prepare_steamcmd()


class UpdateBuildContractTests(unittest.TestCase):
    def test_steamcmd_is_checksum_pinned_and_elf_dependencies_checked(self):
        text = (SOURCE / "Dockerfile").read_text()
        self.assertIn("cebf0046bfd08cf45da6bc094ae47aa39ebf4155e5ede41373b579b8f1071e7c", text)
        self.assertIn("ldd /opt/fs25/steamcmd/linux32/steamcmd", text)
        self.assertIn("STEAM_DOWNLOAD_MODE=client", text)

    def test_egg_has_explicit_optin_and_still_no_authentication_secrets(self):
        egg = json.loads((SOURCE / "egg-farming-simulator-25.json").read_text())
        variables = {item["env_variable"]: item for item in egg["variables"]}
        self.assertEqual(variables["STEAM_DOWNLOAD_MODE"]["default_value"], "client")
        self.assertIn("in:client,steamcmd", variables["STEAM_DOWNLOAD_MODE"]["rules"])
        self.assertFalse(any("STEAM" in key and any(secret in key for secret in ("PASSWORD", "GUARD", "TOKEN", "USERNAME")) for key in variables))

    def test_candidate_pushes_never_set_latest_tag(self):
        text = (SOURCE / ".github/workflows/docker.yml").read_text()
        self.assertIn("'codex/**'", text)
        self.assertIn('echo "$name:sha-$GITHUB_SHA"', text)
        self.assertIn('if [ "$GITHUB_REF" = refs/heads/main ]; then\n              echo "$name:latest"', text)
        self.assertNotIn(":steam-wine1119", text)


class MaintenanceOwnerTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.proc = self.home / "proc"
        self.enterContext(mock.patch.object(ctl, "PROC_DIR", self.proc))
        self.enterContext(mock.patch.object(ctl.os, "getuid", return_value=os.getuid() if hasattr(os, "getuid") else 0, create=True))

    def owner(self):
        pid = 31337
        path = self.proc / str(pid)
        path.mkdir(parents=True)
        (path / "stat").write_text(f'{pid} (owner (with spaces)) ' + ' '.join(["S"] + ["0"] * 18 + ["123"]))
        (path / "cmdline").write_bytes(b"python3\0/opt/fs25/fs25ctl.py\0start-webserver\0")
        (self.home / ".fs25-webserver.json").write_text(json.dumps({"pid": pid, "start_time": "123"}))
        return pid

    def test_verified_owner_and_parenthesized_stat_name(self):
        pid = self.owner()
        self.assertEqual(ctl.server_owner(), pid)

    def test_pid_reuse_is_not_signalled(self):
        pid = self.owner()
        (self.proc / str(pid) / "stat").write_text(f'{pid} (new process) ' + ' '.join(["S"] + ["0"] * 18 + ["456"]))
        self.assertIsNone(ctl.server_owner())

    def test_foreign_controller_is_rejected(self):
        pid = self.owner()
        (self.proc / str(pid) / "cmdline").write_bytes(b"python3\0unrelated.py\0start-webserver\0")
        with self.assertRaises(ctl.MaintenanceBusy):
            ctl.server_owner()


class ActivityLockTests(unittest.TestCase):
    def test_shared_and_exclusive_locks_are_nonblocking_and_inode_is_kept(self):
        for exclusive, expected in ((False, 5), (True, 6)):
            fcntl = mock.Mock(LOCK_EX=2, LOCK_SH=1, LOCK_NB=4, LOCK_UN=8)
            with self.subTest(exclusive=exclusive), tempfile.TemporaryDirectory() as home, \
                    mock.patch.object(ctl, "HOME", pathlib.Path(home)), mock.patch.dict(sys.modules, {"fcntl": fcntl}):
                with self.assertRaisesRegex(ValueError, "test exit"):
                    with ctl.activity_lock(exclusive=exclusive):
                        raise ValueError("test exit")
                self.assertTrue((pathlib.Path(home) / ".fs25-maintenance.lock").exists())
            self.assertEqual([call.args[1] for call in fcntl.flock.call_args_list], [expected, 8])

    def test_busy_maintenance_does_not_enter_body(self):
        fcntl = mock.Mock(LOCK_EX=2, LOCK_SH=1, LOCK_NB=4, LOCK_UN=8)
        fcntl.flock.side_effect = BlockingIOError("busy")
        with tempfile.TemporaryDirectory() as home, mock.patch.object(ctl, "HOME", pathlib.Path(home)), \
                mock.patch.dict(sys.modules, {"fcntl": fcntl}):
            with self.assertRaises(ctl.MaintenanceBusy):
                with ctl.activity_lock(exclusive=True):
                    self.fail("Entered update while busy")

    @unittest.skipUnless(sys.platform == "linux", "Real flock requires Linux")
    def test_real_shared_starts_exclude_update_and_exclusive_update_excludes_starts(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(ctl, "HOME", pathlib.Path(home)):
            with ctl.activity_lock():
                with ctl.activity_lock():
                    pass
                with self.assertRaises(ctl.MaintenanceBusy):
                    with ctl.activity_lock(exclusive=True):
                        pass
            with ctl.activity_lock(exclusive=True):
                for exclusive in (False, True):
                    with self.assertRaises(ctl.MaintenanceBusy):
                        with ctl.activity_lock(exclusive=exclusive):
                            pass
            with ctl.activity_lock(exclusive=True):
                pass


@unittest.skipUnless(sys.platform == "linux", "Real controlling PTY integration requires Linux")
class InteractiveTerminalTests(unittest.TestCase):
    def execute(self, command):
        import pty
        import termios
        import types
        with tempfile.TemporaryDirectory() as home:
            master, slave = pty.openpty()
            try:
                before = termios.tcgetattr(slave)
                output = io.BytesIO()
                with mock.patch.object(ctl.sys, "stdin", types.SimpleNamespace(fileno=lambda: slave)), \
                        mock.patch.object(ctl.sys, "stdout", types.SimpleNamespace(buffer=output)):
                    result = ctl.interactive_process(command, cwd=pathlib.Path(home), capture=True)
                self.assertEqual(termios.tcgetattr(slave), before)
                return result, output.getvalue()
            finally:
                os.close(master)
                os.close(slave)

    def test_real_child_gets_controlling_terminal_and_prompt_without_newline_is_forwarded(self):
        command = [sys.executable, "-c", 'import os,sys; print("PTY="+str(os.isatty(0) and os.isatty(1)),flush=True); fd=os.open("/dev/tty",os.O_RDWR);os.close(fd);sys.stdout.write("PROMPT>");sys.stdout.flush()']
        result, output = self.execute(command)
        self.assertIn("PTY=True", result)
        self.assertTrue(output.endswith(b"PROMPT>"))

    def test_nonzero_exit_is_reported_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "status 8"):
            self.execute([sys.executable, "-c", "raise SystemExit(8)"])


if __name__ == "__main__":
    unittest.main()
