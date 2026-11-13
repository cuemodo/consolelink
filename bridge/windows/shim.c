/*
 * SmartSoft bridge -- a stand-in hid.dll for SmartSoft (32-bit, Windows) under CrossOver/Wine.
 *
 * SmartSoft for Windows doesn't use a USB library. It reaches the SmartFade ML through ETC's
 * kernel driver (etchsp.sys), with plain Win32 calls:
 *   - SetupDi on the driver's interface GUID {8D7140C7-BCAE-4BE2-A5C2-DFE48EBB759A} to find
 *     the device path;
 *   - CreateFileW on it, then DeviceIoControl 0x222000 / 0x222004 / 0x222008 to read the
 *     product ID / bcdDevice / product string, keeping the device whose product ID matches;
 *   - CreateFileA on the same path, then synchronous ReadFile / WriteFile, one call per bulk
 *     transfer on the vendor interface's IN / OUT endpoints.
 * At startup SmartSoft.exe also lists HID devices (HidD_GetHidGuid, SetupDi,
 * HidD_GetAttributes) to pick the console model: VID 0x14D5 / PID 0x0201 selects SmartFade
 * ML and loads its engine, sfmlEngine.dll, through QLibrary. Without a match it asks the user.
 *
 * CrossOver can't pass the USB device through, so this DLL fakes both devices at the Win32 API
 * level and hands the bulk transfers to a backend (bridge.h). It is loaded instead of Wine's
 * hid.dll through a DllOverride for SmartSoft.exe, exports the two HidD_ functions
 * SmartSoft.exe imports, and patches import tables:
 *   - SmartSoft.exe: SetupDi*, CreateFileW, CloseHandle (the HID listing);
 *   - QtCore4.dll: LoadLibraryW / LoadLibraryExW, to catch the engine DLL when it loads;
 *   - sfmlEngine.dll / sfEngine.dll: SetupDi*, CreateFileA/W, ReadFile, WriteFile,
 *     DeviceIoControl, CloseHandle.
 * Calls about anything other than the two fake devices go straight to the real functions.
 *
 * Environment (CrossOver passes the Mac environment through):
 *   SMARTSOFT_BRIDGE        backend: "tcp" (default; ConsoleLink on 127.0.0.1:47561), tcp:PORT,
 *                           tcp:HOST:PORT, or "stub" (built-in fake console, no hardware)
 *   SMARTSOFT_BRIDGE_LOG    log file (default: %TEMP%\smartsoft-bridge.log)
 *   SMARTSOFT_BRIDGE_TRACE  1: log every transfer in hex
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <setupapi.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include "bridge.h"

static const GUID GUID_ETC_HSP = {0x8d7140c7, 0xbcae, 0x4be2,
                                  {0xa5, 0xc2, 0xdf, 0xe4, 0x8e, 0xbb, 0x75, 0x9a}};
static const GUID GUID_HID_IF = {0x4d1e55b2, 0xf16f, 0x11cf,
                                 {0x88, 0xcb, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30}};

#define ETC_IOCTL_GET_PID     0x222000
#define ETC_IOCTL_GET_BCD     0x222004
#define ETC_IOCTL_GET_PRODUCT 0x222008
#define IOCTL_HID_GET_COLLECTION_INFORMATION 0x000b01ab

/* SmartSoft.exe's poll loop only checks how many bytes ReadFile returned: a short read is
   logged as a timeout and retried 20 ms later. */
#define READ_TIMEOUT_MS 500

#define ETC_PATH_LITERAL "\\\\?\\usb#vid_14d5&pid_0201&mi_01#consolelink#{8d7140c7-bcae-4be2-a5c2-dfe48ebb759a}"
#define HID_PATH_LITERAL "\\\\?\\hid#vid_14d5&pid_0201&mi_00#consolelink#{4d1e55b2-f16f-11cf-88cb-001111000030}"
#define WIDEN2(x) L##x
#define WIDEN(x) WIDEN2(x)
static const WCHAR ETC_PATH_W[] = WIDEN(ETC_PATH_LITERAL);
static const char ETC_PATH_A[] = ETC_PATH_LITERAL;
static const WCHAR HID_PATH_W[] = WIDEN(HID_PATH_LITERAL);
static const char HID_PATH_A[] = HID_PATH_LITERAL;

