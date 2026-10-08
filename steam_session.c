/* Probe the installed game's Steam API without launching the game or logging in.
 * This executable runs under FS25's full Proton and bridges to native Linux Steam.
 * Copyright (c) 2026 Eric (DasEric). MIT, see LICENSE.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdbool.h>
#include <stdio.h>

typedef bool (__cdecl *steam_init_fn)(void);
typedef void (__cdecl *steam_void_fn)(void);
typedef void *(__cdecl *steam_user_fn)(void);
typedef bool (__cdecl *steam_logged_on_fn)(void *);

int wmain(int argc, wchar_t **argv)
{
    if (argc != 2) {
        puts("STEAM_API_PATH_REQUIRED");
        return 2;
    }
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    HMODULE api = LoadLibraryExW(argv[1], NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!api) {
        puts("STEAM_API_LOAD_FAILED");
        return 2;
    }
    steam_init_fn init = (steam_init_fn)GetProcAddress(api, "SteamAPI_Init");
    steam_void_fn shutdown = (steam_void_fn)GetProcAddress(api, "SteamAPI_Shutdown");
    steam_void_fn callbacks = (steam_void_fn)GetProcAddress(api, "SteamAPI_RunCallbacks");
    steam_logged_on_fn logged_on = (steam_logged_on_fn)GetProcAddress(api, "SteamAPI_ISteamUser_BLoggedOn");
    steam_user_fn user = NULL;
    /* Steam API exports are versioned; use only a version the game DLL exports. */
    for (int version = 23; version >= 17 && !user; --version) {
        char name[64];
        snprintf(name, sizeof(name), "SteamAPI_SteamUser_v%03d", version);
        user = (steam_user_fn)GetProcAddress(api, name);
    }
    if (!init || !shutdown || !user || !logged_on) {
        puts("STEAM_API_UNSUPPORTED");
        FreeLibrary(api);
        return 2;
    }
    if (!init()) {
        puts("STEAM_SESSION_PENDING");
        FreeLibrary(api);
        return 1;
    }
    if (callbacks)
        callbacks();
    void *user_interface = user();
    bool ready = user_interface && logged_on(user_interface);
    shutdown();
    FreeLibrary(api);
    puts(ready ? "STEAM_SESSION_READY" : "STEAM_SESSION_PENDING");
    return ready ? 0 : 1;
}
