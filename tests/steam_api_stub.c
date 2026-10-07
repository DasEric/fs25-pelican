/* Isolated test double for the probe's Windows DLL loading/flat-API ABI.
 * It never connects to Steam. MIT, see LICENSE.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdbool.h>
#include <string.h>

static char mode[32];
static bool initialized;
static bool callbacks_seen;

__declspec(dllexport) bool __cdecl SteamAPI_Init(void)
{
    GetEnvironmentVariableA("FS25_STEAM_STUB_MODE", mode, sizeof(mode));
    initialized = strcmp(mode, "initfail") != 0;
    callbacks_seen = false;
    return initialized;
}

__declspec(dllexport) void __cdecl SteamAPI_RunCallbacks(void)
{
    callbacks_seen = true;
}

__declspec(dllexport) void __cdecl SteamAPI_Shutdown(void)
{
    initialized = false;
}

#ifndef FS25_STUB_UNSUPPORTED
__declspec(dllexport) void *__cdecl SteamAPI_SteamUser_v023(void)
{
    return strcmp(mode, "nouser") == 0 ? NULL : &initialized;
}
#endif

__declspec(dllexport) bool __cdecl SteamAPI_ISteamUser_BLoggedOn(void *user)
{
    return user == &initialized && initialized && callbacks_seen && strcmp(mode, "offline") != 0;
}
