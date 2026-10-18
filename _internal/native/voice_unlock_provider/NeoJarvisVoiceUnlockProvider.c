// NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
/*
 * NEO JARVIS Voice Unlock Credential Provider (x64)
 * HOTFIX47
 *
 * Minimal event-driven Windows Credential Provider used only for
 * CPUS_UNLOCK_WORKSTATION.  It is intentionally built without a CRT so the
 * DLL has a very small dependency surface when loaded by LogonUI.
 *
 * Design notes:
 * - The normal JARVIS process validates the configured account password first.
 * - When the exact code phrase is heard, JARVIS writes a short-lived request
 *   to ProgramData.  The password payload is protected by Windows DPAPI with
 *   CRYPTPROTECT_LOCAL_MACHINE; plaintext is never written to disk.
 * - This provider watches that request and calls CredentialsChanged so LogonUI
 *   re-enumerates it.  GetCredentialCount then requests auto-logon with the
 *   default credential.
 * - GetSerialization decrypts the one-time request only inside LogonUI,
 *   protects the password with CredProtectW, packs a
 *   KERB_INTERACTIVE_UNLOCK_LOGON with KerbWorkstationUnlockLogon, and hands
 *   it back to Winlogon/LSA.
 * - Secrets are zeroed promptly and the one-time request is deleted.
 *
 * This implementation follows the public Microsoft Credential Provider sample
 * flow, but is purpose-built for NEO JARVIS and has no visible input fields.
 */

#pragma clang diagnostic ignored "-Wmicrosoft-cast"

#define WIN32_LEAN_AND_MEAN 1
#define WINAPI __stdcall
#define STDMETHODCALLTYPE __stdcall
#define STDAPICALLTYPE __stdcall
#define EXTERN_C extern

/* Basic Win32 types (kept local so the binary can be cross-built without a SDK). */
typedef unsigned char BYTE;
typedef unsigned short WORD;
typedef unsigned short USHORT;
typedef unsigned int UINT;
typedef unsigned long DWORD;
typedef unsigned long ULONG;
typedef long LONG;
typedef long HRESULT;
typedef int BOOL;
typedef unsigned long long ULONGLONG;
typedef long long LONGLONG;
typedef void *HANDLE;
typedef void *HINSTANCE;
typedef void *HMODULE;
typedef void *HWND;
typedef void *HBITMAP;
typedef void *LPVOID;
typedef const void *LPCVOID;
typedef unsigned short WCHAR;
typedef WCHAR *LPWSTR;
typedef const WCHAR *LPCWSTR;
typedef char CHAR;
typedef CHAR *PCHAR;
typedef const CHAR *PCSTR;
typedef __SIZE_TYPE__ SIZE_T;
typedef __UINTPTR_TYPE__ UINT_PTR;
typedef __UINTPTR_TYPE__ ULONG_PTR;
typedef __UINTPTR_TYPE__ uintptr_t;
typedef LONG NTSTATUS;

typedef struct _GUID {
    ULONG Data1;
    USHORT Data2;
    USHORT Data3;
    BYTE Data4[8];
} GUID;
typedef const GUID *REFIID;
typedef const GUID *REFCLSID;

typedef struct _FILETIME { DWORD dwLowDateTime; DWORD dwHighDateTime; } FILETIME;
typedef struct _DATA_BLOB { DWORD cbData; BYTE *pbData; } DATA_BLOB;
typedef struct _LSA_STRING { USHORT Length; USHORT MaximumLength; PCHAR Buffer; } LSA_STRING;
typedef struct _LUID { DWORD LowPart; LONG HighPart; } LUID;
typedef struct _UNICODE_STRING { USHORT Length; USHORT MaximumLength; LPWSTR Buffer; } UNICODE_STRING;

_Static_assert(sizeof(WCHAR) == 2, "Windows provider requires 16-bit wchar_t");
_Static_assert(sizeof(GUID) == 16, "GUID ABI mismatch");

/* HRESULT / Win32 constants. */
#define S_OK ((HRESULT)0x00000000L)
#define S_FALSE ((HRESULT)0x00000001L)
#define E_NOTIMPL ((HRESULT)0x80004001L)
#define E_NOINTERFACE ((HRESULT)0x80004002L)
#define E_POINTER ((HRESULT)0x80004003L)
#define E_FAIL ((HRESULT)0x80004005L)
#define E_UNEXPECTED ((HRESULT)0x8000FFFFL)
#define E_INVALIDARG ((HRESULT)0x80070057L)
#define E_OUTOFMEMORY ((HRESULT)0x8007000EL)
#define CLASS_E_NOAGGREGATION ((HRESULT)0x80040110L)
#define CLASS_E_CLASSNOTAVAILABLE ((HRESULT)0x80040111L)

#define TRUE 1
#define FALSE 0
#define CPUS_INVALID 0
#define CPUS_LOGON 1
#define CPUS_UNLOCK_WORKSTATION 2
#define CPUS_CHANGE_PASSWORD 3
#define CPUS_CREDUI 4
#define CPUS_PLAP 5
#define CREDENTIAL_PROVIDER_NO_DEFAULT 0xFFFFFFFFUL
#define CPGSR_NO_CREDENTIAL_NOT_FINISHED 0
#define CPGSR_RETURN_CREDENTIAL_FINISHED 2
#define CPSI_NONE 0
#define CPFT_LARGE_TEXT 1
#define CPFS_HIDDEN 0
#define CPFIS_NONE 0
#define KerbInteractiveLogon 2
#define KerbWorkstationUnlockLogon 7