typedef struct {
    ULONG Size;
    USHORT VendorID;
    USHORT ProductID;
    USHORT VersionNumber;
} BRIDGE_HIDD_ATTRIBUTES;

typedef struct {
    ULONG DescriptorSize;
    BOOLEAN Polled;
    UCHAR Reserved1[1];
    USHORT VendorID;
    USHORT ProductID;
    USHORT VersionNumber;
} BRIDGE_HID_COLLECTION_INFORMATION;

/* ---------------------------------------------------------------- logging */

static CRITICAL_SECTION log_cs;
static FILE *log_f;
static int trace_io;

void bridge_log(const char *fmt, ...)
{
    if (!log_f)
        return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    EnterCriticalSection(&log_cs);
    fprintf(log_f, "%02u:%02u:%02u.%03u [%04lx] ", t.wHour, t.wMinute, t.wSecond,
            t.wMilliseconds, (unsigned long)GetCurrentThreadId());
    va_list ap;
    va_start(ap, fmt);
    vfprintf(log_f, fmt, ap);
    va_end(ap);
    fputc('\n', log_f);
    fflush(log_f);
    LeaveCriticalSection(&log_cs);
}

void bridge_log_hex(const char *tag, const void *buf, unsigned n)
{
    if (!trace_io || !log_f)
        return;
    const unsigned char *b = buf;
    char *s = malloc(n * 2 + 1);
    if (!s)
        return;
    for (unsigned i = 0; i < n; i++)
        sprintf(s + i * 2, "%02x", b[i]);
    s[n * 2] = 0;
    bridge_log("%s %4u %s", tag, n, s);
    free(s);
}

static void open_log(void)
{
    WCHAR path[MAX_PATH];
    DWORD len = GetEnvironmentVariableW(L"SMARTSOFT_BRIDGE_LOG", path, MAX_PATH);
    if (!len || len >= MAX_PATH) {
        len = GetTempPathW(MAX_PATH, path);
        if (!len || len + 24 >= MAX_PATH)
            return;
        wcscat(path, L"smartsoft-bridge.log");
    }
    log_f = _wfopen(path, L"a");
}

/* ---------------------------------------------------------------- backend */

static const struct bridge_backend *backend = &tcp_backend;

static void choose_backend(void)
{
    char spec[160];
    DWORD len = GetEnvironmentVariableA("SMARTSOFT_BRIDGE", spec, sizeof(spec));
    if (!len || len >= sizeof(spec))
        spec[0] = 0;
    if (!_stricmp(spec, "stub")) {
        backend = &stub_backend;
    } else if (!spec[0] || !_strnicmp(spec, "tcp", 3)) {
        backend = &tcp_backend;
        if (spec[0])
            tcp_backend_configure(spec + 3);
    } else {
        bridge_log("SMARTSOFT_BRIDGE='%s' not understood, using tcp", spec);
        backend = &tcp_backend;
    }
}

/* ---------------------------------------------------------------- fake devices */

enum fake_kind { FAKE_NONE, FAKE_ETC, FAKE_HID };

static const char *kind_name(enum fake_kind k)
{
    return k == FAKE_ETC ? "ETC" : k == FAKE_HID ? "HID" : "-";
}

/* SP_DEVICE_INTERFACE_DATA.Reserved of a fake entry points at one of these. */
static char magic_etc, magic_hid;

#define MAX_TRACKED 16

struct fake_set {          /* an HDEVINFO listing one of our GUIDs */
    HDEVINFO h;
    enum fake_kind kind;
    DWORD fake_count;      /* 1 if the fake device is listed first, else 0 */
};

struct fake_handle {       /* an opened fake device */
    HANDLE h;
    enum fake_kind kind;
    bridge_session *session;
    bridge_identity id;
    unsigned long reads, writes, timeouts;
    unsigned long failing;   /* transfers failed in a row (logged once, then summed up) */
};

static CRITICAL_SECTION fake_cs;
static struct fake_set sets[MAX_TRACKED];
static struct fake_handle handles[MAX_TRACKED];

static enum fake_kind kind_for_guid(const GUID *g)
{
    if (!g)
        return FAKE_NONE;
    if (IsEqualGUID(g, &GUID_ETC_HSP))
        return FAKE_ETC;
    if (IsEqualGUID(g, &GUID_HID_IF))
        return FAKE_HID;
    return FAKE_NONE;
}

