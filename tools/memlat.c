// Memory latency and read bandwidth, for comparing RAM settings on the same PC. NOT AIDA64: the numbers are only
// comparable with other runs of this program on the same machine, same clocks and same page size.
//   memlat.exe                 latency (random pointer chase through 1 GiB, one cache line per step, single thread)
//                              and read bandwidth (all threads summing a 4 GiB buffer). Prints: latency_ns read_GBps
//   memlat.exe --large-pages   the same with 2 MiB pages, which removes most TLB misses from the latency figure
//                              (closer to what AIDA reports). Needs the "Lock pages in memory" right
//                              (SeLockMemoryPrivilege) for your account, then a new logon; falls back to 4 KiB
//                              pages with a note on stderr if it is not available.
//   memlat.exe --cpu N         pin the latency thread to logical CPU N (default 2)
// Build: zig cc -O2 -o memlat.exe memlat.c -target x86_64-windows-gnu -ladvapi32
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static uint64_t rng = 88172645463325252ull;
static uint64_t next(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }

static double now(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}

static BOOL enableLockPages(void) {
    HANDLE tok;
    TOKEN_PRIVILEGES tp;
    tp.PrivilegeCount = 1;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &tok)) return FALSE;
    BOOL ok = LookupPrivilegeValueW(NULL, L"SeLockMemoryPrivilege", &tp.Privileges[0].Luid);
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
    ok = ok && AdjustTokenPrivileges(tok, FALSE, &tp, 0, NULL, NULL) && GetLastError() == ERROR_SUCCESS;
    CloseHandle(tok);
    return ok;
}

// Large pages when asked and possible, else normal pages. Size is rounded up to the large page size.
static void *alloc(size_t *size, BOOL large, BOOL *gotLarge) {
    *gotLarge = FALSE;
    if (large) {
        size_t lp = GetLargePageMinimum();
        if (lp) {
            size_t s = (*size + lp - 1) / lp * lp;
            void *p = VirtualAlloc(NULL, s, MEM_COMMIT | MEM_RESERVE | MEM_LARGE_PAGES, PAGE_READWRITE);
            if (p) {
                *size = s;
                *gotLarge = TRUE;
                return p;
            }
        }
    }
    return VirtualAlloc(NULL, *size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
}

typedef struct { const uint64_t *p; size_t n; uint64_t sum; } Job;
static DWORD WINAPI reader(LPVOID a) {
    Job *j = a;
    uint64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    for (int r = 0; r < 4; r++)
        for (size_t i = 0; i < j->n; i += 4) s0 += j->p[i], s1 += j->p[i + 1], s2 += j->p[i + 2], s3 += j->p[i + 3];
    j->sum = s0 + s1 + s2 + s3;
    return 0;
}

int main(int argc, char **argv) {
    BOOL large = FALSE, gotLarge = FALSE, gotLarge2 = FALSE;
    int cpu = 2;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--large-pages")) large = TRUE;
        else if (!strcmp(argv[i], "--cpu") && i + 1 < argc) cpu = atoi(argv[++i]);
    }
    if (large && !enableLockPages()) {
        fprintf(stderr, "note: no SeLockMemoryPrivilege, using 4 KiB pages\n");
        large = FALSE;
    }
    SetThreadAffinityMask(GetCurrentThread(), (DWORD_PTR)1 << cpu);
    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
    size_t bytes = 1ull << 30;
    char *buf = alloc(&bytes, large, &gotLarge);
    const size_t lines = (1ull << 30) / 64;
    uint32_t *order = malloc(lines * sizeof(uint32_t));
    if (!buf || !order) return 1;
    for (size_t i = 0; i < lines; i++) order[i] = (uint32_t)i;
    for (size_t i = lines - 1; i > 0; i--) { size_t j = next() % (i + 1); uint32_t t = order[i]; order[i] = order[j]; order[j] = t; }
    for (size_t i = 0; i < lines; i++) *(void **)(buf + (size_t)order[i] * 64) = buf + (size_t)order[(i + 1) % lines] * 64;
    free(order);
    void **p = (void **)buf;
    for (size_t i = 0; i < lines; i++) p = *p; // warm the page tables
    double best = 1e9;
    for (int run = 0; run < 3; run++) {
        size_t steps = 20000000;
        double t0 = now();
        for (size_t i = 0; i < steps; i++) p = *p;
        double ns = (now() - t0) * 1e9 / (double)steps;
        if (ns < best) best = ns;
    }
    VirtualFree(buf, 0, MEM_RELEASE);

    SetThreadAffinityMask(GetCurrentThread(), (DWORD_PTR)-1);
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    int threads = (int)si.dwNumberOfProcessors;
    if (threads > 64) threads = 64;
    size_t bigBytes = 4ull << 30;
    uint64_t *big = alloc(&bigBytes, large, &gotLarge2);
    if (!big) return 2;
    size_t words = (4ull << 30) / 8;
    for (size_t i = 0; i < words; i++) big[i] = i;
    Job jobs[64];
    HANDLE h[64];
    double gbps = 0;
    for (int run = 0; run < 3; run++) {
        double t0 = now();
        for (int t = 0; t < threads; t++) {
            jobs[t].p = big + words / threads * t, jobs[t].n = words / threads / 4 * 4;
            h[t] = CreateThread(NULL, 0, reader, &jobs[t], 0, NULL);
        }
        WaitForMultipleObjects(threads, h, TRUE, INFINITE);
        double s = now() - t0;
        for (int t = 0; t < threads; t++) CloseHandle(h[t]);
        double g = 4.0 * (double)(words * 8) / s / 1e9;
        if (g > gbps) gbps = g;
    }
    VirtualFree(big, 0, MEM_RELEASE);
    // The pointer is printed so the compiler cannot drop the chase. Third field: page size used.
    printf("%.1f %.1f %s %p\n", best, gbps, gotLarge ? "large" : "4k", (void *)p);
    return 0;
}