#define GENERIC_READ 0x80000000UL
#define FILE_SHARE_READ 0x00000001UL
#define FILE_SHARE_WRITE 0x00000002UL
#define FILE_SHARE_DELETE 0x00000004UL
#define OPEN_EXISTING 3UL
#define FILE_ATTRIBUTE_NORMAL 0x00000080UL
#define INVALID_FILE_ATTRIBUTES 0xFFFFFFFFUL
#define INVALID_HANDLE_VALUE ((HANDLE)(uintptr_t)(-1))
#define WAIT_OBJECT_0 0x00000000UL
#define INFINITE 0xFFFFFFFFUL
#define ERROR_INSUFFICIENT_BUFFER 122UL
#define MAX_REQUEST_BYTES (64UL * 1024UL)
#define REQUEST_VERSION 47UL
#define MAX_DOMAIN_CHARS 256UL
#define MAX_USER_CHARS 512UL
#define MAX_PASSWORD_BLOB_BYTES (32UL * 1024UL)

/* COM / Credential Provider declarations. */
typedef struct IUnknown IUnknown;
typedef struct IClassFactory IClassFactory;
typedef struct ICredentialProvider ICredentialProvider;
typedef struct ICredentialProviderCredential ICredentialProviderCredential;
typedef struct ICredentialProviderEvents ICredentialProviderEvents;
typedef struct ICredentialProviderCredentialEvents ICredentialProviderCredentialEvents;

typedef struct _CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION {
    ULONG ulAuthenticationPackage;
    GUID clsidCredentialProvider;
    ULONG cbSerialization;
    BYTE *rgbSerialization;
} CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION;

typedef struct _CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR {
    DWORD dwFieldID;
    int cpft;
    LPWSTR pszLabel;
    GUID guidFieldType;
} CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR;

typedef struct ICredentialProviderEventsVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(ICredentialProviderEvents*, REFIID, void**);
    ULONG (STDMETHODCALLTYPE *AddRef)(ICredentialProviderEvents*);
    ULONG (STDMETHODCALLTYPE *Release)(ICredentialProviderEvents*);
    HRESULT (STDMETHODCALLTYPE *CredentialsChanged)(ICredentialProviderEvents*, UINT_PTR);
} ICredentialProviderEventsVtbl;
struct ICredentialProviderEvents { ICredentialProviderEventsVtbl *lpVtbl; };

typedef struct ICredentialProviderVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(ICredentialProvider*, REFIID, void**);
    ULONG (STDMETHODCALLTYPE *AddRef)(ICredentialProvider*);
    ULONG (STDMETHODCALLTYPE *Release)(ICredentialProvider*);
    HRESULT (STDMETHODCALLTYPE *SetUsageScenario)(ICredentialProvider*, int, DWORD);
    HRESULT (STDMETHODCALLTYPE *SetSerialization)(ICredentialProvider*, const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*);
    HRESULT (STDMETHODCALLTYPE *Advise)(ICredentialProvider*, ICredentialProviderEvents*, UINT_PTR);
    HRESULT (STDMETHODCALLTYPE *UnAdvise)(ICredentialProvider*);
    HRESULT (STDMETHODCALLTYPE *GetFieldDescriptorCount)(ICredentialProvider*, DWORD*);
    HRESULT (STDMETHODCALLTYPE *GetFieldDescriptorAt)(ICredentialProvider*, DWORD, CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR**);
    HRESULT (STDMETHODCALLTYPE *GetCredentialCount)(ICredentialProvider*, DWORD*, DWORD*, BOOL*);
    HRESULT (STDMETHODCALLTYPE *GetCredentialAt)(ICredentialProvider*, DWORD, ICredentialProviderCredential**);
} ICredentialProviderVtbl;
struct ICredentialProvider { ICredentialProviderVtbl *lpVtbl; };

typedef struct ICredentialProviderCredentialVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(ICredentialProviderCredential*, REFIID, void**);
    ULONG (STDMETHODCALLTYPE *AddRef)(ICredentialProviderCredential*);
    ULONG (STDMETHODCALLTYPE *Release)(ICredentialProviderCredential*);
    HRESULT (STDMETHODCALLTYPE *Advise)(ICredentialProviderCredential*, ICredentialProviderCredentialEvents*);
    HRESULT (STDMETHODCALLTYPE *UnAdvise)(ICredentialProviderCredential*);
    HRESULT (STDMETHODCALLTYPE *SetSelected)(ICredentialProviderCredential*, BOOL*);
    HRESULT (STDMETHODCALLTYPE *SetDeselected)(ICredentialProviderCredential*);
    HRESULT (STDMETHODCALLTYPE *GetFieldState)(ICredentialProviderCredential*, DWORD, int*, int*);
    HRESULT (STDMETHODCALLTYPE *GetStringValue)(ICredentialProviderCredential*, DWORD, LPWSTR*);
    HRESULT (STDMETHODCALLTYPE *GetBitmapValue)(ICredentialProviderCredential*, DWORD, HBITMAP*);
    HRESULT (STDMETHODCALLTYPE *GetCheckboxValue)(ICredentialProviderCredential*, DWORD, BOOL*, LPWSTR*);
    HRESULT (STDMETHODCALLTYPE *GetSubmitButtonValue)(ICredentialProviderCredential*, DWORD, DWORD*);
    HRESULT (STDMETHODCALLTYPE *GetComboBoxValueCount)(ICredentialProviderCredential*, DWORD, DWORD*, DWORD*);
    HRESULT (STDMETHODCALLTYPE *GetComboBoxValueAt)(ICredentialProviderCredential*, DWORD, DWORD, LPWSTR*);
    HRESULT (STDMETHODCALLTYPE *SetStringValue)(ICredentialProviderCredential*, DWORD, LPCWSTR);
    HRESULT (STDMETHODCALLTYPE *SetCheckboxValue)(ICredentialProviderCredential*, DWORD, BOOL);
    HRESULT (STDMETHODCALLTYPE *SetComboBoxSelectedValue)(ICredentialProviderCredential*, DWORD, DWORD);
    HRESULT (STDMETHODCALLTYPE *CommandLinkClicked)(ICredentialProviderCredential*, DWORD);
    HRESULT (STDMETHODCALLTYPE *GetSerialization)(ICredentialProviderCredential*, int*, CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*, LPWSTR*, int*);
    HRESULT (STDMETHODCALLTYPE *ReportResult)(ICredentialProviderCredential*, NTSTATUS, NTSTATUS, LPWSTR*, int*);
} ICredentialProviderCredentialVtbl;
struct ICredentialProviderCredential { ICredentialProviderCredentialVtbl *lpVtbl; };

