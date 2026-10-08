/* Synthetic Steam-named DLLs: exercise Wine's import loader, not Steam APIs.
 * Build this source separately as the dependency, client and probe for each
 * architecture. No Valve binaries, accounts or game files are used.
 */
#include <windows.h>
#include <stdio.h>

#if defined(FS25_IMPORT_DEPENDENCY)

__declspec(dllexport) int __cdecl FS25FixtureDependency(void)
{
    return 2525;
}

#elif defined(FS25_IMPORT_CLIENT)

__declspec(dllimport) int __cdecl FS25FixtureDependency(void);

__declspec(dllexport) int __cdecl FS25FixtureClient(void)
{
    return FS25FixtureDependency();
}

#else

typedef int (__cdecl *fixture_client_fn)(void);

static LONG WINAPI import_exception(EXCEPTION_POINTERS *exception)
{
    (void)exception;
    fputs("STEAM_IMPORT_EXCEPTION\n", stderr);
    fflush(stderr);
    ExitProcess(3);
    return EXCEPTION_EXECUTE_HANDLER;
}

int wmain(int argc, wchar_t **argv)
{
    HMODULE client;
    fixture_client_fn check;
    int value;

    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    SetUnhandledExceptionFilter(import_exception);
    if (argc != 2)
    {
        fputs("STEAM_IMPORT_PATH_REQUIRED\n", stderr);
        return 2;
    }
    client = LoadLibraryExW(argv[1], NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!client)
    {
        fprintf(stderr, "STEAM_IMPORT_LOAD_FAILED %lu\n", (unsigned long)GetLastError());
        return 2;
    }
    check = (fixture_client_fn)GetProcAddress(client, "FS25FixtureClient");
    if (!check)
    {
        fputs("STEAM_IMPORT_EXPORT_MISSING\n", stderr);
        FreeLibrary(client);
        return 2;
    }
    value = check();
    FreeLibrary(client);
    if (value != 2525)
    {
        fputs("STEAM_IMPORT_DEPENDENCY_FAILED\n", stderr);
        return 1;
    }
#ifdef _WIN64
    puts("STEAM_IMPORT_READY_64");
#else
    puts("STEAM_IMPORT_READY_32");
#endif
    return 0;
}

#endif