static struct fake_set *find_set(HDEVINFO h)
{
    for (int i = 0; i < MAX_TRACKED; i++)
        if (sets[i].kind != FAKE_NONE && sets[i].h == h)
            return &sets[i];
    return NULL;
}

/* The entry stays valid until the handle is closed; SmartSoft uses each handle from one
   thread, so its counters are updated without the lock. */
static int lookup_handle(HANDLE h, struct fake_handle **out)
{
    int found = 0;
    EnterCriticalSection(&fake_cs);
    for (int i = 0; i < MAX_TRACKED; i++)
        if (handles[i].kind != FAKE_NONE && handles[i].h == h) {
            *out = &handles[i];
            found = 1;
            break;
        }
    LeaveCriticalSection(&fake_cs);
    return found;
}

static HANDLE open_fake(enum fake_kind kind, const char *via)
{
    bridge_identity id = {0x14d5, 0x0201, 0x0001, "ETC Smartfade"};
    bridge_session *s = NULL;
    if (kind == FAKE_ETC) {
        s = backend->open(&id);
        if (!s) {
            bridge_log("%s(%s): console not reachable", via, kind_name(kind));
            SetLastError(ERROR_DEVICE_NOT_CONNECTED);
            return INVALID_HANDLE_VALUE;
        }
    }
    /* A real, otherwise unused kernel handle: unique, and safe to CloseHandle. */
    HANDLE h = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!h) {
        if (s)
            backend->close(s);
        return INVALID_HANDLE_VALUE;
    }
    EnterCriticalSection(&fake_cs);
    int slot = -1;
    for (int i = 0; i < MAX_TRACKED; i++)
        if (handles[i].kind == FAKE_NONE) {
            slot = i;
            break;
        }
    if (slot >= 0) {
        memset(&handles[slot], 0, sizeof(handles[slot]));
        handles[slot].h = h;
        handles[slot].kind = kind;
        handles[slot].session = s;
        handles[slot].id = id;
    }
    LeaveCriticalSection(&fake_cs);
    if (slot < 0) {
        bridge_log("%s(%s): too many open handles", via, kind_name(kind));
        if (s)
            backend->close(s);
        CloseHandle(h);
        SetLastError(ERROR_TOO_MANY_OPEN_FILES);
        return INVALID_HANDLE_VALUE;
    }
    bridge_log("%s(%s) -> %p (%04x:%04x bcd %04x \"%s\")", via, kind_name(kind), h, id.vid,
               id.pid, id.bcd, id.product);
    return h;
}

/* ---------------------------------------------------------------- hooks: SetupDi */