typedef struct IClassFactoryVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(IClassFactory*, REFIID, void**);
    ULONG (STDMETHODCALLTYPE *AddRef)(IClassFactory*);
    ULONG (STDMETHODCALLTYPE *Release)(IClassFactory*);
    HRESULT (STDMETHODCALLTYPE *CreateInstance)(IClassFactory*, IUnknown*, REFIID, void**);
    HRESULT (STDMETHODCALLTYPE *LockServer)(IClassFactory*, BOOL);
} IClassFactoryVtbl;
struct IClassFactory { IClassFactoryVtbl *lpVtbl; };

/* Authentication structures. */
typedef struct _KERB_INTERACTIVE_LOGON {
    int MessageType;
    UNICODE_STRING LogonDomainName;
    UNICODE_STRING UserName;
    UNICODE_STRING Password;
} KERB_INTERACTIVE_LOGON;
typedef struct _KERB_INTERACTIVE_UNLOCK_LOGON {
    KERB_INTERACTIVE_LOGON Logon;
    LUID LogonId;
} KERB_INTERACTIVE_UNLOCK_LOGON;

/* One-time request. Kept byte-packed so Python can write it deterministically. */
#pragma pack(push, 1)
typedef struct _NEO_UNLOCK_REQUEST_HEADER {
    BYTE magic[8];              /* "NJUNLK47" */
    DWORD version;              /* 47 */
    ULONGLONG expires_unix;     /* request expiry */
    DWORD domain_chars;         /* UTF-16 code units, no terminator */
    DWORD user_chars;           /* UTF-16 code units, no terminator */
    DWORD password_blob_bytes;  /* CryptProtectData output */
} NEO_UNLOCK_REQUEST_HEADER;
#pragma pack(pop)

/* Win32 imports. */
__declspec(dllimport) HANDLE WINAPI CreateThread(void*, SIZE_T, DWORD (WINAPI *)(void*), void*, DWORD, DWORD*);
__declspec(dllimport) void WINAPI Sleep(DWORD);
__declspec(dllimport) DWORD WINAPI WaitForSingleObject(HANDLE, DWORD);
__declspec(dllimport) BOOL WINAPI CloseHandle(HANDLE);
__declspec(dllimport) DWORD WINAPI GetEnvironmentVariableW(LPCWSTR, LPWSTR, DWORD);
__declspec(dllimport) DWORD WINAPI GetFileAttributesW(LPCWSTR);
__declspec(dllimport) HANDLE WINAPI CreateFileW(LPCWSTR, DWORD, DWORD, void*, DWORD, DWORD, HANDLE);
__declspec(dllimport) DWORD WINAPI GetFileSize(HANDLE, DWORD*);
__declspec(dllimport) BOOL WINAPI ReadFile(HANDLE, LPVOID, DWORD, DWORD*, void*);
__declspec(dllimport) BOOL WINAPI DeleteFileW(LPCWSTR);
__declspec(dllimport) void WINAPI GetSystemTimeAsFileTime(FILETIME*);
__declspec(dllimport) HANDLE WINAPI LocalFree(HANDLE);
__declspec(dllimport) DWORD WINAPI GetLastError(void);

__declspec(dllimport) LPVOID WINAPI CoTaskMemAlloc(SIZE_T);
__declspec(dllimport) void WINAPI CoTaskMemFree(LPVOID);

__declspec(dllimport) BOOL WINAPI CryptUnprotectData(DATA_BLOB*, LPWSTR*, DATA_BLOB*, LPVOID, LPVOID, DWORD, DATA_BLOB*);
__declspec(dllimport) BOOL WINAPI CredProtectW(BOOL, LPWSTR, DWORD, LPWSTR, DWORD*, int*);
__declspec(dllimport) NTSTATUS WINAPI LsaConnectUntrusted(HANDLE*);
__declspec(dllimport) NTSTATUS WINAPI LsaLookupAuthenticationPackage(HANDLE, LSA_STRING*, ULONG*);
__declspec(dllimport) NTSTATUS WINAPI LsaDeregisterLogonProcess(HANDLE);

