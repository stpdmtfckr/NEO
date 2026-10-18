/* NEO process host: runs NEO's Python runtime inside a process named NEO with NEO's icon.
 *
 * Windows shows a process by its executable: NEO used to run inside pythonw.exe, so Task
 * Manager showed "Python" with the Python icon. This host is copied into the NEO runtime
 * (<runtime>\Scripts\NEO.exe). It reads <runtime>\pyvenv.cfg, loads that Python's
 * python3XY.dll into this process and calls Py_Main with the same command line, exactly
 * like pythonw.exe. __PYVENV_LAUNCHER__ (the variable Windows' own venv launcher uses)
 * points at this file, so sys.executable is this host and the virtual environment is
 * active; helper processes NEO starts with sys.executable are "NEO" as well.
 *
 * It contains no application code and runs nothing but the NEO runtime it sits in.
 * Build: x86_64-w64-mingw32-gcc -municode -mwindows -Os -s neo_host.c neo_host.res -lshell32 -o NEO.exe
 *    (or zig cc -target x86_64-windows-gnu with the same flags), see build_host.py.
 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>
#include <stdio.h>

#define CAP 32768
typedef int (*py_main_t)(int, wchar_t **);
static wchar_t self_path[CAP], runtime[CAP], cfg_path[CAP], home[CAP], dll[CAP];

static int report(const wchar_t *message, DWORD code) {
    HANDLE err = GetStdHandle(STD_ERROR_HANDLE);
    if (err && err != INVALID_HANDLE_VALUE) {
        char text[2048];
        int n = WideCharToMultiByte(CP_UTF8, 0, message, -1, text, sizeof(text) - 64, NULL, NULL);
        if (n > 0) {
            DWORD written = 0;
            char tail[64];
            _snprintf(tail, sizeof(tail), " (code %lu)\r\n", (unsigned long)code);
            WriteFile(err, text, (DWORD)strlen(text), &written, NULL);
            WriteFile(err, tail, (DWORD)strlen(tail), &written, NULL);
        }
    } else {
        wchar_t detail[1400];
        _snwprintf(detail, 1400, L"%ls\n\nCode: %lu\nRun NEO.cmd --repair.", message, (unsigned long)code);
        detail[1399] = 0;
        MessageBoxW(NULL, detail, L"NEO", MB_OK | MB_ICONERROR);
    }
    return code ? (int)code : 1;
}

/* key = value from pyvenv.cfg (UTF-8, one pair per line). */
static int read_value(const char *text, const char *key, wchar_t *out, int cap) {
    size_t key_len = strlen(key);
    const char *line = text;
    while (line && *line) {
        const char *next = strchr(line, '\n');
        while (*line == ' ' || *line == '\t') line++;
        if (_strnicmp(line, key, key_len) == 0) {
            const char *p = line + key_len;
            while (*p == ' ' || *p == '\t') p++;
            if (*p == '=') {
                p++;
                while (*p == ' ' || *p == '\t') p++;
                const char *end = next ? next : p + strlen(p);
                while (end > p && (end[-1] == '\r' || end[-1] == ' ' || end[-1] == '\t')) end--;
                int n = MultiByteToWideChar(CP_UTF8, 0, p, (int)(end - p), out, cap - 1);
                if (n <= 0) return 0;
                out[n] = 0;
                return 1;
            }
        }
        line = next ? next + 1 : NULL;
    }
    return 0;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous; (void)arguments; (void)show;
    DWORD n = GetModuleFileNameW(NULL, self_path, CAP);
    if (!n || n >= CAP - 1) return report(L"NEO: cannot determine its own path.", GetLastError());
    wcscpy(runtime, self_path);
    for (int up = 0; up < 2; up++) {           /* <runtime>\Scripts\NEO.exe -> <runtime> */
        wchar_t *slash = wcsrchr(runtime, L'\\');
        if (!slash) return report(L"NEO: unexpected host location.", 2);
        *slash = 0;
    }
    _snwprintf(cfg_path, CAP, L"%ls\\pyvenv.cfg", runtime);
    HANDLE file = CreateFileW(cfg_path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return report(L"NEO: the NEO runtime (pyvenv.cfg) was not found next to the host.", GetLastError());
    char text[8192];
    DWORD got = 0;
    BOOL ok = ReadFile(file, text, sizeof(text) - 1, &got, NULL);
    CloseHandle(file);
    if (!ok) return report(L"NEO: cannot read pyvenv.cfg.", GetLastError());
    text[got] = 0;
    wchar_t version[64];
    if (!read_value(text, "home", home, CAP)) return report(L"NEO: pyvenv.cfg has no home.", 2);
    if (!read_value(text, "version_info", version, 64) && !read_value(text, "version", version, 64))
        return report(L"NEO: pyvenv.cfg has no version.", 2);
    int major = 0, minor = 0;
    if (swscanf(version, L"%d.%d", &major, &minor) != 2 || major != 3 || minor < 8 || minor > 40)
        return report(L"NEO: unsupported Python version in pyvenv.cfg.", 2);
    _snwprintf(dll, CAP, L"%ls\\python%d%d.dll", home, major, minor);
    SetDllDirectoryW(home);                      /* vcruntime and python3.dll live next to it */
    HMODULE python = LoadLibraryExW(dll, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!python) return report(L"NEO: cannot load the Python runtime DLL.", GetLastError());
    py_main_t py_main = (py_main_t)(void *)GetProcAddress(python, "Py_Main");
    if (!py_main) return report(L"NEO: the Python runtime has no Py_Main.", 127);
    /* The same variable Windows' venv launcher sets: sys.executable = this host, venv active. */
    SetEnvironmentVariableW(L"__PYVENV_LAUNCHER__", self_path);
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return report(L"NEO: cannot parse the command line.", GetLastError());
    argv[0] = self_path;
    int code = py_main(argc, argv);
    LocalFree(argv);
    return code;
}
