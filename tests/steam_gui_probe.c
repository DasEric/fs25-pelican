/* Check the Win32 operations mentioned in Steam startup errors, without Steam.
 * The isolated Wine build test exercises both WoW64 and x64 using Xvfb.
 */
#include <windows.h>
#include <stdio.h>

static DWORD WINAPI worker(void *argument)
{
    (void)argument;
    return 25;
}

static int check_threads(void)
{
    DWORD id, status = 0;
    HANDLE thread = CreateThread(NULL, 0, worker, NULL, CREATE_SUSPENDED, &id);
    if (!thread)
    {
        fprintf(stderr, "STEAM_GUI_THREAD_CREATE_FAILED %lu\n", (unsigned long)GetLastError());
        return 1;
    }
    if (ResumeThread(thread) == (DWORD)-1 || WaitForSingleObject(thread, 10000) != WAIT_OBJECT_0 ||
        !GetExitCodeThread(thread, &status) || status != 25)
    {
        fprintf(stderr, "STEAM_GUI_THREAD_WAIT_FAILED %lu\n", (unsigned long)GetLastError());
        CloseHandle(thread);
        return 1;
    }
    CloseHandle(thread);
    return 0;
}

static int check_font(const wchar_t *face)
{
    HDC dc = CreateCompatibleDC(NULL);
    HFONT font;
    HGDIOBJ previous;
    SIZE size = {0};
    TEXTMETRICW metrics;
    int ready = 0;
    if (!dc)
    {
        fputs("STEAM_GUI_DC_FAILED\n", stderr);
        return 1;
    }
    font = CreateFontW(-16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                       DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                       DEFAULT_QUALITY, DEFAULT_PITCH | FF_DONTCARE, face);
    previous = font ? SelectObject(dc, font) : NULL;
    if (previous && previous != HGDI_ERROR)
    {
        ready = GetTextExtentPoint32W(dc, L"Steam FS25", 10, &size) &&
                GetTextMetricsW(dc, &metrics) && size.cx > 0 && size.cy > 0 && metrics.tmHeight > 0;
        SelectObject(dc, previous);
    }
    if (font) DeleteObject(font);
    DeleteDC(dc);
    if (!ready) fprintf(stderr, "STEAM_GUI_FONT_MEASURE_FAILED %ls\n", face);
    return ready ? 0 : 1;
}

int main(void)
{
    static const wchar_t *const faces[] = {L"Tahoma", L"Arial", L"Verdana", L"Times New Roman"};
    unsigned int index;
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    if (check_threads()) return 1;
    for (index = 0; index < sizeof(faces) / sizeof(faces[0]); ++index)
        if (check_font(faces[index])) return 1;
#ifdef _WIN64
    puts("STEAM_GUI_READY_64");
#else
    puts("STEAM_GUI_READY_32");
#endif
    return 0;
}
