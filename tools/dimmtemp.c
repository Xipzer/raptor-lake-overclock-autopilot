// DDR5 DIMM temperatures from the SPD5118 hubs, read over the Intel SMBus through PawnIO's signed SmbusI801
// module. Read only: it never writes to the bus. Needs admin rights (PawnIO).
//   dimmtemp.exe                  one line: time,slot0,slot1,...
//   dimmtemp.exe <seconds> <file> append one line every 5 s for that long
// Needs PawnIO installed and SmbusI801.bin (from PawnIO.Modules) beside the exe. The sensors read 0 after a cold
// boot until something initialises the SPD hubs (HWiNFO does); the harness can run an init command for that.
// The bus mutex is the one HWiNFO and others share, so reads do not collide. Intel I801 SMBus only.
// Build: zig cc -O2 -o dimmtemp.exe dimmtemp.c -target x86_64-windows-gnu
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

typedef HRESULT(WINAPI *PioOpen)(PHANDLE);
typedef HRESULT(WINAPI *PioLoad)(HANDLE, const UCHAR *, SIZE_T);
typedef HRESULT(WINAPI *PioExec)(HANDLE, PCSTR, const ULONG64 *, SIZE_T, PULONG64, SIZE_T, PSIZE_T);
static PioExec pexec;
static HANDLE pio, busMutex;

static BOOL readByte(int addr, int reg, int *out) {
    ULONG64 in[4] = {(ULONG64)addr, 1, (ULONG64)reg, 2}, o[1] = {0};
    SIZE_T n = 0;
    if (WaitForSingleObject(busMutex, 1000) == WAIT_TIMEOUT) return FALSE;
    HRESULT hr = pexec(pio, "ioctl_smbus_xfer", in, 4, o, 1, &n); // out_size must be 1 for a byte read
    ReleaseMutex(busMutex);
    if (hr != S_OK) return FALSE;
    *out = (int)(o[0] & 0xFF);
    return TRUE;
}

static BOOL readWord(int addr, int reg, int *out) {
    int lo, hi;
    if (!readByte(addr, reg, &lo) || !readByte(addr, reg + 1, &hi)) return FALSE;
    *out = lo | hi << 8;
    return TRUE;
}

// MR49/MR50: temperature, 0.25 C per step in bits 12:2, bit 12 is the sign.
static double temp(int addr) {
    int raw;
    if (!readWord(addr, 0x31, &raw)) return -999;
    int v = raw >> 2 & 0x7FF;
    if (raw & 0x1000) v -= 0x800;
    return v * 0.25;
}

int main(int argc, char **argv) {
    WCHAR lib[MAX_PATH], mod[MAX_PATH];
    ExpandEnvironmentStringsW(L"%ProgramFiles%\\PawnIO\\PawnIOLib.dll", lib, MAX_PATH);
    HMODULE h = LoadLibraryW(lib);
    if (!h) return 2;
    PioOpen popen = (PioOpen)GetProcAddress(h, "pawnio_open");
    PioLoad pload = (PioLoad)GetProcAddress(h, "pawnio_load");
    pexec = (PioExec)GetProcAddress(h, "pawnio_execute");
    GetModuleFileNameW(NULL, mod, MAX_PATH);
    *(wcsrchr(mod, L'\\') + 1) = 0;
    wcscat(mod, L"SmbusI801.bin");
    HANDLE f = CreateFileW(mod, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return 3;
    DWORD size = GetFileSize(f, NULL), got;
    UCHAR *blob = malloc(size);
    ReadFile(f, blob, size, &got, NULL);
    CloseHandle(f);
    if (popen(&pio) != S_OK || pload(pio, blob, size) != S_OK) return 4;
    busMutex = CreateMutexW(NULL, FALSE, L"Global\\Access_SMBUS.HTP.Method");
    if (argc > 1 && !strcmp(argv[1], "id")) {
        ULONG64 o[3] = {0};
        SIZE_T n;
        HRESULT hr = pexec(pio, "ioctl_identity", NULL, 0, o, 3, &n);
        printf("identity hr=%lx type=%llx base=%llx pci=%llx\n", hr, o[0], o[1], o[2]);
        for (int a = 0x50; a <= 0x52; a += 2)
            for (int r = 0x30; r <= 0x33; r++) {
                ULONG64 in[4] = {(ULONG64)a, 1, (ULONG64)r, 2}, q[5] = {0};
                WaitForSingleObject(busMutex, 1000);
                hr = pexec(pio, "ioctl_smbus_xfer", in, 4, q, 1, &n);
                ReleaseMutex(busMutex);
                printf("%02X MR%02X hr=%lx v=%llx n=%zu\n", a, r, hr, q[0], n);
            }
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "dump")) {
        for (int a = 0x50; a <= 0x57; a++) {
            ULONG64 in[4] = {(ULONG64)a, 1, 0, 2}, o[5] = {0};
            SIZE_T n;
            int ok = 0;
            printf("%02X:", a);
            for (int r = 0; r < 0x34; r += (r < 4 ? 1 : 0x10)) {
                in[2] = (ULONG64)r, o[0] = 0, o[1] = 0;
                WaitForSingleObject(busMutex, 500);
                HRESULT hr = pexec(pio, "ioctl_smbus_xfer", in, 4, o, 5, &n);
                ReleaseMutex(busMutex);
                if (hr == S_OK) printf(" %02X=%llX/%llX", r, o[0], o[1]), ok = 1;
                else if (!r) break;
            }
            printf("%s\n", ok ? "" : " none");
        }
        return 0;
    }
    int seconds = argc > 2 ? atoi(argv[1]) : 0;
    FILE *out = argc > 2 ? fopen(argv[2], "a") : stdout;
    DWORD end = GetTickCount() + (DWORD)seconds * 1000;
    do {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(out, "%02d:%02d:%02d", st.wHour, st.wMinute, st.wSecond);
        for (int a = 0x50; a <= 0x57; a++) {
            double t = temp(a);
            if (t > -100 && t < 125) fprintf(out, ",%d:%.2f", a - 0x50, t);
        }
        fprintf(out, "\n");
        fflush(out);
        if (seconds) Sleep(5000);
    } while (seconds && (int)(end - GetTickCount()) > 0);
    return 0;
}