/* GUIDs. */
static const GUID IID_IUnknown_ = {0x00000000UL,0x0000,0x0000,{0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
static const GUID IID_IClassFactory_ = {0x00000001UL,0x0000,0x0000,{0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
static const GUID IID_ICredentialProvider_ = {0xD27C3481UL,0x5A1C,0x45B2,{0x8A,0xAA,0xC2,0x0E,0xBB,0xE8,0x22,0x9E}};
static const GUID IID_ICredentialProviderCredential_ = {0x63913A93UL,0x40C1,0x481A,{0x81,0x8D,0x40,0x72,0xFF,0x8C,0x70,0xCC}};
/* {7A0B07ED-99E8-4B79-A61E-E5B4EA1FB347} */
static const GUID CLSID_NeoJarvisVoiceUnlock_ = {0x7A0B07EDUL,0x99E8,0x4B79,{0xA6,0x1E,0xE5,0xB4,0xEA,0x1F,0xB3,0x47}};
static const GUID GUID_NULL_ = {0,0,0,{0,0,0,0,0,0,0,0}};

static BOOL guid_equal(REFIID a, REFIID b) {
    const BYTE *pa = (const BYTE*)a;
    const BYTE *pb = (const BYTE*)b;
    UINT i;
    if (!a || !b) return FALSE;
    for (i = 0; i < 16; ++i) if (pa[i] != pb[i]) return FALSE;
    return TRUE;
}

static SIZE_T wlen(LPCWSTR s) {
    SIZE_T n = 0;
    if (!s) return 0;
    while (s[n]) ++n;
    return n;
}

static void zero_secure(void *p, SIZE_T n) {
    volatile BYTE *v = (volatile BYTE*)p;
    while (p && n--) *v++ = 0;
}

static void mem_copy(void *dst, const void *src, SIZE_T n) {
    BYTE *d = (BYTE*)dst;
    const BYTE *s = (const BYTE*)src;
    while (n--) *d++ = *s++;
}

static BOOL append_w(LPWSTR dst, DWORD cap, LPCWSTR src) {
    DWORD d = 0, s = 0;
    if (!dst || !src || cap == 0) return FALSE;
    while (d < cap && dst[d]) ++d;
    if (d >= cap) return FALSE;
    while (src[s]) {
        if (d + 1 >= cap) return FALSE;
        dst[d++] = src[s++];
    }
    dst[d] = 0;
    return TRUE;
}

static BOOL request_path(LPWSTR out, DWORD cap) {
    DWORD n;
    static const WCHAR envName[] = {'P','r','o','g','r','a','m','D','a','t','a',0};
    static const WCHAR suffix[] = {'\\','N','e','o','J','a','r','v','i','s','\\','v','o','i','c','e','_','u','n','l','o','c','k','\\','r','e','q','u','e','s','t','.','b','i','n',0};
    if (!out || cap < 64) return FALSE;
    out[0] = 0;
    n = GetEnvironmentVariableW(envName, out, cap);
    if (n == 0 || n >= cap) return FALSE;
    return append_w(out, cap, suffix);
}

static ULONGLONG unix_now(void) {
    FILETIME ft;
    ULONGLONG ticks;
    GetSystemTimeAsFileTime(&ft);
    ticks = ((ULONGLONG)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
    if (ticks < 116444736000000000ULL) return 0;
    return (ticks - 116444736000000000ULL) / 10000000ULL;
}

static BOOL request_header_valid(const NEO_UNLOCK_REQUEST_HEADER *h, DWORD size) {
    static const BYTE magic[8] = {'N','J','U','N','L','K','4','7'};
    DWORD i;
    ULONGLONG expected;
    if (!h || size < (DWORD)sizeof(*h)) return FALSE;
    for (i = 0; i < 8; ++i) if (h->magic[i] != magic[i]) return FALSE;
    if (h->version != REQUEST_VERSION) return FALSE;
    if (!h->expires_unix || h->expires_unix < unix_now() || h->expires_unix > unix_now() + 30ULL) return FALSE;
    if (!h->domain_chars || h->domain_chars > MAX_DOMAIN_CHARS) return FALSE;
    if (!h->user_chars || h->user_chars > MAX_USER_CHARS) return FALSE;
    if (!h->password_blob_bytes || h->password_blob_bytes > MAX_PASSWORD_BLOB_BYTES) return FALSE;
    expected = (ULONGLONG)sizeof(*h) + ((ULONGLONG)h->domain_chars + h->user_chars) * 2ULL + h->password_blob_bytes;
    return expected == size && expected <= MAX_REQUEST_BYTES;
}

static BOOL read_request(BYTE **outBuffer, DWORD *outSize) {
    WCHAR path[512];
    HANDLE h;
    DWORD size, got = 0;
    BYTE *buf;
    if (outBuffer) *outBuffer = 0;
    if (outSize) *outSize = 0;
    if (!outBuffer || !outSize || !request_path(path, 512)) return FALSE;
    h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, 0, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) return FALSE;
    size = GetFileSize(h, 0);
    if (!size || size == 0xFFFFFFFFUL || size > MAX_REQUEST_BYTES) {
        CloseHandle(h);
        return FALSE;
    }
    buf = (BYTE*)CoTaskMemAlloc(size);
    if (!buf) { CloseHandle(h); return FALSE; }
    if (!ReadFile(h, buf, size, &got, 0) || got != size) {
        CoTaskMemFree(buf); CloseHandle(h); return FALSE;
    }
    CloseHandle(h);
    if (!request_header_valid((const NEO_UNLOCK_REQUEST_HEADER*)buf, size)) {
        CoTaskMemFree(buf);
        return FALSE;
    }
    *outBuffer = buf;
    *outSize = size;
    return TRUE;
}

static BOOL request_ready(void) {
    BYTE *buf = 0;
    DWORD size = 0;
    BOOL ok = read_request(&buf, &size);
    if (buf) CoTaskMemFree(buf);
    return ok;
}

static void delete_request(void) {
    WCHAR path[512];
    if (request_path(path, 512)) DeleteFileW(path);
}

static LPWSTR duplicate_request_string(const BYTE *src, DWORD chars) {
    LPWSTR out;
    if (!src || !chars) return 0;
    out = (LPWSTR)CoTaskMemAlloc(((SIZE_T)chars + 1) * sizeof(WCHAR));
    if (!out) return 0;
    mem_copy(out, src, (SIZE_T)chars * sizeof(WCHAR));
    out[chars] = 0;
    return out;
}

static HRESULT retrieve_negotiate_auth_package(ULONG *package) {
    HANDLE lsa = 0;
    LSA_STRING name;
    static CHAR negotiate[] = "Negotiate";
    NTSTATUS st;
    if (!package) return E_POINTER;
    st = LsaConnectUntrusted(&lsa);
    if (st != 0 || !lsa) return E_FAIL;
    name.Buffer = negotiate;
    name.Length = 9;
    name.MaximumLength = 10;
    st = LsaLookupAuthenticationPackage(lsa, &name, package);
    LsaDeregisterLogonProcess(lsa);
    return st == 0 ? S_OK : E_FAIL;
}

static HRESULT protect_password(LPWSTR plain, LPWSTR *protectedOut) {
    DWORD need = 0;
    int protectionType = 0;
    LPWSTR out;
    DWORD chars;
    if (!plain || !protectedOut) return E_POINTER;
    *protectedOut = 0;
    chars = (DWORD)wlen(plain) + 1;
    (void)CredProtectW(FALSE, plain, chars, 0, &need, &protectionType);
    if (!need) return E_FAIL;
    out = (LPWSTR)CoTaskMemAlloc((SIZE_T)need * sizeof(WCHAR));
    if (!out) return E_OUTOFMEMORY;
    if (!CredProtectW(FALSE, plain, chars, out, &need, &protectionType)) {
        zero_secure(out, (SIZE_T)need * sizeof(WCHAR));
        CoTaskMemFree(out);
        return E_FAIL;
    }
    *protectedOut = out;
    return S_OK;
}

static void pack_unicode(const UNICODE_STRING *src, BYTE **cursor, BYTE *base, UNICODE_STRING *dst) {
    dst->Length = src->Length;
    dst->MaximumLength = src->Length;
    dst->Buffer = (LPWSTR)(uintptr_t)(*cursor - base);
    if (src->Length) mem_copy(*cursor, src->Buffer, src->Length);
    *cursor += src->Length;
}

static HRESULT build_kerb_unlock_serialization(LPCWSTR domain, LPCWSTR user, LPCWSTR protectedPassword, int messageType, BYTE **out, ULONG *outBytes) {
    SIZE_T dchars, uchars, pchars;
    DWORD dbytes, ubytes, pbytes;
    SIZE_T total;
    KERB_INTERACTIVE_UNLOCK_LOGON *k;
    BYTE *cursor, *base;
    UNICODE_STRING d, u, p;
    if (!domain || !user || !protectedPassword || !out || !outBytes) return E_POINTER;
    *out = 0; *outBytes = 0;
    dchars = wlen(domain); uchars = wlen(user); pchars = wlen(protectedPassword);
    if (dchars > 32767 || uchars > 32767 || pchars > 32767) return E_INVALIDARG;
    dbytes = (DWORD)(dchars * sizeof(WCHAR));
    ubytes = (DWORD)(uchars * sizeof(WCHAR));
    pbytes = (DWORD)(pchars * sizeof(WCHAR));
    total = sizeof(*k) + dbytes + ubytes + pbytes;
    if (total > 0xFFFFFFFFUL) return E_INVALIDARG;
    k = (KERB_INTERACTIVE_UNLOCK_LOGON*)CoTaskMemAlloc(total);
    if (!k) return E_OUTOFMEMORY;
    zero_secure(k, sizeof(*k));
    base = (BYTE*)k;
    cursor = base + sizeof(*k);
    k->Logon.MessageType = messageType;
    d.Length = (USHORT)dbytes; d.MaximumLength = (USHORT)dbytes; d.Buffer = (LPWSTR)domain;
    u.Length = (USHORT)ubytes; u.MaximumLength = (USHORT)ubytes; u.Buffer = (LPWSTR)user;
    p.Length = (USHORT)pbytes; p.MaximumLength = (USHORT)pbytes; p.Buffer = (LPWSTR)protectedPassword;
    pack_unicode(&d, &cursor, base, &k->Logon.LogonDomainName);
    pack_unicode(&u, &cursor, base, &k->Logon.UserName);
    pack_unicode(&p, &cursor, base, &k->Logon.Password);
    k->LogonId.LowPart = 0;
    k->LogonId.HighPart = 0;
    *out = (BYTE*)k;
    *outBytes = (ULONG)total;
    return S_OK;
}

/* Provider object. */
typedef struct _Provider {
    ICredentialProvider iface;
    volatile LONG refs;
    int usage;
    ICredentialProviderEvents *events;
    UINT_PTR adviseContext;
    HANDLE watcher;
    volatile LONG stopWatcher;
} Provider;

typedef struct _Credential {
    ICredentialProviderCredential iface;
    volatile LONG refs;
    int usage;
} Credential;

typedef struct _ClassFactory { IClassFactory iface; } ClassFactory;

static ULONG provider_addref(ICredentialProvider *iface);
static ULONG provider_release(ICredentialProvider *iface);

static LONG atomic_inc(volatile LONG *p) { return __atomic_add_fetch(p, 1, __ATOMIC_SEQ_CST); }
static LONG atomic_dec(volatile LONG *p) { return __atomic_sub_fetch(p, 1, __ATOMIC_SEQ_CST); }

static HRESULT STDMETHODCALLTYPE provider_qi(ICredentialProvider *iface, REFIID iid, void **out) {
    if (!out) return E_POINTER;
    *out = 0;
    if (guid_equal(iid, &IID_IUnknown_) || guid_equal(iid, &IID_ICredentialProvider_)) {
        *out = iface; provider_addref(iface); return S_OK;
    }
    return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE provider_addref(ICredentialProvider *iface) {
    Provider *p = (Provider*)iface;
    return (ULONG)atomic_inc(&p->refs);
}
static ULONG STDMETHODCALLTYPE provider_release(ICredentialProvider *iface) {
    Provider *p = (Provider*)iface;
    LONG r = atomic_dec(&p->refs);
    if (r == 0) {
        if (p->events) p->events->lpVtbl->Release(p->events);
        if (p->watcher) CloseHandle(p->watcher);
        zero_secure(p, sizeof(*p));
        CoTaskMemFree(p);
    }
    return (ULONG)(r < 0 ? 0 : r);
}
static HRESULT STDMETHODCALLTYPE provider_set_usage(ICredentialProvider *iface, int cpus, DWORD flags) {
    Provider *p = (Provider*)iface;
    (void)flags;
    /* Windows 10+ commonly uses CPUS_LOGON for both logon and unlock, while
       policy/remote scenarios may still send CPUS_UNLOCK_WORKSTATION. */
    if (cpus != CPUS_LOGON && cpus != CPUS_UNLOCK_WORKSTATION) return E_NOTIMPL;
    p->usage = cpus;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE provider_set_serialization(ICredentialProvider *iface, const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION *s) {
    (void)iface; (void)s; return E_NOTIMPL;
}

static DWORD WINAPI provider_watch_thread(void *ctx) {
    Provider *p = (Provider*)ctx;
    BOOL wasReady = FALSE;
    while (!p->stopWatcher) {
        BOOL ready = request_ready();
        if (ready && !wasReady && p->events) {
            p->events->lpVtbl->CredentialsChanged(p->events, p->adviseContext);
        }
        wasReady = ready;
        Sleep(90);
    }
    provider_release(&p->iface);
    return 0;
}

static HRESULT STDMETHODCALLTYPE provider_advise(ICredentialProvider *iface, ICredentialProviderEvents *events, UINT_PTR context) {
    Provider *p = (Provider*)iface;
    if (!events) return E_POINTER;
    if (p->events) p->events->lpVtbl->Release(p->events);
    p->events = events;
    p->events->lpVtbl->AddRef(p->events);
    p->adviseContext = context;
    p->stopWatcher = 0;
    if (!p->watcher) {
        DWORD tid = 0;
        provider_addref(iface);
        p->watcher = CreateThread(0, 0, provider_watch_thread, p, 0, &tid);
        if (!p->watcher) {
            provider_release(iface);
            return E_FAIL;
        }
    }
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE provider_unadvise(ICredentialProvider *iface) {
    Provider *p = (Provider*)iface;
    p->stopWatcher = 1;
    if (p->watcher) {
        WaitForSingleObject(p->watcher, 1500);
        CloseHandle(p->watcher);
        p->watcher = 0;
    }
    if (p->events) {
        p->events->lpVtbl->Release(p->events);
        p->events = 0;
    }
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE provider_field_count(ICredentialProvider *iface, DWORD *count) {
    (void)iface; if (!count) return E_POINTER; *count = 1; return S_OK;
}
static HRESULT STDMETHODCALLTYPE provider_field_at(ICredentialProvider *iface, DWORD index, CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR **out) {
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR *d;
    static const WCHAR label[] = {'N','E','O',' ','J','A','R','V','I','S',' ','V','o','i','c','e',' ','U','n','l','o','c','k',0};
    SIZE_T bytes = sizeof(label);
    (void)iface;
    if (!out) return E_POINTER;
    *out = 0;
    if (index != 0) return E_INVALIDARG;
    d = (CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR*)CoTaskMemAlloc(sizeof(*d));
    if (!d) return E_OUTOFMEMORY;
    d->dwFieldID = 0;
    d->cpft = CPFT_LARGE_TEXT;
    d->guidFieldType = GUID_NULL_;
    d->pszLabel = (LPWSTR)CoTaskMemAlloc(bytes);
    if (!d->pszLabel) { CoTaskMemFree(d); return E_OUTOFMEMORY; }
    mem_copy(d->pszLabel, label, bytes);
    *out = d;
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE provider_credential_count(ICredentialProvider *iface, DWORD *count, DWORD *def, BOOL *autoLogon) {
    Provider *p = (Provider*)iface;
    BOOL ready;
    if (!count || !def || !autoLogon) return E_POINTER;
    ready = (p->usage == CPUS_LOGON || p->usage == CPUS_UNLOCK_WORKSTATION) && request_ready();
    if (ready) {
        *count = 1; *def = 0; *autoLogon = TRUE;
    } else {
        *count = 0; *def = CREDENTIAL_PROVIDER_NO_DEFAULT; *autoLogon = FALSE;
    }
    return S_OK;
}

/* Credential methods. */
static ULONG STDMETHODCALLTYPE cred_addref(ICredentialProviderCredential *iface);
static ULONG STDMETHODCALLTYPE cred_release(ICredentialProviderCredential *iface);
static HRESULT STDMETHODCALLTYPE cred_qi(ICredentialProviderCredential *iface, REFIID iid, void **out) {
    if (!out) return E_POINTER;
    *out = 0;
    if (guid_equal(iid, &IID_IUnknown_) || guid_equal(iid, &IID_ICredentialProviderCredential_)) {
        *out = iface; cred_addref(iface); return S_OK;
    }
    return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE cred_addref(ICredentialProviderCredential *iface) {
    Credential *c = (Credential*)iface; return (ULONG)atomic_inc(&c->refs);
}
static ULONG STDMETHODCALLTYPE cred_release(ICredentialProviderCredential *iface) {
    Credential *c = (Credential*)iface; LONG r = atomic_dec(&c->refs);
    if (r == 0) { zero_secure(c, sizeof(*c)); CoTaskMemFree(c); }
    return (ULONG)(r < 0 ? 0 : r);
}
static HRESULT STDMETHODCALLTYPE cred_advise(ICredentialProviderCredential *c, ICredentialProviderCredentialEvents *e) { (void)c; (void)e; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE cred_unadvise(ICredentialProviderCredential *c) { (void)c; return S_OK; }
static HRESULT STDMETHODCALLTYPE cred_selected(ICredentialProviderCredential *c, BOOL *autoLogon) { (void)c; if (!autoLogon) return E_POINTER; *autoLogon = request_ready(); return S_OK; }
static HRESULT STDMETHODCALLTYPE cred_deselected(ICredentialProviderCredential *c) { (void)c; return S_OK; }
static HRESULT STDMETHODCALLTYPE cred_field_state(ICredentialProviderCredential *c, DWORD id, int *state, int *interactive) {
    (void)c; if (!state || !interactive) return E_POINTER; if (id != 0) return E_INVALIDARG; *state = CPFS_HIDDEN; *interactive = CPFIS_NONE; return S_OK;
}
static HRESULT STDMETHODCALLTYPE cred_string(ICredentialProviderCredential *c, DWORD id, LPWSTR *out) {
    LPWSTR s; (void)c; if (!out) return E_POINTER; *out = 0; if (id != 0) return E_INVALIDARG;
    s = (LPWSTR)CoTaskMemAlloc(sizeof(WCHAR)); if (!s) return E_OUTOFMEMORY; s[0] = 0; *out = s; return S_OK;
}
static HRESULT STDMETHODCALLTYPE cred_bitmap(ICredentialProviderCredential *c, DWORD id, HBITMAP *out) { (void)c;(void)id;if(out)*out=0;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_checkbox(ICredentialProviderCredential *c, DWORD id, BOOL *checked, LPWSTR *label) { (void)c;(void)id;if(checked)*checked=FALSE;if(label)*label=0;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_submit(ICredentialProviderCredential *c, DWORD id, DWORD *adj) { (void)c;(void)id;if(adj)*adj=0;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_combo_count(ICredentialProviderCredential *c, DWORD id, DWORD *n, DWORD *sel) { (void)c;(void)id;if(n)*n=0;if(sel)*sel=0;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_combo_at(ICredentialProviderCredential *c, DWORD id, DWORD item, LPWSTR *out) { (void)c;(void)id;(void)item;if(out)*out=0;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_set_string(ICredentialProviderCredential *c, DWORD id, LPCWSTR s) { (void)c;(void)id;(void)s;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_set_checkbox(ICredentialProviderCredential *c, DWORD id, BOOL b) { (void)c;(void)id;(void)b;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_set_combo(ICredentialProviderCredential *c, DWORD id, DWORD item) { (void)c;(void)id;(void)item;return E_INVALIDARG; }
static HRESULT STDMETHODCALLTYPE cred_command(ICredentialProviderCredential *c, DWORD id) { (void)c;(void)id;return E_INVALIDARG; }

static HRESULT STDMETHODCALLTYPE cred_serialization(ICredentialProviderCredential *iface, int *response, CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION *ser, LPWSTR *statusText, int *statusIcon) {
    BYTE *request = 0;
    DWORD requestBytes = 0;
    NEO_UNLOCK_REQUEST_HEADER *h;
    BYTE *cursor;
    LPWSTR domain = 0, user = 0, protectedPassword = 0;
    DATA_BLOB inBlob = {0, 0}, outBlob = {0, 0};
    LPWSTR description = 0;
    HRESULT hr = E_FAIL;
    BYTE *packed = 0;
    ULONG packedBytes = 0, package = 0;
    (void)iface;
    if (!response || !ser || !statusText || !statusIcon) return E_POINTER;
    *response = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    *statusText = 0;
    *statusIcon = CPSI_NONE;
    ser->ulAuthenticationPackage = 0;
    ser->clsidCredentialProvider = GUID_NULL_;
    ser->cbSerialization = 0;
    ser->rgbSerialization = 0;

    if (!read_request(&request, &requestBytes)) return E_FAIL;
    h = (NEO_UNLOCK_REQUEST_HEADER*)request;
    cursor = request + sizeof(*h);
    domain = duplicate_request_string(cursor, h->domain_chars);
    cursor += h->domain_chars * 2UL;
    user = duplicate_request_string(cursor, h->user_chars);
    cursor += h->user_chars * 2UL;
    if (!domain || !user) { hr = E_OUTOFMEMORY; goto cleanup; }

    inBlob.cbData = h->password_blob_bytes;
    inBlob.pbData = cursor;
    outBlob.cbData = 0; outBlob.pbData = 0;
    if (!CryptUnprotectData(&inBlob, &description, 0, 0, 0, 0, &outBlob) || !outBlob.pbData || outBlob.cbData < 2) {
        hr = E_FAIL; goto cleanup;
    }
    /* Python stores a UTF-16LE password including the terminating NUL. */
    ((WCHAR*)outBlob.pbData)[(outBlob.cbData / 2UL) - 1UL] = 0;
    hr = protect_password((LPWSTR)outBlob.pbData, &protectedPassword);
    if (hr != S_OK) goto cleanup;
    hr = build_kerb_unlock_serialization(
        domain, user, protectedPassword,
        ((Credential*)iface)->usage == CPUS_UNLOCK_WORKSTATION ? KerbWorkstationUnlockLogon : KerbInteractiveLogon,
        &packed, &packedBytes
    );
    if (hr != S_OK) goto cleanup;
    hr = retrieve_negotiate_auth_package(&package);
    if (hr != S_OK) goto cleanup;

    ser->ulAuthenticationPackage = package;
    ser->clsidCredentialProvider = CLSID_NeoJarvisVoiceUnlock_;
    ser->cbSerialization = packedBytes;
    ser->rgbSerialization = packed;
    packed = 0; /* ownership passes to LogonUI */
    *response = CPGSR_RETURN_CREDENTIAL_FINISHED;
    hr = S_OK;

cleanup:
    delete_request();
    if (description) LocalFree(description);
    if (outBlob.pbData) {
        zero_secure(outBlob.pbData, outBlob.cbData);
        LocalFree(outBlob.pbData);
    }
    if (protectedPassword) {
        zero_secure(protectedPassword, (wlen(protectedPassword) + 1) * sizeof(WCHAR));
        CoTaskMemFree(protectedPassword);
    }
    if (domain) CoTaskMemFree(domain);
    if (user) CoTaskMemFree(user);
    if (packed) { zero_secure(packed, packedBytes); CoTaskMemFree(packed); }
    if (request) { zero_secure(request, requestBytes); CoTaskMemFree(request); }
    return hr;
}
static HRESULT STDMETHODCALLTYPE cred_report(ICredentialProviderCredential *c, NTSTATUS status, NTSTATUS sub, LPWSTR *text, int *icon) {
    (void)c;(void)status;(void)sub; if(text)*text=0;if(icon)*icon=CPSI_NONE; delete_request(); return S_OK;
}

static ICredentialProviderCredentialVtbl g_cred_vtbl = {
    cred_qi, cred_addref, cred_release, cred_advise, cred_unadvise, cred_selected, cred_deselected,
    cred_field_state, cred_string, cred_bitmap, cred_checkbox, cred_submit, cred_combo_count, cred_combo_at,
    cred_set_string, cred_set_checkbox, cred_set_combo, cred_command, cred_serialization, cred_report
};

static HRESULT STDMETHODCALLTYPE provider_credential_at(ICredentialProvider *iface, DWORD index, ICredentialProviderCredential **out) {
    Credential *c;
    (void)iface;
    if (!out) return E_POINTER;
    *out = 0;
    if (index != 0 || !request_ready()) return E_INVALIDARG;
    c = (Credential*)CoTaskMemAlloc(sizeof(*c));
    if (!c) return E_OUTOFMEMORY;
    c->iface.lpVtbl = &g_cred_vtbl;
    c->refs = 1;
    c->usage = ((Provider*)iface)->usage;
    *out = &c->iface;
    return S_OK;
}

static ICredentialProviderVtbl g_provider_vtbl = {
    provider_qi, provider_addref, provider_release, provider_set_usage, provider_set_serialization,
    provider_advise, provider_unadvise, provider_field_count, provider_field_at,
    provider_credential_count, provider_credential_at
};

static HRESULT create_provider(REFIID iid, void **out) {
    Provider *p;
    HRESULT hr;
    if (!out) return E_POINTER;
    *out = 0;
    p = (Provider*)CoTaskMemAlloc(sizeof(*p));
    if (!p) return E_OUTOFMEMORY;
    zero_secure(p, sizeof(*p));
    p->iface.lpVtbl = &g_provider_vtbl;
    p->refs = 1;
    p->usage = -1;
    hr = provider_qi(&p->iface, iid, out);
    provider_release(&p->iface);
    return hr;
}

/* Class factory. */
static HRESULT STDMETHODCALLTYPE factory_qi(IClassFactory *iface, REFIID iid, void **out) {
    if (!out) return E_POINTER;
    *out = 0;
    if (guid_equal(iid, &IID_IUnknown_) || guid_equal(iid, &IID_IClassFactory_)) { *out = iface; return S_OK; }
    return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE factory_addref(IClassFactory *iface) { (void)iface; return 2; }
static ULONG STDMETHODCALLTYPE factory_release(IClassFactory *iface) { (void)iface; return 1; }
static HRESULT STDMETHODCALLTYPE factory_create(IClassFactory *iface, IUnknown *outer, REFIID iid, void **out) {
    (void)iface;
    if (outer) return CLASS_E_NOAGGREGATION;
    return create_provider(iid, out);
}
static HRESULT STDMETHODCALLTYPE factory_lock(IClassFactory *iface, BOOL lock) { (void)iface;(void)lock;return S_OK; }
static IClassFactoryVtbl g_factory_vtbl = { factory_qi, factory_addref, factory_release, factory_create, factory_lock };
static ClassFactory g_factory = { { &g_factory_vtbl } };

__declspec(dllexport) HRESULT STDAPICALLTYPE DllGetClassObject(REFCLSID clsid, REFIID iid, void **out) {
    if (!out) return E_POINTER;
    *out = 0;
    if (!guid_equal(clsid, &CLSID_NeoJarvisVoiceUnlock_)) return CLASS_E_CLASSNOTAVAILABLE;
    return factory_qi(&g_factory.iface, iid, out);
}

__declspec(dllexport) HRESULT STDAPICALLTYPE DllCanUnloadNow(void) {
    /* LogonUI may keep the provider loaded for the whole lock-screen lifetime. */
    return S_FALSE;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)instance; (void)reason; (void)reserved;
    return TRUE;
}