static HDEVINFO WINAPI hook_SetupDiGetClassDevsW(const GUID *g, PCWSTR enumerator, HWND parent,
                                                 DWORD flags)
{
    HDEVINFO h = SetupDiGetClassDevsW(g, enumerator, parent, flags);
    enum fake_kind k = (flags & DIGCF_DEVICEINTERFACE) ? kind_for_guid(g) : FAKE_NONE;
    if (k == FAKE_NONE)
        return h;
    if (h == INVALID_HANDLE_VALUE)   /* no real device class registered: start from an empty set */
        h = SetupDiCreateDeviceInfoList(NULL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return h;
    DWORD fake_count = backend->present() ? 1 : 0;
    EnterCriticalSection(&fake_cs);
    for (int i = 0; i < MAX_TRACKED; i++)
        if (sets[i].kind == FAKE_NONE) {
            sets[i].h = h;
            sets[i].kind = k;
            sets[i].fake_count = fake_count;
            break;
        }
    LeaveCriticalSection(&fake_cs);
    bridge_log("SetupDiGetClassDevsW(%s, flags %#lx) -> %p, fake device %s", kind_name(k),
               (unsigned long)flags, h, fake_count ? "listed" : "absent (console not reachable)");
    return h;
}

static BOOL WINAPI hook_SetupDiEnumDeviceInterfaces(HDEVINFO h, PSP_DEVINFO_DATA dev,
                                                    const GUID *g, DWORD index,
                                                    PSP_DEVICE_INTERFACE_DATA data)
{
    EnterCriticalSection(&fake_cs);
    struct fake_set *set = find_set(h);
    enum fake_kind k = set ? set->kind : FAKE_NONE;
    DWORD fake_count = set ? set->fake_count : 0;
    LeaveCriticalSection(&fake_cs);

    if (k == FAKE_NONE || kind_for_guid(g) != k)
        return SetupDiEnumDeviceInterfaces(h, dev, g, index, data);
    if (index < fake_count) {
        if (!data || data->cbSize != sizeof(*data)) {
            SetLastError(ERROR_INVALID_USER_BUFFER);
            return FALSE;
        }
        data->InterfaceClassGuid = *g;
        data->Flags = SPINT_ACTIVE;
        data->Reserved = (ULONG_PTR)(k == FAKE_ETC ? &magic_etc : &magic_hid);
        bridge_log("SetupDiEnumDeviceInterfaces(%s, %lu) -> fake device", kind_name(k),
                   (unsigned long)index);
        return TRUE;
    }
    return SetupDiEnumDeviceInterfaces(h, dev, g, index - fake_count, data);
}

static BOOL WINAPI hook_SetupDiGetDeviceInterfaceDetailW(HDEVINFO h, PSP_DEVICE_INTERFACE_DATA data,
                                                         PSP_DEVICE_INTERFACE_DETAIL_DATA_W detail,
                                                         DWORD size, PDWORD required,
                                                         PSP_DEVINFO_DATA dev)
{
    const WCHAR *path = NULL;
    if (data && data->Reserved == (ULONG_PTR)&magic_etc)
        path = ETC_PATH_W;
    else if (data && data->Reserved == (ULONG_PTR)&magic_hid)
        path = HID_PATH_W;
    if (!path)
        return SetupDiGetDeviceInterfaceDetailW(h, data, detail, size, required, dev);

    DWORD need = offsetof(SP_DEVICE_INTERFACE_DETAIL_DATA_W, DevicePath)
                 + (wcslen(path) + 1) * sizeof(WCHAR);
    if (required)
        *required = need;
    if (!detail || size < need) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }
    if (detail->cbSize != sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)) {
        SetLastError(ERROR_INVALID_USER_BUFFER);
        return FALSE;
    }
    wcscpy(detail->DevicePath, path);
    if (dev && dev->cbSize == sizeof(*dev)) {
        memset(&dev->ClassGuid, 0, sizeof(*dev) - offsetof(SP_DEVINFO_DATA, ClassGuid));
    }
    return TRUE;
}

static BOOL WINAPI hook_SetupDiDestroyDeviceInfoList(HDEVINFO h)
{
    EnterCriticalSection(&fake_cs);
    struct fake_set *set = find_set(h);
    if (set)
        set->kind = FAKE_NONE;
    LeaveCriticalSection(&fake_cs);
    return SetupDiDestroyDeviceInfoList(h);
}

/* ---------------------------------------------------------------- hooks: file API */

static HANDLE WINAPI hook_CreateFileW(LPCWSTR name, DWORD access, DWORD share,
                                      LPSECURITY_ATTRIBUTES sa, DWORD disposition, DWORD flags,
                                      HANDLE tmpl)
{
    if (name && !_wcsicmp(name, ETC_PATH_W))
        return open_fake(FAKE_ETC, "CreateFileW");
    if (name && !_wcsicmp(name, HID_PATH_W))
        return open_fake(FAKE_HID, "CreateFileW");
    return CreateFileW(name, access, share, sa, disposition, flags, tmpl);
}

static HANDLE WINAPI hook_CreateFileA(LPCSTR name, DWORD access, DWORD share,
                                      LPSECURITY_ATTRIBUTES sa, DWORD disposition, DWORD flags,
                                      HANDLE tmpl)
{
    if (name && !_stricmp(name, ETC_PATH_A))
        return open_fake(FAKE_ETC, "CreateFileA");
    if (name && !_stricmp(name, HID_PATH_A))
        return open_fake(FAKE_HID, "CreateFileA");
    return CreateFileA(name, access, share, sa, disposition, flags, tmpl);
}

