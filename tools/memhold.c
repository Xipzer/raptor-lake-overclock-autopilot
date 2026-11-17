// memhold: an at-rest memory test. Fills a large block with known patterns, leaves it untouched, and checks it every
// few minutes. Data that nobody reads or writes depends on DRAM refresh alone, so a bit that flips here is direct
// evidence of a refresh/retention fault (tREFI, tRFC), with the CPU out of the picture. Run a heavy load beside it
// (for heat and traffic) in whatever memory is left.
//
// Usage: memhold.exe <GiB> <minutes> <check_every_seconds> <logfile>
// A fifth argument "inject" flips one bit on purpose after the fill, to prove the detector works.
// Exit code: 0 no flips, 2 flips found, 1 could not run.
// Patterns per 1 GiB chunk (chunk index mod 4): all zeros, all ones, alternating 0xAA../0x55.. words, pseudo-random.
// Each mismatch is read twice more straight from the DIMM (cache line flushed first): "held" = the wrong value is
// really stored (retention or a bad write), "transient" = the DIMM holds the right value (a read/bus error),
// "unstable" = the fresh reads disagree. The memory controller scrambles data, so do not read meaning into which
// pattern a flip lands in. Counts are per 128-byte block (the on-die ECC unit), shown as an XOR map.
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <io.h>
#include <string.h>
#include <emmintrin.h>

#define CHUNK (1ull << 30)
#define THREADS 8

static uint64_t *base;
static uint64_t words;
static FILE *logf;
static HANDLE logh = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION cs;
static volatile LONG64 held_total, transient_total;
static int shown;

static uint64_t mix(uint64_t x) {
    x += 0x9E3779B97F4A7C15ull;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
    return x ^ (x >> 31);
}

static inline uint64_t expect(uint64_t i) {
    switch ((i / (CHUNK / 8)) & 3) {
        case 0: return 0;
        case 1: return ~0ull;
        case 2: return (i & 1) ? 0x5555555555555555ull : 0xAAAAAAAAAAAAAAAAull;
        default: return mix(i);
    }
}

static void stamp(char *out) {
    SYSTEMTIME t;
    GetLocalTime(&t);
    sprintf(out, "%02d:%02d:%02d", t.wHour, t.wMinute, t.wSecond);
}

static void say(const char *line) {
    char ts[16];
    stamp(ts);
    printf("%s %s\n", ts, line);
    fflush(stdout);
    if (logf) {
        fprintf(logf, "%s %s\n", ts, line);
        fflush(logf);
        if (logh != INVALID_HANDLE_VALUE) FlushFileBuffers(logh);
    }
}

typedef struct { uint64_t from, to; int fill; } Job;

// Read one word from the DIMM itself: without the flush a second read is served from the CPU cache and can never
// differ from the first (found in review).
static inline uint64_t dram_read(volatile uint64_t *p) {
    _mm_clflush((const void *)p);
    _mm_mfence();
    return *p;
}

static DWORD WINAPI work(LPVOID a) {
    Job *j = a;
    if (j->fill) {
        for (uint64_t i = j->from; i < j->to; i++) base[i] = expect(i);
        return 0;
    }
    for (uint64_t i = j->from; i < j->to; i++) {
        uint64_t want = expect(i);
        volatile uint64_t *p = (volatile uint64_t *)&base[i];
        uint64_t got = *p;
        if (got == want) continue;
        uint64_t r2 = dram_read(p), r3 = dram_read(p);
        // held: the wrong value is really stored. transient: the DIMM holds the right value, the first read was bad.
        // unstable: the two fresh reads disagree with each other.
        const char *kind = (r2 == got && r3 == got) ? "held" : (r2 == want && r3 == want) ? "transient" : "unstable";
        if (kind[0] == 'h') InterlockedIncrement64(&held_total); else InterlockedIncrement64(&transient_total);
        // On-die ECC works on 128 bits per chip per burst = one byte lane across 16 consecutive words (128 bytes).
        // Show the whole 128-byte block so that pattern is visible, then put the block right and move past it.
        uint64_t b0 = i & ~15ull, b1 = b0 + 16 > j->to ? j->to : b0 + 16;
        if (b0 < j->from) b0 = j->from;
        EnterCriticalSection(&cs);
        if (shown < 200) {
            char line[1024];
            int n = sprintf(line, "FLIP %s offset 0x%010llx (GiB %llu, pattern %llu) expected %016llx read %016llx bits %016llx fresh reads %016llx %016llx | 128-byte block xor:",
                            kind, (unsigned long long)(i * 8), (unsigned long long)(i / (CHUNK / 8)),
                            (unsigned long long)((i / (CHUNK / 8)) & 3), (unsigned long long)want, (unsigned long long)got,
                            (unsigned long long)(got ^ want), (unsigned long long)r2, (unsigned long long)r3);
            for (uint64_t k = b0; k < b1; k++) {
                uint64_t x = dram_read((volatile uint64_t *)&base[k]) ^ expect(k);
                n += x ? sprintf(line + n, " %016llx", (unsigned long long)x) : sprintf(line + n, " 0");
            }
            say(line);
            shown++;
        }
        LeaveCriticalSection(&cs);
        for (uint64_t k = b0; k < b1; k++) base[k] = expect(k);  // a later flip in the same block is counted again
        i = b1 - 1;
    }
    return 0;
}

