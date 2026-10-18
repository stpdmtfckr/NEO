/* NEO Windows GUI entry point.
 * This source build never invokes the historical bundled NEO_JARVIS launcher.
 * Compile: zig cc -target x86_64-windows-gnu -municode -Wl,--subsystem,windows -Os -s
 *          neo_launcher.c -lshell32 -luser32 -o NEO.exe
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
static wchar_t root[CAP], script[CAP], shell[CAP], command[CAP], logpath[CAP];

static int fail(const wchar_t *message, DWORD code) {
    wchar_t detail[1024];
    _snwprintf(detail, 1024, L"%ls\n\nCode: %lu\nLogs: %%LOCALAPPDATA%%\\NeoJarvis\\bootstrap\nRun NEO.cmd --doctor or NEO.cmd --collect-logs.", message, (unsigned long)code);
    detail[1023] = 0;
    MessageBoxW(NULL, detail, L"NEO", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
    return code ? (int)code : 1;
}

static HWND show_startup_status(HINSTANCE instance) {
    WNDCLASSW wc = {0};
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = instance;
    wc.hCursor = LoadCursorW(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    wc.lpszClassName = L"NEOBootstrapStatus145";
    RegisterClassW(&wc);
    HWND status = CreateWindowExW(WS_EX_TOOLWINDOW, wc.lpszClassName, L"NEO - Starting",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, CW_USEDEFAULT, CW_USEDEFAULT,
        540, 190, NULL, NULL, instance, NULL);
    if (status) {
        CreateWindowExW(0, L"STATIC",
            L"Preparing NEO and starting the assistant.\r\n\r\n"
            L"The first start may download Python and required packages.\r\n"
            L"This window closes when the assistant is ready.\r\n"
            L"Startup errors will be shown here; details are kept in the logs.",
            WS_CHILD | WS_VISIBLE, 18, 18, 492, 125, status, NULL, instance, NULL);
        ShowWindow(status, SW_SHOWNOACTIVATE);
        UpdateWindow(status);
    }
    return status;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous; (void)arguments; (void)show;
    DWORD n = GetModuleFileNameW(NULL, root, CAP);
    if (!n || n >= CAP - 1) return fail(L"Cannot determine the NEO package path.", GetLastError());
    wchar_t *end = wcsrchr(root, L'\\');
    if (!end) return fail(L"Invalid NEO package path.", 2);
    *end = 0;
    if (wcslen(root) + 80 >= CAP) return fail(L"NEO package path is too long.", 206);
    _snwprintf(script, CAP, L"%ls\\_internal\\launch.ps1", root);
    if (GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES)
        return fail(L"NEO package is incomplete. Extract the entire archive before launching.", 2);
    n = GetSystemDirectoryW(shell, CAP);
    if (!n || n + 60 >= CAP) return fail(L"Cannot find Windows PowerShell.", GetLastError());
    wcscat(shell, L"\\WindowsPowerShell\\v1.0\\powershell.exe");
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return fail(L"Cannot parse the NEO command line.", GetLastError());
    /* Diagnostic commands intentionally use CMD so their complete output is visible. */
    if (argc > 1 && wcscmp(argv[1], L"--autostart") && wcscmp(argv[1], L"--remote-start")) {
        LocalFree(argv);
        MessageBoxW(NULL, L"Use NEO.cmd for --doctor, --self-test, --repair, --tests, --integration-test, --collect-logs or --uninstall.", L"NEO", MB_OK);
        return 2;
    }
    LocalFree(argv);
    /* Windows filenames cannot contain a double quote. -File prevents PowerShell
       from interpreting spaces, apostrophes or ampersands as script expressions. */
    if (_snwprintf(command, CAP, L"\"%ls\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"%ls\"", shell, script) < 0)
        return fail(L"NEO command line is too long.", 206);
    n = GetEnvironmentVariableW(L"LOCALAPPDATA", logpath, CAP);
    if (!n || n + 80 >= CAP) return fail(L"LOCALAPPDATA is unavailable.", 2);
    wcscat(logpath, L"\\NeoJarvis");
    CreateDirectoryW(logpath, NULL);
    wcscat(logpath, L"\\bootstrap");
    CreateDirectoryW(logpath, NULL);
    wcscat(logpath, L"\\gui-launcher.log");
    SECURITY_ATTRIBUTES security = {sizeof(SECURITY_ATTRIBUTES), NULL, TRUE};
    HANDLE output = CreateFileW(logpath, FILE_APPEND_DATA | GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (output == INVALID_HANDLE_VALUE) return fail(L"Cannot write the NEO startup log.", GetLastError());
    LARGE_INTEGER size;
    if (GetFileSizeEx(output, &size) && size.QuadPart > 2 * 1024 * 1024) {
        CloseHandle(output);
        output = CreateFileW(logpath, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (output == INVALID_HANDLE_VALUE) return fail(L"Cannot rotate the NEO startup log.", GetLastError());
    }
    SYSTEMTIME now;
    GetSystemTime(&now);
    char banner[160];
    int banner_len = _snprintf(banner, sizeof(banner),
        "\r\n=== NEO 145 GUI launcher UTC %04u-%02u-%02u %02u:%02u:%02u ===\r\n",
        now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond);
    DWORD written;
    if (banner_len > 0 && banner_len < (int)sizeof(banner)) WriteFile(output, banner, (DWORD)banner_len, &written, NULL);
    HANDLE input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (input == INVALID_HANDLE_VALUE) { CloseHandle(output); return fail(L"Cannot initialize hidden startup.", GetLastError()); }
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_HIDE;
    startup.hStdInput = input;
    startup.hStdOutput = output;
    startup.hStdError = output;
    BOOL started = CreateProcessW(shell, command, NULL, NULL, TRUE, CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, NULL, root, &startup, &process);
    DWORD error = GetLastError();
    CloseHandle(input);
    CloseHandle(output);
    if (!started) return fail(L"Could not start the NEO bootstrap.", error);
    CloseHandle(process.hThread);
    /* Keep setup detached from CMD, but do not leave a slow first install
       without feedback. A static native status window adds no render timer. */
    ULONGLONG started_at = GetTickCount64();
    HWND status = NULL;
    for (;;) {
        DWORD waited = MsgWaitForMultipleObjects(1, &process.hProcess, FALSE, 250, QS_ALLINPUT);
        if (waited == WAIT_OBJECT_0) break;
        if (waited == WAIT_FAILED) {
            DWORD wait_error = GetLastError();
            CloseHandle(process.hProcess);
            if (status && IsWindow(status)) DestroyWindow(status);
            return fail(L"Could not observe NEO startup. The background process was left running.", wait_error);
        }
        if (!status && GetTickCount64() - started_at >= 4000) status = show_startup_status(instance);
        MSG message;
        while (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    if (status && IsWindow(status)) DestroyWindow(status);
    DWORD exit_code = 1;
    GetExitCodeProcess(process.hProcess, &exit_code);
    CloseHandle(process.hProcess);
    if (exit_code) return fail(L"NEO did not confirm successful startup. Details are in the startup log.", exit_code);
    return 0;
}