static BOOL WINAPI hook_CloseHandle(HANDLE h)
{
    struct fake_handle closed = {0};
    EnterCriticalSection(&fake_cs);
    for (int i = 0; i < MAX_TRACKED; i++)
        if (handles[i].kind != FAKE_NONE && handles[i].h == h) {
            closed = handles[i];
            handles[i].kind = FAKE_NONE;
            break;
        }
    LeaveCriticalSection(&fake_cs);
    if (closed.kind != FAKE_NONE) {
        bridge_log("CloseHandle(%s %p): %lu reads, %lu writes, %lu read timeouts",
                   kind_name(closed.kind), h, closed.reads, closed.writes, closed.timeouts);
        if (closed.session)
            backend->close(closed.session);
    }
    return CloseHandle(h);
}

static BOOL WINAPI hook_DeviceIoControl(HANDLE h, DWORD code, LPVOID in, DWORD in_size,
                                        LPVOID out, DWORD out_size, LPDWORD returned,
                                        LPOVERLAPPED ov)
{
    struct fake_handle *f;
    if (!lookup_handle(h, &f))
        return DeviceIoControl(h, code, in, in_size, out, out_size, returned, ov);
    if (f->kind != FAKE_ETC) {
        SetLastError(ERROR_INVALID_FUNCTION);
        return FALSE;
    }
    DWORD n = 0;
    switch (code) {
    case ETC_IOCTL_GET_PID:
    case ETC_IOCTL_GET_BCD:
        if (!out || out_size < 2) {
            SetLastError(ERROR_INSUFFICIENT_BUFFER);
            return FALSE;
        }
        *(USHORT *)out = code == ETC_IOCTL_GET_PID ? f->id.pid : f->id.bcd;
        n = 2;
        break;
    case ETC_IOCTL_GET_PRODUCT:   /* SmartSoft reads it but doesn't check it */
        n = (DWORD)strlen(f->id.product) + 1;
        if (!out || out_size < n) {
            SetLastError(ERROR_INSUFFICIENT_BUFFER);
            return FALSE;
        }
        memcpy(out, f->id.product, n);
        break;
    default:
        bridge_log("DeviceIoControl(ETC): unknown code %#lx", (unsigned long)code);
        SetLastError(ERROR_INVALID_FUNCTION);
        return FALSE;
    }
    if (returned)
        *returned = n;
    bridge_log("DeviceIoControl(ETC, %#lx) -> %lu bytes", (unsigned long)code, (unsigned long)n);
    return TRUE;
}

/* A failed transfer is logged once; the rest of the run is summed up when transfers work again,
   so an outage doesn't fill the log. */
static void transfer_failed(struct fake_handle *f, const char *what, DWORD n)
{
    if (f->failing++ == 0)
        bridge_log("%s(ETC, %lu): backend error; retrying until ConsoleLink answers", what,
                   (unsigned long)n);
}

static void transfer_ok(struct fake_handle *f)
{
    if (f->failing) {
        bridge_log("transfers work again, after %lu failed", f->failing);
        f->failing = 0;
    }
}

static BOOL WINAPI hook_WriteFile(HANDLE h, LPCVOID buf, DWORD n, LPDWORD written,
                                  LPOVERLAPPED ov)
{
    struct fake_handle *f;
    if (!lookup_handle(h, &f))
        return WriteFile(h, buf, n, written, ov);
    if (written)
        *written = 0;
    if (f->kind != FAKE_ETC) {
        SetLastError(ERROR_INVALID_FUNCTION);
        return FALSE;
    }
    if (ov)
        bridge_log("WriteFile(ETC): overlapped I/O not supported, doing it synchronously");
    bridge_log_hex("OUT", buf, n);
    int r = backend->write(f->session, buf, n);
    if (r < 0) {
        transfer_failed(f, "WriteFile", n);
        SetLastError(ERROR_DEVICE_NOT_CONNECTED);
        return FALSE;
    }
    transfer_ok(f);
    f->writes++;
    if (written)
        *written = (DWORD)r;
    return TRUE;
}