static void run(int fill) {
    HANDLE h[THREADS];
    Job jobs[THREADS];
    for (int t = 0; t < THREADS; t++) {
        jobs[t].from = words / THREADS * t;
        jobs[t].to = t == THREADS - 1 ? words : words / THREADS * (t + 1);
        jobs[t].fill = fill;
        h[t] = CreateThread(NULL, 0, work, &jobs[t], 0, NULL);
    }
    WaitForMultipleObjects(THREADS, h, TRUE, INFINITE);
    for (int t = 0; t < THREADS; t++) CloseHandle(h[t]);
}

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "usage: memhold <GiB> <minutes> <check_every_seconds> <logfile>\n");
        return 1;
    }
    uint64_t gib = strtoull(argv[1], NULL, 10);
    int minutes = atoi(argv[2]), every = atoi(argv[3]);
    if (gib < 1 || gib > 512 || minutes < 1 || every < 5) return 1;
    logf = fopen(argv[4], "a");
    if (logf) logh = (HANDLE)_get_osfhandle(_fileno(logf));
    InitializeCriticalSection(&cs);
    char line[256];
    uint64_t bytes = gib * CHUNK;
    words = bytes / 8;

    // Keep the block in physical memory: a page that Windows trims, compresses or writes to the pagefile would come
    // back rewritten, which is exactly what this test must avoid.
    int ws = SetProcessWorkingSetSizeEx(GetCurrentProcess(), bytes + (256ull << 20), bytes + (1024ull << 20),
                                        QUOTA_LIMITS_HARDWS_MIN_ENABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE);
    base = VirtualAlloc(NULL, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!base) {
        sprintf(line, "could not allocate %llu GiB (error %lu)", (unsigned long long)gib, GetLastError());
        say(line);
        return 1;
    }
    sprintf(line, "memhold start: %llu GiB, %d min, check every %d s, working-set floor %s", (unsigned long long)gib,
            minutes, every, ws ? "set" : "NOT set");
    say(line);
    uint64_t locked = 0;   // lock first, so that no page can be trimmed and brought back between fill and check
    for (uint64_t off = 0; off < bytes; off += CHUNK)
        if (VirtualLock((char *)base + off, CHUNK)) locked++;
    run(1);
    sprintf(line, "filled; %llu of %llu GiB locked in physical memory%s", (unsigned long long)locked,
            (unsigned long long)gib, locked == gib ? "" : " (WARNING: unlocked pages can be paged out and would hide flips)");
    say(line);

    if (argc > 5 && !strcmp(argv[5], "inject")) {  // self-test of the detector: one wrong bit, as a flip would leave it
        base[words / 2 + 12345] ^= 1ull << 7;
        say("self-test: one bit flipped on purpose");
    }
    ULONGLONG end = GetTickCount64() + (ULONGLONG)minutes * 60000;
    int pass = 0;
    while (GetTickCount64() < end) {
        ULONGLONG left = end - GetTickCount64();
        Sleep((DWORD)(left < (ULONGLONG)every * 1000 ? left : (ULONGLONG)every * 1000));
        LONG64 h0 = held_total, t0 = transient_total;
        ULONGLONG a = GetTickCount64();
        run(0);
        pass++;
        sprintf(line, "check %d: %lld held, %lld transient/unstable blocks this pass (%.1f s); totals %lld held, %lld transient",
                pass, (long long)(held_total - h0), (long long)(transient_total - t0), (GetTickCount64() - a) / 1000.0,
                (long long)held_total, (long long)transient_total);
        say(line);
    }
    sprintf(line, "memhold done: %d checks, %lld held flips, %lld transient mismatches", pass, (long long)held_total,
            (long long)transient_total);
    say(line);
    return (held_total || transient_total) ? 2 : 0;
}