static BOOL WINAPI hook_ReadFile(HANDLE h, LPVOID buf, DWORD n, LPDWORD got, LPOVERLAPPED ov)
{
    struct fake_handle *f;
    if (!lookup_handle(h, &f))
        return ReadFile(h, buf, n, got, ov);
    if (got)
        *got = 0;
    if (f->kind != FAKE_ETC) {
        SetLastError(ERROR_INVALID_FUNCTION);
        return FALSE;
    }
    if (ov)
        bridge_log("ReadFile(ETC): overlapped I/O not supported, doing it synchronously");
    int r = backend->read(f->session, buf, n, READ_TIMEOUT_MS);
    if (r < 0) {
        transfer_failed(f, "ReadFile", n);
        SetLastError(ERROR_DEVICE_NOT_CONNECTED);
        return FALSE;
    }
    transfer_ok(f);
    if (r == 0) {
        f->timeouts++;
        SetLastError(ERROR_SEM_TIMEOUT);
        return FALSE;
    }
    bridge_log_hex("IN ", buf, (unsigned)r);
    f->reads++;
    if (got)
        *got = (DWORD)r;
    return TRUE;
}

/* ---------------------------------------------------------------- HID exports */

__declspec(dllexport) void WINAPI HidD_GetHidGuid(GUID *g)
{
    if (g)
        *g = GUID_HID_IF;
}

__declspec(dllexport) BOOLEAN WINAPI HidD_GetAttributes(HANDLE h, BRIDGE_HIDD_ATTRIBUTES *attr)
{
    if (!attr)
        return FALSE;
    struct fake_handle *f;
    if (lookup_handle(h, &f)) {
        attr->Size = sizeof(*attr);
        attr->VendorID = 0x14d5;
        attr->ProductID = 0x0201;
        attr->VersionNumber = 0x0001;
        bridge_log("HidD_GetAttributes(fake) -> 14d5:0201");
        return TRUE;
    }
    /* A real HID device: same query as Wine's own hid.dll. */
    BRIDGE_HID_COLLECTION_INFORMATION info;
    DWORD n;
    if (!DeviceIoControl(h, IOCTL_HID_GET_COLLECTION_INFORMATION, NULL, 0, &info, sizeof(info),
                         &n, NULL))
        return FALSE;
    attr->Size = sizeof(*attr);
    attr->VendorID = info.VendorID;
    attr->ProductID = info.ProductID;
    attr->VersionNumber = info.VersionNumber;
    return TRUE;
}

/* ---------------------------------------------------------------- import patching */

struct hook {
    const char *dll;
    const char *name;
    void *fn;
};

static const struct hook exe_hooks[] = {
    {"setupapi.dll", "SetupDiGetClassDevsW", hook_SetupDiGetClassDevsW},
    {"setupapi.dll", "SetupDiEnumDeviceInterfaces", hook_SetupDiEnumDeviceInterfaces},
    {"setupapi.dll", "SetupDiGetDeviceInterfaceDetailW", hook_SetupDiGetDeviceInterfaceDetailW},
    {"setupapi.dll", "SetupDiDestroyDeviceInfoList", hook_SetupDiDestroyDeviceInfoList},
    {"kernel32.dll", "CreateFileW", hook_CreateFileW},
    {"kernel32.dll", "CloseHandle", hook_CloseHandle},
};

static const struct hook engine_hooks[] = {
    {"setupapi.dll", "SetupDiGetClassDevsW", hook_SetupDiGetClassDevsW},
    {"setupapi.dll", "SetupDiEnumDeviceInterfaces", hook_SetupDiEnumDeviceInterfaces},
    {"setupapi.dll", "SetupDiGetDeviceInterfaceDetailW", hook_SetupDiGetDeviceInterfaceDetailW},
    {"setupapi.dll", "SetupDiDestroyDeviceInfoList", hook_SetupDiDestroyDeviceInfoList},
    {"kernel32.dll", "CreateFileW", hook_CreateFileW},
    {"kernel32.dll", "CreateFileA", hook_CreateFileA},
    {"kernel32.dll", "CloseHandle", hook_CloseHandle},
    {"kernel32.dll", "ReadFile", hook_ReadFile},
    {"kernel32.dll", "WriteFile", hook_WriteFile},
    {"kernel32.dll", "DeviceIoControl", hook_DeviceIoControl},
};

static HMODULE WINAPI hook_LoadLibraryW(LPCWSTR name);
static HMODULE WINAPI hook_LoadLibraryExW(LPCWSTR name, HANDLE file, DWORD flags);

static const struct hook qtcore_hooks[] = {
    {"kernel32.dll", "LoadLibraryW", hook_LoadLibraryW},
    {"kernel32.dll", "LoadLibraryExW", hook_LoadLibraryExW},
};

#define COUNT(a) (sizeof(a) / sizeof((a)[0]))

/* Points each import named in hooks at its hook; returns how many entries changed. */
static int patch_imports(HMODULE mod, const struct hook *hooks, unsigned n_hooks)
{
    BYTE *base = (BYTE *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE)
        return 0;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY *dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir->VirtualAddress)
        return 0;
    int changed = 0;
    for (IMAGE_IMPORT_DESCRIPTOR *d = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir->VirtualAddress);
         d->Name; d++) {
        const char *dll = (const char *)(base + d->Name);
        if (!d->OriginalFirstThunk) {
            bridge_log("patch: %s imports have no name table, skipped", dll);
            continue;
        }
        IMAGE_THUNK_DATA *names = (IMAGE_THUNK_DATA *)(base + d->OriginalFirstThunk);
        IMAGE_THUNK_DATA *slots = (IMAGE_THUNK_DATA *)(base + d->FirstThunk);
        for (; names->u1.AddressOfData; names++, slots++) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))
                continue;
            const char *fn = (const char *)((IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData))->Name;
            for (unsigned i = 0; i < n_hooks; i++) {
                if (_stricmp(dll, hooks[i].dll) || strcmp(fn, hooks[i].name))
                    continue;
                if (slots->u1.Function != (ULONG_PTR)hooks[i].fn) {
                    DWORD old;
                    VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function),
                                   PAGE_READWRITE, &old);
                    slots->u1.Function = (ULONG_PTR)hooks[i].fn;
                    VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), old, &old);
                    changed++;
                }
            }
        }
    }
    return changed;
}

static void patch_module(HMODULE mod, const char *label, const struct hook *hooks, unsigned n)
{
    if (!mod)
        return;
    int changed = patch_imports(mod, hooks, n);
    if (changed)
        bridge_log("patched %d imports in %s", changed, label);
}

static void patch_engine_if_loaded(void)
{
    patch_module(GetModuleHandleW(L"sfmlEngine.dll"), "sfmlEngine.dll", engine_hooks,
                 COUNT(engine_hooks));
    patch_module(GetModuleHandleW(L"sfEngine.dll"), "sfEngine.dll", engine_hooks,
                 COUNT(engine_hooks));
}

static HMODULE WINAPI hook_LoadLibraryW(LPCWSTR name)
{
    HMODULE m = LoadLibraryW(name);
    if (m)
        patch_engine_if_loaded();
    return m;
}

static HMODULE WINAPI hook_LoadLibraryExW(LPCWSTR name, HANDLE file, DWORD flags)
{
    HMODULE m = LoadLibraryExW(name, file, flags);
    if (m && !(flags & (LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_DATAFILE_EXCLUSIVE
                        | LOAD_LIBRARY_AS_IMAGE_RESOURCE | DONT_RESOLVE_DLL_REFERENCES)))
        patch_engine_if_loaded();
    return m;
}

/* ---------------------------------------------------------------- entry point */

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason != DLL_PROCESS_ATTACH)
        return TRUE;
    DisableThreadLibraryCalls(inst);
    InitializeCriticalSection(&log_cs);
    InitializeCriticalSection(&fake_cs);
    open_log();
    char trace[8];
    DWORD len = GetEnvironmentVariableA("SMARTSOFT_BRIDGE_TRACE", trace, sizeof(trace));
    trace_io = len && len < sizeof(trace) && trace[0] == '1';
    choose_backend();
    bridge_log("---- SmartSoft bridge loaded (backend %s%s%s)", backend->name,
               backend == &tcp_backend ? " " : "",
               backend == &tcp_backend ? tcp_backend_address() : "");
    if (trace_io)
        bridge_log("tracing every transfer");
    patch_module(GetModuleHandleW(NULL), "SmartSoft.exe", exe_hooks, COUNT(exe_hooks));
    patch_module(GetModuleHandleW(L"QtCore4.dll"), "QtCore4.dll", qtcore_hooks,
                 COUNT(qtcore_hooks));
    patch_engine_if_loaded();
    return TRUE;
}
