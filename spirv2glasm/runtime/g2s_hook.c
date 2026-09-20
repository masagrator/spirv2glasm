/*
 * g2s_hook.c -- the one place where a GLASM listing can be pushed into the
 * compiler, and the one place its own listing can be read out.
 *
 * WHY A HOOK AND NOT A REIMPLEMENTATION
 * ------------------------------------
 * GLSLC 17.24 does not go from GLSL to SASS in one step.  Its pipeline is
 *
 *     GLSL --[cgc front end]--> GLASM text --[asmp assembler]--> OCG --> SASS
 *
 * and the GLASM in the middle is a real, complete, NUL-terminated
 * NV_gpu_program5 listing -- the same text the compiler later stores in the
 * .dbgi section and (with comments stripped) in the fat control section.  That
 * is not an inference: the handover is visible in the translated code.
 *
 * In f_71010d76b0 -- the function that owns the whole "produce GLASM, then
 * assemble it" step -- the sequence is, block for block:
 *
 *     f_7100ef60e0(cpu, 0);                 ; print the program as GLASM text
 *     X23 = [X29-0x10];                     ; the printer's ERROR-message slot
 *     X25 = X0;                             ; <-- the GLASM text
 *     if (X23 != 0) { ...report the error and stop... }
 *     X0  = strlen(X25);
 *     X26 = X0;                             ; <-- its length
 *     ...
 *     X5 = X25; X6 = (uint32_t)X26;
 *     f_71010ab160(cpu, 0);                 ; assemble it
 *
 * So the assembler's view of the program is EXACTLY the two values (X25, X26),
 * both derived from the single pointer the printer returns in X0, and
 * everything after that point -- parsing, binding and reflection tables, the
 * OCG phase pipeline, register allocation, scheduling, the GM20x encoder, the
 * SPH, the container -- consumes nothing else from the front end.  Replacing
 * that one pointer therefore replaces the entire program being compiled, and
 * does so through the compiler's own back end rather than through a model of
 * it.  That is what makes a GLASM->SASS conversion exact by construction
 * instead of exact up to whatever the model got right.
 *
 * Verified before this file was written, with the stock (unhooked) binary
 * under gdb, by overriding X0 on return from f_7100ef60e0:
 *
 *   * a three-line stub vertex shader, driven with a corpus shader's GLASM,
 *     produced a .code section byte-identical to compiling that corpus shader
 *     directly, and a control section differing only in the eight bytes at
 *     +0x7D0 -- the hash OF THE GLSL SOURCE TEXT, which no GLASM can carry;
 *   * the same holds for the comment-stripped listing taken out of a fat
 *     control section, so both of the forms the container stores are accepted.
 *
 * WHY THE LENGTH IS NOT SET HERE
 * ------------------------------
 * X26 is computed by the guest itself, as strlen of the pointer this hook
 * installs, in the block immediately after the override.  Writing a length
 * here would either duplicate that or contradict it.  The one requirement the
 * replacement text must meet is therefore that it is NUL-TERMINATED, which
 * g2s_set_glasm enforces by copying.
 *
 * WHY THE ERROR SLOT IS LEFT ALONE
 * --------------------------------
 * [X29-0x10] is the printer's error-message out-parameter, not a second copy
 * of the text: it reads 0 on every successful print (measured).  The block
 * that dereferences it runs only when it is non-zero, and a front end that
 * has just succeeded leaves it zero, so the substituted listing reaches the
 * assembler by the normal path.  Clearing or setting it here would be
 * inventing a state the compiler never produces.
 */

/* The tracer at the bottom of this file needs sigaction/sigsetjmp, and the
 * tree is built with -std=c11, which hides POSIX.  Declared here rather than
 * beside the tracer because a feature-test macro only works before the first
 * include. */
#define _POSIX_C_SOURCE 200809L

#include <stdlib.h>
#include <string.h>

static int g2s_want(const char *name);
/* the program pointer, remembered so a hook patched onto a helper that
 * does not receive it can still dump the allocator records (notes/45). */
static unsigned long long g2s_prog_seen;
/* The node pools the constructors have been called with, recorded by
 * g2s_trace_mkpool and walked by g2s_trace_stamps -- declared here because
 * the walker appears earlier in this file than the recorder. */
static unsigned long long g2s_pools[16];
static int g2s_npools;

#include "guest_rt.h"

/* The write watch's arming state, defined at g2s_watch_hit below.  Declared
 * here because hooks above the definition arm it.  The STORE side of the
 * watch -- the test `st_i32`/`st_i64` make before every guest store -- lived
 * in a modified include/guest_rt.h that this package does not carry, so in a
 * tree built from the stock header the watch is armed and never fires
 * (HANDOVER sec.3.2).  Arming it is harmless. */
extern uint64_t g2s_watch_addr;
extern uint64_t g2s_watch_lo, g2s_watch_hi;

/* The replacement listing, or NULL for "let the front end's own text through".
 * Owned by this module: g2s_set_glasm copies, so the caller's buffer may go
 * away and the text is guaranteed NUL-terminated whatever the caller passed. */
static char   *g2s_replacement;
static size_t  g2s_replacement_len;

/* The last listing the front end produced, captured on the way past.  This is
 * the compiler's own GLASM for whatever it was just asked to compile, in the
 * exact form the assembler consumes -- which is what makes a self-test
 * possible in one process, with no container parsing and no gdb.
 *
 * It is a pointer INTO GUEST-OWNED MEMORY and the guest frees it when the
 * compilation object is finalised, so it is copied rather than kept. */
static char   *g2s_captured;
static size_t  g2s_captured_len;

/* Set by the hook every time it runs, so a caller can tell "the front end was
 * never reached" (0) from "it ran and produced nothing" -- two very different
 * failures that otherwise look the same from outside. */
static unsigned long g2s_hook_calls;


void g2s_set_glasm(const char *text, size_t len)
{
    free(g2s_replacement);
    g2s_replacement = NULL;
    g2s_replacement_len = 0;
    if (!text)
        return;
    if (len == (size_t)-1)
        len = strlen(text);
    g2s_replacement = (char *)malloc(len + 1);
    if (!g2s_replacement)
        return;
    memcpy(g2s_replacement, text, len);
    g2s_replacement[len] = '\0';
    g2s_replacement_len = len;
}

const char *g2s_get_captured(size_t *len_out)
{
    if (len_out)
        *len_out = g2s_captured_len;
    return g2s_captured;
}

unsigned long g2s_hook_call_count(void)
{
    return g2s_hook_calls;
}

void g2s_reset_capture(void)
{
    free(g2s_captured);
    g2s_captured = NULL;
    g2s_captured_len = 0;
    g2s_hook_calls = 0;
}

/*
 * Called from f_71010d76b0 on the instruction boundary immediately after
 * f_7100ef60e0 returns, before anything reads X0.
 *
 * Guest X0 is at GUEST_OFF_X0; the GST_I64 macro needs a local called `cpu`,
 * which is this function's parameter.
 *
 * The pointer installed is a HOST pointer.  That is correct here and not a
 * shortcut: this port maps the guest's own heap onto host malloc (see
 * guest_host_ptr in guest_decls.h -- an address outside the two image windows
 * IS a host pointer), and the text the printer returns is itself a host
 * pointer from the guest allocator.  The assembler only ever reads it.
 */
void g2s_after_glasm_print(cpu_t *cpu)
{
    uint64_t produced = GST_I64(GUEST_OFF_X0);

    g2s_hook_calls++;

    if (produced) {
        const char *src = (const char *)(uintptr_t)produced;
        size_t n = strlen(src);
        char *copy = (char *)malloc(n + 1);
        if (copy) {
            memcpy(copy, src, n + 1);
            free(g2s_captured);
            g2s_captured = copy;
            g2s_captured_len = n;
        }
    }

    if (g2s_replacement)
        GST_I64(GUEST_OFF_X0) = (uint64_t)(uintptr_t)g2s_replacement;
}

/* ---------------------------------------------------------------------------
 * g2s_trace -- the call tracer trace_patch.py installs.
 *
 * The port turns every guest function into ordinary C, so observing the front
 * end does not need gdb or an emulator: a call at the top of f_<addr> sees the
 * same machine state a breakpoint there would, and can print guest memory as
 * well as guest registers.  That is what this is for -- it is a MEASUREMENT
 * INSTRUMENT, not part of the compiler, and it is off unless G2S_TRACE is set
 * in the environment.
 *
 * Output goes to stderr, one line per call:
 *
 *     g2s_trace f_7100fcdcc0 x0=... x1=... ... x7=...
 *
 * The first eight guest registers are the AArch64 argument registers, so for a
 * function reached by an ordinary call they are its arguments.  Nothing here
 * interprets them: which of them is a pointer, and to what, is exactly the
 * question being asked, and printing a guess would answer it in advance.
 *
 * G2S_TRACE=<n> limits the output to the first n lines; G2S_TRACE=1 (or any
 * non-numeric value) means no limit.  A limit matters because a front end
 * compiling a real shader calls some of these hundreds of thousands of times.
 */
#include <stdio.h>
#include <execinfo.h>

static long   g2s_trace_budget = -2;     /* -2 = not yet read from the env */
static unsigned long g2s_trace_count;

void g2s_trace(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        if (!e || !*e)
            g2s_trace_budget = 0;
        else {
            char *end = NULL;
            long v = strtol(e, &end, 0);
            g2s_trace_budget = (end && *end == '\0' && v > 1) ? v : -1;
        }
    }
    if (g2s_trace_budget == 0)
        return;
    if (g2s_trace_budget > 0 && (long)g2s_trace_count >= g2s_trace_budget)
        return;
    g2s_trace_count++;
    /* LR as well as the arguments.  The port reaches some guest functions
     * through a computed pointer -- f_7100f27da0, which emits one `#var`
     * line, has NO reference anywhere in the generated tree or in .data -- so
     * the static call graph simply stops there.  The return address is the
     * caller, and printing it is the cheapest way to find a walker that
     * cannot be found by grepping. */
    fprintf(stderr, "g2s_trace f_%lx lr=%llx", addr,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
    for (int i = 0; i < 8; ++i)
        fprintf(stderr, " x%d=%016llx", i,
                (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * i));
    fputc('\n', stderr);
}

/* Read a NUL-terminated string out of guest memory, for a trace that wants to
 * print a name rather than a pointer.  Guest heap addresses in this port ARE
 * host addresses (guest_host_ptr), which is why this is a cast and not a walk. */
const char *g2s_trace_str(unsigned long long p)
{
    return p ? (const char *)(uintptr_t)p : "(null)";
}

/* Copy a printable NUL-terminated string out of guest memory, or fail.
 *
 * TWO ATTEMPTS AT THIS WERE WRONG, and the way they were wrong is the reason
 * this is now written the only way that can work.
 *
 *   1. A RANGE TEST -- "the value is between 0x1000 and 0x7fffffffffff, so it
 *      is a pointer" -- and then a dereference.  That is not a test: almost
 *      every 64-bit value passes it.  It crashed on the fifth traced call.
 *   2. A write(2) PROBE to /dev/null, which returns EFAULT instead of raising
 *      SIGSEGV for an unreadable buffer.  Sound in principle and it still
 *      crashed, so whatever faults here is not a plain unmapped page.
 *
 * The property actually needed is "reading this does not kill the process",
 * and the only thing that can decide that is a handler for the fault itself.
 * So the scan runs under a SIGSEGV/SIGBUS handler that longjmps out, the
 * previous handlers are restored around it, and a fault simply makes the value
 * print as a number.
 *
 * That matters beyond tidiness: the port installs its OWN SIGSEGV handler to
 * report guest faults, and without saving and restoring it a fault inside the
 * tracer is reported as "guest: fatal signal 11", which is how two hours went
 * into looking for a bug in the guest that was in the instrument. */
#include <setjmp.h>
#include <unistd.h>
#include <signal.h>

#define G2S_STR_MAX 64

static sigjmp_buf g2s_fault_jmp;
static volatile sig_atomic_t g2s_in_probe;

static void g2s_fault(int sig)
{
    (void)sig;
    if (g2s_in_probe)
        siglongjmp(g2s_fault_jmp, 1);
    _exit(139);                  /* not ours: do not swallow a real crash */
}

/* Returns the string in `out` (NUL-terminated) and 1, or 0 if `v` is not a
 * readable printable string. */
static int g2s_get_string(unsigned long long v, char *out, size_t outsz)
{
    struct sigaction sa, old_segv, old_bus;
    const unsigned char *p;
    size_t i;
    int ok = 0;

    if (v < 0x1000 || v > 0x7fffffffffffULL)
        return 0;

    sa.sa_handler = g2s_fault;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    if (sigaction(SIGSEGV, &sa, &old_segv) != 0)
        return 0;
    sigaction(SIGBUS, &sa, &old_bus);

    g2s_in_probe = 1;
    if (sigsetjmp(g2s_fault_jmp, 1) == 0) {
        p = (const unsigned char *)(uintptr_t)v;
        for (i = 0; i + 1 < outsz; ++i) {
            unsigned char c = p[i];
            if (c == 0) {
                out[i] = 0;
                ok = (i > 0);
                break;
            }
            if (c < 0x20 || c > 0x7e)
                break;           /* not text */
            out[i] = (char)c;
        }
    }
    g2s_in_probe = 0;

    sigaction(SIGSEGV, &old_segv, NULL);
    sigaction(SIGBUS, &old_bus, NULL);
    return ok;
}

/* The variadic-print tracer.
 *
 * A GLASM line is assembled by one formatter that takes a printf-style format
 * and its arguments, and the FORMAT is the thing that says which line is being
 * built -- '%svar %s%d %s', 'OPTION NV_unroll_none;\n', '%s : %s : %s'.  So
 * tracing that one function with its format and arguments spelled out shows
 * the listing being built piece by piece, with each piece's origin.
 *
 * This is what `g2s_trace` cannot do: the interesting values are pointers to
 * strings the compiler built at run time, and a register dump shows only the
 * addresses.  `$vout.PSIZE` is the case that forced this -- "PSIZE" does not
 * occur anywhere in the image, so there was no way to reach it statically.
 *
 * Guarded by G2S_TRACE like everything else here, and it prints at most the
 * first six argument registers because that is what the AArch64 ABI puts in
 * registers; a seventh argument would be on the stack and is not followed. */
void g2s_trace_fmt(cpu_t *cpu, unsigned long addr)
{
    int i;
    unsigned long long fmt = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        if (!e || !*e)
            g2s_trace_budget = 0;
        else {
            char *end = NULL;
            long v = strtol(e, &end, 0);
            g2s_trace_budget = (end && *end == '\0' && v > 1) ? v : -1;
        }
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("fmt"))
        return;
    if (g2s_trace_budget > 0 && (long)g2s_trace_count >= g2s_trace_budget)
        return;
    g2s_trace_count++;

    fprintf(stderr, "g2s_fmt f_%lx lr=%llx", addr,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
    for (i = 0; i < 8; ++i) {
        char buf[G2S_STR_MAX];
        unsigned long long v = GST_I64(GUEST_OFF_X0 + 8 * i);
        if (g2s_get_string(v, buf, sizeof buf)) {
            const char *q;
            fprintf(stderr, " x%d=\"", i);
            for (q = buf; *q; ++q) {
                if (*q == '\n') fputs("\\n", stderr);
                else fputc(*q, stderr);
            }
            fputc('"', stderr);
        } else {
            fprintf(stderr, " x%d=%llx", i, v);
        }
    }
    fputc('\n', stderr);
    (void)fmt;
}

/* ---------------------------------------------------------------------------
 * g2s_trace_semtab -- dump the semantic descriptor tables.
 *
 * `f_7100f26080` (BindSemantic_HAL, named by its own diagnostic) is what gives
 * an interface symbol the (kind, slot) pair that the GLASM printer later turns
 * into `vertex.attrib[3]` or `result.color`.  It gets that pair out of a table
 * it searches by name id -- `f_7100f2aeb0`, a linear scan over 28-byte records
 * at ctx[1064] + 2360 with the count at + 2368 -- and that table is built from
 * five 56-byte static tables at ctx[1064] + 2376, +2392, +2408, +2424, +2440,
 * counts at + 2384, +2400, +2416, +2432, +2448.
 *
 * Neither table can be reached statically: the profile descriptor at
 * ctx[1064] is assembled at run time, so the pointers do not appear as
 * relocations and grepping for stores to those offsets finds only unrelated
 * stack traffic.  The port makes reading them at run time a printf, which is
 * why this exists.  Dumped once per process; off unless G2S_TRACE is set.
 */
void g2s_trace_semtab(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long ctx, prof, p;
    unsigned n, i, t;
    static const unsigned srcs[5][2] = {
        {2376, 2384}, {2392, 2400}, {2408, 2416}, {2424, 2432}, {2440, 2448}
    };

    if (done)
        return;
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    done = 1;

    ctx = GST_I64(GUEST_OFF_X0);
    prof = ld_i64(ctx + 1064);
    fprintf(stderr, "g2s_semtab from f_%lx ctx=%llx profile=%llx\n",
            addr, ctx, prof);

    p = ld_i64(prof + 2360);
    n = ld_i32(prof + 2368);
    fprintf(stderr, "g2s_semtab merged %u records of 28 at %llx\n", n, p);
    for (i = 0; i < n && i < 4096; ++i) {
        unsigned long long r = p + (unsigned long long)i * 28;
        unsigned j;
        fprintf(stderr, "  m[%4u]", i);
        for (j = 0; j < 28; j += 4)
            fprintf(stderr, " %08x", ld_i32(r + j));
        fputc('\n', stderr);
    }

    for (t = 0; t < 5; ++t) {
        unsigned long long q = ld_i64(prof + srcs[t][0]);
        unsigned m = ld_i32(prof + srcs[t][1]);
        fprintf(stderr, "g2s_semtab src[%u] %u records of 56 at %llx\n",
                t, m, q);
        for (i = 0; i < m && i < 4096; ++i) {
            unsigned long long r = q + (unsigned long long)i * 56;
            char buf[G2S_STR_MAX];
            unsigned j;
            fprintf(stderr, "  s%u[%4u]", t, i);
            for (j = 0; j < 56; j += 4)
                fprintf(stderr, " %08x", ld_i32(r + j));
            for (j = 0; j < 56; j += 8)
                if (g2s_get_string(ld_i64(r + j), buf, sizeof buf))
                    fprintf(stderr, "  +%u=\"%s\"", j, buf);
            fputc('\n', stderr);
        }
    }
    fflush(stderr);
}

/* ---------------------------------------------------------------------------
 * g2s_trace_sym -- the symbol behind each `#var` line.
 *
 * `f_7100bd2370` prints one `#var` line for one symbol, and its third argument
 * (X2) is that symbol.  The binding pair the declaration block later needs is
 * sym[144] (`reg`: slot in the low byte, flags above) and sym[148] (`kind`),
 * neither of which appears in the `#var` line, so there is no way to read them
 * off a listing.  Printing them here, in the SAME ORDER the `#var` lines come
 * out, pairs every line of the listing with its (kind, reg) without needing the
 * symbol's name -- the name is built by the caller and reaching it from here
 * would mean calling back into the guest.
 *
 * The other fields are the ones the printer itself reads: sym[8] is the
 * symbol's own kind (0..2 leaf, 5..7 aggregate), sym[12] the flag word whose
 * bit 8 is the `used` column, sym[24] the -1 that ends every line, sym[28] the
 * type code that `f_710000415c0` turns into `float4`, and sym[36] the array
 * length.
 */
void g2s_trace_sym(cpu_t *cpu, unsigned long addr)
{
    unsigned long long s;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;

    s = GST_I64(GUEST_OFF_X0 + 8 * 2);
    if (!s)
        return;
    fprintf(stderr, "g2s_sym f_%lx sym=%llx kind8=%u flags=%08x len24=%d "
            "type28=%u arr36=%d reg144=%08x bindkind148=%02x\n",
            addr, s, ld_i32(s + 8), ld_i32(s + 12), (int)ld_i32(s + 24),
            ld_i32(s + 28), (int)ld_i32(s + 36), ld_i32(s + 144),
            ld_i32(s + 148));
}

/* ---------------------------------------------------------------------------
 * g2s_trace_regtab -- which register-name table a profile actually uses.
 *
 * The `#var` line's register column comes from `ctx[120]`'s vtable slot 8
 * (notes/20), and the eighteen candidate tables of notes/07 live in .data with
 * NO code reference: the profile descriptor that picks one is assembled at run
 * time, so scanning the image for a pointer to a table base finds nothing and
 * only the running compiler can say which one a profile gets.
 *
 * This hooks `f_7100041080`, the semantic-column builder, whose X0 is that
 * ctx.  It prints the object at ctx[120], its vtable, and -- for each of the
 * object's first sixteen fields that is a readable pointer to what looks like
 * an array of string pointers -- the first eight names.  A table whose first
 * name is `ATTR0` is one of the eighteen, and its address is the answer.
 *
 * Dumped once per process; off unless G2S_TRACE is set.
 */
/* Read eight bytes of guest memory, or fail, under the same fault guard as
 * g2s_get_string.  An unguarded `ld_i64` on a field that merely LOOKS like a
 * pointer is the mistake notes/11 already records: the third attempt at a
 * tracer died on exactly this, and a plain range test does not decide it. */

/* g2s_want -- per-hook gating.  G2S_TRACE turns tracing on at all; G2S_ONLY,
 * when set, is a comma-separated list of the hooks that may print, so a run can
 * be narrowed to the one instrument a question needs.  Without it every hook
 * prints, which on a real corpus shader is millions of lines and minutes of
 * wall clock (notes/45). */
static int g2s_want(const char *name)
{
    const char *only = getenv("G2S_ONLY");
    size_t n;
    const char *p;

    if (!only || !*only)
        return 1;
    n = strlen(name);
    for (p = only; *p; ) {
        const char *e = strchr(p, ',');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        if (len == n && !memcmp(p, name, n))
            return 1;
        if (!e)
            break;
        p = e + 1;
    }
    return 0;
}

static int g2s_try_u64(unsigned long long v, unsigned long long *out)
{
    struct sigaction sa, old_segv, old_bus;
    int ok = 0;

    if (v < 0x1000 || v > 0x7fffffffffffULL || (v & 7))
        return 0;
    sa.sa_handler = g2s_fault;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    if (sigaction(SIGSEGV, &sa, &old_segv) != 0)
        return 0;
    sigaction(SIGBUS, &sa, &old_bus);
    g2s_in_probe = 1;
    if (sigsetjmp(g2s_fault_jmp, 1) == 0) {
        memcpy(out, (const void *)(uintptr_t)v, sizeof *out);
        ok = 1;
    }
    g2s_in_probe = 0;
    sigaction(SIGSEGV, &old_segv, NULL);
    sigaction(SIGBUS, &old_bus, NULL);
    return ok;
}

void g2s_trace_regtab(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long ctx, obj, f, p;
    int i, j;

    if (done)
        return;
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    done = 1;

    ctx = GST_I64(GUEST_OFF_X0);
    if (!g2s_try_u64(ctx + 120, &obj))
        return;
    fprintf(stderr, "g2s_regtab addr=%lx ctx=%llx obj=%llx\n", addr, ctx, obj);

    /* The object holds the table somewhere in its first few fields, and the
     * table's first entry is a pointer to "ATTR0" (notes/07).  So: for every
     * field that reads, and for every slot of what it points at, look for that
     * name -- the address that has it IS one of the eighteen. */
    for (i = 0; i < 24; ++i) {
        char buf[G2S_STR_MAX];

        if (!g2s_try_u64(obj + 8 * (unsigned)i, &f))
            continue;
        for (j = 0; j < 4; ++j) {
            if (!g2s_try_u64(f + 8 * (unsigned)j, &p))
                break;
            if (!g2s_get_string(p, buf, sizeof buf))
                break;
            if (j == 0)
                fprintf(stderr, "  obj[%2d] = %llx ->", i, f);
            fprintf(stderr, " %s", buf);
        }
        if (j)
            fputc('\n', stderr);
    }

    /* And the table itself, once found by eye, printed in full: any field
     * whose first name is ATTR0 gets 128 entries. */
    for (i = 0; i < 24; ++i) {
        char buf[G2S_STR_MAX];

        if (!g2s_try_u64(obj + 8 * (unsigned)i, &f))
            continue;
        if (!g2s_try_u64(f, &p) || !g2s_get_string(p, buf, sizeof buf) ||
            strcmp(buf, "ATTR0") != 0)
            continue;
        fprintf(stderr, "g2s_regtab TABLE at %llx (obj[%d])\n", f, i);
        for (j = 0; j < 128; ++j) {
            if (!g2s_try_u64(f + 8 * (unsigned)j, &p) ||
                !g2s_get_string(p, buf, sizeof buf))
                break;
            fprintf(stderr, "  [%3d] %s\n", j, buf);
        }
    }
    fflush(stderr);
}

/* ---------------------------------------------------------------------------
 * g2s_trace_vtable -- print the virtual table of the object in X0.
 *
 * The GLASM body printers call each other through vtable slots, not by name:
 * `f_710005c1dc` builds a line with slot 72 (the mnemonic, `f_7100bd61f4`),
 * slot 136 (the destination operand) and slot 152 (a source operand), and none
 * of those three has a reference anywhere in the image (notes/21).  Printing
 * the table once turns three slot numbers into three guest addresses, which is
 * all that is needed to go on reading.
 *
 * Every read is fault-guarded: a vtable slot that is not a function pointer,
 * or an object that is not what this hook assumed, must print as a number
 * rather than kill the compile.
 */
void g2s_trace_vtable(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long obj, vt, p;
    int i;

    if (done)
        return;
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    done = 1;

    obj = GST_I64(GUEST_OFF_X0);
    if (!g2s_try_u64(obj, &vt)) {
        fprintf(stderr, "g2s_vtable f_%lx obj=%llx unreadable\n", addr, obj);
        return;
    }
    fprintf(stderr, "g2s_vtable f_%lx obj=%llx vtable=%llx\n", addr, obj, vt);
    for (i = 0; i < 48; ++i) {
        if (!g2s_try_u64(vt + 8 * (unsigned)i, &p))
            break;
        fprintf(stderr, "  slot %3d (+%3d) = %llx\n", i, i * 8, p);
    }
    fflush(stderr);
}

/* ---------------------------------------------------------------------------
 * g2s_trace_idsets -- what is in the reader's two id sets.
 *
 * `ctx[176]` and `ctx[208]` are hash SETS of SPIR-V `<id>`s (every insert
 * passes the id as both key and value), and they decide whether the reader
 * builds anything at all: a variable whose TYPE is in `ctx[176]` becomes
 * `@skippedVariable.<n>`, and a load or store touching an id in `ctx[208]`
 * produces no node (notes/25).  Which ids they hold cannot be read out of the
 * image -- the type handlers put them there while the module is being read --
 * so this prints both sets at the point the GLASM printer runs, by which time
 * they are complete.
 *
 * A generic map's layout is (count, buckets, ...) with the bucket array at
 * +8; each entry is 40 bytes with the key at +0 (f_7100fce108's allocation
 * uses 40-byte entries and a 5-word stride).  Everything is fault-guarded,
 * because this is a guess about a layout and a wrong guess must print nothing
 * rather than crash the compile.
 */
void g2s_trace_idsets(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long ctx, m, n, buckets, k;
    int which, i;

    if (done)
        return;
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    done = 1;

    ctx = GST_I64(GUEST_OFF_X0);
    for (which = 0; which < 2; ++which) {
        unsigned off = which ? 208 : 176;

        if (!g2s_try_u64(ctx + off, &m) || !m)
            continue;
        if (!g2s_try_u64(m, &n))            /* the count is the first word */
            continue;
        n &= 0xffffffffu;
        if (!g2s_try_u64(m + 8, &buckets))
            continue;
        fprintf(stderr, "g2s_idset ctx[%u] map=%llx size=%llu buckets=%llx\n",
                off, m, n, buckets);
        for (i = 0; i < (int)n && i < 512; ++i) {
            if (!g2s_try_u64(buckets + 40ULL * (unsigned)i, &k))
                break;
            if ((unsigned)k && (unsigned)k != 0xffffffffu)
                fprintf(stderr, "  [%3d] id %u\n", i, (unsigned)k);
        }
    }
    fflush(stderr);
}

/* g2s_trace_node -- dump the IR node a body line is being printed from.
 *
 * `f_710005c1dc` and `f_710005c71c` are the two line printers (notes/23), and
 * the node is their X2.  The printed line and this dump appear in lockstep on
 * stderr and stdout respectively, so one run pairs every GLASM instruction
 * with the node that produced it -- which is the only way to establish the
 * node layout, since the printer reaches its fields through a vtable and the
 * static call graph stops there.
 *
 * Words are read with the fault guard, so a field that is not a valid pointer
 * prints as `--------` rather than killing the run. */
void g2s_trace_node(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("node"))
        return;
    /* stderr.  Writing this to stdout instead, to interleave it with the
     * listing, does NOT work: the guest buffers its listing in guest memory
     * and writes the whole thing at the end, so every node record lands
     * before every line however the host stream is flushed.  The two streams
     * are joined afterwards, by MNEMONIC rather than by position. */
    unsigned long long node = GST_I64(GUEST_OFF_X0 + 8 * 2);
    fprintf(stderr, "g2s_node f_%lx node=%llx", addr, node);
    if (node) {
        /* 384, not 160: the OPERAND SLOTS are at node+0xa8 with a stride of
         * 0x28 -- the destination first, then node[153] sources -- which is
         * what the line printer itself walks (0x5c8f8..0x5c9bc), so a dump
         * that stops at 160 shows no operand at all and one that stops at 256
         * cuts the second source in half (notes/42). */
        for (int off = 0; off < 384; off += 8) {
            unsigned long long w;
            if (g2s_try_u64(node + (unsigned)off, &w))
                fprintf(stderr, " +%d=%llx", off, w);
            else
                fprintf(stderr, " +%d=?", off);
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_node_lr -- the node dump PLUS the return address.
 *
 * The body printer's driver is reached through a computed pointer, like the
 * printers themselves, so the only way to name it is to print the LR of the
 * per-node entry point (notes/30). */
void g2s_trace_node_lr(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("node"))
        return;
    fprintf(stderr, "g2s_nodelr f_%lx lr=%llx node=%llx\n", addr,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 2));
}

/* g2s_trace_bodylist -- dump the whole printed body list, once.
 *
 * `f_7100040a10` is the body printer's driver (notes/31).  Its X1 is the
 * program, and the list it walks is:
 *
 *     program[184] -> [0]        the first BLOCK
 *     block[288]                 the next block
 *     block[32] -> [0]           the first INSTRUCTION of that block
 *     instr[8]                   the next instruction
 *     instr[56]                  the DAG node the line printers take
 *
 * so one hook at the driver's entry can print the list in the order it is
 * about to be printed in, with every instruction's own fields -- which is what
 * says whether the record carries a source position the emitted order could be
 * derived from, or whether the order IS the list.
 */
void g2s_trace_bodylist(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    unsigned long long prog = GST_I64(GUEST_OFF_X0 + 8 * 1), head, blk;
    fprintf(stderr, "g2s_bodylist f_%lx prog=%llx\n", addr, prog);
    if (!g2s_try_u64(prog + 184, &head) || !head)
        return;
    if (!g2s_try_u64(head, &blk))
        return;
    for (int b = 0; blk && b < 512; ++b) {
        unsigned long long ilist, ins;
        fprintf(stderr, "  block %d @%llx\n", b, blk);
        if (g2s_try_u64(blk + 32, &ilist) && ilist
            && g2s_try_u64(ilist, &ins)) {
            for (int i = 0; ins && i < 4096; ++i) {
                fprintf(stderr, "    instr %d @%llx", i, ins);
                for (int off = 0; off < 80; off += 8) {
                    unsigned long long w;
                    if (g2s_try_u64(ins + (unsigned)off, &w))
                        fprintf(stderr, " +%d=%llx", off, w);
                    else
                        fprintf(stderr, " +%d=?", off);
                }
                fputc('\n', stderr);
                if (!g2s_try_u64(ins + 8, &ins))
                    break;
            }
        }
        if (!g2s_try_u64(blk + 288, &blk))
            break;
    }
}

/* g2s_indirect -- log the target of an indirect call in an instrumented
 * function.  The compile driver `f_7100ef56a0` reaches its passes through a
 * vtable (notes/21: the pass list is assembled at run time and has no static
 * reference), so the only way to name them is to print the pointer the call
 * actually goes through. */
void g2s_indirect(unsigned long site, unsigned long long target)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("indirect"))
        return;
    /* The site is printed in HEX because tools/indirect_patch.py passes the
     * call's RETURN ADDRESS as the site (notes/45: vtable slot numbers collide
     * across objects and made two unrelated targets share one site number). */
    fprintf(stderr, "g2s_indirect site=%#lx target=f_%llx\n", site, target);
}

/* g2s_trace_bodycount -- how many body instructions exist right now.
 *
 * The pass pipeline is a vtable walk (notes/21) and the passes were named by
 * logging the indirect-call targets.  To find WHICH of them builds the printed
 * body list, this prints the list's size at each pass's entry, trying both X0
 * and X1 as the program pointer -- the passes do not share a signature.  The
 * pass the count first becomes non-zero after is the one that built it. */
void g2s_trace_bodycount(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("bodycount"))
        return;
    /* Four candidates, not two: the allocator's passes take an allocation
     * CONTEXT whose [8] is the program (`f_710003ba20` passes `x19[8]` to its
     * per-node walker), so the program has to be looked for one level down as
     * well as in the argument itself. */
    for (int a = 0; a < 4; ++a) {
        unsigned long long arg = GST_I64(GUEST_OFF_X0 + 8 * (a & 1)), prog;
        unsigned long long head, blk;
        if (a < 2)
            prog = arg;
        else if (!arg || !g2s_try_u64(arg + 8, &prog))
            continue;
        if (!prog || !g2s_try_u64(prog + 184, &head) || !head)
            continue;
        if (!g2s_try_u64(head, &blk))
            continue;
        int nb = 0, ni = 0, nd = 0;
        for (; blk && nb < 512; ++nb) {
            unsigned long long il, ins, e;
            if (g2s_try_u64(blk + 32, &il) && il && g2s_try_u64(il, &ins))
                for (; ins && ni < 100000; ++ni)
                    if (!g2s_try_u64(ins + 8, &ins))
                        break;
            /* block[80] is the chain of DAG statements: e[32] is the node
             * (`f_710004a2e0`'s init loop).  Counting it says WHEN the DAG
             * itself comes into existence, which the instruction count does
             * not. */
            if (g2s_try_u64(blk + 80, &e))
                for (; e && nd < 100000; ++nd)
                    if (!g2s_try_u64(e, &e))
                        break;
            if (!g2s_try_u64(blk + 288, &blk))
                break;
        }
        if (nb)
            fprintf(stderr,
                    "g2s_bodycount f_%lx arg%d blocks=%d instrs=%d dag=%d\n",
                    addr, a, nb, ni, nd);
    }
}

/* g2s_trace_dag -- the DAG as it stands BEFORE the instruction list is built.
 *
 * `f_7100036760` (the first per-block pass of the allocator driver
 * `f_710003ba20`) reads two per-block lists, `block[80]` and `block[72]`, each
 * a singly-linked chain whose `[0]` is the next link and whose `[16]` is the
 * object.  Those are the statement roots: at that point `block[32]`'s
 * instruction list is still empty, so this is the shape the SPIR-V reader
 * left behind, before scheduling and register allocation touch it. */
void g2s_trace_dag(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    for (int a = 0; a < 2; ++a) {
        unsigned long long prog = GST_I64(GUEST_OFF_X0 + 8 * a), head, blk;
        if (!prog || !g2s_try_u64(prog + 184, &head) || !head)
            continue;
        if (!g2s_try_u64(head, &blk) || !blk)
            continue;
        fprintf(stderr, "g2s_dag f_%lx prog=%llx\n", addr, prog);
        for (int b = 0; blk && b < 512; ++b) {
            fprintf(stderr, "  block %d @%llx\n", b, blk);
            for (int which = 0; which < 2; ++which) {
                unsigned long long link;
                if (!g2s_try_u64(blk + (which ? 72 : 80), &link))
                    continue;
                for (int i = 0; link && i < 4096; ++i) {
                    unsigned long long obj;
                    if (!g2s_try_u64(link + 16, &obj))
                        break;
                    fprintf(stderr, "    list%d[%d] link=%llx obj=%llx",
                            which, i, link, obj);
                    for (int off = 0; obj && off < 160; off += 8) {
                        unsigned long long w;
                        if (g2s_try_u64(obj + (unsigned)off, &w))
                            fprintf(stderr, " +%d=%llx", off, w);
                    }
                    fputc('\n', stderr);
                    if (!g2s_try_u64(link, &link))
                        break;
                }
            }
            if (!g2s_try_u64(blk + 288, &blk))
                break;
        }
        return;
    }
}

/* g2s_trace_alloc -- the arena allocator's size and caller.
 *
 * The GLASM DAG nodes are 160+ bytes and are built by a constructor that has
 * no static reference (the class vtable is heap-allocated, notes/21).  Logging
 * the allocation size and the return address names the constructor, and from
 * there the function that chooses the node's opcode. */
void g2s_trace_alloc(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("alloc"))
        return;
    /* The arena allocator takes (arena, size) and the small allocator takes
     * (size, arena), so both argument positions are checked. */
    unsigned long long a0 = GST_I64(GUEST_OFF_X0) & 0xffffffff;
    unsigned long long a1 = GST_I64(GUEST_OFF_X0 + 8) & 0xffffffff;
    unsigned long long size = (a0 >= 120 && a0 <= 400) ? a0 : a1;
    if (size < 120 || size > 400)
        return;
    fprintf(stderr, "g2s_alloc f_%lx size=%llu lr=%llx\n", addr, size,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
}

/* g2s_trace_proto -- the DAG node constructor's PROTOTYPE argument.
 *
 * `f_710004f770(cg, proto, mask)` copies the node's opcode from `proto[0]`,
 * its modifier from `proto[4]`, and the scheduler's first three keys from
 * `proto[20]`, `proto[22]` and `proto[24]` (0x710004f7bc..0x710004f7f0).  So
 * every GLASM instruction the compiler builds is described by one of these
 * prototype objects, and dumping them with the caller's return address is
 * what turns "which mnemonic does this SPIR-V opcode get" from a guess into
 * a lookup. */
void g2s_trace_proto(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("proto"))
        return;
    unsigned long long proto = GST_I64(GUEST_OFF_X0 + 8);
    unsigned long long lr = GST_I64(GUEST_OFF_X0 + 8 * 30);
    fprintf(stderr, "g2s_proto lr=%llx proto=%llx", lr, proto);
    if (proto) {
        for (int off = 0; off < 40; off += 4) {
            unsigned long long w;
            if (g2s_try_u64(proto + (unsigned)(off & ~7), &w))
                fprintf(stderr, " +%d=%x", off,
                        (unsigned)((off & 4) ? (w >> 32) : w));
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_alloc_ret -- the pointer an allocation returned, with the caller.
 *
 * Logging the SIZE and the caller at entry is not enough to say which
 * function built a given node: the node addresses are known only at print
 * time (`g2s_trace_node`).  Printing the RETURNED pointer lets the two logs
 * be joined, which names the constructor of any node in the listing without
 * guessing. */
void g2s_trace_alloc_ret(cpu_t *cpu, unsigned long lr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("alloc"))
        return;
    fprintf(stderr, "g2s_allocret ptr=%llx lr=%lx\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0), lr);
}

/* g2s_trace_factory -- the instruction FACTORY vtable.
 *
 * `f_7100f0972c` builds a GLASM node by calling `x0[0][1064]`'s vtable slot
 * 840; the node's opcode is not an argument, so each operation has its own
 * factory slot and the slot's implementation writes the opcode.  That vtable
 * is the compiler's own operation-to-opcode map, built at run time like every
 * other table in this compiler (notes/21), so it can only be read here.
 * Dumping it once gives every slot's target, which then disassembles to a
 * single `mov w, #opcode`. */
void g2s_trace_factory(cpu_t *cpu, unsigned long addr)
{
    static int done;
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || done)
        return;
    /* `f_7100f0972c` does `ldr x0,[x0]; ldr x0,[x0,#1064]; ldr x8,[x0,#840];
     * blr x8` -- the factory is a STRUCT OF FUNCTION POINTERS and 840 is a
     * byte offset into it, not a vtable slot.  So the table is read directly
     * rather than through a vtable pointer. */
    unsigned long long cg = GST_I64(GUEST_OFF_X0), obj, fac, f;
    if (!g2s_try_u64(cg, &obj) || !g2s_try_u64(obj + 1064, &fac) || !fac)
        return;
    done = 1;
    fprintf(stderr, "g2s_factory f_%lx factory=%llx\n", addr, fac);
    for (int i = 0; i < 512; ++i)
        if (g2s_try_u64(fac + 8 * (unsigned)i, &f)
            && f >= 0x7100000000ULL && f < 0x7102000000ULL)
            fprintf(stderr, "  +%d = f_%llx\n", i * 8, f);
}

/* g2s_trace_ctor_ret -- the node a class constructor returned, and its +8.
 *
 * `f_710004e174` zeroes `+8` and `f_710004fa10` copies it from a prototype
 * only when one is passed (it is not, here), yet the printed node carries an
 * opcode there.  Printing `+8` at the constructor's RETURN says whether the
 * constructor set it after all or whether the caller does. */
void g2s_trace_ctor_ret(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    unsigned long long n = GST_I64(GUEST_OFF_X0), w = 0;
    g2s_try_u64(n + 8, &w);
    fprintf(stderr, "g2s_ctorret f_%lx node=%llx +8=%llx\n", addr, n, w);
}

/* g2s_opcode_watch -- when do the DAG nodes get their opcodes?
 *
 * `f_710004f770` ends with `node[72] = cg[1096]; cg[1096] = node`, so every
 * node the code generator builds is on one list threaded through `+72` and
 * headed at `cg[1096]`.  `g2s_note_cg` captures that `cg` the first time a
 * constructor runs; `g2s_trace_opcodes` then walks the list from anywhere and
 * reports how many nodes have a non-zero `+8`.  The pass the count jumps at
 * is the one that assigns opcodes -- which is the link notes/32 is missing. */
static unsigned long long g2s_cg;

void g2s_note_cg(cpu_t *cpu)
{
    /* Keep the LATEST context, not the first: the cgc node list and the GLASM
     * one are built by the same classes, and pinning the first made the
     * histogram follow the wrong list (notes/46). */
    unsigned long long v = GST_I64(GUEST_OFF_X0);
    if (v)
        g2s_cg = v;
}

void g2s_trace_opcodes(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_cg)
        return;
    if (!g2s_want("opcodes"))
        return;
    unsigned long long n, w;
    int total = 0, withop = 0, i, nhist = 0;
    unsigned hist[64], cnt[64];
    if (!g2s_try_u64(g2s_cg + 1096, &n))
        return;
    for (; n && total < 20000; ++total) {
        if (g2s_try_u64(n + 8, &w) && (w & 0xffffffff)) {
            unsigned o = (unsigned)(w & 0xffffffff);
            ++withop;
            for (i = 0; i < nhist && hist[i] != o; ++i)
                ;
            if (i == nhist && nhist < 64) {
                hist[nhist] = o;
                cnt[nhist++] = 0;
            }
            if (i < 64)
                ++cnt[i];
        }
        if (!g2s_try_u64(n + 72, &n))
            break;
    }
    fprintf(stderr, "g2s_opcodes f_%lx nodes=%d withop=%d |", addr,
            total, withop);
    for (i = 0; i < nhist; ++i)
        fprintf(stderr, " %#x*%u", hist[i], cnt[i]);
    fputc('\n', stderr);
}

/* g2s_trace_opmap -- f_7100f28a00 takes an operation code in W1 and an
 * out-pointer in X8, and its body is one big switch that ends in
 * `mov w27, <constant>` with the constants 0x47, 0x90, 0x18 ... exactly the
 * values seen in node[8].  This prints the code it was given and the object it
 * was given to fill, so the two can be joined to g2s_trace_node's addresses. */
void g2s_trace_opmap(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    fprintf(stderr, "g2s_opmap f_%lx op=%#llx x2=%#llx x3=%#llx x4=%#llx "
            "x5=%#llx x8=%#llx\n", addr,
            (unsigned long long)(GST_I64(GUEST_OFF_X0 + 8 * 1) & 0xffffffff),
            (unsigned long long)(GST_I64(GUEST_OFF_X0 + 8 * 2) & 0xffffffff),
            (unsigned long long)(GST_I64(GUEST_OFF_X0 + 8 * 3) & 0xffffffff),
            (unsigned long long)(GST_I64(GUEST_OFF_X0 + 8 * 4) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 5),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 8));
}

/* g2s_trace_setop -- f_710004f530 is the node's field-group WRITER: it copies a
 * 36-byte temp back into the node, and temp[0] lands in node[8], the GLASM
 * opcode (the matching reader f_710004fe90 copies node[8] out to temp[0]).
 * Patched onto that one function, this prints EVERY opcode assignment in the
 * whole run together with the caller that made it, so the assigning pass is
 * named without bisecting passes. */
void g2s_trace_setop(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("setop"))
        return;
    unsigned long long node = GST_I64(GUEST_OFF_X0);
    unsigned long long tmp  = GST_I64(GUEST_OFF_X0 + 8 * 1);
    unsigned long long lr   = GST_I64(GUEST_OFF_X0 + 8 * 30);
    unsigned long long nv = 0, ov = 0;
    (void)addr;
    /* The temp is only 4-aligned, which g2s_try_u64 rejects, so the value
     * about to be written is read 8 bytes below and shifted into place. */
    if (tmp & 7)
        g2s_try_u64(tmp - 4, &nv), nv >>= 32;
    else
        g2s_try_u64(tmp, &nv);
    g2s_try_u64(node + 8, &ov);
    fprintf(stderr, "g2s_setop lr=%#llx node=%#llx %#llx -> %#llx\n",
            lr, node, (unsigned long long)(ov & 0xffffffff),
            (unsigned long long)(nv & 0xffffffff));
}

/* g2s_trace_mknode -- the IR builder's "make a node with this GLASM opcode"
 * helpers (f_7100f08c78, f_7100f0972c, ... : each takes the opcode in W1 and
 * the write mask in W2, allocates from a different instruction-factory slot,
 * and stores W1 into node[8] through f_710004f530).  Printing W1, W2 and the
 * RETURN ADDRESS names the worker that chose the opcode. */
void g2s_trace_mknode(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("mknode"))
        return;
    fprintf(stderr, "g2s_mknode f_%lx lr=%#llx op=%#llx mask=%#llx", addr,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30),
            (unsigned long long)(GST_I64(GUEST_OFF_X0 + 8 * 1) & 0xffffffff),
            (unsigned long long)(GST_I64(GUEST_OFF_X0 + 8 * 2) & 0xffffffff));
    /* AND THE POSITION THE NEW NODE IS ABOUT TO BE STAMPED WITH.
     *
     * `f_7100f0972c(cg, opcode, type)` reads `cg[64]` and `cg[72]` at
     * 0xf09794..0xf09798 into the field group that becomes `node[28..35]` and
     * `node[36]` (notes/40).  `node[36]` is the scheduler's second key, and
     * §8's ledger has it as the one input still taken from the compiler, so
     * this prints it at the moment it is read -- and arms the write watch on
     * `cg + 72` the first time through, so the next change to it is
     * attributed to whatever made it.  Only this constructor is used for the
     * arming: the other `mknode` workers take a different first argument.
     */
    /* EVERY factory reads the position the same way -- `f_7100f08c78` at
     * 0xf08cd8 does `x23 = cg[64]; w24 = cg[72]` exactly as `f_7100f0972c`
     * does at 0xf09794 -- and which one runs depends on the shader, so the
     * arming must not be tied to one of them.  `op_mul.vert` never reaches
     * `f_7100f0972c`'s position read at all: its nodes come from
     * `f_7100f08c78`, which is why arming there caught nothing. */
    if (addr == 0x7100f0972cUL || addr == 0x7100f08c78UL
        || addr == 0x7100f09154UL || addr == 0x7100f0a210UL) {
        unsigned long long cg = GST_I64(GUEST_OFF_X0), w = 0;
        if (cg && !(cg & 7)) {
            if (!g2s_watch_addr && getenv("G2S_WATCH_POS")) {
                g2s_watch_addr = cg + 72;
                fprintf(stderr, "\ng2s_armed cg=%#llx watch=%#llx f_%lx\n",
                        cg, (unsigned long long)g2s_watch_addr, addr);
            }
            if (g2s_try_u64(cg + 64, &w))
                fprintf(stderr, " cg64=%#llx", w);
            if (g2s_try_u64(cg + 72, &w))
                fprintf(stderr, " pos=%u", (unsigned)(w & 0xffffffff));
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_desc -- f_7100f13410 is the generic "emit one instruction" step:
 * its X2 is a DESCRIPTOR whose word 0 is the GLASM opcode it hands the builder
 * (f_7100f0972c) and whose later words carry the node's secondary fields.
 * This prints the descriptor's address, its first six words and the caller, so
 * the descriptor's own origin can be followed. */
void g2s_trace_desc(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("desc"))
        return;
    unsigned long long d = GST_I64(GUEST_OFF_X0 + 8 * 2), w[3] = {0, 0, 0};
    int i;
    for (i = 0; i < 3; ++i)
        g2s_try_u64(d + 8 * (unsigned)i, &w[i]);
    fprintf(stderr, "g2s_desc f_%lx lr=%#llx desc=%#llx"
            " w0=%#llx w1=%#llx w2=%#llx w3=%#llx w4=%#llx w5=%#llx\n", addr,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30), d,
            w[0] & 0xffffffff, w[0] >> 32, w[1] & 0xffffffff, w[1] >> 32,
            w[2] & 0xffffffff, w[2] >> 32);
}

/* g2s_trace_irop -- f_7100f10a30 is the master "emit GLASM for one cgc IR node"
 * step: it reads the node's operation from (node[16] >> 16) and switches on it
 * through a 0xca-entry halfword jump table, each arm supplying the GLASM opcode
 * and a type-variant index.  This prints the node and that operation so the
 * operation numbering can be joined to what the SPIR-V workers build. */
void g2s_trace_irop(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("irop"))
        return;
    unsigned long long n = GST_I64(GUEST_OFF_X0 + 8 * 1), w = 0;
    g2s_try_u64(n + 16, &w);
    fprintf(stderr, "g2s_irop f_%lx lr=%#llx node=%#llx f16=%#llx op=%#llx\n",
            addr, (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30), n,
            w & 0xffffffff, (w & 0xffffffff) >> 16);
    /* A call node (operation 0x36 plus its operand-form variant) carries the
     * CALLEE SYMBOL at node[48][40], and the emitter takes the builtin id from
     * that symbol's [32] (notes/39).  Print the symbol so the table that owns
     * it can be found. */
    if (((w & 0xffffffff) >> 16) >= 0x36 && ((w & 0xffffffff) >> 16) <= 0x39) {
        unsigned long long a, sym, v;
        char nb[G2S_STR_MAX];
        int j;

        if (g2s_try_u64(n + 48, &a) && g2s_try_u64(a + 40, &sym)) {
            fprintf(stderr, "g2s_callee sym=%#llx |", sym);
            {   /* follow the fields that could be a BODY (notes/39): print
                 * what each points at so an inlined builtin can be told from
                 * one that lowers to its opcode. */
                unsigned long long q, r;
                int f;
                for (f = 0; f < 3; ++f) {
                    static const int off[3] = { 80, 184, 192 };
                    if (!g2s_try_u64(sym + off[f], &q) || !q)
                        continue;
                    fprintf(stderr, " <%d>", off[f]);
                    if (g2s_try_u64(q, &r))
                        fprintf(stderr, "[0]=%#llx", r);
                    if (g2s_try_u64(q + 8, &r))
                        fprintf(stderr, "[8]=%#llx", r);
                }
            }
            for (j = 0; j < 32; ++j) {
                if (!g2s_try_u64(sym + 8 * (unsigned)j, &v))
                    break;
                if (!v)
                    continue;
                fprintf(stderr, " [%d]=%#llx", j * 8, v);
                if (g2s_get_string(v, nb, sizeof nb))
                    fprintf(stderr, "(\"%s\")", nb);
            }
            fputc('\n', stderr);
        }
    }
}

/* g2s_dump_builtins -- cgc's builtin table is built at run time (notes/21,
 * notes/39), so the only way to read the NAME -> BUILTIN ID map that
 * `OpExtInst` needs is to dump the table itself once, the way notes/07 dumped
 * the register tables.  `f_7100f457b0` holds it: x20 is the profile (its X2, or
 * cg[1416] when X2 is null) and x20[96] is an open-addressed hash map whose
 * word 0 is the capacity and whose policy object is at +40.  This prints the
 * map's header so the entry array can be located, then stops. */
void g2s_dump_builtins(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long prof, map, w;
    int i;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || done)
        return;
    prof = GST_I64(GUEST_OFF_X0 + 8 * 2);
    if (!prof && !g2s_try_u64(GST_I64(GUEST_OFF_X0) + 1416, &prof))
        return;
    if (!g2s_try_u64(prof + 96, &map) || !map)
        return;
    done = 1;
    /* The X1 symbol is the interned NAME the OpExtInst worker looked up; print
     * its first fields and its string so the global table that owns it can be
     * found (notes/39). */
    {
        unsigned long long sym = GST_I64(GUEST_OFF_X0 + 8 * 1), v, p8;
        char nb[G2S_STR_MAX];
        int j;

        fprintf(stderr, "g2s_namesym sym=%#llx |", sym);
        for (j = 0; j < 10; ++j) {
            if (!g2s_try_u64(sym + 8 * (unsigned)j, &v))
                break;
            fprintf(stderr, " [%d]=%#llx", j * 8, v);
            if (g2s_get_string(v, nb, sizeof nb))
                fprintf(stderr, "(\"%s\")", nb);
        }
        fputc('\n', stderr);
        if (g2s_try_u64(sym + 8, &p8) && g2s_get_string(p8, nb, sizeof nb))
            fprintf(stderr, "g2s_namesym name=\"%s\"\n", nb);
    }
    {
        unsigned long long cap = 0, arr, e, f, nameptr;
        int slot, k;
        char buf[G2S_STR_MAX];

        g2s_try_u64(map, &cap);
        cap &= 0xffffffff;
        fprintf(stderr, "g2s_builtins map=%#llx cap=%llu\n", map, cap);
        /* The two arrays at +48 and +56 are the open-addressing storage; the
         * equality function f_7100f45250 compares key[4], so an entry's name
         * id is the word at +4 of whatever it points at. */
        for (k = 0; k < 2; ++k) {
            if (!g2s_try_u64(map + (k ? 56 : 48), &arr) || !arr)
                continue;
            for (slot = 0; slot < (int)cap && slot < 4096; ++slot) {
                if (!g2s_try_u64(arr + 8 * (unsigned)slot, &e) || !e)
                    continue;
                f = 0;
                g2s_try_u64(e, &f);
                fprintf(stderr, "g2s_builtin arr%d[%d] e=%#llx id=%#llx"
                        " w32=%#llx", k, slot, e,
                        (unsigned long long)((f >> 32) & 0xffffffff), 0ULL);
                if (g2s_try_u64(e + 8, &nameptr) &&
                    g2s_get_string(nameptr, buf, sizeof buf))
                    fprintf(stderr, " name=\"%s\"", buf);
                fputc('\n', stderr);
            }
        }
    }
}

/* g2s_dump_names -- dump cgc's NAME INTERNER whole.
 *
 * notes/39: `f_7100fd1ee0` resolves an OpExtInst by handing the extended set's
 * name string to `cg[2080]`'s first virtual method, and the number that comes
 * back is the builtin id `f_7100f28a00` switches on.  `f_7100f3b030` uses the
 * same object the other way round -- `[[cg[2080]] + 8](table, id)` returns the
 * name for an id -- so the whole id -> name table can be read by calling that
 * method for every id, which is a TABLE DUMP and not a per-shader measurement.
 *
 * The call is made through the port's own dispatcher with the guest registers
 * set up by hand, and the guest's register file is saved and restored around
 * the sweep so the compilation it interrupts is unaffected.
 */
void guest_dispatch(cpu_t *cpu, uint64_t p);

void g2s_dump_names(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long cg, table, vt, method, saved[32];
    int id, i;
    char nb[G2S_STR_MAX];

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || done)
        return;
    cg = GST_I64(GUEST_OFF_X0);
    if (!g2s_try_u64(cg + 2080, &table) || !table)
        return;
    if (!g2s_try_u64(table, &vt) || !g2s_try_u64(vt + 8, &method))
        return;
    /* only a real guest code address is worth calling */
    if (method < 0x7100000000ULL || method > 0x7101ffffffULL)
        return;
    done = 1;
    for (i = 0; i < 32; ++i)
        saved[i] = GST_I64(GUEST_OFF_X0 + 8 * (unsigned)i);
    fprintf(stderr, "g2s_names table=%#llx method=f_%llx\n", table, method);
    for (id = 0; id < 0x1200; ++id) {
        GST_I64(GUEST_OFF_X0) = table;
        GST_I64(GUEST_OFF_X0 + 8) = (unsigned long long)id;
        guest_dispatch(cpu, method);
        if (g2s_get_string(GST_I64(GUEST_OFF_X0), nb, sizeof nb) && nb[0])
            fprintf(stderr, "g2s_name %#x %s\n", id, nb);
    }
    for (i = 0; i < 32; ++i)
        GST_I64(GUEST_OFF_X0 + 8 * (unsigned)i) = saved[i];
    fprintf(stderr, "g2s_names done\n");
}

/* g2s_dump_extset -- dump the extended instruction set's NAME table.
 *
 * `f_7100fd1ee0` turns an OpExtInst into a named intrinsic with
 *
 *     fd216c:  x27 = ctx[120][set_index]      ; a char *
 *
 * so `ctx[120]` is an array of the set's instruction names indexed by the
 * GLSL.std.450 number.  Dumping it gives the last link of notes/39's chain:
 * number -> name -> (notes/name_ids.json) builtin id -> (notes/builtin_opcodes)
 * GLASM opcode.  It is a table dump, not a per-shader measurement.
 */
void g2s_dump_extset(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long ctx, arr, p;
    int i;
    char nb[G2S_STR_MAX];

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || done)
        return;
    ctx = GST_I64(GUEST_OFF_X0);
    if (!g2s_try_u64(ctx + 120, &arr) || !arr)
        return;
    done = 1;
    fprintf(stderr, "g2s_extset arr=%#llx\n", arr);
    for (i = 0; i < 512; ++i) {
        if (!g2s_try_u64(arr + 8 * (unsigned)i, &p) || !p)
            continue;
        if (g2s_get_string(p, nb, sizeof nb) && nb[0])
            fprintf(stderr, "g2s_ext %d %s\n", i, nb);
    }
    fprintf(stderr, "g2s_extset done\n");
}

/* g2s_dump_builtin_kinds -- dump, for every interned name id, the cgc builtin
 * symbol it names and the two fields that decide what an OpExtInst becomes.
 *
 * notes/39: `f_7100f3f910` looks the builtin up with
 * `f_7100f45c60(cg, id, scope)`, walking the scope chain through `+16`, and
 * then tests the symbol's KIND:
 *
 *     f3fa0c:  if (sym[0] == 3)  -> it has an overload list at sym[80]
 *              else              -> it is an intrinsic
 *
 * and an intrinsic is what carries the builtin id at sym[32] and the class at
 * sym[224] that f_7100f28a00 turns into a GLASM opcode.  A kind-3 symbol is
 * INLINED instead, which is why `sign` never becomes a call node.  Calling the
 * lookup for every id therefore gives the whole "is this builtin inlined"
 * table, which is what the emitter needs and what cannot be measured one
 * builtin at a time.
 */
void f_7100f45c60(cpu_t *cpu, uint64_t entry);

void g2s_dump_builtin_kinds(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long cg, scopes[8], sym, v0, v32, v224, saved[32];
    int nsc = 0, id, i, s;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || done)
        return;
    cg = GST_I64(GUEST_OFF_X0);
    if (!g2s_try_u64(cg + 1432, &scopes[0]) || !scopes[0])
        if (!g2s_try_u64(cg + 1416, &scopes[0]) || !scopes[0])
            return;
    nsc = 1;
    while (nsc < 8 && g2s_try_u64(scopes[nsc - 1] + 16, &v0) && v0)
        scopes[nsc++] = v0;
    done = 1;
    for (i = 0; i < 32; ++i)
        saved[i] = GST_I64(GUEST_OFF_X0 + 8 * (unsigned)i);
    fprintf(stderr, "g2s_kinds cg=%#llx scopes=%d\n", cg, nsc);
    for (id = 0; id < 0x1200; ++id) {
        for (s = 0; s < nsc; ++s) {
            GST_I64(GUEST_OFF_X0) = cg;
            GST_I64(GUEST_OFF_X0 + 8) = (unsigned long long)id;
            GST_I64(GUEST_OFF_X0 + 16) = scopes[s];
            f_7100f45c60(cpu, 0);
            sym = GST_I64(GUEST_OFF_X0);
            if (sym)
                break;
        }
        if (!sym)
            continue;
        v0 = v32 = v224 = 0;
        g2s_try_u64(sym, &v0);
        g2s_try_u64(sym + 32, &v32);
        g2s_try_u64(sym + 224, &v224);
        fprintf(stderr, "g2s_kind %#x kind=%llu id=%#llx class=%#llx", id,
                (unsigned long long)(v0 & 0xffffffff),
                (unsigned long long)(v32 & 0xffffffff),
                (unsigned long long)(v224 & 0xffff));
        /* kind 3 is an OVERLOAD SET: sym[80] heads the list.  Print each
         * overload's kind, builtin id and class, and the field that holds a
         * body, so "intrinsic" can be told from "inlined" (notes/39). */
        if ((v0 & 0xffffffff) == 3) {
            unsigned long long ov, k, oid, ocl, body;
            int n;

            if (g2s_try_u64(sym + 80, &ov) && ov &&
                g2s_try_u64(ov, &ov) && ov) {
                for (n = 0; n < 12 && ov; ++n) {
                    k = oid = ocl = body = 0;
                    g2s_try_u64(ov, &k);
                    g2s_try_u64(ov + 32, &oid);
                    g2s_try_u64(ov + 224, &ocl);
                    g2s_try_u64(ov + 184, &body);
                    fprintf(stderr, " | ov%d k=%llu id=%#llx cl=%#llx b=%#llx",
                            n, (unsigned long long)(k & 0xffffffff),
                            (unsigned long long)(oid & 0xffffffff),
                            (unsigned long long)(ocl & 0xffff), body);
                    if (!g2s_try_u64(ov + 16, &ov))
                        break;
                }
            }
        }
        fputc('\n', stderr);
    }
    for (i = 0; i < 32; ++i)
        GST_I64(GUEST_OFF_X0 + 8 * (unsigned)i) = saved[i];
    fprintf(stderr, "g2s_kinds done\n");
}

/* g2s_dump_opnames -- dump the GLASM opcode NAMER whole.
 *
 * notes/19 decoded the three namer functions statically.  Four opcodes came
 * back unnamed (0x63, 0x6c, 0x85, 0xa7) because their arms build the name in a
 * way the static walker does not follow.  The namer is an ordinary function --
 * `f_710005b5fc(x0, x1, x2, x3 = buffer)` through the chain -- so calling it
 * for every opcode from a hook settles them without a decoder.
 */
void f_7100bd5734(cpu_t *cpu, uint64_t entry);

void g2s_dump_opnames(cpu_t *cpu, unsigned long addr)
{
    static int done;
    unsigned long long saved[32], buf;
    int op, i;
    char nb[G2S_STR_MAX];

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || done)
        return;
    if (!g2s_want("opnames"))
        return;
    done = 1;
    for (i = 0; i < 32; ++i)
        saved[i] = GST_I64(GUEST_OFF_X0 + 8 * (unsigned)i);
    /* a scratch buffer of our own: the port runs the guest in this address
     * space, so a static array is addressable by guest code.  (An earlier
     * version used the guest stack below the frame and corrupted it.) */
    {
        static char g2s_namebuf[512];
        buf = (unsigned long long)(uintptr_t)g2s_namebuf;
    }
    fprintf(stderr, "g2s_opnames buf=%#llx\n", buf);
    for (op = 0; op < 0x240; ++op) {
        GST_I64(GUEST_OFF_X0) = saved[0];
        GST_I64(GUEST_OFF_X0 + 8) = saved[1];
        GST_I64(GUEST_OFF_X0 + 16) = (unsigned long long)op;
        GST_I64(GUEST_OFF_X0 + 24) = buf;
        *(char *)(uintptr_t)buf = 0;
        f_7100bd5734(cpu, 0);
        if (g2s_get_string(buf, nb, sizeof nb) && nb[0])
            fprintf(stderr, "g2s_opname %#x %s\n", op, nb);
    }
    for (i = 0; i < 32; ++i)
        GST_I64(GUEST_OFF_X0 + 8 * (unsigned)i) = saved[i];
    fprintf(stderr, "g2s_opnames done\n");
}

/* g2s_trace_setmask -- f_710004f580 is the writer of the SECOND field group,
 * whose temp+4 is node[48], the destination WRITE MASK (notes/29).  Patched
 * onto that one function this prints every mask assignment with the caller
 * that made it, the same trick that found the opcode assignment (notes/33). */
void g2s_trace_setmask(cpu_t *cpu, unsigned long addr)
{
    unsigned long long node, tmp, lr, nv = 0, ov = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("setmask"))
        return;
    (void)addr;
    node = GST_I64(GUEST_OFF_X0);
    tmp = GST_I64(GUEST_OFF_X0 + 8 * 1);
    lr = GST_I64(GUEST_OFF_X0 + 8 * 30);
    if ((tmp + 4) & 7)
        g2s_try_u64(tmp, &nv), nv >>= 32;
    else
        g2s_try_u64(tmp + 4, &nv);
    if (node & 7)
        ov = 0;
    else if (g2s_try_u64(node + 48, &ov))
        ov &= 0xffffffff;
    fprintf(stderr, "g2s_setmask lr=%#llx node=%#llx %#llx -> %#llx\n", lr,
            node, ov, nv & 0xffffffff);
}

/* g2s_dump_vregs -- dump the allocator's records.
 *
 * notes/31 and notes/45: `program[808]` is the number of virtual registers and
 * `program[816]` is an array of 224-byte records, one per vreg, which is what
 * `f_710003b310` colours and what the printer reads a register NAME out of.
 * Patched onto the driver `f_710003ba20` (whose X1 is the program) this prints
 * every record's first words before and after colouring, which is the only way
 * to see what the colouring actually decided without reading 2,124 lines
 * first. */
void g2s_dump_vregs(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, n = 0, arr, w;
    int i, j;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("vregs"))
        return;
    /* The program is reached differently from each step -- `[X0]+8` for
     * f_710003bb10, X1 for f_710003ba20 -- so try the candidates and take the
     * first that looks like a program (a sane vreg count and a record array). */
    {
        unsigned long long cand[3], a2, n2;
        int c;

        prog = 0;
        cand[0] = 0;
        g2s_try_u64(GST_I64(GUEST_OFF_X0) + 8, &cand[0]);
        cand[1] = GST_I64(GUEST_OFF_X0 + 8 * 1);
        cand[2] = GST_I64(GUEST_OFF_X0);
        for (c = 0; c < 3; ++c) {
            if (!cand[c] || (cand[c] & 7))
                continue;
            if (!g2s_try_u64(cand[c] + 808, &n2))
                continue;
            n2 &= 0xffffffff;
            if (!n2 || n2 > 4096)
                continue;
            if (!g2s_try_u64(cand[c] + 816, &a2) || !a2 || (a2 & 7))
                continue;
            prog = cand[c];
            break;
        }
        if (!prog)
            prog = g2s_prog_seen;        /* fall back on the last one found */
        else
            g2s_prog_seen = prog;
        if (!prog)
            return;
    }
    if (!g2s_try_u64(prog + 808, &n))
        return;
    n &= 0xffffffff;
    if (!g2s_try_u64(prog + 816, &arr) || !arr || n > 4096)
        return;
    /* COMPACT: only record[64], the allocated register offset (notes/45), so
     * this can be patched onto every pass and the moment it stops being -1
     * names the phase that colours. */
    fprintf(stderr, "g2s_vregs f_%lx n=%llu |", addr, n);
    for (i = 0; i < (int)n; ++i) {
        if (!g2s_try_u64(arr + 224ULL * (unsigned)i + 64, &w))
            break;
        fprintf(stderr, " %d", (int)(w & 0xffffffff));
    }
    fputc('\n', stderr);
    /* THE INTERFERENCE GRAPH ITSELF.  notes/52: `record[0xd8]` is the head of
     * a per-vreg ADJACENCY LIST -- nodes of {int32 vreg index at +0, next at
     * +8} -- built by `f_7100043460` and consulted by `f_7100044e10`, which
     * turns each allocated neighbour's `record[64]` into occupied bits.  So
     * the relation a candidate liveness rule has to reproduce can be READ OFF
     * rather than inferred from which registers came out equal, exactly as
     * `g2s_trace_graph` made the edge builder checkable.
     *
     *   g2s_ifg f_<addr> vreg=<i> reg=<record[64]> | <neighbour> <neighbour>...
     */
    /* `record[0x90]` is the COMPACT conflict list `f_7100043460` builds and
     * then FILTERS into `record[0xd8]`: nodes of {next at +0, int32 index at
     * +8, four 4-bit component masks packed in the u16 at +0xc}.  Dumping it
     * beside the converted form says whether an edge the final graph does not
     * have was never built or was dropped -- which is the difference between
     * a liveness rule and a filter. */
    /* PER-VREG FLAGS.  `f_7100043460`'s dataflow skips a record whose
     * `record[0x9d]` has BIT 3 set (`L_7100043684`), so that byte decides what
     * takes part in liveness at all.  Printed beside the kind, the class and
     * the symbol so a candidate rule for the block-marker injection (notes/52
     * section 7) can be checked against it. */
    if (g2s_want("vflag")) {
        for (i = 0; i < (int)n; ++i) {
            unsigned long long b = arr + 224ULL * (unsigned)i, r = 0;
            unsigned f9d = 0, kind = 0, cls = 0, sym = 0;

            if (g2s_try_u64(b + 0x98, &r))
                f9d = (unsigned)((r >> 40) & 0xff);      /* +0x9d */
            if (g2s_try_u64(b + 0x18, &r)) {
                kind = (unsigned)(r >> 32);              /* +0x1c */
            }
            if (g2s_try_u64(b + 0x10, &r)) {
                sym = (unsigned)(r & 0xffffffff);        /* +0x10 */
                cls = (unsigned)((r >> 48) & 0xffff);    /* +0x16 */
            }
            fprintf(stderr, "g2s_vflag vreg=%d kind=%u cls=%u sym=%u "
                    "f9d=%#x bit3=%u\n", i, kind, cls, sym, f9d,
                    (f9d >> 3) & 1);
        }
    }
    if (g2s_want("ifg0")) {
        for (i = 0; i < (int)n; ++i) {
            unsigned long long b = arr + 224ULL * (unsigned)i, head = 0;
            int guard = 0;

            if (!g2s_try_u64(b + 144, &head) || !head)
                continue;
            fprintf(stderr, "g2s_ifg0 vreg=%d |", i);
            while (head && !(head & 7) && ++guard < 4096) {
                unsigned long long w2 = 0;
                if (!g2s_try_u64(head + 8, &w2))
                    break;
                fprintf(stderr, " %d[%04x]", (int)(unsigned)(w2 & 0xffffffff),
                        (unsigned)((w2 >> 32) & 0xffff));
                if (!g2s_try_u64(head, &head))
                    break;
            }
            fputc('\n', stderr);
        }
    }
    if (g2s_want("ifg")) {
        for (i = 0; i < (int)n; ++i) {
            unsigned long long b = arr + 224ULL * (unsigned)i, head = 0, r = 0;
            int guard = 0;

            if (!g2s_try_u64(b + 216, &head))
                continue;
            if (!head)
                continue;
            g2s_try_u64(b + 64, &r);
            fprintf(stderr, "g2s_ifg f_%lx vreg=%d reg=%d |", addr, i,
                    (int)(unsigned)(r & 0xffffffff));
            while (head && !(head & 7) && ++guard < 4096) {
                unsigned long long w2 = 0, c0 = 0, c2 = 0;
                if (!g2s_try_u64(head, &w2))
                    break;
                /* `f_7100044e10` loops a component counter 0..3 and indexes
                 * this node at `+0x10 + 4*c`, so the edge is recorded PER
                 * COMPONENT -- which is how two vregs that interfere can
                 * still share a register in different components. */
                g2s_try_u64(head + 16, &c0);
                g2s_try_u64(head + 24, &c2);
                fprintf(stderr, " %d[%x,%x,%x,%x]",
                        (int)(unsigned)(w2 & 0xffffffff),
                        (unsigned)(c0 & 0xffffffff), (unsigned)(c0 >> 32),
                        (unsigned)(c2 & 0xffffffff), (unsigned)(c2 >> 32));
                if (!g2s_try_u64(head + 8, &head))
                    break;
            }
            fputc('\n', stderr);
        }
    }
    (void)j;
    /* G2S_WATCH_VREG=<k> arms the write watch on vreg record k's `+64`, the
     * allocated register offset, so the function that COLOURS names itself in
     * a backtrace instead of being guessed from which hook first sees a
     * non -1.  G2S_WATCH_VREG=all watches the whole record array's span. */
    if (!g2s_watch_addr && !g2s_watch_hi) {
        const char *want = getenv("G2S_WATCH_VREG");
        if (want && *want) {
            if (!strcmp(want, "all")) {
                g2s_watch_lo = arr;
                g2s_watch_hi = arr + 224ULL * (unsigned)n;
                fprintf(stderr, "g2s_watch vregs lo=%#llx hi=%#llx n=%llu\n",
                        (unsigned long long)g2s_watch_lo,
                        (unsigned long long)g2s_watch_hi, n);
            } else {
                /* `<k>` watches record k's `+64`; `<k>:<off>` watches any
                 * other field of that record, so the creator of the SYMBOL id
                 * at `+16` can name itself the same way. */
                int k = atoi(want);
                const char *colon = strchr(want, ':');
                int off = colon ? atoi(colon + 1) : 64;
                if (k >= 0 && k < (int)n) {
                    g2s_watch_addr = arr + 224ULL * (unsigned)k
                                     + (unsigned)off;
                    fprintf(stderr, "g2s_watch vreg %d+%d addr=%#llx\n", k,
                            off, (unsigned long long)g2s_watch_addr);
                }
            }
        }
    }
}

/* g2s_trace_defsrc -- is the thing `f_7100052040` reports a def for a NODE or
 * a VREG RECORD?
 *
 * notes/52 flags this: phase 1 calls it with `records + i*0xe0` in X1, which
 * says record, while the values that come out match the nodes' `n92` on every
 * probe, which says node.  This prints X1, whether it lands on a record
 * boundary, and the `+0x5c` it copies, so the question is settled by
 * measurement instead of by assuming one of the two.
 *
 *   g2s_defsrc x1=<p> rec=<index or -1> f5c=<value>
 */
void g2s_trace_defsrc(cpu_t *cpu, unsigned long addr);
void g2s_trace_defsrc(cpu_t *cpu, unsigned long addr)
{
    unsigned long long x1, arr = 0, n = 0, w;
    long long idx = -1;
    unsigned f5c = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("defsrc"))
        return;
    (void)addr;
    x1 = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (x1 && g2s_try_u64((x1 + 0x5c) & ~7ULL, &w))
        f5c = (unsigned)(((x1 + 0x5c) & 7) ? (w >> 32) : (w & 0xffffffff));
    if (g2s_prog_seen && g2s_try_u64(g2s_prog_seen + 816, &arr) && arr
        && g2s_try_u64(g2s_prog_seen + 808, &n)) {
        n &= 0xffffffff;
        if (x1 >= arr && (x1 - arr) % 224 == 0 && (x1 - arr) / 224 < n)
            idx = (long long)((x1 - arr) / 224);
    }
    fprintf(stderr, "g2s_defsrc x1=%#llx rec=%lld f5c=%u\n",
            (unsigned long long)x1, idx, f5c);
}

/* g2s_trace_blockout -- what `f_7100048820` adds to a block's live-out.
 *
 * notes/52: the nibble array is written by `f_7100058a20`, and the caller that
 * puts a KILLED value back at full width is `f_7100048820`, which walks a list
 * hanging off `block[0x18]` and adds each member with a mask.  That list is a
 * FRONT-END annotation on the block, and it is what the over-approximation
 * comes from -- on `un_clamp.vert` it is what returns vreg 3 to 0xf right
 * after its definition killed it, and what carries vreg 2 although it is dead.
 *
 * Patched at the `f_7100058a20` call inside `f_7100048820`, this prints the
 * member and the mask, which is the annotation itself.
 *
 *   g2s_blockout vreg=<i> mask=<m>
 */
void g2s_trace_blockout(cpu_t *cpu, unsigned long addr);
void g2s_trace_blockout(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("blockout"))
        return;
    (void)addr;
    fprintf(stderr, "g2s_blockout vreg=%d mask=%#x\n",
            (int)(unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 1) & 0xffffffff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 2) & 0xffffffff));
}

/* g2s_trace_blockseq -- one line per BLOCK, so the annotation above can be
 * segmented.  `f_7100048820` is called once per block from `L_71000435d8`, in
 * the same reverse order the sweep uses. */
void g2s_trace_blockseq(cpu_t *cpu, unsigned long addr);
void g2s_trace_blockseq(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("blockout"))
        return;
    (void)addr;
    fprintf(stderr, "g2s_blockseq block=%#llx\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 1));
}

/* g2s_trace_seed -- the per-block bitset the live set is RE-SEEDED from.
 *
 * notes/52: `f_7100043460` zeroes the live set's count at `L_710004360c` and
 * then, at `L_7100043624`, walks the SET BITS of a bitset in X23 and adds each
 * one.  That bitset is the block's live-out, and it is the last unread thing
 * in the allocator.  This prints it at the top of the seeding loop, and with
 * G2S_WATCH_SEED arms the write watch on its word array so whoever fills it
 * names itself.
 *
 *   g2s_seed bs=<p> w=<w0>,<w1>,<w2>,<w3>
 */
void g2s_trace_seed(cpu_t *cpu, unsigned long addr);
void g2s_trace_seed(cpu_t *cpu, unsigned long addr)
{
    unsigned long long bs, v;
    int k;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("seed"))
        return;
    (void)addr;
    /* At `L_7100043638` the member being APPENDED is in X28 and the live
     * set's current count in X24[0x14]; print both, which is the seed's
     * contents in the order they are added. */
    bs = GST_I64(192);                          /* X22, the live set */
    v = 0;
    if (bs && g2s_try_u64((bs + 0x14) & ~7ULL, &v))
        v = ((bs + 0x14) & 7) ? (v >> 32) : (v & 0xffffffff);
    fprintf(stderr, "g2s_seed add=%d at=%u bs=%#llx w=",
            (int)(unsigned)(GST_I64(224) & 0xffffffff), (unsigned)v,
            (unsigned long long)GST_I64(200));
    {
        /* The added member is the BIT INDEX the walk returned, so the seed
         * bitset is whichever object here has exactly those bits set.  Print
         * every callee-saved register that looks like a pointer, and the word
         * it points at, and let the dump identify it. */
        int off;

        for (off = 168; off <= 248; off += 8) {
            unsigned long long q = GST_I64(off), x = 0, y = 0;

            if (!q || (q & 7) || !g2s_try_u64(q, &x))
                continue;
            fprintf(stderr, " x%d=[%llx", (off - 16) / 8,
                    (unsigned long long)x);
            if (x && !(x & 7) && g2s_try_u64(x, &y))
                fprintf(stderr, "->%llx", (unsigned long long)y);
            fputc(']', stderr);
        }
        (void)k;
    }
    fputc('\n', stderr);
    if (!g2s_watch_addr && !g2s_watch_hi && getenv("G2S_WATCH_SEED")) {
        unsigned long long src = GST_I64(200);

        unsigned long long bits = 0;

        if (g2s_try_u64(src, &bits) && bits && !(bits & 7))
            src = bits;
        g2s_watch_lo = src;
        g2s_watch_hi = src + 32;
        fprintf(stderr, "g2s_watch seed lo=%#llx hi=%#llx\n",
                (unsigned long long)g2s_watch_lo,
                (unsigned long long)g2s_watch_hi);
    }
    if (!g2s_watch_addr && !g2s_watch_hi && getenv("G2S_WATCH_SEED")) {
        /* Watch the whole head of the object: the layout is not settled, so
         * cover both a words-pointer at +0x08 and an inline bitmap. */
        g2s_watch_lo = bs;
        g2s_watch_hi = bs + 64;
        fprintf(stderr, "g2s_watch seed lo=%#llx hi=%#llx\n",
                (unsigned long long)g2s_watch_lo,
                (unsigned long long)g2s_watch_hi);
    }
}

/* g2s_trace_liveset -- the live set the interference builder is handed.
 *
 * notes/52: the primary edges are not added by `f_7100043460` itself.  A write
 * watch on `record[0x90]` names `f_7100042c90 <- f_71000432c0 <-
 * f_7100043460`, and `f_71000432c0` is called at `L_71000438a0` with
 *
 *     X0 = ?          X1 = ctx        X2 = the live set's COUNT
 *     X3 = sp+0x4b0, an int32 array of LIVE VREG INDICES
 *     X4 = sp+0xb0, a bitset          X5 = the vreg being defined
 *
 * so the set a derivation has to reproduce can simply be printed, once per
 * definition, instead of being inferred from which registers came out equal.
 *
 *   g2s_liveset cur=<vreg> n=<count> | <vreg> <vreg> ...
 */
void g2s_trace_liveset(cpu_t *cpu, unsigned long addr);
void g2s_trace_liveset(cpu_t *cpu, unsigned long addr)
{
    unsigned long long arr, cnt, w;
    unsigned i;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("liveset"))
        return;
    (void)addr;
    cnt = GST_I64(GUEST_OFF_X0 + 8 * 2) & 0xffffffff;
    arr = GST_I64(GUEST_OFF_X0 + 8 * 3);
    /* G2S_WATCH_LIVE arms the write watch on the LIVE SET's count at `+0x14`,
     * so whoever fills it names itself in a backtrace -- the same move that
     * named `f_71000432c0` from `record[0x90]`. */
    if (!g2s_watch_addr && getenv("G2S_WATCH_LIVE")) {
        unsigned long long ls = GST_I64(GUEST_OFF_X0);
        if (ls && !((ls + 0x14) & 3)) {
            g2s_watch_addr = ls + 0x14;
            fprintf(stderr, "g2s_watch liveset addr=%#llx\n",
                    (unsigned long long)g2s_watch_addr);
        }
    }
    fprintf(stderr, "g2s_liveset x0=%u x4=%#llx x5=%u x6=%u n=%u |",
            (unsigned)(GST_I64(GUEST_OFF_X0) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 4),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 5) & 0xffffffff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 6) & 0xffffffff),
            (unsigned)cnt);
    if (arr && cnt <= 4096) {
        for (i = 0; i < (unsigned)cnt; ++i) {
            if (!g2s_try_u64((arr + 4ULL * i) & ~7ULL, &w))
                break;
            fprintf(stderr, " %d",
                    (int)(unsigned)(((arr + 4ULL * i) & 7)
                                    ? (w >> 32) : (w & 0xffffffff)));
        }
    }
    /* X4 is a parallel array of COMPONENT MASKS for the defs above, and X6
     * counts DOWN -- this is a backward liveness walk and the X3 array is the
     * DEFS at that position.
     *
     * THE LIVE SET IS ARG0.  `f_71000432c0` reads its count at `+0x14` and an
     * int32 array at `+0x18` (`L_7100043380`, `L_71000433bc`) and, for every
     * def `d` and every member `m != d`, adds the edge `records[m] -> d`
     * (`L_7100043394`).  So printing that array is printing the relation a
     * derivation has to reproduce. */
    {
        unsigned long long ls = GST_I64(GUEST_OFF_X0), v;
        unsigned lc = 0, k;

        if (ls && g2s_try_u64((ls + 0x14) & ~7ULL, &v))
            lc = (unsigned)(((ls + 0x14) & 7) ? (v >> 32) : (v & 0xffffffff));
        fprintf(stderr, " live=%u |", lc);
        if (ls && lc <= 4096 && g2s_try_u64(ls + 0x18, &v) && v) {
            unsigned long long a = v;

            for (k = 0; k < lc; ++k) {
                if (!g2s_try_u64((a + 4ULL * k) & ~7ULL, &v))
                    break;
                fprintf(stderr, " %d",
                        (int)(unsigned)(((a + 4ULL * k) & 7) ? (v >> 32)
                                        : (v & 0xffffffff)));
            }
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_live -- the compiler's LIVE SET, at each definition.
 *
 * notes/52 §7: `f_7100043460` keeps the set of currently-live vregs as an
 * int32 array at `sp+0x4b0`, walks it at `L_71000439fc`, and for each member
 * calls `f_7100042c90(ctx, recA, .., idxB, mask)` -- which pushes {index at
 * +8, nibble mask at +0xc} onto `recA`'s `record[0x90]` list.  So the graph is
 * an ordinary linear sweep: at a definition, edge it to everything live.
 *
 * Rather than read the three more functions that MAINTAIN that set, this
 * prints it.  A derivation that computes the same live set at the same points
 * produces the same graph, and where the two differ names the exact
 * definition whose liveness is misunderstood.
 *
 *   g2s_live cur=<vreg> member=<vreg>
 */
void g2s_trace_live(cpu_t *cpu, unsigned long addr);
void g2s_trace_live(cpu_t *cpu, unsigned long addr)
{
    unsigned long long rec, arr = 0, n = 0;
    long long cur = -1;
    int member;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("live"))
        return;
    (void)addr;
    /* At `L_7100043a24` the arguments to `f_7100042c90` are already staged:
     * X19 the context, X24 the record the edge is ADDED TO, X27 the other
     * vreg's index, X8 the component mask.  So this is the edge stream in
     * construction order, which is what says what the sweep thought was live
     * at each definition. */
    member = (int)(unsigned)GST_I64(232);       /* X27 */
    rec = GST_I64(208);                         /* X24 */
    if (g2s_prog_seen && g2s_try_u64(g2s_prog_seen + 816, &arr) && arr
        && g2s_try_u64(g2s_prog_seen + 808, &n)) {
        n &= 0xffffffff;
        if (rec >= arr && (rec - arr) % 224 == 0 && (rec - arr) / 224 < n)
            cur = (long long)((rec - arr) / 224);
    }
    fprintf(stderr, "g2s_live owner=%lld member=%d mask=%#x\n", cur, member,
            (unsigned)(GST_I64(80) & 0xffffffff));
}

/* g2s_trace_ival -- the LIVE INTERVALS the interference builder compares.
 *
 * notes/52 §7: `f_7100043460`'s phase 2 walks, for each vreg, a stack array of
 * candidate vreg indices and consults a structure whose `+0x14` and `+0x20`
 * are two int32s compared against each other -- the shape of an interval
 * {start, end}.  Patched at `L_7100043af0`, this prints the pair under
 * consideration with those numbers, which is what says WHY a value is still
 * live where a plain last-use sweep says it is dead.
 *
 *   g2s_ival other=<idx> f88=<int16> s=<+0x14> e=<+0x20> p=<+0x18>
 */
void g2s_trace_ival(cpu_t *cpu, unsigned long addr);
void g2s_trace_ival(cpu_t *cpu, unsigned long addr)
{
    unsigned long long ent, iv, recs, other = 0, w = 0;
    int f88 = 0, st = 0, en = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("ival"))
        return;
    (void)addr;
    /* The port spells Xn as GST_I64(16 + 8*n), so the three the block holds
     * are X25, X22 and X28 -- not the numbers a first reading of the
     * decompiled names suggests. */
    ent = GST_I64(216);                         /* X25, the array entry  */
    iv = GST_I64(192);                          /* X22, the interval     */
    recs = GST_I64(240);                        /* X28, the other record */
    if (ent && g2s_try_u64(ent & ~7ULL, &w))
        other = (ent & 4) ? (w >> 32) : (w & 0xffffffff);
    if (recs && g2s_try_u64((recs + 0x88) & ~7ULL, &w))
        f88 = (int)(short)((w >> (8 * ((recs + 0x88) & 7))) & 0xffff);
    if (iv && g2s_try_u64((iv + 0x14) & ~7ULL, &w))
        st = (int)(unsigned)((iv + 0x14) & 7 ? (w >> 32) : (w & 0xffffffff));
    if (iv && g2s_try_u64((iv + 0x20) & ~7ULL, &w))
        en = (int)(unsigned)((iv + 0x20) & 7 ? (w >> 32) : (w & 0xffffffff));
    fprintf(stderr, "g2s_ival other=%d f88=%d s=%d e=%d",
            (int)(unsigned)other, f88, st, en);
    /* and every callee-saved register that currently looks like a vreg
     * record, so the pair (defining vreg, live member) can be read off. */
    {
        unsigned long long arr = 0, n = 0, r;
        int off;

        if (g2s_prog_seen && g2s_try_u64(g2s_prog_seen + 816, &arr)
            && g2s_try_u64(g2s_prog_seen + 808, &n))
            n &= 0xffffffff;
        else
            n = 0;
        for (off = 168; off <= 248; off += 8) {
            r = GST_I64(off);
            if (n && arr && r >= arr && (r - arr) % 224 == 0
                && (r - arr) / 224 < n)
                fprintf(stderr, " x%d=%d", (off - 16) / 8,
                        (int)((r - arr) / 224));
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_regpick -- the register the allocator settles on, BEFORE alignment.
 *
 * notes/52 named the engines: `f_7100045ed0` makes a first attempt that the
 * driver throws away and `f_7100045530` writes the answer.  Its final store is
 * one block, `L_7100045e48`:
 *
 *     record[0x40] = X0 & (0xfffffffc << (X15 & 31));
 *
 * so the decision is a CANDIDATE in X0 aligned down by a shift in X15, and
 * everything upstream of it is the search.  Rather than read 4,000 lines of
 * that search blind, this prints its result for every record -- the candidate
 * before alignment, the shift, and which vreg is being assigned -- so the rule
 * can be found in the data and then CONFIRMED in the code, which is the same
 * order the edge builder and the operand enumeration were read in.
 *
 *   g2s_pick vreg=<i> cand=<X0> shift=<X15> reg=<the aligned result>
 */
void g2s_trace_regpick(cpu_t *cpu, unsigned long addr);
void g2s_trace_regpick(cpu_t *cpu, unsigned long addr)
{
    unsigned long long rec, cand, shift, arr = 0, n = 0;
    long long idx = -1;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("regpick"))
        return;
    (void)addr;
    cand = GST_I64(GUEST_OFF_X0) & 0xffffffffu;
    shift = GST_I64(GUEST_OFF_X0 + 8 * 15) & 0x1f;
    rec = GST_I64(GUEST_OFF_X0 + 8 * 21);
    /* Name the record by its INDEX, which is stable across runs where the
     * address is not, using the program the other hooks already found. */
    if (g2s_prog_seen && g2s_try_u64(g2s_prog_seen + 816, &arr) && arr
        && g2s_try_u64(g2s_prog_seen + 808, &n)) {
        n &= 0xffffffff;
        if (rec >= arr && (rec - arr) % 224 == 0 && (rec - arr) / 224 < n)
            idx = (long long)((rec - arr) / 224);
    }
    {
        unsigned long long cost = 0, cmask = 0, sym = 0;
        g2s_try_u64(rec + 48, &cost);
        g2s_try_u64(rec + 80, &cmask);
        g2s_try_u64(rec + 16, &sym);
        fprintf(stderr,
                "g2s_pick vreg=%lld rec=%#llx cost=%llu cm=%#llx sym=%llu "
                "cand=%#llx shift=%llu reg=%d",
                idx, rec, cost & 0xffffffffu, cmask & 0xffffffffu,
                sym & 0xffff, cand, shift,
                (int)(unsigned)(cand & (0xfffffffcu << shift)));
        /* THE WHOLE COST VECTOR, so the first pick of an attempt shows the
         * keys the list was built from rather than whatever the engine has
         * left in `record[48]` by the time it settles. */
        {
            /* `arr` is not always resolvable, so the window is taken around
             * the record itself and aligned offline by address. */
            long j;
            unsigned long long c = 0, m = 0, base = arr ? arr : 0;
            fprintf(stderr, " |");
            if (base) {
                unsigned long long i;
                for (i = 0; i < n && i < 64; i++) {
                    if (!g2s_try_u64(base + i * 224 + 48, &c))
                        c = 0;
                    if (!g2s_try_u64(base + i * 224 + 80, &m))
                        m = 0;
                    fprintf(stderr, " %llu/%llx", c & 0xffffffffu,
                            m & 0xffffffffu);
                }
            } else {
                for (j = -12; j <= 12; j++) {
                    unsigned long long a = rec + (unsigned long long)(j * 224);
                    if (!g2s_try_u64(a + 48, &c))
                        continue;
                    if (!g2s_try_u64(a + 80, &m))
                        m = 0;
                    fprintf(stderr, " %#llx:%llu/%llx", a,
                            c & 0xffffffffu, m & 0xffffffffu);
                }
            }
        }
        fprintf(stderr, "\n");
    }
}

/* g2s_trace_class -- print the class VTABLE a node constructor installs, with
 * the constructor's address, so the four vtables seen in a node dump
 * (notes/46) can be matched to the builders of notes/33. */
void g2s_trace_class(cpu_t *cpu, unsigned long addr)
{
    unsigned long long node, vt = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    if (!g2s_want("class"))
        return;
    node = GST_I64(GUEST_OFF_X0);
    g2s_try_u64(node, &vt);
    fprintf(stderr, "g2s_class f_%lx node=%#llx vt=%#llx lr=%#llx\n", addr,
            node, vt, (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
}

/* g2s_trace_fold -- the folding pass, node by node.
 *
 * `f_7100056fa0` (notes/43) is the per-node pass that clears bit 0 of
 * `node[152]`, the flag that decides whether a node prints a line of its own
 * or is inlined into its consumer.  Its X1 is the node.  The pass is the ONLY
 * place that clears the bit, so hooking its entry sees every node the emitter
 * considers -- including the ones that never reach a printer, which is exactly
 * the set `g2s_trace_node` cannot show.
 *
 * Printed per call: the node, its opcode (node[8]), the whole flag byte before
 * the pass runs, the operand count (node[153]) and, for each operand slot at
 * node+0xc0 with a stride of 0x28, the operand pointer, that operand's own
 * opcode and flag byte, and the five words of the slot itself -- the slot is
 * where an operand's modifiers (negate, swizzle) live, so a difference between
 * `a0` and `-a0` shows up here and nowhere else. */
void g2s_trace_fold(cpu_t *cpu, unsigned long addr)
{
    unsigned long long node, w, op = 0, fl = 0, nops = 0, slot, p, pop, pfl;
    int i, j;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("fold"))
        return;
    (void)addr;
    node = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!node || (node & 7))
        return;
    if (g2s_try_u64(node + 8, &op))
        op &= 0xffffffff;
    /* node[152] and node[153] are bytes inside the 8-byte word at +152. */
    if (g2s_try_u64(node + 152, &w)) {
        fl = w & 0xff;
        nops = (w >> 8) & 0xff;
    }
    fprintf(stderr, "g2s_fold node=%#llx op=%#llx flags=%#llx nops=%llu",
            node, op, fl, nops);
    /* The three fields the SCHEDULER keys on (notes/31) and the virtual
     * register the printer names, so one fold trace is a complete DAG dump:
     *   node[32] / node[36]   the comparator's two words, node[36] being the
     *                         `vr` number tools/nodedump.py reports
     *   node[48]              the destination write mask (notes/41)
     *   node[92]              the virtual register (the high word at +88) */
    if (g2s_try_u64(node + 32, &w))
        fprintf(stderr, " seq=%llu/%llu", w & 0xffffffff, w >> 32);
    if (g2s_try_u64(node + 48, &w))
        fprintf(stderr, " mask=%#llx", w & 0xffffffff);
    if (g2s_try_u64(node + 88, &w))
        fprintf(stderr, " vr=%llu", w >> 32);
    if (nops > 16)
        nops = 16;
    for (i = 0; i < (int)nops; ++i) {
        slot = node + 0xc0 + 0x28ULL * (unsigned)i;
        pop = pfl = 0;
        if (!g2s_try_u64(slot, &p))
            p = 0;
        if (p && !(p & 7)) {
            if (g2s_try_u64(p + 8, &pop))
                pop &= 0xffffffff;
            if (g2s_try_u64(p + 152, &pfl))
                pfl &= 0xff;
        }
        fprintf(stderr, " | opnd%d=%#llx op=%#llx flags=%#llx slot:", i, p,
                pop, pfl);
        for (j = 8; j < 0x28; j += 8) {
            if (g2s_try_u64(slot + (unsigned)j, &w))
                fprintf(stderr, " %#llx", w);
            else
                fprintf(stderr, " ?");
        }
        /* And the SOURCE slots by the printer's own offsets (notes/47):
         * source i is at node + 0xa8 + 0x28 * i, its modifier the high word of
         * slot+8 and its node the pointer at slot+24. */
        {
            unsigned long long ss = node + 0xa8 + 0x28ULL * (unsigned)i;
            unsigned long long mod = 0, sn = 0, sw = 0, svr = 0, inl = 0;
            g2s_try_u64(ss + 8, &mod);
            g2s_try_u64(ss + 16, &inl);
            g2s_try_u64(ss + 24, &sn);
            g2s_try_u64(ss + 32, &sw);
            if (sn && !(sn & 7))
                g2s_try_u64(sn + 88, &svr);
            /* `inl` is `vt[32]` = `f_710004f170` = `*(int *)(node + 40*i +
             * 184)`, the flag `f_7100049940` tests at 0x49a08 to decide
             * whether to RELEASE this operand or to recurse into its own
             * slots.  It belongs with the slot it describes. */
            fprintf(stderr, " src%d=%#llx vr=%llu mod=%llu swz=%#llx inl%d=%u",
                    i, sn, svr >> 32, mod >> 32, sw, i,
                    (unsigned)(inl & 0xffffffff));
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_carrier -- the materialisation test in the assignment lowering.
 *
 * Inside `f_7100f10a30` (the cgc assignment statement lowering, notes/44) the
 * sequence at 0xf11530..0xf11580 builds a carrier node and then decides
 * whether to stack a SECOND `MOV` on top of it:
 *
 *     f11530:  x23 = f_7100f0c180(cg, ..., x22)      ; the carrier
 *     f11544:  w0  = cg[0][1064][984](x23)           ; a predicate on it
 *     f11548:  w8  = x21[16]                         ; the SOURCE node's flags
 *     f1154c:  if (w8 & 2)                  -> skip
 *     f11558:  if (!((w0 ^ ((w8 & 4) >> 2)) & 1)) -> skip
 *     f1156c:  if (carrier opcode != 0x47)  -> skip
 *     f11580:  otherwise build a second MOV
 *
 * Called at the top of the block at 0xf11548, this prints the predicate's
 * answer together with both nodes, which is the only way to see WHICH of the
 * three tests separates `gl_Position = a0` (one MOV) from `gl_Position = -a0`
 * (two).  The register numbering is the port's: GST_I64(8*n) is Xn. */
void g2s_trace_carrier(cpu_t *cpu, unsigned long addr)
{
    unsigned long long pred, src, car, w, sf = 0, co = 0, cf = 0, cm = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("carrier"))
        return;
    (void)addr;
    pred = GST_I64(GUEST_OFF_X0) & 0xffffffff;
    src = GST_I64(GUEST_OFF_X0 + 8 * 21);
    car = GST_I64(GUEST_OFF_X0 + 8 * 23);
    if (src && !(src & 7) && g2s_try_u64(src + 16, &w))
        sf = w & 0xffffffff;
    if (car && !(car & 7)) {
        if (g2s_try_u64(car + 8, &w))
            co = w & 0xffffffff;
        if (g2s_try_u64(car + 16, &w))
            cf = w & 0xffffffff;
        if (g2s_try_u64(car + 48, &w))
            cm = w & 0xffffffff;
    }
    fprintf(stderr,
            "g2s_carrier pred=%llu src=%#llx src16=%#llx car=%#llx op=%#llx "
            "flags=%#llx mask=%#llx -> second=%d\n",
            pred, src, sf, car, co, cf, cm,
            (!(sf & 2) && ((pred ^ ((sf & 4) >> 2)) & 1) && co == 0x47));
}

/* g2s_trace_expr -- one line per cgc IR expression lowered to GLASM nodes.
 *
 * `f_7100f10a30` is the IR-expression lowering (notes/34): its X1 is the IR
 * node and it dispatches on `(node[16] >> 16)`, the operation number, through
 * a 0x2ca-entry table.  Printing the operation together with the RETURN
 * ADDRESS says which statement lowering asked for it, which is the only way to
 * find the caller that materialises a value into a temp before a scalarised
 * store -- the difference between `gl_Position = a0` and `gl_Position = -a0`
 * (notes/44). */
void g2s_trace_expr(cpu_t *cpu, unsigned long addr)
{
    unsigned long long lr, node, w = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("expr"))
        return;
    lr = GST_I64(GUEST_OFF_X0 + 8 * 30);
    node = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (node && !(node & 7))
        g2s_try_u64(node + 16, &w);
    fprintf(stderr, "g2s_expr f_%lx lr=%#llx node=%#llx n16=%#llx op=%#llx\n",
            addr, lr, node, w & 0xffffffff, (w >> 16) & 0xffff);
}

/* g2s_dump_graph -- the interference graph and the colours, per virtual
 * register.
 *
 * notes/48 names the path: `f_7100045530` is the colour SEARCH and it is
 * entered once per record list with the program in X1.  By then
 * `f_7100043460` has built each record's neighbour list -- `record[216]`,
 * whose entries are {next, vreg, flags} of 16 bytes (0x43ec8..0x43ee8) -- so
 * one dump here has everything the allocator decides from, and a second dump
 * after colouring has what it decided.
 *
 * Printed per record: its colour `record[64]`, its chain link `record[92]`,
 * the packed (base : 28, index : 4) of `record[88]`, its type `record[8]`,
 * the size halfword `record[22]`, the component byte `record[24]`, and every
 * neighbour vreg.  Everything is read through the fault guard. */
void g2s_dump_graph(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, n = 0, arr, rec, w, e;
    int i, guard;

    if (g2s_trace_budget == -2) {
        const char *e2 = getenv("G2S_TRACE");
        g2s_trace_budget = (!e2 || !*e2) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("graph"))
        return;
    prog = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!prog || (prog & 7) || !g2s_try_u64(prog + 808, &n))
        return;
    n &= 0xffffffff;
    if (!g2s_try_u64(prog + 816, &arr) || !arr || n > 65536)
        return;
    fprintf(stderr, "g2s_graph f_%lx prog=%#llx n=%llu\n", addr, prog, n);
    for (i = 0; i < (int)n; ++i) {
        rec = arr + 224ULL * (unsigned)i;
        fprintf(stderr, "g2s_gv %d", i);
        if (g2s_try_u64(rec + 64, &w))
            fprintf(stderr, " colour=%d", (int)(unsigned)w);
        if (g2s_try_u64(rec + 88, &w))
            fprintf(stderr, " base=%llu idx=%llu",
                    (w & 0xfffffffULL), ((w >> 28) & 0xfULL));
        if (g2s_try_u64(rec + 92, &w))
            fprintf(stderr, " chain=%llu", (w >> 32) & 0xffffffffULL);
        if (g2s_try_u64(rec + 8, &w))
            fprintf(stderr, " type=%#llx", w & 0xffffffff);
        if (g2s_try_u64(rec + 16, &w))
            fprintf(stderr, " size=%llu comp=%#llx",
                    (w >> 48) & 0xffff, (w >> 32) & 0xff);
        fprintf(stderr, " nb:");
        /* record[216] is the head of the FINAL neighbour list, built at
         * 0x43f94..0x43fe0: each entry is
         *     +0   the neighbour's vreg number (a word)
         *     +8   the next entry
         *     +16..+28  four component masks, each a 4-bit mask expanded to
         *               one byte per component by f_7100051980's table
         *               (0 -> 0x00000000, 1 -> 0x000000ff, ... f -> 0xffffffff)
         * which is the same per-byte encoding as a write mask (notes/41).
         * The earlier, 16-byte {next, vreg, flags} form is record[144], the
         * scratch the final list is built FROM, and reading that layout here
         * printed pointers where vreg numbers belong. */
        if (g2s_try_u64(rec + 216, &e)) {
            for (guard = 0; e && !(e & 7) && guard < 4096; ++guard) {
                unsigned long long v = 0, m0 = 0, m1 = 0;
                if (g2s_try_u64(e, &v) && g2s_try_u64(e + 16, &m0)
                        && g2s_try_u64(e + 24, &m1))
                    fprintf(stderr, " %u:%08x,%08x,%08x,%08x",
                            (unsigned)v, (unsigned)m0, (unsigned)(m0 >> 32),
                            (unsigned)m1, (unsigned)(m1 >> 32));
                if (!g2s_try_u64(e + 8, &e))
                    break;
            }
        }
        fputc('\n', stderr);
    }
}

/* g2s_trace_pick / g2s_trace_picked -- the scheduler's selection, candidates
 * and answer.
 *
 * `f_710004b930` is the ready-list scan (notes/31).  Its X2 is the list
 * container, whose [16] is the head; entries chain through [0], each entry's
 * [8] is the NODE and [52] its release stamp.  The comparator keys on the
 * node's halfwords at +28 and +30 and its words at +32 and +36.
 *
 * The entry hook prints every candidate with those five fields; the hook at
 * the return block (0x4ba60) prints the entry the scan kept, which is in X24
 * there.  One pair of lines per selection is the whole rule, measured instead
 * of simulated -- and a simulation that disagrees can be pointed at the exact
 * selection where it first went wrong. */
void g2s_trace_pick(cpu_t *cpu, unsigned long addr)
{
    unsigned long long list, e, w, node;
    int guard;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("pick"))
        return;
    (void)addr;
    list = GST_I64(GUEST_OFF_X0 + 8 * 2);
    fprintf(stderr, "g2s_pick list=%#llx |", list);
    if (!list || (list & 7) || !g2s_try_u64(list + 16, &e))
        e = 0;
    for (guard = 0; e && !(e & 7) && guard < 4096; ++guard) {
        node = 0;
        g2s_try_u64(e + 8, &node);
        fprintf(stderr, " e=%#llx", e);
        if (g2s_try_u64(e + 48, &w))
            fprintf(stderr, ",st=%d", (int)(unsigned)(w >> 32));
        if (node && !(node & 7)) {
            fprintf(stderr, ",n=%#llx", node);
            if (g2s_try_u64(node + 24, &w))
                fprintf(stderr, ",h28=%llu,h30=%llu",
                        (w >> 32) & 0xffff, (w >> 48) & 0xffff);
            if (g2s_try_u64(node + 32, &w))
                fprintf(stderr, ",w32=%llu,w36=%llu",
                        w & 0xffffffff, w >> 32);
        }
        if (!g2s_try_u64(e, &e))
            break;
    }
    fputc('\n', stderr);
}

void g2s_trace_picked(cpu_t *cpu, unsigned long addr)
{
    unsigned long long best, node = 0;

    if (g2s_trace_budget == 0 || !g2s_want("pick"))
        return;
    (void)addr;
    best = GST_I64(GUEST_OFF_X0 + 8 * 24);
    if (best && !(best & 7))
        g2s_try_u64(best + 8, &node);
    fprintf(stderr, "g2s_picked e=%#llx n=%#llx\n", best, node);
}

/* g2s_trace_emit -- the moment a node is put on a block's instruction list.
 *
 * `f_7100030c74(list, program, node, extra)` allocates the 64-byte instruction
 * record and links it in: `instr[8]` takes the list's current head and the
 * head becomes the new record, so the list grows at the FRONT and the
 * printer, which walks `list[0]` and then `instr[8]` (notes/29), prints the
 * newest first.  Printing the node here gives the insertion order directly,
 * which is what says how the scheduler's PICK order (g2s_trace_pick) becomes
 * the printed order -- the two are not reverses of each other (notes/49). */
void g2s_trace_emit(cpu_t *cpu, unsigned long addr)
{
    unsigned long long list, node, w = 0, seq = 0, mask = 0;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("emit"))
        return;
    list = GST_I64(GUEST_OFF_X0);
    node = GST_I64(GUEST_OFF_X0 + 8 * 2);
    if (node && !(node & 7)) {
        if (g2s_try_u64(node + 32, &w))
            seq = w >> 32;
        if (g2s_try_u64(node + 48, &w))
            mask = w & 0xffffffff;
        w = 0;
        g2s_try_u64(node + 8, &w);
    }
    /* WHICH END.  `f_7100030c74` pushes at the FRONT (it writes list[0]) and
     * its sibling `f_7100030cfc` pushes at the BACK (it writes list[8]), so a
     * list is a deque and the printed order is front-pushes reversed followed
     * by back-pushes in order.  Printing the site is what separates them. */
    fprintf(stderr, "g2s_emit f_%lx list=%#llx node=%#llx op=%#llx seq=%llu "
            "mask=%#llx", addr, list, node, w & 0xffffffff, seq, mask);
    /* X4 and X5, which BOTH insertion functions read (`GST_I64(32)` and
     * `GST_I64(40)` at the top of each) and neither of the two callers sets
     * the same way.  `f_710004a2e0` leaves them stale; `f_710004b220` sets
     * X5 to the record it is walking and X4 to that record's `[0]`, so
     * printing them is what makes the SECOND pass's walk readable -- which is
     * the one open question in notes/51 sec.4. */
    {
        unsigned long long x4 = GST_I64(GUEST_OFF_X0 + 8 * 4);
        unsigned long long x5 = GST_I64(GUEST_OFF_X0 + 8 * 5);
        unsigned long long x3 = GST_I64(GUEST_OFF_X0 + 8 * 3);
        /* THE CALL SITE.  The port writes the guest return address into
         * GST_I64(256) immediately before every call, so at a hook on the
         * callee's first block that word still holds it -- the same trick
         * `indirect_patch.py` uses to make a site nameable (notes/45, 48).
         * `f_710004b220` back-pushes from several places and they are not the
         * same walk; without this the stream cannot be split. */
        fprintf(stderr, " from=%#llx x3=%#llx x4=%#llx x5=%#llx",
                (unsigned long long)GST_I64(256), x3, x4, x5);
        if (x5 && !(x5 & 7)) {
            unsigned long long f;
            if (g2s_try_u64(x5, &f))
                fprintf(stderr, " x5[0]=%#llx", f);
            if (g2s_try_u64(x5 + 32, &f))
                fprintf(stderr, " x5[32]=%#llx", f);
            if (g2s_try_u64(x5 + 56, &f))
                fprintf(stderr, " x5[56]=%#llx", f);
            if (g2s_try_u64(x5 + 64, &f))
                fprintf(stderr, " x5[64]=%#llx", f & 0xffffffff);
        }
    }
    fputc('\n', stderr);
}

/* g2s_dump_implicit -- the release edges that are NOT in the slot array.
 *
 * `f_7100049940` does two things.  Its loop walks the node's operand slots
 * (dumped as `src=` / `inl=` above).  Then, at 0x49a24, it falls through to a
 * SECOND set of releases that no slot names -- the node's implicit register
 * reads -- and those are edges a model built only on `src=` can never have.
 * Replicated here instruction for instruction:
 *
 *   49a24:  w8 = node[8]                                ; the opcode
 *   49a28:  w9 = w8 - 0x2b
 *   49a2c:  if (w9 <=u 0x2f && (1 << w9) & 0x0000900000008001) return
 *                                                       ; 0x2b 0x3a 0x57 0x5a
 *   49a4c:  if ((w8 - 1) <u 2) return                    ; 0x1 0x2
 *   49a74:  x24 = (int32_t)node[92]                      ; the opcode's row
 *   49a78:  w23 = 0x03020100                             ; identity swizzle
 *   49a84:  x25 = program[816]                           ; the row table
 *   49a88:  for k in 0..3:                               ; node[48+k], the
 *             if (((uint8_t *)node)[48 + k]) {           ;   per-component
 *                 rec = *(void **)(x25 + 224*x24 + 104 + 8*k);
 *                 while (rec) {                          ;   enable bytes
 *                     t = rec[8];
 *                     f_7100049780(prog, x21, x20, t, 0x03020100, t[48], 0);
 *                     rec = rec[0];
 *                 }
 *             }
 *   49b88:  x8 = (int32_t)node[120]
 *   49b8c:  if (x8) {
 *               chain = program[200][96][0][x8];
 *               for (rec = chain[0]; rec; rec = rec[0]) {
 *                   t = rec[16];
 *                   if (t && t != node)
 *                       f_7100049780(prog, x21, x20, t, 0x03020100, t[48], 0);
 *               }
 *           }
 *
 * The two `f_7100049780` arguments this dump drops are the swizzle and the
 * mask, which the release uses only to decide WHICH components arrive; the
 * edge -- consumer `cur`, operand `t` -- is the part the order model needs,
 * and it is printed as `irr=`.  `none` is printed when the opcode returns
 * early, which is a fact about the opcode and not a failure to read.
 */
static void g2s_dump_implicit(cpu_t *cpu, unsigned long long prog,
                              unsigned long long node)
{
    unsigned long long w, tab, rec, t, chain, idx2;
    long long row;
    int k, n = 0, guard;
    unsigned op;

    (void)cpu;
    fputs(" irr=", stderr);
    if (!prog || (prog & 7) || !node || (node & 7)) {
        fputs("?", stderr);
        return;
    }
    if (!g2s_try_u64(node + 8, &w)) {
        fputs("?", stderr);
        return;
    }
    op = (unsigned)(w & 0xffffffff);
    if (op - 0x2bu <= 0x2fu
        && ((1ULL << (op - 0x2bu)) & 0x0000900000008001ULL)) {
        fputs("none", stderr);
        return;
    }
    if (op - 1u < 2u) {
        fputs("none", stderr);
        return;
    }
    /* group one: the opcode row's four component lists */
    if (g2s_try_u64(node + 88, &w)) {
        row = (long long)(int)(unsigned)(w >> 32);        /* ldrsw [x19,#92] */
        if (row && g2s_try_u64(prog + 816, &tab) && tab && !(tab & 7)) {
            unsigned long long enables = 0;
            if (g2s_try_u64(node + 48, &enables)) {
                for (k = 0; k < 4; ++k) {
                    if (!((enables >> (8 * k)) & 0xff))
                        continue;
                    rec = 0;
                    if (!g2s_try_u64(tab + 224ULL * (unsigned long long)row
                                     + 104 + 8ULL * (unsigned)k, &rec))
                        continue;
                    for (guard = 0; rec && !(rec & 7) && guard < 256;
                         ++guard) {
                        t = 0;
                        if (g2s_try_u64(rec + 8, &t) && t && !(t & 7))
                            fprintf(stderr, "%s%#llx", n++ ? "," : "", t);
                        if (!g2s_try_u64(rec, &rec))
                            break;
                    }
                }
            }
        }
    }
    /* group two: node[120]'s chain through program[200].  The SAME chain is
     * walked again by `f_710004b220` at 0x4b460 to build one kind-0 edge per
     * member, so it is printed separately as `irx=` further down. */
    if (g2s_try_u64(node + 120, &w)) {
        idx2 = (unsigned long long)(long long)(int)(unsigned)(w & 0xffffffff);
        if ((int)(unsigned)(w & 0xffffffff) != 0
            && g2s_try_u64(prog + 200, &chain) && chain && !(chain & 7)
            && g2s_try_u64(chain + 96, &chain) && chain && !(chain & 7)
            && g2s_try_u64(chain, &chain) && chain && !(chain & 7)
            && g2s_try_u64(chain + 8ULL * idx2, &chain) && chain
            && !(chain & 7)
            && g2s_try_u64(chain, &rec)) {
            for (guard = 0; rec && !(rec & 7) && guard < 256; ++guard) {
                t = 0;
                if (g2s_try_u64(rec + 16, &t) && t && !(t & 7) && t != node)
                    fprintf(stderr, "%s%#llx", n++ ? "," : "", t);
                if (!g2s_try_u64(rec, &rec))
                    break;
            }
        }
    }
    if (!n)
        fputs("none", stderr);
}

/* g2s_dump_n120 -- node[120]'s chain on its own.
 *
 * `g2s_dump_implicit` folds it into `irr=` because the release treats it like
 * the rest.  The EDGE builder does not: 0x4b460 walks this chain alone and
 * makes one kind-0 edge per member, from this node's record to the other's,
 * outside `f_710004ab80` entirely.  So it is printed a second time under its
 * own name rather than having the model guess which half of `irr=` it is.
 */
static void g2s_dump_n120(cpu_t *cpu, unsigned long long prog,
                          unsigned long long node)
{
    unsigned long long w, chain, rec, t, idx;
    int n = 0, guard;

    (void)cpu;
    fputs(" irx=", stderr);
    if (!prog || (prog & 7) || !node || (node & 7)
        || !g2s_try_u64(node + 120, &w)) {
        fputs("none", stderr);
        return;
    }
    idx = (unsigned long long)(long long)(int)(unsigned)(w & 0xffffffff);
    if ((int)(unsigned)(w & 0xffffffff) != 0
        && g2s_try_u64(prog + 200, &chain) && chain && !(chain & 7)
        && g2s_try_u64(chain + 96, &chain) && chain && !(chain & 7)
        && g2s_try_u64(chain, &chain) && chain && !(chain & 7)
        && g2s_try_u64(chain + 8ULL * idx, &chain) && chain && !(chain & 7)
        && g2s_try_u64(chain, &rec)) {
        for (guard = 0; rec && !(rec & 7) && guard < 256; ++guard) {
            t = 0;
            if (g2s_try_u64(rec + 16, &t) && t && !(t & 7) && t != node)
                fprintf(stderr, "%s%#llx", n++ ? "," : "", t);
            if (!g2s_try_u64(rec, &rec))
                break;
        }
    }
    if (!n)
        fputs("none", stderr);
}

/* g2s_trace_block -- the per-block emitter's INPUT, at its entry.
 *
 * notes/51 reads `f_710004a2e0` as the thing that emits ONE block: every node
 * it schedules is linked into that block's own `block[32]`, and the four
 * lists notes/49 saw were four calls.  The order model that follows from
 * that consumes three things the DAG builder decided and this converter does
 * not yet produce -- which block a node is in, its pending-use count
 * `node[88]`, and the block's terminator -- so testing the model needs them
 * as INPUTS rather than as things to guess.
 *
 * This prints exactly them, at the entry to the emitter, before it has
 * changed anything:
 *
 *   g2s_blk block=<b> list=<b[32]> nodes=<b[56]> term=<node of b[88]>
 *   g2s_ble entry=<e> node=<n> op=<n[8]> pend=<n[88]> mask=<n[48]> seq=<n[36]>
 *
 * one `g2s_ble` per entry of `block[80]`, in the chain's own order, which is
 * the order the seeding loop walks them in.  `tools/schedcheck.py` runs the
 * model on that and compares with the printed order.
 *
 * The arguments are (cg-holder, program, block) = X0, X1, X2, so the block is
 * X2.  Every read goes through g2s_try_u64 (notes/11): a field this reading
 * has wrong must print `?` rather than kill the run.
 */
void g2s_trace_block(cpu_t *cpu, unsigned long addr)
{
    unsigned long long blk, w, e, node, guard, prog_of_block;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("block"))
        return;
    (void)addr;
    blk = GST_I64(GUEST_OFF_X0 + 8 * 2);
    /* f_710004a2e0(cg, program, block): 0x4a310 puts X1 in x20 and that is
     * the `x22` f_7100049940 indexes at +816 and +200. */
    prog_of_block = GST_I64(GUEST_OFF_X0 + 8);
    if (!blk || (blk & 7))
        return;
    fprintf(stderr, "g2s_blk block=%#llx", blk);
    if (g2s_try_u64(blk + 32, &w))
        fprintf(stderr, " list=%#llx", w);
    else
        fprintf(stderr, " list=?");
    if (g2s_try_u64(blk + 56, &w))
        fprintf(stderr, " nodes=%llu", w & 0xffffffff);
    if (g2s_try_u64(blk, &w))
        fprintf(stderr, " own=%#llx", w);
    /* The terminator: block[88] is an INSTRUCTION record and its node is at
     * +32, the same shape as a block[80] entry (0x4a468). */
    if (g2s_try_u64(blk + 88, &w) && w && !(w & 7)) {
        unsigned long long tn = 0;
        if (g2s_try_u64(w + 32, &tn))
            fprintf(stderr, " term=%#llx", tn);
    }
    fputc('\n', stderr);

    /* THE DAG REACHABLE FROM THIS BLOCK'S ROOTS, from pass 1's own compile.
     *
     * `g2s_trace_fold` dumps the same thing and cannot be used with this one:
     * it runs in the OTHER compile (notes/05 -- the module is read twice) and
     * the allocator reuses addresses, so a node at a given address is usually
     * a different node in the two dumps (the opcode agrees on 48 of 180
     * measured).  Simulating pass 1 needs the DAG and the block roots to be
     * the SAME objects, so the walk happens here.
     *
     * Sources are at node + 0xa8 + 0x28*i with node[153] of them (notes/47).
     * The walk is iterative over a small explicit stack and every node is
     * printed once, both so a diamond does not blow up and so a cycle in a
     * misreading cannot hang the instrument.
     */
    {
        unsigned long long stack[512], seen[512];
        int sp_ = 0, ns = 0, i_;
        unsigned long long cur, w2, nops2, sn2, root;
        if (g2s_try_u64(blk + 80, &root)) {
            for (guard = 0; root && !(root & 7) && guard < 4096; ++guard) {
                if (g2s_try_u64(root + 32, &cur) && cur && !(cur & 7)
                    && sp_ < 512)
                    stack[sp_++] = cur;
                if (!g2s_try_u64(root, &root))
                    break;
            }
        }
        /* THE TERMINATOR IS A ROOT TOO.  It hangs off `block[88]`, not
         * `block[80]` (notes/51 §2.2), and a block whose work is all
         * control flow -- a switch arm, say -- has nothing in `block[80]`
         * at all, so seeding only from there leaves its `0x95`, `0x7e` and
         * `0x1a` out of the DAG entirely and a model built on it never
         * schedules them. */
        if (g2s_try_u64(blk + 88, &root) && root && !(root & 7)
            && g2s_try_u64(root + 32, &cur) && cur && !(cur & 7)
            && sp_ < 512)
            stack[sp_++] = cur;
        while (sp_ > 0) {
            cur = stack[--sp_];
            for (i_ = 0; i_ < ns; ++i_)
                if (seen[i_] == cur)
                    break;
            if (i_ < ns)
                continue;
            if (ns < 512)
                seen[ns++] = cur;
            fprintf(stderr, "g2s_dag node=%#llx", cur);
            if (g2s_try_u64(cur + 8, &w2))
                fprintf(stderr, " op=%#llx", w2 & 0xffffffff);
            if (g2s_try_u64(cur + 32, &w2))
                fprintf(stderr, " seq=%llu", w2 >> 32);
            if (g2s_try_u64(cur + 48, &w2))
                fprintf(stderr, " mask=%#llx", w2 & 0xffffffff);
            nops2 = 0;
            if (g2s_try_u64(cur + 152, &w2)) {
                fprintf(stderr, " flags=%#llx", w2 & 0xff);
                nops2 = (w2 >> 8) & 0xff;
            }
            /* node[144] -- the OWNER a folded node is attached to.  The
             * release step redirects to it (`f_7100049780` at 0x49880:
             * `target = node[144] ? ... : node`, with an exception for
             * opcode 0x5f and a `vt[464]` predicate), so a model that
             * ignores it decrements the wrong counter and releases the
             * wrong node.  Dumped here because it is part of the DAG, not
             * part of the schedule. */
            if (g2s_try_u64(cur + 144, &w2))
                fprintf(stderr, " owner=%#llx", w2);
            if (nops2 > 16)
                nops2 = 16;
            fputs(" src=", stderr);
            if (!nops2)
                fputs("none", stderr);
            for (i_ = 0; i_ < (int)nops2; ++i_) {
                sn2 = 0;
                g2s_try_u64(cur + 0xa8 + 0x28ULL * (unsigned)i_ + 24, &sn2);
                fprintf(stderr, "%s%#llx", i_ ? "," : "", sn2);
                if (sn2 && !(sn2 & 7) && sp_ < 512)
                    stack[sp_++] = sn2;
            }
            /* THE SLOT'S `inline` FLAG, which is what decides whether the
             * scheduler releases an operand or looks THROUGH it.
             *
             * `f_7100049940` enumerates a node's slots through the node
             * vtable: `vt[16]` = `f_710004f158` = `node[153]` is the count,
             * `vt[24]` = `f_710004f160` = `*(node + 40*i + 192)` is the slot's
             * node, and `vt[32]` = `f_710004f170` = `*(int *)(node + 40*i +
             * 184)` is a flag tested at 0x49a08:
             *
             *     if (flag == 0)  ->  0x49994: vt[40](i, &sel, &mask)
             *                                  f_7100049780(..., operand, ...)
             *     else            ->  0x49a1c: f_7100049940(..., operand)
             *
             * -- a zero flag RELEASES the operand, a non-zero flag RECURSES
             * into its own slots with the consumer unchanged.  That is the
             * whole of the folded-subtree question notes/51 left open: the
             * enumeration really does hand back the raw slot node, and the
             * flattening is this branch, not an owner lookup.
             */
            fputs(" inl=", stderr);
            if (!nops2)
                fputs("none", stderr);
            for (i_ = 0; i_ < (int)nops2; ++i_) {
                sn2 = 0;
                g2s_try_u64(cur + 0xa8 + 0x28ULL * (unsigned)i_ + 16, &sn2);
                fprintf(stderr, "%s%llu", i_ ? "," : "",
                        (unsigned long long)(unsigned)(sn2 & 0xffffffff));
            }
            /* THE EDGE BUILDER'S INPUTS.
             *
             * `f_710004ab80` -- the one place an edge is created -- reads
             * five things about a node and nothing else:
             *
             *   node[40]   non-zero means it is not a definition at all
             *   node[92]   a row index into `program[816]`, 224 bytes a row
             *   node[136]  the INSTRUCTION RECORD; edges join records, and
             *              `record[56]` is the successor list the order
             *              model is handed today
             *   row[24]    the destination's component permutation, applied
             *              to a write mask by `f_7100051890`
             *   row[28] / row[16] / row[64]
             *              the register CLASS and the bit position; when
             *              `row[28]` is zero and `row[16] - 0x6f <=u 0x90`
             *              the position is `(row[16] << 3) - 0x378`, else
             *              it is `row[64]` and -1 means there is none
             *   row[8]     a type id, which `f_7100bdfaa8` turns into the
             *              number of BITS a component occupies
             *
             * All six are dumped so the model can do that arithmetic itself
             * rather than being handed the answer.
             */
            {
                unsigned long long n40 = 0, n92 = 0, ent = 0, row = 0, r = 0;
                if (g2s_try_u64(cur + 40, &n40))
                    /* node[40] and node[44] share the word at +40:
                     * `f_710004ab80` refuses a node whose [40] is set, and
                     * `f_7100056910`'s deep arm tests slot 0's [44]. */
                    fprintf(stderr, " n40=%u n44=%u",
                            (unsigned)(n40 & 0xffffffff),
                            (unsigned)(n40 >> 32));
                if (g2s_try_u64(cur + 88, &n92))
                    n92 >>= 32;
                else
                    n92 = 0;
                fprintf(stderr, " n92=%u", (unsigned)n92);
                if (g2s_try_u64(cur + 136, &ent))
                    fprintf(stderr, " ent=%#llx", ent);
                fputs(" row=", stderr);
                if (n92 && prog_of_block && !(prog_of_block & 7)
                    && g2s_try_u64(prog_of_block + 816, &row) && row
                    && !(row & 7)) {
                    unsigned long long base = row + 224ULL * n92;
                    unsigned r8 = 0, r16 = 0, r24 = 0;
                    int r28 = 0, r64 = -1;
                    if (g2s_try_u64(base + 8, &r))
                        r8 = (unsigned)(r & 0xffffffff);
                    if (g2s_try_u64(base + 16, &r))
                        r16 = (unsigned)(r & 0xffffffff);
                    if (g2s_try_u64(base + 24, &r)) {
                        r24 = (unsigned)(r & 0xffffffff);
                        r28 = (int)(unsigned)(r >> 32);
                    }
                    if (g2s_try_u64(base + 64, &r))
                        r64 = (int)(unsigned)(r & 0xffffffff);
                    fprintf(stderr, "%u,%u,%#x,%d,%d", r8, r16, r24, r28,
                            r64);
                } else {
                    fputs("none", stderr);
                }
            }
            /* Slot i's SOURCE selector and mask, `vt[40]` =
             * `f_710004d4d4`: `*(node + 40*i + 200)` and `+ 204`.  These
             * are what `f_7100049780` and `f_710004bd60` hand to the edge
             * builder as the components a use actually reads. */
            fputs(" sel=", stderr);
            if (!nops2)
                fputs("none", stderr);
            for (i_ = 0; i_ < (int)nops2; ++i_) {
                sn2 = 0;
                g2s_try_u64(cur + 0xa8 + 0x28ULL * (unsigned)i_ + 32, &sn2);
                fprintf(stderr, "%s%#x/%#x", i_ ? "," : "",
                        (unsigned)(sn2 & 0xffffffff), (unsigned)(sn2 >> 32));
            }
            g2s_dump_implicit(cpu, prog_of_block, cur);
            g2s_dump_n120(cpu, prog_of_block, cur);
            fputc('\n', stderr);
        }
    }

    if (!g2s_try_u64(blk + 80, &e))
        return;
    /* The chain is walked with a bound rather than to its end: a field read
     * wrong here would otherwise be an infinite loop inside the instrument,
     * which looks exactly like the compiler hanging. */
    for (guard = 0; e && !(e & 7) && guard < 4096; ++guard) {
        node = 0;
        if (!g2s_try_u64(e + 32, &node))
            node = 0;
        fprintf(stderr, "g2s_ble entry=%#llx node=%#llx", e, node);
        if (node && !(node & 7)) {
            if (g2s_try_u64(node + 8, &w))
                fprintf(stderr, " op=%#llx", w & 0xffffffff);
            if (g2s_try_u64(node + 88, &w))
                fprintf(stderr, " pend=%lld vr=%llu",
                        (long long)(int)(w & 0xffffffff), w >> 32);
            if (g2s_try_u64(node + 48, &w))
                fprintf(stderr, " mask=%#llx", w & 0xffffffff);
            if (g2s_try_u64(node + 32, &w))
                fprintf(stderr, " seq=%llu/%llu", w & 0xffffffff, w >> 32);
            if (g2s_try_u64(node + 136, &w))
                fprintf(stderr, " sent=%#llx", w);
        }
        fputc('\n', stderr);
        if (!g2s_try_u64(e, &e))
            break;
    }
}

/* g2s_dump_sd -- every node the edge builder can reach, at the right moment.
 *
 * `g2s_trace_stamps` prints the block's instruction list, which is the nodes
 * that will be SCHEDULED.  `f_710004ab80` is also called with nodes that will
 * not be -- an operand is a node like any other -- so a model built only on
 * the list silently skips most of the calls (measured on `cf_while.vert`: 44
 * of the compiler's 96).
 *
 * `g2s_trace_block` does walk the whole DAG, but in PASS 1, and the row at
 * `program[816] + 224*node[92]` is filled in between the passes: `row[64]`
 * is the assigned register and reads -1 before the allocator has run.  So the
 * walk has to happen here, at the second pass's entry, and this is it.
 *
 *   g2s_sd node=<n> op=<..> n40=.. n44=.. n92=.. ent=.. row=..
 *          src=.. inl=.. sel=.. irx=..
 *
 * one line per node, each printed once per firing.
 */
static void g2s_dump_sd(cpu_t *cpu, unsigned long long prog,
                        unsigned long long node,
                        unsigned long long *seen, int *ns, int depth)
{
    unsigned long long w, nops = 0, sn, rw, r;
    int i;

    if (!node || (node & 7) || depth > 64)
        return;
    for (i = 0; i < *ns; ++i)
        if (seen[i] == node)
            return;
    if (*ns >= 2048)
        return;
    seen[(*ns)++] = node;

    fprintf(stderr, "g2s_sd node=%#llx", node);
    /* node[0] is the CLASS vtable.  `node[36]` is a fresh creation ordinal
     * for some nodes and a copy of the source's for others, and nothing
     * dumped so far separates the two -- op, n92 and the row are the same on
     * both sides of the split.  The two sets sit in different arenas, which
     * means different constructors, which means different classes, so this
     * is where the difference should show. */
    if (g2s_try_u64(node, &w))
        fprintf(stderr, " vt=%#llx", w);
    if (g2s_try_u64(node + 8, &w))
        fprintf(stderr, " op=%#llx", w & 0xffffffff);
    /* THE WHOLE SOURCE POSITION, not just its third word.  notes/40 has
     * `node[28..35] = cg[64]` and `node[36] = cg[72]`, and the selector's two
     * keys are the halfwords at +28 / +30 (tested for EQUALITY, 0x4b9b8) and
     * the word at +36 (tested for ORDER, 0x4b9dc).  Printing all four says
     * what the position is made of. */
    if (g2s_try_u64(node + 32, &w))
        /* node[32] low and node[36] high.  node[36] is the creation ordinal:
         * measured over all 113 probes it equals the node's first-appearance
         * index at `f_710004f530`, 930 of 930 exactly -- but only 63% of
         * scheduled nodes reach that function, so the rest take their number
         * some other way and this prints it for every node so that rule can
         * be found instead of guessed. */
        fprintf(stderr, " w32=%u n36=%u", (unsigned)(w & 0xffffffff),
                (unsigned)(w >> 32));
    if (g2s_try_u64(node + 24, &w))
        fprintf(stderr, " h28=%u h30=%u", (unsigned)((w >> 32) & 0xffff),
                (unsigned)((w >> 48) & 0xffff));
    if (g2s_try_u64(node + 40, &w))
        fprintf(stderr, " n40=%u n44=%u", (unsigned)(w & 0xffffffff),
                (unsigned)(w >> 32));
    if (g2s_try_u64(node + 48, &w))
        fprintf(stderr, " mask=%#llx", w & 0xffffffff);
    w = 0;
    if (!g2s_try_u64(node + 88, &w))
        w = 0;
    fprintf(stderr, " n92=%u", (unsigned)(w >> 32));
    {
        unsigned long long n92 = w >> 32;
        if (g2s_try_u64(node + 136, &r))
            fprintf(stderr, " ent=%#llx", r);
        fputs(" row=", stderr);
        if (n92 && prog && !(prog & 7) && g2s_try_u64(prog + 816, &rw)
            && rw && !(rw & 7)) {
            unsigned long long b2 = rw + 224ULL * n92;
            unsigned q8 = 0, q16 = 0, q24 = 0;
            int q28 = 0, q64 = -1;
            if (g2s_try_u64(b2 + 8, &r))
                q8 = (unsigned)(r & 0xffffffff);
            if (g2s_try_u64(b2 + 16, &r))
                q16 = (unsigned)(r & 0xffffffff);
            if (g2s_try_u64(b2 + 24, &r)) {
                q24 = (unsigned)(r & 0xffffffff);
                q28 = (int)(unsigned)(r >> 32);
            }
            if (g2s_try_u64(b2 + 64, &r))
                q64 = (int)(unsigned)(r & 0xffffffff);
            fprintf(stderr, "%u,%u,%#x,%d,%d", q8, q16, q24, q28, q64);
            /* The allocator's own inputs, so a candidate rule for WHICH
             * register a node gets can be checked against them instead of
             * inferred from the answer: `+0x30` is the cost it computes,
             * `+0x50` the component mask it popcounts, and `+0x58` the packed
             * TIE link -- low 28 bits a signed vreg index, top 4 bits a signed
             * component delta -- which is what makes a destination share its
             * source's register. */
            {
                unsigned q48 = 0, q80 = 0, q88 = 0;
                if (g2s_try_u64(b2 + 48, &r))
                    q48 = (unsigned)(r & 0xffffffff);
                if (g2s_try_u64(b2 + 80, &r))
                    q80 = (unsigned)(r & 0xffffffff);
                if (g2s_try_u64(b2 + 88, &r))
                    q88 = (unsigned)(r & 0xffffffff);
                fprintf(stderr, " cost=%u cm=%#x tie=%#x", q48, q80, q88);
            }
            /* The IMPLICIT REGISTER READS (notes/51): a node can read vregs
             * that are not among its operands, through the opcode row's four
             * component lists and through `node[120]`'s chain.  Printed here
             * because the liveness sweep's block nodes appear to make vregs
             * live with no operand to explain it (notes/52 section 7). */

        } else {
            fputs("none", stderr);
        }
        /* The IMPLICIT REGISTER READS (notes/51): a node can read vregs that
         * are not among its operands, through the opcode row's four component
         * lists and through `node[120]`'s chain.  Printed for EVERY node,
         * including the `0x8` block nodes, because those are what the liveness
         * sweep makes vregs live at with no operand to explain it
         * (notes/52 section 7). */
        g2s_dump_implicit(cpu, prog, node);
    }
    /* node[72] -- THE POOL'S ALLOCATION CHAIN.
     *
     * `f_710004f770` ends with `node[72] = pool[1096]; pool[1096] = node`
     * (0x4f828), so every node a pool makes is pushed onto a list through
     * this field, newest first.  `node[36]` is that same pool's counter
     * (0x4f7f4), which makes the chain the allocation ORDER itself -- and
     * unlike `node[36]` it also says WHICH pool a node belongs to, which is
     * what address clustering cannot: two pools draw from one heap and their
     * nodes interleave 0xd0 apart, which is why two adjacent nodes can carry
     * the same number. */
    if (g2s_try_u64(node + 72, &r))
        fprintf(stderr, " link=%#llx", r);
    if (g2s_try_u64(node + 152, &w))
        nops = (w >> 8) & 0xff;
    if (nops > 16)
        nops = 16;
    fputs(" src=", stderr);
    if (!nops)
        fputs("none", stderr);
    for (i = 0; i < (int)nops; ++i) {
        sn = 0;
        g2s_try_u64(node + 0xa8 + 0x28ULL * (unsigned)i + 24, &sn);
        fprintf(stderr, "%s%#llx", i ? "," : "", sn);
    }
    fputs(" inl=", stderr);
    if (!nops)
        fputs("none", stderr);
    for (i = 0; i < (int)nops; ++i) {
        sn = 0;
        g2s_try_u64(node + 0xa8 + 0x28ULL * (unsigned)i + 16, &sn);
        fprintf(stderr, "%s%u", i ? "," : "", (unsigned)(sn & 0xffffffff));
    }
    fputs(" sel=", stderr);
    if (!nops)
        fputs("none", stderr);
    for (i = 0; i < (int)nops; ++i) {
        sn = 0;
        g2s_try_u64(node + 0xa8 + 0x28ULL * (unsigned)i + 32, &sn);
        fprintf(stderr, "%s%#x/%#x", i ? "," : "",
                (unsigned)(sn & 0xffffffff), (unsigned)(sn >> 32));
    }
    g2s_dump_n120(cpu, prog, node);
    fputc('\n', stderr);

    for (i = 0; i < (int)nops; ++i) {
        sn = 0;
        if (g2s_try_u64(node + 0xa8 + 0x28ULL * (unsigned)i + 24, &sn) && sn)
            g2s_dump_sd(cpu, prog, sn, seen, ns, depth + 1);
    }
}

/* g2s_trace_sel -- the SECOND pass's selector, list and choice.
 *
 * notes/51 §4: `f_710004b220` schedules each block a second time and its
 * output is the printed order.  Its selector is `f_710004ba90`, whose
 * comparator keeps the SMALLEST `node[36]` and breaks ties on the EARLIEST
 * `entry[68]` -- and that does not by itself explain `op_pow.vert`, whose
 * four `POW`s carry `node[36]` 3, 4, 5, 6 and print 3, 6, 5, 4.  Either the
 * readiness filter at 0x4bb0c staggers the candidates or a key is misread,
 * and the difference is visible only in the list the selector is looking at.
 *
 * So this prints the whole candidate list at the selector's entry, with both
 * keys per entry, exactly as `g2s_trace_pick` does for the first pass
 * (notes/49) -- the same instrument, pointed at the other scheduler:
 *
 *   g2s_sel list=<x2[16]> | e=<entry>,n=<node>,w32=..,w36=..,t68=..  ...
 *   g2s_selected e=<entry> n=<node>
 *
 * The list is at `x2[16]` and chained through `entry[0]`, and the entry's
 * node is at `entry[8]` -- both read out of `f_710004ba90` itself
 * (0x4baa8, 0x4baf0, 0x4bb14).
 */
void g2s_trace_sel(cpu_t *cpu, unsigned long addr)
{
    unsigned long long head, e, node, w, guard;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("sel"))
        return;
    (void)addr;
    head = GST_I64(GUEST_OFF_X0 + 8 * 2);
    if (!head || (head & 7))
        return;
    if (!g2s_try_u64(head + 16, &e))
        return;
    fprintf(stderr, "g2s_sel list=%#llx", head + 16);
    /* The CYCLE CLOCK the readiness predicate tests against.  `f_71000476f0`
     * compares `entry[52]` with `cg[28]` and admits the entry only if the
     * clock has reached it, with `cg[8]` choosing the direction of the
     * comparison and `cg[20]` a resource mask; `cg` is `x0[8]` here, the same
     * object whose `vt[16]` the selector calls (0x4baf8). */
    {
        unsigned long long holder = GST_I64(GUEST_OFF_X0), cg = 0, w;
        if (holder && !(holder & 7) && g2s_try_u64(holder + 8, &cg)
            && cg && !(cg & 7)) {
            if (g2s_try_u64(cg + 24, &w))
                fprintf(stderr, " clock=%lld", (long long)(int)(w >> 32));
            if (g2s_try_u64(cg + 8, &w))
                fprintf(stderr, " dir=%llu", w & 0xff);
            if (g2s_try_u64(cg + 16, &w))
                fprintf(stderr, " rmask=%#llx", w >> 32);
        }
    }
    fputs(" |", stderr);
    for (guard = 0; e && !(e & 7) && guard < 4096; ++guard) {
        node = 0;
        if (!g2s_try_u64(e + 8, &node))
            node = 0;
        fprintf(stderr, " e=%#llx,n=%#llx", e, node);
        if (node && !(node & 7)) {
            if (g2s_try_u64(node + 32, &w))
                fprintf(stderr, ",w32=%llu,w36=%llu", w & 0xffffffff, w >> 32);
            if (g2s_try_u64(node + 8, &w))
                fprintf(stderr, ",op=%#llx", w & 0xffffffff);
            if (g2s_try_u64(node + 48, &w))
                fprintf(stderr, ",mask=%#llx", w & 0xffffffff);
        }
        /* entry[52] is the FIRST pass's depth (`f_7100049780` writes
         * `cg[28] - 1` into it) and is the field the predicate gates on, so
         * it belongs beside the two comparator keys. */
        if (g2s_try_u64(e + 48, &w))
            fprintf(stderr, ",p52=%lld", (long long)(int)(w >> 32));
        if (g2s_try_u64(e + 64, &w))
            fprintf(stderr, ",t68=%lld", (long long)(int)(w >> 32));
        if (!g2s_try_u64(e, &e))
            break;
    }
    fputc('\n', stderr);
}

/* g2s_trace_selected -- what `f_710004ba90` returned, on its return block.
 * The chosen entry comes back in X0 (0x4bb9c: `mov x0, x22`). */
void g2s_trace_selected(cpu_t *cpu, unsigned long addr)
{
    unsigned long long e, node = 0;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("sel"))
        return;
    (void)addr;
    /* X22 holds the running best and is what the epilogue moves into X0; the
     * hook sits on the block that does that, so X22 is the answer and X0 is
     * not yet set. */
    e = GST_I64(GUEST_OFF_X0 + 8 * 22);
    if (e && !(e & 7))
        g2s_try_u64(e + 8, &node);
    fprintf(stderr, "g2s_selected e=%#llx n=%#llx\n", e, node);
}

/* g2s_trace_stamps -- what the FIRST pass leaves behind, at the second's entry.
 *
 * notes/51: the two passes talk to each other through two fields of the
 * scheduling entry.  `entry[52]` is the depth `f_71000476f0` gates on and
 * `entry[68]` is the tie-break the selector uses, and both are written during
 * pass 1 -- so a model of pass 2 that takes them from the compiler is only
 * half a model, and the half that is missing is whatever pass 1 computed.
 *
 * `f_710004b220` is pass 2 and has not run its loop at its entry, so `p52` and
 * `t68` here are exactly pass 1's output.
 *
 * WHAT IS NOT SOUND HERE: `succ=` and `npred=`.  The dependence edges are
 * built INSIDE `f_710004b220` (0x4b5ec calls `f_710004bd60`), so at this hook
 * they still hold whatever was left from before -- an entry that reads
 * `npred=1` here has `npred=4` once the build has run.  Use `g2s_trace_push`
 * for the graph; these two fields are printed only because seeing them stale
 * is more useful than not seeing them at all (notes/51 §6).
 *
 * Per block, per instruction in `block[32]`:
 *
 *   g2s_st blk=<b> node=<n> op=<..> seq=<..> entry=<e> p52=<..> t68=<..>
 *
 * The instruction list is walked the way notes/29 reads it -- `block[32]` to
 * the first record, `instr[8]` to the next -- and the node is at `instr[56]`.
 */
void g2s_trace_stamps(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, head, blk, il, ins, node, ent, w, guard, bg;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("stamps"))
        return;
    (void)addr;
    /* f_710004b220(x0 = the step holder, x1 = program, x2 = the block chain) */
    prog = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!prog || (prog & 7))
        return;
    if (!g2s_try_u64(prog + 184, &head) || !head)
        return;
    if (!g2s_try_u64(head, &blk))
        return;
    /* EVERY POOL'S ALLOCATION CHAIN, newest first.  Position from the END is
     * the node's `node[36]`: the counter and the push happen in the same
     * constructor, one per allocation.  Printed before the DAG so a node's
     * pool and index are available when its `g2s_sd` line is read. */
    {
        int pi;
        for (pi = 0; pi < g2s_npools; ++pi) {
            unsigned long long p = g2s_pools[pi], rec = 0, g3, ctr = 0;
            int n3 = 0;
            if (!g2s_try_u64(p + 1096, &rec))
                continue;
            g2s_try_u64(p + 1104, &ctr);
            fprintf(stderr, "g2s_pool pool=%#llx ctr=%u\n", p,
                    (unsigned)(ctr & 0xffffffff));
            for (g3 = 0; rec && !(rec & 7) && g3 < 4096; ++g3) {
                unsigned long long q = 0, nxt = 0, vt = 0, op = 0;
                g2s_try_u64(rec + 32, &q);
                /* node[0] is the CLASS vtable.  The counter does not advance
                 * for every allocation -- nine nodes here carry 1,1,1,3,3,3,
                 * 3,3,4 -- so nodes fall into runs that share a number, and
                 * the selector compares those runs.  If a run boundary is a
                 * change of class, the runs are derivable; this prints the
                 * class so that can be tested rather than assumed. */
                g2s_try_u64(rec, &vt);
                g2s_try_u64(rec + 8, &op);
                fprintf(stderr,
                        "g2s_pn pool=%#llx node=%#llx n36=%u vt=%#llx"
                        " op=%#llx\n",
                        p, rec, (unsigned)(q >> 32), vt,
                        op & 0xffffffff);
                ++n3;
                if (!g2s_try_u64(rec + 72, &nxt))
                    break;
                rec = nxt;
            }
            fprintf(stderr, "g2s_pend pool=%#llx len=%d\n", p, n3);
        }
    }

    /* THE WHOLE DAG, at this moment -- see g2s_dump_sd.  It runs before the
     * per-block listing so the `g2s_sd` lines are all in one place. */
    {
        static unsigned long long sdseen[2048];
        int sdn = 0;
        unsigned long long b2 = blk, i2, n2, g2;
        for (g2 = 0; b2 && !(b2 & 7) && g2 < 4096; ++g2) {
            if (g2s_try_u64(b2 + 32, &i2) && i2 && g2s_try_u64(i2, &n2)) {
                unsigned long long gg;
                for (gg = 0; n2 && !(n2 & 7) && gg < 4096; ++gg) {
                    unsigned long long nd = 0;
                    if (g2s_try_u64(n2 + 56, &nd) && nd && !(nd & 7))
                        g2s_dump_sd(cpu, prog, nd, sdseen, &sdn, 0);
                    if (!g2s_try_u64(n2 + 8, &n2))
                        break;
                }
            }
            /* the block chain's link is at +288, as the listing walk
             * below uses -- not +0. */
            if (!g2s_try_u64(b2 + 288, &b2))
                break;
        }
    }
    for (bg = 0; blk && !(blk & 7) && bg < 4096; ++bg) {
        if (g2s_try_u64(blk + 32, &il) && il && g2s_try_u64(il, &ins)) {
            for (guard = 0; ins && !(ins & 7) && guard < 4096; ++guard) {
                node = ent = 0;
                if (!g2s_try_u64(ins + 56, &node))
                    node = 0;
                fprintf(stderr, "g2s_st blk=%#llx node=%#llx", blk, node);
                if (node && !(node & 7)) {
                    if (g2s_try_u64(node + 8, &w))
                        fprintf(stderr, " op=%#llx", w & 0xffffffff);
                    if (g2s_try_u64(node + 32, &w))
                        fprintf(stderr, " seq=%llu", w >> 32);
                    if (g2s_try_u64(node + 48, &w))
                        fprintf(stderr, " mask=%#llx", w & 0xffffffff);
                    if (!g2s_try_u64(node + 136, &ent))
                        ent = 0;
                    /* THE SOURCE SLOTS, from the same compile as everything
                     * else this hook prints.  `g2s_trace_fold` already dumps
                     * them, but it runs in the OTHER compile (notes/05: the
                     * module is read twice) and the allocator reuses
                     * addresses, so joining the two dumps by node address
                     * joins different objects -- measured, the opcode agrees
                     * on 48 of 180 nodes.  Source i is at node + 0xa8 +
                     * 0x28*i and node[153] counts them (notes/47). */
                    {
                        unsigned long long nops = 0, si, sn;
                        int k;
                        if (g2s_try_u64(node + 152, &nops))
                            nops = (nops >> 8) & 0xff;
                        if (nops > 16)
                            nops = 16;
                        fputs(" src=", stderr);
                        if (!nops)
                            fputs("none", stderr);
                        for (k = 0; k < (int)nops; ++k) {
                            si = node + 0xa8 + 0x28ULL * (unsigned)k;
                            sn = 0;
                            g2s_try_u64(si + 24, &sn);
                            fprintf(stderr, "%s%#llx", k ? "," : "", sn);
                        }
                        /* THE EDGE BUILDER'S INPUTS, READ HERE AND NOT IN
                         * `g2s_trace_block`.
                         *
                         * The row at `program[816] + 224*node[92]` is filled
                         * in between the two passes: measured on
                         * `op_mul.vert`, node[92] == 2's row reads
                         * `row[16] = 0, row[64] = -1` at `f_710004a2e0`'s
                         * entry and `row[16] = 512, row[64] = 0` at
                         * `f_710004b220`'s.  `row[64]` is the ASSIGNED
                         * register, so reading it in pass 1 gets the value
                         * before the assignment and every class-3 node looks
                         * like it has no place at all.  This hook runs at the
                         * second pass's entry, before the edges are built,
                         * which is the moment `f_710004ab80` sees.
                         */
                        fputs(" inl=", stderr);
                        if (!nops)
                            fputs("none", stderr);
                        for (k = 0; k < (int)nops; ++k) {
                            sn = 0;
                            g2s_try_u64(node + 0xa8 + 0x28ULL * (unsigned)k
                                        + 16, &sn);
                            fprintf(stderr, "%s%u", k ? "," : "",
                                    (unsigned)(sn & 0xffffffff));
                        }
                        fputs(" sel=", stderr);
                        if (!nops)
                            fputs("none", stderr);
                        for (k = 0; k < (int)nops; ++k) {
                            sn = 0;
                            g2s_try_u64(node + 0xa8 + 0x28ULL * (unsigned)k
                                        + 32, &sn);
                            fprintf(stderr, "%s%#x/%#x", k ? "," : "",
                                    (unsigned)(sn & 0xffffffff),
                                    (unsigned)(sn >> 32));
                        }
                    }
                    {
                        unsigned long long n40 = 0, n92 = 0, rw = 0, r = 0;
                        if (g2s_try_u64(node + 40, &n40))
                            fprintf(stderr, " n40=%u n44=%u",
                                    (unsigned)(n40 & 0xffffffff),
                                    (unsigned)(n40 >> 32));
                        if (g2s_try_u64(node + 88, &n92))
                            n92 >>= 32;
                        else
                            n92 = 0;
                        fprintf(stderr, " n92=%u", (unsigned)n92);
                        fputs(" row=", stderr);
                        if (n92 && g2s_try_u64(prog + 816, &rw) && rw
                            && !(rw & 7)) {
                            unsigned long long b2 = rw + 224ULL * n92;
                            unsigned q8 = 0, q16 = 0, q24 = 0;
                            int q28 = 0, q64 = -1;
                            if (g2s_try_u64(b2 + 8, &r))
                                q8 = (unsigned)(r & 0xffffffff);
                            if (g2s_try_u64(b2 + 16, &r))
                                q16 = (unsigned)(r & 0xffffffff);
                            if (g2s_try_u64(b2 + 24, &r)) {
                                q24 = (unsigned)(r & 0xffffffff);
                                q28 = (int)(unsigned)(r >> 32);
                            }
                            if (g2s_try_u64(b2 + 64, &r))
                                q64 = (int)(unsigned)(r & 0xffffffff);
                            fprintf(stderr, "%u,%u,%#x,%d,%d", q8, q16, q24,
                                    q28, q64);
                        } else {
                            fputs("none", stderr);
                        }
                        g2s_dump_n120(cpu, prog, node);
                    }
                }
                fprintf(stderr, " entry=%#llx", ent);
                if (ent && !(ent & 7)) {
                    unsigned long long r, rg;
                    if (g2s_try_u64(ent + 48, &w))
                        fprintf(stderr, " p52=%lld",
                                (long long)(int)(w >> 32));
                    if (g2s_try_u64(ent + 64, &w))
                        fprintf(stderr, " t68=%lld",
                                (long long)(int)(w >> 32));
                    /* The RESOURCE RECORDS.  `f_71000476f0` walks the chain at
                     * entry[24] through r[0] and returns the first whose
                     * r[8] does not overlap the cycle's mask, and
                     * `f_7100047768` ORs that r[8] in.  So the chain is the
                     * entry's alternative functional units and r[8] is the
                     * unit's bit -- neither is the node's opcode. */
                    /* The SUCCESSOR EDGES, entry[56] chained through e[0],
                     * each edge's successor entry at e[8] and its kind at
                     * e[16] (f_710004ab80, 0x4acb8..0x4acf0).  The order of
                     * this list decides the worklist's order and so the
                     * scan's answer, and it is not derivable from the block's
                     * list order -- two probes give opposite answers -- so it
                     * is dumped rather than inferred.  The successor's NODE is
                     * printed, since that is what the listing names. */
                    if (g2s_try_u64(ent + 56, &r)) {
                        unsigned long long se, sn;
                        fputs(" succ=", stderr);
                        for (rg = 0; r && !(r & 7) && rg < 32; ++rg) {
                            se = sn = 0;
                            if (g2s_try_u64(r + 8, &se) && se && !(se & 7))
                                g2s_try_u64(se + 8, &sn);
                            if (g2s_try_u64(r + 16, &w))
                                fprintf(stderr, "%s%#llx/%llu", rg ? "," : "",
                                        sn, w & 0xffffffff);
                            else
                                fprintf(stderr, "%s%#llx", rg ? "," : "", sn);
                            if (!g2s_try_u64(r, &r))
                                break;
                        }
                        if (!rg)
                            fputs("none", stderr);
                    }
                    if (g2s_try_u64(ent + 64, &w))
                        fprintf(stderr, " npred=%llu", w & 0xffffffff);
                    if (g2s_try_u64(ent + 24, &r)) {
                        fputs(" res=", stderr);
                        for (rg = 0; r && !(r & 7) && rg < 16; ++rg) {
                            if (g2s_try_u64(r + 8, &w))
                                fprintf(stderr, "%s%#llx", rg ? "," : "",
                                        w & 0xffffffff);
                            if (!g2s_try_u64(r, &r))
                                break;
                        }
                        if (!rg)
                            fputs("none", stderr);
                    }
                }
                fputc('\n', stderr);
                if (!g2s_try_u64(ins + 8, &ins))
                    break;
            }
        }
        if (!g2s_try_u64(blk + 288, &blk))
            break;
    }
}

/* g2s_trace_push -- when an entry enters the SECOND pass's worklist, and the
 * predecessor count that let it.
 *
 * notes/51 §6 reads the release at 0x4b73c and the two pushes at 0x4b7a0 and
 * 0x4b80c, and a model built on that reading puts nodes on the worklist the
 * compiler does not -- `un_sqrt.vert`'s four `0x47` nodes arrive around clock
 * 128 with their depths raised to 128, not at clock 0 when their only
 * predecessor in the DAG issues.  Rather than guess which extra edge holds
 * them back, this prints the events themselves:
 *
 *   g2s_dec  node=<n> npred=<after> clock=<cg[28]>    at 0x4b740
 *   g2s_push node=<n> p52=<..> clock=<..> site=<..>   at 0x4b7a0 / 0x4b80c
 *
 * The site distinguishes the initial build from the release, which is the
 * difference between "it was ready at the start" and "a predecessor freed it".
 */
void g2s_trace_push(cpu_t *cpu, unsigned long addr)
{
    unsigned long long ent, node = 0, w, prog, cg = 0;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("push"))
        return;
    /* 0x4b740 decrements through X8; both pushes have the entry in X8 too
     * (0x4b7a0 reloads it from the edge, 0x4b80c has it in X23). */
    ent = GST_I64(GUEST_OFF_X0 + 8 * (addr == 0x710004b80cUL ? 23 : 8));
    if (!ent || (ent & 7))
        return;
    g2s_try_u64(ent + 8, &node);
    prog = GST_I64(GUEST_OFF_X0 + 8 * 19);          /* x19 = the program */
    if (prog && !(prog & 7) && g2s_try_u64(prog + 768, &cg) && cg && !(cg & 7))
        g2s_try_u64(cg + 24, &cg);
    fprintf(stderr, "%s node=%#llx", addr == 0x710004b740UL ? "g2s_dec"
                                                            : "g2s_push",
            node);
    if (g2s_try_u64(ent + 64, &w))
        fprintf(stderr, " npred=%lld", (long long)(int)(w & 0xffffffff));
    if (g2s_try_u64(ent + 48, &w))
        fprintf(stderr, " p52=%lld", (long long)(int)(w >> 32));
    fprintf(stderr, " clock=%lld site=%#lx\n",
            (long long)(int)(cg >> 32), addr);
}

/* g2s_trace_graph -- the dependence graph the SECOND pass actually schedules.
 *
 * notes/51 §6: `g2s_trace_stamps` runs at `f_710004b220`'s entry, which is
 * BEFORE the edges are built, so its `succ=`/`npred=` are stale.  This runs at
 * 0x4b598, which is reached once a block's edges are built and its initial
 * worklist made -- so what it prints is the real input to the scheduling loop.
 *
 * The graph is not the DAG.  `f_710004ab80`'s `kind == 2` arm swaps producer
 * and consumer (0x4acd0), so an ANTI-dependence is an edge the other way, and
 * `f_710004b220` asks for that kind (0x4b5e8).  On `un_sqrt.vert` one `0x47`
 * node has four predecessors where the DAG gives it one, which is exactly the
 * gap between a model built on the DAG and the compiler.
 *
 *   g2s_gr blk=<b> node=<n> npred=<n> p52=<..> t68=<..> succ=<node>/<kind>,...
 */
void g2s_trace_graph(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, head, blk, il, ins, node, ent, w, r, guard, bg, rg;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("graph2"))
        return;
    (void)addr;
    prog = GST_I64(GUEST_OFF_X0 + 8 * 19);          /* x19 = the program */
    if (!prog || (prog & 7))
        return;
    if (!g2s_try_u64(prog + 184, &head) || !head)
        return;
    if (!g2s_try_u64(head, &blk))
        return;
    for (bg = 0; blk && !(blk & 7) && bg < 4096; ++bg) {
        if (g2s_try_u64(blk + 32, &il) && il && g2s_try_u64(il, &ins)) {
            for (guard = 0; ins && !(ins & 7) && guard < 4096; ++guard) {
                node = ent = 0;
                if (!g2s_try_u64(ins + 56, &node))
                    node = 0;
                if (node && !(node & 7))
                    g2s_try_u64(node + 136, &ent);
                if (node && ent && !(ent & 7)) {
                    fprintf(stderr, "g2s_gr blk=%#llx node=%#llx", blk, node);
                    if (g2s_try_u64(ent + 64, &w))
                        fprintf(stderr, " npred=%lld",
                                (long long)(int)(w & 0xffffffff));
                    if (g2s_try_u64(ent + 48, &w))
                        fprintf(stderr, " p52=%lld",
                                (long long)(int)(w >> 32));
                    if (g2s_try_u64(ent + 64, &w))
                        fprintf(stderr, " t68=%lld",
                                (long long)(int)(w >> 32));
                    fputs(" succ=", stderr);
                    if (g2s_try_u64(ent + 56, &r)) {
                        for (rg = 0; r && !(r & 7) && rg < 64; ++rg) {
                            unsigned long long se = 0, sn = 0, kind = 0;
                            if (g2s_try_u64(r + 8, &se) && se && !(se & 7))
                                g2s_try_u64(se + 8, &sn);
                            g2s_try_u64(r + 16, &kind);
                            fprintf(stderr, "%s%#llx/%llu", rg ? "," : "",
                                    sn, kind & 0xffffffff);
                            if (!g2s_try_u64(r, &r))
                                break;
                        }
                        if (!rg)
                            fputs("none", stderr);
                    }
                    fputc('\n', stderr);
                }
                if (!g2s_try_u64(ins + 8, &ins))
                    break;
            }
        }
        if (!g2s_try_u64(blk + 288, &blk))
            break;
    }
}

/* g2s_trace_edge -- every call on the edge builder, at its entry.
 *
 * notes/51 §8 reads `f_710004ab80` as the only place an edge is made, and
 * `tools/edges.py` runs that reading.  This is what the reading is checked
 * against: the arguments the compiler really passes, in order.
 *
 *   g2s_edge node=<x1> mask=<w2> ctx=<x3> kind=<w4> n92=<..> row64=<..>
 *
 * `row64` is printed as well because the model's first disagreement was
 * there -- the class-3 rows it reads carry -1, which the reading says means
 * "no position", and yet the compiler builds edges for those nodes.  Printing
 * it beside the call says whether the field or the reading is wrong.
 */
void g2s_trace_edge(cpu_t *cpu, unsigned long addr);
void g2s_trace_edge(cpu_t *cpu, unsigned long addr)
{
    unsigned long long node, ctx, w, prog, row;
    unsigned mask, kind, n92 = 0;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("edge"))
        return;
    (void)addr;
    node = GST_I64(GUEST_OFF_X0 + 8 * 1);
    mask = (unsigned)GST_I64(GUEST_OFF_X0 + 8 * 2);
    ctx = GST_I64(GUEST_OFF_X0 + 8 * 3);
    kind = (unsigned)GST_I64(GUEST_OFF_X0 + 8 * 4);
    fprintf(stderr, "g2s_edge node=%#llx mask=%#x ctx=%#llx kind=%u",
            node, mask, ctx, kind);
    if (node && !(node & 7) && g2s_try_u64(node + 88, &w)) {
        n92 = (unsigned)(w >> 32);
        fprintf(stderr, " n92=%u", n92);
    }
    {
        unsigned long long S = GST_I64(GUEST_OFF_X0 + 8 * 0);
        prog = 0;
        if (n92 && S && !(S & 7) && g2s_try_u64(S, &prog) && prog
            && !(prog & 7) && g2s_try_u64(prog + 816, &row) && row
            && !(row & 7)) {
            unsigned long long base = row + 224ULL * n92, cnt = 0, p, rec;
            int cls = 0, bit = -1, guard;
            if (g2s_try_u64(base + 24, &w))
                cls = (int)(unsigned)(w >> 32);
            if (g2s_try_u64(base + 16, &w))
                fprintf(stderr, " row16=%u", (unsigned)(w & 0xffffffff));
            fprintf(stderr, " row28=%d", cls);
            if (g2s_try_u64(base + 64, &w))
                bit = (int)(unsigned)(w & 0xffffffff);
            fprintf(stderr, " row64=%d", bit);
            /* AND THE LIVENESS STATE THE OVERLAP TEST IS ABOUT TO READ.
             * `S[16][class][reg]` records are live and `S[8][class][reg]` is
             * the list; the model's first disagreement that the call
             * sequences could not explain was here. */
            if (cls == 0) {
                unsigned long long r16 = 0;
                if (g2s_try_u64(base + 16, &r16)
                    && ((unsigned)(r16 & 0xffffffff) - 0x6fu) <= 0x90u)
                    bit = (int)((unsigned)(r16 & 0xffffffff) << 3) - 0x378;
            }
            if (bit != -1) {
                int reg = bit >> 3;
                fprintf(stderr, " reg=%d", reg);
                if (g2s_try_u64(S + 16, &p) && p && !(p & 7)
                    && g2s_try_u64(p + 8ULL * (unsigned)cls, &p) && p
                    && !(p & 7) && g2s_try_u64(p + 4ULL * (unsigned)reg, &w))
                    cnt = w & 0xffffffff;
                fprintf(stderr, " cnt=%llu live=", cnt);
                if (g2s_try_u64(S + 8, &p) && p && !(p & 7)
                    && g2s_try_u64(p + 8ULL * (unsigned)cls, &p) && p
                    && !(p & 7)
                    && g2s_try_u64(p + 8ULL * (unsigned)reg, &rec)) {
                    for (guard = 0; guard < (int)cnt && rec && !(rec & 7);
                         ++guard) {
                        unsigned long long who = 0, cm = 0;
                        g2s_try_u64(rec + 8, &who);
                        g2s_try_u64(rec + 16, &cm);
                        fprintf(stderr, "%s%#llx/%#x", guard ? "," : "", who,
                                (unsigned)(cm & 0xffffffff));
                        if (!g2s_try_u64(rec, &rec))
                            break;
                    }
                    if (!cnt)
                        fputs("none", stderr);
                } else {
                    fputs("?", stderr);
                }
            }
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_def -- the DEFINITION side, at `f_710004ad20`'s entry.
 *
 * The companion to `g2s_trace_edge`.  `f_710004ad20`'s second argument is
 * `f_7100056910(node) & 1`, which decides whether the previous definitions of
 * the register are KILLED, and that predicate has a deep arm this model has
 * to guess at (it asks slot 0 for `node[44]` with an index the caller left
 * in w1).  Printing the node and the flag says whether the guess holds
 * without having to guess again.
 *
 *   g2s_def node=<x1> flag=<w2>
 */
void g2s_trace_def(cpu_t *cpu, unsigned long addr);
void g2s_trace_def(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("def"))
        return;
    (void)addr;
    fprintf(stderr, "g2s_def node=%#llx flag=%u\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 1),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 2) & 1));
}

/* THE WRITE WATCH -- who sets `cg[72]`, the word that becomes `node[36]`.
 *
 * notes/51 reads `node[36] = cg[72]` at the node constructor (0xf097b4) and
 * shows the position's other two components are zero on every probe, so
 * `cg[72]` alone is the selector's key.  Twelve thousand instructions in the
 * image store a word at `+72`, which is no way to find the one that runs.
 *
 * `st_i32`/`st_i64` in include/guest_rt.h now test one global before every
 * store, and this is what they call when it matches.  The port's functions
 * are the guest's -- `f_<guest addr>` -- so the HOST return addresses name
 * the guest functions, and `tools/whowrote.py` maps them back.
 *
 * `g2s_watch_set` is called from the node constructor's hook with `cg + 72`.
 */
uint64_t g2s_watch_addr;
uint64_t g2s_watch_lo, g2s_watch_hi;
static int g2s_watch_seen;

void g2s_watch_hit(uint64_t a, uint64_t v, unsigned width);
void g2s_watch_hit(uint64_t a, uint64_t v, unsigned width)
{
    void *bt[24];
    int n, i;

    if (g2s_trace_budget == 0 || !g2s_want("watch"))
        return;
    /* The cap used to be 4000, which a wide range blows through before the
     * interesting write happens.  G2S_WATCH_MAX raises it; the default stays
     * low so a stray arming cannot fill the log. */
    {
        const char *mx = getenv("G2S_WATCH_MAX");
        int cap = mx && *mx ? atoi(mx) : 4000;
        if (++g2s_watch_seen > cap)
            return;
    }
    fprintf(stderr, "g2s_write addr=%#llx width=%u val=%#llx |",
            (unsigned long long)a, width, (unsigned long long)v);
    n = backtrace(bt, 24);
    for (i = 1; i < n; ++i)
        fprintf(stderr, " %p", bt[i]);
    fputc('\n', stderr);
}

/* g2s_trace_pos -- who stamps `node[36]`, with a guest backtrace.
 *
 * Every node's `node[36]` is written by ONE function: `f_710004f530(node,
 * tmp)` copies the 36-byte field group, `node[36] = tmp[28]` (notes/33).  So
 * the question "what sets the scheduler's second key" is "what was in
 * `tmp[28]` and who put it there", and the caller is the answer.
 *
 * The port compiles each guest function as a C function named after its guest
 * address, so a HOST backtrace is a guest backtrace; `tools/whowrote.py`
 * resolves it.  `tmp` is only 4-aligned, which `g2s_try_u64` rejects, so the
 * word is read 8 bytes below and shifted, as `g2s_trace_setop` does.
 *
 *   g2s_pos node=<x0> val=<tmp[28]> | <caller> <- <caller> ...
 */
void g2s_trace_pos(cpu_t *cpu, unsigned long addr);
void g2s_trace_pos(cpu_t *cpu, unsigned long addr)
{
    unsigned long long node, tmp, v = 0;
    void *bt[16];
    int n, i;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("pos"))
        return;
    (void)addr;
    node = GST_I64(GUEST_OFF_X0);
    tmp = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!tmp)
        return;
    if ((tmp + 28) & 7)
        g2s_try_u64(tmp + 24, &v), v >>= 32;
    else
        g2s_try_u64(tmp + 28, &v);
    /* ARM THE WATCH on one node's `+36`, chosen by ORDINAL so the choice is
     * stable across runs (the addresses are not).  `G2S_WATCH_NODE=<n>`
     * watches the n-th distinct node this hook sees, which makes every LATER
     * write to that node's position attributable -- `f_710004f530` is not the
     * only writer, and the value a node ends up with is not the one it was
     * created with. */
    {
        static unsigned long long armed_on;
        static unsigned seen_nodes;
        const char *want = getenv("G2S_WATCH_NODE");
        if (want && *want && node && node != armed_on) {
            armed_on = node;
            if (++seen_nodes == (unsigned)atoi(want)) {
                g2s_watch_addr = node + 36;
                fprintf(stderr, "g2s_armed node=%#llx watch=%#llx\n", node,
                        (unsigned long long)g2s_watch_addr);
            }
        }
    }
    /* READ THE FIELD, not a reconstruction of it.  Deriving `node[36]` from
     * `tmp[28]` needs the temp's base right, and getting it wrong is silent:
     * an earlier version reported values the node never held.  So print the
     * node's own `node[36]` as it stands at this call's ENTRY as well -- the
     * sequence of those across calls is the field's actual history -- and the
     * whole temp group beside it, so a disagreement names itself. */
    {
        unsigned long long now = 0, t0 = 0, t16 = 0;
        g2s_try_u64(node + 32, &now);
        if ((tmp + 0) & 7)
            g2s_try_u64(tmp - 4, &t0), t0 >>= 32;
        else
            g2s_try_u64(tmp, &t0);
        if ((tmp + 16) & 7)
            g2s_try_u64(tmp + 12, &t16), t16 >>= 32;
        else
            g2s_try_u64(tmp + 16, &t16);
        fprintf(stderr,
                "g2s_pos node=%#llx now36=%u tmp0=%#x tmp16=%u tmp28=%u |",
                node, (unsigned)(now >> 32),
                (unsigned)(t0 & 0xffffffff), (unsigned)(t16 & 0xffffffff),
                (unsigned)(v & 0xffffffff));
    }
    n = backtrace(bt, 16);
    for (i = 1; i < n; ++i)
        fprintf(stderr, " %p", bt[i]);
    fputc('\n', stderr);
}

/* g2s_trace_desc12 -- the OPERAND DESCRIPTOR that carries `node[36]`.
 *
 * `f_7100f0bd20(factory, desc)` materialises an operand as a node, and at
 * 0xf0bdcc it does `w21 = desc[12]; tmp[28] = w21; f_710004f530(node, tmp)`
 * -- so `node[36]`, the scheduler's second key, IS `desc[12]`.  Watching one
 * node's `+36` shows it written once and never changed, so the descriptor is
 * where the number comes from and this is where to look for it.
 *
 * The descriptor's shape, from the tests f_7100f0bd20 makes on it:
 *
 *   desc[0]  a swizzle, compared against 0x03020100
 *   desc[4]  a type, compared against the node's node[24]
 *   desc[8]  a flag; non-zero takes the other arm
 *   desc[12] THE NUMBER that becomes node[36]
 *   desc[16] the operand's node, if it already has one
 *
 * `G2S_WATCH_DESC=<n>` arms the write watch on the n-th distinct
 * descriptor's `+12`, so whatever assigns the number is named.
 */
void g2s_trace_desc12(cpu_t *cpu, unsigned long addr);
void g2s_trace_desc12(cpu_t *cpu, unsigned long addr)
{
    unsigned long long d, w = 0, w2 = 0;
    static unsigned long long armed_on;
    static unsigned seen;
    const char *want;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("desc12"))
        return;
    (void)addr;
    d = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!d || (d & 3))
        return;
    g2s_try_u64(d, &w);
    g2s_try_u64(d + 8, &w2);
    fprintf(stderr, "g2s_desc12 desc=%#llx swz=%#x type=%u flag=%u num=%u"
            " node=%#llx\n", d, (unsigned)(w & 0xffffffff),
            (unsigned)(w >> 32), (unsigned)(w2 & 0xffffffff),
            (unsigned)(w2 >> 32), (unsigned long long)0);
    want = getenv("G2S_WATCH_DESC");
    if (want && *want && d != armed_on) {
        armed_on = d;
        if (++seen == (unsigned)atoi(want)) {
            g2s_watch_addr = d + 12;
            fprintf(stderr, "g2s_armed desc=%#llx watch=%#llx\n", d,
                    (unsigned long long)g2s_watch_addr);
        }
    }
}

/* g2s_trace_num -- `node[36]`'s value at the instruction that reads it.
 *
 * Patched at the LABEL 0xf0bdcc, which is
 *
 *     f0bdcc:  ldr w21, [x19, #12]          ; x19 = the operand descriptor
 *     f0bde4:  str w21, [sp, #28]           ; -> tmp[28]
 *     f0bde8:  bl 4f530                     ; -> node[36]
 *
 * so it prints exactly the number the node is about to be stamped with,
 * together with the descriptor it came from.  Reading the descriptor at
 * `f_7100f0bd20`'s ENTRY is not the same thing -- measured on `op_mul.vert`,
 * the two descriptors seen there both hold 6 while the nodes end up with 4,
 * 6, 9 and 12, so the field is written between the entry and this point.
 */
void g2s_trace_num(cpu_t *cpu, unsigned long addr);
void g2s_trace_num(cpu_t *cpu, unsigned long addr)
{
    unsigned long long d, w = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("num"))
        return;
    (void)addr;
    d = GST_I64(GUEST_OFF_X0 + 8 * 19);          /* x19 */
    if (!d || (d & 3))
        return;
    if ((d + 12) & 7)
        g2s_try_u64(d + 8, &w), w >>= 32;
    else
        g2s_try_u64(d + 12, &w);
    fprintf(stderr, "g2s_num desc=%#llx num=%u\n", d,
            (unsigned)(w & 0xffffffff));
}

/* g2s_trace_stmt -- the AST STATEMENT whose position becomes `node[36]`.
 *
 * `f_7100f103d0(cg, ?, stmt)` is the per-statement lowering, and at 0xf10440:
 *
 *     f10400:  cg[136] = stmt
 *     f10440:  x8 = stmt[64];  w9 = stmt[72]
 *     f10448:  cg[72] = w9;  cg[64] = x8
 *
 * so the position every node is stamped with (`node[36] = cg[72]`, notes/40)
 * is the STATEMENT'S OWN, adopted for as long as that statement is lowered.
 * That is why nodes from one statement share a number, why there are gaps,
 * and why the pre-scheduling list can disagree with it.
 *
 * `G2S_WATCH_STMT=<n>` arms the write watch on the n-th statement's `+72`,
 * which is where the number itself is assigned.
 */
void g2s_trace_stmt(cpu_t *cpu, unsigned long addr);
void g2s_trace_stmt(cpu_t *cpu, unsigned long addr)
{
    unsigned long long st, w = 0, lo = 0, op = 0, pos = 0;
    static unsigned long long armed_on;
    static unsigned seen;
    const char *want;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("stmt"))
        return;
    (void)addr;
    st = GST_I64(GUEST_OFF_X0 + 8 * 2);
    /* Print even when the argument looks wrong: the first version returned
     * silently on `!st || (st & 7)` and produced nothing at all, which said
     * nothing about WHY.  An instrument that cannot distinguish "not called"
     * from "called with something unexpected" is not an instrument. */
    if (!st || (st & 7)) {
        fprintf(stderr, "g2s_stmt stmt=%#llx REJECTED\n", st);
        return;
    }
    g2s_try_u64(st, &op);
    g2s_try_u64(st + 64, &lo);
    g2s_try_u64(st + 72, &pos);
    (void)w;
    fprintf(stderr, "g2s_stmt stmt=%#llx w0=%#llx loc64=%#llx pos=%u\n",
            st, op, lo, (unsigned)(pos & 0xffffffff));
    want = getenv("G2S_WATCH_STMT");
    if (want && *want && st != armed_on) {
        armed_on = st;
        if (++seen == (unsigned)atoi(want)) {
            g2s_watch_addr = st + 72;
            fprintf(stderr, "g2s_armed stmt=%#llx watch=%#llx\n", st,
                    (unsigned long long)g2s_watch_addr);
        }
    }
}

/* g2s_trace_alloc -- the node constructor, with its pool and its counter.
 *
 * `f_710004f770(pool, template, extra)` is the node constructor reached from
 * every factory through `f_7100f28990`.  It allocates 0xd0 bytes, optionally
 * copies a template's fields, then
 *
 *     4f7f4:  w8 = pool[1104];  w8 += 1;  pool[1104] = w8
 *     4f800:  node[36] = w8                       ; the scheduler's key
 *     4f828:  node[72] = pool[1096];  pool[1096] = node
 *
 * so the pool holds BOTH a counter and a list of everything it has made.
 * Hooked at ENTRY the node does not exist yet, so this prints the pool and
 * the counter as it stands BEFORE the allocation -- the difference between
 * successive lines is what the pool actually did, which is the thing three
 * linked nodes sharing one `node[36]` contradicts.
 *
 * The five sibling constructors (0x4f930, 0x4fa10, 0x4faf0, 0x4fbd0,
 * 0x4fcb0) use the same pool and the same counter offset, so whichever runs,
 * the log is one sequence.
 */
void g2s_trace_mkpool(cpu_t *cpu, unsigned long addr);
void g2s_trace_mkpool(cpu_t *cpu, unsigned long addr)
{
    unsigned long long pool, tmpl, w = 0, head = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("mkpool"))
        return;
    pool = GST_I64(GUEST_OFF_X0);
    tmpl = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!pool || (pool & 7))
        return;
    g2s_try_u64(pool + 1104, &w);
    g2s_try_u64(pool + 1096, &head);
    /* REMEMBER THE POOL.  `node[36]` is `pool[1104]`, one counter per pool,
     * and nothing on a node says which pool made it -- two pools draw from
     * one heap and their nodes interleave, so addresses cannot separate them.
     * The pool's own list can: `pool[1096]` is the head and `node[72]` the
     * link, so walking it gives both membership and allocation order, for
     * every node INCLUDING the ones the builder later discards.  The pools
     * are recorded here, where they are in a register, and walked at the
     * second pass's entry, where the nodes still exist. */
    {
        int i;
        for (i = 0; i < g2s_npools; ++i)
            if (g2s_pools[i] == pool)
                break;
        if (i == g2s_npools && g2s_npools < 16)
            g2s_pools[g2s_npools++] = pool;
    }
    fprintf(stderr, "g2s_mkpool f_%lx pool=%#llx ctr=%u head=%#llx tmpl=%#llx lr=%#llx\n",
            addr, pool, (unsigned)(w & 0xffffffff), head, tmpl,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
    /* ARM ON THE COUNTER ITSELF.  These six constructors account for only two
     * of `fr_mrt.frag`'s eight nodes, so something else increments the same
     * `pool[1104]`; watching it names that something.  The pool address is
     * not knowable before the run, which is why the arming lives here. */
    /* G2S_WATCH_NODES=<n> watches the whole arena around the pool's node
     * list, so writes to nodes nothing has named are still attributed. */
    if (!g2s_watch_hi && getenv("G2S_WATCH_NODES") && head) {
        unsigned long long span = (unsigned long long)
            atoi(getenv("G2S_WATCH_NODES"));
        if (!span)
            span = 0x20000;
        /* The nodes that INHERIT their number live in a different arena from
         * the ones that are numbered, so a span sized to one arena misses
         * exactly the nodes the question is about. */
        g2s_watch_lo = head > span ? head - span : 0;
        g2s_watch_hi = head + span;
        fprintf(stderr, "g2s_armed range=[%#llx,%#llx)\n",
                (unsigned long long)g2s_watch_lo,
                (unsigned long long)g2s_watch_hi);
    }
    if (!g2s_watch_addr && getenv("G2S_WATCH_CTR")) {
        g2s_watch_addr = pool + 1104;
        fprintf(stderr, "g2s_armed pool=%#llx watch=%#llx\n", pool,
                (unsigned long long)g2s_watch_addr);
    }
}

/* g2s_trace_rel -- the RELEASE EDGES pass 1 actually walks.
 *
 * A model of pass 1 built on the raw source slots at `node + 0xa8` schedules
 * too few nodes: `fr_texfetch.frag`'s `0x3b` never becomes ready, because the
 * chain 0x2c -> 0x5f -> 0xcb -> 0xb5 is FOLDED and the slots only connect its
 * ends.  `f_7100049940` does not read the slots -- it asks the node for its
 * operands through `vt[16]` / `vt[24]`, and what those return is the
 * scheduler's own view, which is not the slot array.
 *
 * So rather than guess how the virtual call flattens a folded subtree, this
 * prints the pairs `f_7100049780` is actually called with:
 *
 *   g2s_rel consumer=<x2> operand=<x3>
 *
 * X2 is the node being scheduled and X3 the operand being released
 * (0x497d4..0x49878 for the transparent arm, 0x49880 for the ordinary one).
 */
void g2s_trace_rel(cpu_t *cpu, unsigned long addr)
{
    unsigned long long consumer, operand, w;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("rel"))
        return;
    (void)addr;
    consumer = GST_I64(GUEST_OFF_X0 + 8 * 2);
    operand = GST_I64(GUEST_OFF_X0 + 8 * 3);
    if (!operand || (operand & 7))
        return;
    fprintf(stderr, "g2s_rel consumer=%#llx operand=%#llx", consumer, operand);
    if (g2s_try_u64(operand + 8, &w))
        fprintf(stderr, " op=%#llx", w & 0xffffffff);
    if (g2s_try_u64(operand + 144, &w))
        fprintf(stderr, " owner=%#llx", w);
    fputc('\n', stderr);
}

/* ---------------------------------------------------------------------------
 * The allocator's SIMPLIFY and SELECT phases, dumped whole (notes/54).
 *
 * `f_7100046b60` (the per-class driver) calls, per attempt,
 *
 *     list  = this->vt[160](this, prog, list, &budget, &cls, attempt,
 *                           &widthmask, this[8], K)       -- f_7100045ed0
 *     slots = this->vt[152](this, prog, list, ...)        -- f_7100045530
 *
 * and the ORDER the second one visits the records in is the list the first
 * one returns.  These three hooks print everything either function reads, so
 * a transcription of both can be run offline against the compiler's own
 * decisions, attempt by attempt:
 *
 *   g2s_simp   at f_7100045ed0's entry: the arguments, then one `g2s_srec`
 *              per record of the input list, in list order, with every field
 *              the phase reads and the record's final neighbour list
 *              (record[216], the 0x20-byte form f_7100044e10 walks);
 *   g2s_simped at L_7100046820, the return block: the list it hands back
 *              (x25), head first -- the visiting order;
 *   g2s_eng    at L_7100046f18 in the driver, just after the engine returned
 *              (w0): every record of that list with the slot and swizzle the
 *              engine gave it (record[64], record[24]).
 */
static void g2s_srec(unsigned long long prog, unsigned long long rec)
{
    unsigned long long arr = 0, n = 0, w = 0, w2 = 0, e;
    long long idx = -1;
    int guard;

    if (g2s_try_u64(prog + 816, &arr) && g2s_try_u64(prog + 808, &n)) {
        n &= 0xffffffff;
        if (rec >= arr && (rec - arr) % 224 == 0 && (rec - arr) / 224 < n)
            idx = (long long)((rec - arr) / 224);
    }
    fprintf(stderr, "g2s_srec vreg=%lld", idx);
    if (g2s_try_u64(rec + 8, &w))
        fprintf(stderr, " type=%u b12=%u b13=%u b14=%u b15=%u",
                (unsigned)(w & 0xffffffff), (unsigned)((w >> 32) & 0xff),
                (unsigned)((w >> 40) & 0xff), (unsigned)((w >> 48) & 0xff),
                (unsigned)((w >> 56) & 0xff));
    if (g2s_try_u64(rec + 16, &w))
        fprintf(stderr, " sym=%u h20=%d h22=%d",
                (unsigned)(w & 0xffffffff), (int)(short)((w >> 32) & 0xffff),
                (int)(short)((w >> 48) & 0xffff));
    if (g2s_try_u64(rec + 24, &w))
        fprintf(stderr, " swz=%#x w28=%u", (unsigned)(w & 0xffffffff),
                (unsigned)(w >> 32));
    if (g2s_try_u64(rec + 48, &w) && g2s_try_u64(rec + 56, &w2))
        fprintf(stderr, " c48=%d c52=%d c56=%d c60=%d",
                (int)(unsigned)w, (int)(unsigned)(w >> 32),
                (int)(unsigned)w2, (int)(unsigned)(w2 >> 32));
    if (g2s_try_u64(rec + 64, &w))
        fprintf(stderr, " w64=%d w68=%d", (int)(unsigned)w,
                (int)(unsigned)(w >> 32));
    if (g2s_try_u64(rec + 80, &w))
        fprintf(stderr, " cm=%#x w84=%d", (unsigned)w,
                (int)(unsigned)(w >> 32));
    if (g2s_try_u64(rec + 88, &w))
        fprintf(stderr, " tie=%#x chain=%d", (unsigned)w,
                (int)(unsigned)(w >> 32));
    if (g2s_try_u64(rec + 96, &w))
        fprintf(stderr, " w96=%d", (int)(unsigned)w);
    if (g2s_try_u64(rec + 136, &w))
        fprintf(stderr, " h136=%d", (int)(short)(w & 0xffff));
    if (g2s_try_u64(rec + 152, &w))
        fprintf(stderr, " w156=%#x", (unsigned)(w >> 32));
    if (g2s_try_u64(rec + 200, &w))
        fprintf(stderr, " w200=%d", (int)(unsigned)w);
    if (g2s_try_u64(rec + 208, &w) && w) {
        unsigned long long h = 0;
        int k, cnt = 0;
        if (g2s_try_u64(rec + 16, &h))
            cnt = (int)(short)((h >> 48) & 0xffff);
        fprintf(stderr, " grp=");
        for (k = 0; k < cnt && k < 16; ++k) {
            unsigned long long q = 0;
            if (g2s_try_u64((w + 4ULL * k) & ~7ULL, &q))
                fprintf(stderr, "%s%d", k ? "," : "",
                        (int)(unsigned)((w + 4ULL * k) & 4 ? q >> 32 : q));
        }
    }
    fprintf(stderr, " nb:");
    if (g2s_try_u64(rec + 216, &e)) {
        for (guard = 0; e && !(e & 7) && guard < 4096; ++guard) {
            unsigned long long v = 0, m0 = 0, m1 = 0, c = 0;
            if (g2s_try_u64(e, &v) && g2s_try_u64(e + 16, &m0)
                    && g2s_try_u64(e + 24, &m1)) {
                g2s_try_u64(e + 32, &c);
                fprintf(stderr, " %u:%08x,%08x,%08x,%08x/%u", (unsigned)v,
                        (unsigned)m0, (unsigned)(m0 >> 32), (unsigned)m1,
                        (unsigned)(m1 >> 32), (unsigned)c);
            }
            if (!g2s_try_u64(e + 8, &e))
                break;
        }
    }
    fputc('\n', stderr);
}

void g2s_trace_simp(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, list, sp, w = 0, cg = 0, b = 0;
    int guard;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("simp"))
        return;
    (void)addr;
    prog = GST_I64(GUEST_OFF_X0 + 8 * 1);
    list = GST_I64(GUEST_OFF_X0 + 8 * 2);
    sp = GST_I64(GUEST_OFF_SP);
    fprintf(stderr, "g2s_simp attempt=%d",
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 5));
    if (g2s_try_u64(sp & ~7ULL, &w))
        fprintf(stderr, " K=%d", (int)(unsigned)w);
    if (g2s_try_u64(GST_I64(GUEST_OFF_X0 + 8 * 3) & ~7ULL, &w))
        fprintf(stderr, " budget=%d",
                (int)(unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 3) & 4 ? w >> 32 : w));
    if (g2s_try_u64(GST_I64(GUEST_OFF_X0 + 8 * 4) + 8, &w))
        fprintf(stderr, " cls8=%d cls12=%d", (int)(unsigned)w,
                (int)(unsigned)(w >> 32));
    if (g2s_try_u64(GST_I64(GUEST_OFF_X0 + 8 * 6) & ~7ULL, &w))
        fprintf(stderr, " wmask=%#x",
                (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 6) & 4 ? w >> 32 : w));
    fprintf(stderr, " avoid=%#llx",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 7));
    if (g2s_try_u64(prog + 376, &w))
        fprintf(stderr, " p376=%d", (int)(unsigned)w);
    if (g2s_try_u64(prog + 768, &cg) && g2s_try_u64(cg + 1232, &b))
        fprintf(stderr, " cg1232=%u", (unsigned)(b & 0xff));
    /* cg[36 + 4*class], the per-class register count the limit is made of
     * (vt[248] = f_7100bdfbc4 rounds it up to a multiple of 4). */
    if (cg) {
        int k;
        fprintf(stderr, " cg36=");
        for (k = 0; k < 8; ++k) {
            unsigned long long a = cg + 36 + 4ULL * k, q = 0;
            int v = -1;
            if (g2s_try_u64(a & ~7ULL, &q))
                v = (int)(unsigned)((a & 4) ? q >> 32 : q);
            fprintf(stderr, "%s%d", k ? "," : "", v);
        }
    }
    fputc('\n', stderr);
    for (guard = 0; list && !(list & 7) && guard < 65536; ++guard) {
        g2s_srec(prog, list);
        if (!g2s_try_u64(list, &list))
            break;
    }
}

static void g2s_list_idx(const char *tag, unsigned long long prog,
                         unsigned long long list, int with_slot)
{
    unsigned long long arr = 0, n = 0, w = 0, w2 = 0;
    int guard;

    g2s_try_u64(prog + 816, &arr);
    g2s_try_u64(prog + 808, &n);
    n &= 0xffffffff;
    fprintf(stderr, "%s", tag);
    for (guard = 0; list && !(list & 7) && guard < 65536; ++guard) {
        long long idx = -1;
        if (arr && list >= arr && (list - arr) % 224 == 0
                && (list - arr) / 224 < n)
            idx = (long long)((list - arr) / 224);
        fprintf(stderr, " %lld", idx);
        if (with_slot && g2s_try_u64(list + 64, &w)
                && g2s_try_u64(list + 24, &w2))
            fprintf(stderr, ":%d/%#x", (int)(unsigned)w,
                    (unsigned)(w2 & 0xffffffff));
        if (!g2s_try_u64(list, &list))
            break;
    }
    fputc('\n', stderr);
}

void g2s_trace_simped(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("simp"))
        return;
    (void)addr;
    /* x22 is the program throughout f_7100045ed0 (0x45f18), x25 the list. */
    g2s_list_idx("g2s_simped order", GST_I64(GUEST_OFF_X0 + 8 * 22),
                 GST_I64(GUEST_OFF_X0 + 8 * 25), 0);
}

void g2s_trace_eng(cpu_t *cpu, unsigned long addr)
{
    char tag[64];

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("simp"))
        return;
    (void)addr;
    /* x19 is the program in f_7100046b60 (0x46b88), x28 the list the
     * engine was handed, w0 its return value (the highest slot used + 1,
     * or -1). */
    {
        /* The engine's w3 is the budget word at [x29-4] (0x46f0c) and its w4
         * the class (w24).  Read them back so the bitset size is known. */
        unsigned long long fp = GST_I64(GUEST_OFF_X0 + 8 * 29), q = 0;
        int w3 = -1;
        if (g2s_try_u64((fp - 4) & ~7ULL, &q))
            w3 = (int)(unsigned)(((fp - 4) & 4) ? q >> 32 : q);
        snprintf(tag, sizeof tag, "g2s_eng ret=%d w3=%d class=%d list",
                 (int)(unsigned)GST_I64(GUEST_OFF_X0), w3,
                 (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 24));
    }
    g2s_list_idx(tag, GST_I64(GUEST_OFF_X0 + 8 * 19),
                 GST_I64(GUEST_OFF_X0 + 8 * 28), 1);
}

/* ---------------------------------------------------------------------------
 * The allocator DRIVER, `f_7100046b60`, dumped at three points (notes/54 §9):
 *
 *   g2s_drv     at its entry: the arguments and every record of the program
 *               BEFORE the driver's first sweep changes anything -- which is
 *               what the single-component count at 0x46c40 reads;
 *   g2s_drvg    at L_7100046d00, just after vt[16] built the graph: every
 *               record again, with its neighbour list (`g2s_srec` lines);
 *   g2s_drvout  at L_7100047368, after colouring, tie resolution and
 *               symbol naming: every record's colour, swizzle and symbol,
 *               and the two numbers the "used" word is made of.
 *
 * With those, the driver's list, costs, attempt loop, tie arm and naming
 * sweep can be run offline against the compiler (tools/drivercheck.py).
 */
static void g2s_all_recs(const char *tag, unsigned long long prog, int nb)
{
    unsigned long long n = 0, arr = 0, i;

    if (!g2s_try_u64(prog + 808, &n) || !g2s_try_u64(prog + 816, &arr)
            || !arr)
        return;
    n &= 0xffffffff;
    if (n > 65536)
        return;
    fprintf(stderr, "%s n=%llu\n", tag, n);
    for (i = 0; i < n; ++i) {
        unsigned long long rec = arr + 224 * i;
        if (nb) {
            g2s_srec(prog, rec);
        } else {
            unsigned long long w = 0, w2 = 0, w3 = 0;
            fprintf(stderr, "g2s_drec vreg=%llu", i);
            if (g2s_try_u64(rec + 8, &w))
                fprintf(stderr, " type=%u b12=%u b13=%u b14=%u b15=%u",
                        (unsigned)(w & 0xffffffff),
                        (unsigned)((w >> 32) & 0xff),
                        (unsigned)((w >> 40) & 0xff),
                        (unsigned)((w >> 48) & 0xff),
                        (unsigned)((w >> 56) & 0xff));
            if (g2s_try_u64(rec + 16, &w))
                fprintf(stderr, " sym=%u h20=%d h22=%d",
                        (unsigned)(w & 0xffffffff),
                        (int)(short)((w >> 32) & 0xffff),
                        (int)(short)((w >> 48) & 0xffff));
            if (g2s_try_u64(rec + 24, &w))
                fprintf(stderr, " swz=%#x w28=%d",
                        (unsigned)(w & 0xffffffff), (int)(unsigned)(w >> 32));
            if (g2s_try_u64(rec + 64, &w))
                fprintf(stderr, " w64=%d", (int)(unsigned)w);
            if (g2s_try_u64(rec + 80, &w))
                fprintf(stderr, " cm=%#x w84=%d", (unsigned)w,
                        (int)(unsigned)(w >> 32));
            if (g2s_try_u64(rec + 88, &w2))
                fprintf(stderr, " tie=%#x chain=%d", (unsigned)w2,
                        (int)(unsigned)(w2 >> 32));
            if (g2s_try_u64(rec + 152, &w3))
                fprintf(stderr, " w156=%#x", (unsigned)(w3 >> 32));
            fputc('\n', stderr);
        }
    }
}

void g2s_trace_drv(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, sp, w = 0;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("drv"))
        return;
    prog = GST_I64(GUEST_OFF_X0 + 8 * 1);
    sp = GST_I64(GUEST_OFF_SP);
    fprintf(stderr, "g2s_drv f_%lx class=%d w5=%d w6=%d w7=%d", addr,
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 3),
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 5),
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 6),
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 7));
    if (g2s_try_u64(prog + 376, &w))
        fprintf(stderr, " p376=%d", (int)(unsigned)w);
    if (g2s_try_u64(sp, &w))
        fprintf(stderr, " usedptr=%#llx", w);
    fputc('\n', stderr);
    g2s_all_recs("g2s_drvrecs", prog, 0);
}

void g2s_trace_drvg(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("drv"))
        return;
    (void)addr;
    /* x19 is the program throughout the driver (0x46b88). */
    g2s_all_recs("g2s_drvg", GST_I64(GUEST_OFF_X0 + 8 * 19), 1);
}

void g2s_trace_drvout(cpu_t *cpu, unsigned long addr)
{
    unsigned long long fp, q = 0, sp;
    int cls8 = -1, used64 = -1;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("drv"))
        return;
    (void)addr;
    fp = GST_I64(GUEST_OFF_X0 + 8 * 29);
    sp = GST_I64(GUEST_OFF_SP);
    if (g2s_try_u64((fp - 32) & ~7ULL, &q))
        cls8 = (int)(unsigned)(((fp - 32) & 4) ? q >> 32 : q);
    if (g2s_try_u64((sp + 64) & ~7ULL, &q))
        used64 = (int)(unsigned)(((sp + 64) & 4) ? q >> 32 : q);
    fprintf(stderr, "g2s_drvout w20=%d cls8=%d used=%d\n",
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 20), cls8, used64);
    g2s_all_recs("g2s_drvfin", GST_I64(GUEST_OFF_X0 + 8 * 19), 0);
}

/* ---------------------------------------------------------------------------
 * The interference-graph builder's INPUT (notes/54 §10), for
 * tools/graphcheck.py:
 *
 *   g2s_ifgin   at f_7100043460's entry: the class, then every position of
 *               the instruction list the sweep walks (x4: count at [0], an
 *               array of instruction records at [8]; each record's block at
 *               +24 and node at +56), then every node reachable from them
 *               through their operand slots, once each:
 *                 op=node[8] n40 n48 (write mask) n92 (vreg) f152 (byte)
 *                 and per slot  inline(+184) node(+192) sel(+200) mask(+204)
 *               -- which is everything f_7100052680 (defs) and
 *               f_71000523c0 (uses) read for the node classes the probes use
 *               (their vt[8] is f_710004f150, which returns 0);
 *   g2s_ifgseed at L_71000435e4, just after f_7100048820 seeded a block:
 *               the block and every element of the live set (prog+0x3a0)
 *               with its component nibble.
 */
static void g2s_ifg_node(unsigned long long node, unsigned long long *seen,
                         int *nseen, int depth)
{
    unsigned long long w = 0, w40 = 0, w48 = 0, w88 = 0, w152 = 0;
    int i, n;

    if (!node || (node & 7) || depth > 64)
        return;
    for (i = 0; i < *nseen; ++i)
        if (seen[i] == node)
            return;
    if (*nseen >= 8192)
        return;
    seen[(*nseen)++] = node;
    if (!g2s_try_u64(node + 8, &w) || !g2s_try_u64(node + 40, &w40)
            || !g2s_try_u64(node + 48, &w48) || !g2s_try_u64(node + 88, &w88)
            || !g2s_try_u64(node + 152, &w152))
        return;
    n = (int)((w152 >> 8) & 0xff);
    fprintf(stderr, "g2s_inode %#llx op=%#x n40=%d n48=%#x n92=%d f152=%#x "
            "nslot=%d slots:", node, (unsigned)(w & 0xffffffff),
            (int)(unsigned)w40, (unsigned)w48, (int)(unsigned)(w88 >> 32),
            (unsigned)(w152 & 0xff), n);
    for (i = 0; i < n && i < 8; ++i) {
        unsigned long long a = node + 40ULL * i, q1 = 0, q2 = 0, q3 = 0;
        g2s_try_u64(a + 184, &q1);          /* inline (low word)       */
        g2s_try_u64(a + 192, &q2);          /* the slot's node         */
        g2s_try_u64(a + 200, &q3);          /* sel (low) / mask (high) */
        fprintf(stderr, " %d:%#llx:%#x:%#x", (int)(unsigned)q1, q2,
                (unsigned)(q3 & 0xffffffff), (unsigned)(q3 >> 32));
    }
    fputc('\n', stderr);
    for (i = 0; i < n && i < 8; ++i) {
        unsigned long long q2 = 0;
        if (g2s_try_u64(node + 40ULL * i + 192, &q2))
            g2s_ifg_node(q2, seen, nseen, depth + 1);
    }
}

void g2s_trace_ifgin(cpu_t *cpu, unsigned long addr)
{
    static unsigned long long seen[8192];
    int nseen = 0;
    unsigned long long pos, arr = 0, q = 0, k;
    long long cnt;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("ifgin"))
        return;
    (void)addr;
    pos = GST_I64(GUEST_OFF_X0 + 8 * 4);
    if (!g2s_try_u64(pos, &q) || !g2s_try_u64(pos + 8, &arr))
        return;
    cnt = (long long)(int)(unsigned)q;
    fprintf(stderr, "g2s_ifgin class=%d npos=%lld\n",
            (int)(unsigned)GST_I64(GUEST_OFF_X0 + 8 * 3), cnt);
    for (k = 0; (long long)k < cnt && k < 65536; ++k) {
        unsigned long long ent = 0, blk = 0, node = 0;
        if (!g2s_try_u64(arr + 8 * k, &ent))
            break;
        g2s_try_u64(ent + 24, &blk);
        g2s_try_u64(ent + 56, &node);
        fprintf(stderr, "g2s_ipos %llu block=%#llx node=%#llx\n", k, blk,
                node);
    }
    for (k = 0; (long long)k < cnt && k < 65536; ++k) {
        unsigned long long ent = 0, node = 0;
        if (!g2s_try_u64(arr + 8 * k, &ent))
            break;
        if (g2s_try_u64(ent + 56, &node))
            g2s_ifg_node(node, seen, &nseen, 0);
    }
}

void g2s_trace_ifgseed(cpu_t *cpu, unsigned long addr)
{
    unsigned long long prog, words = 0, q = 0, i, n;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("ifgin"))
        return;
    (void)addr;
    prog = GST_I64(GUEST_OFF_X0 + 8 * 19);
    if (!g2s_try_u64(prog + 0x3a0, &words)
            || !g2s_try_u64(prog + 0x3a0 + 16, &q))
        return;
    n = q & 0xffffffff;
    fprintf(stderr, "g2s_iseed block=%#llx n=%llu |",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 27), n);
    for (i = 1; i <= n && i < 65536; ++i) {
        unsigned long long a = words + 4 * ((i - 1) >> 3), v = 0;
        unsigned nib;
        if (!g2s_try_u64(a & ~7ULL, &v))
            break;
        v = (a & 4) ? v >> 32 : v & 0xffffffff;
        nib = (unsigned)(v >> (((i - 1) & 7) * 4)) & 0xf;
        if (nib)
            fprintf(stderr, " %llu:%x", i, nib);
    }
    fputc('\n', stderr);
}


/* g2s_trace_seedin / g2s_trace_seedout -- f_7100048820's inputs and result.
 *
 * notes/55.  At the entry (x0 = program, x1 = block, x2 = the per-class
 * counter array) this prints what the seed reads: the block's last node and
 * its operand's opcode (the 0x1e/0x5f test at 0x48878), the block that
 * precedes it on the program's block chain (prog[184], next at +0x120) and
 * both blocks' +0xb0 name sets as `name:nibble`, and for every name either
 * set holds, the record `prog[832][name][80]` it maps to (vt[696] is the
 * identity, f_710003c33c).  At 0x489d8 it prints the six counters.
 *
 *   g2s_seedin block=<b> last=<op> lastop72=<op> prev=<b> n=<cg808>
 *              | own <i:nib>... | prev <i:nib>... | map <i:rec>...
 *   g2s_seedout c=<c0>,...,<c5>
 */
static unsigned long long g2s_u32at(unsigned long long a)
{
    unsigned long long v = 0;
    if (!g2s_try_u64(a & ~7ULL, &v))
        return 0;
    return (a & 4) ? (v >> 32) : (v & 0xffffffff);
}

static void g2s_nibset(unsigned long long set, const char *tag,
                       unsigned char *seen, int nseen)
{
    unsigned long long words = 0, nw = 0, i;

    fprintf(stderr, " | %s", tag);
    if (!g2s_try_u64(set, &words) || !words)
        return;
    nw = g2s_u32at(set + 8);
    for (i = 1; i <= 8 * nw && i < 65536; ++i) {
        unsigned nib = (unsigned)(g2s_u32at(words + 4 * ((i - 1) >> 3))
                                  >> (((i - 1) & 7) * 4)) & 0xf;
        if (nib) {
            fprintf(stderr, " %llu:%x", i, nib);
            if ((int)i < nseen)
                seen[i] = 1;
        }
    }
}

void g2s_trace_seedin(cpu_t *cpu, unsigned long addr);
void g2s_trace_seedin(cpu_t *cpu, unsigned long addr)
{
    static unsigned char seen[65536];
    unsigned long long prog, block, last = 0, op72n = 0, p, prev = 0, cur;
    unsigned long long syms = 0, i;
    int guard;

    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("seedin"))
        return;
    (void)addr;
    prog = GST_I64(GUEST_OFF_X0);
    block = GST_I64(GUEST_OFF_X0 + 8);
    memset(seen, 0, sizeof seen);
    g2s_try_u64(block + 24, &last);
    fprintf(stderr, "g2s_seedin block=%#llx last=", block);
    if (last) {
        fprintf(stderr, "%llu", g2s_u32at(last + 8));
        if (g2s_try_u64(last + 72, &op72n) && op72n)
            fprintf(stderr, " lastop72=%llu", g2s_u32at(op72n + 8));
        else
            fprintf(stderr, " lastop72=-");
    } else
        fprintf(stderr, "- lastop72=-");
    p = 0;
    g2s_try_u64(prog + 184, &p);        /* the block list object; [0] = first */
    for (guard = 0; p && guard < 100000; ++guard) {
        cur = 0;
        if (!g2s_try_u64(p, &cur) || !cur || cur == block)
            break;
        prev = cur;
        p = cur + 0x120;
    }
    if (cur != block)
        prev = 0;
    fprintf(stderr, " prev=%#llx n=%llu", prev,
            g2s_u32at(prog + 808));
    g2s_nibset(block + 0xb0, "own", seen, 65536);
    g2s_nibset(prev ? prev + 0xb0 : 0, "prev", seen, 65536);
    fprintf(stderr, " | map");
    g2s_try_u64(prog + 832, &syms);
    for (i = 1; syms && i < 65536; ++i) {
        unsigned long long s = 0;
        if (!seen[i])
            continue;
        if (g2s_try_u64(syms + 8 * i, &s) && s)
            fprintf(stderr, " %llu:%d", i, (int)g2s_u32at(s + 80));
    }
    fputc('\n', stderr);
}

void g2s_trace_seedout(cpu_t *cpu, unsigned long addr);
void g2s_trace_seedout(cpu_t *cpu, unsigned long addr)
{
    unsigned long long out;
    int k;

    if (g2s_trace_budget == 0 || !g2s_want("seedin"))
        return;
    (void)addr;
    out = GST_I64(GUEST_OFF_X0 + 8 * 19);
    fprintf(stderr, "g2s_seedout c=");
    for (k = 0; k < 6; ++k)
        fprintf(stderr, "%s%d", k ? "," : "",
                (int)g2s_u32at(out + 4 * k));
    fputc('\n', stderr);
}

/* g2s_trace_dfin / g2s_trace_dfout -- the front end's block dataflow,
 * f_710006e7c0 (notes/55).  At the entry (x0 = program, w2/w3 = the two
 * flags) and at 0x6f238 (the main path's end, x19 = program) this prints,
 * for every block on the chain prog[184] (next at +0x120):
 *
 *   g2s_dfblk tag=<in|out> blk=<b> w40=.. w60=.. w192=.. b296=.. b249=..
 *            succ=<b,...> s272=<b> s280=<b>
 *   g2s_dfent blk=<b> list=<72|80> name=<entry[28]> mask=<entry[72]>
 *            f76=<entry[76]> node=<op>/<node[48]>
 *   g2s_dfset blk=<b> set=<68|80|98|b0> | <name:nib>...
 *
 * `succ` is the list at block[312] (entries e[16] = block, chained e[0]).
 */
static void g2s_df_dump(unsigned long long prog, const char *tag)
{
    static const int sets[4] = {0x68, 0x80, 0x98, 0xb0};
    unsigned long long b = 0, p = 0;
    int guard, k;

    g2s_try_u64(prog + 184, &p);        /* the block list object; [0] = first */
    if (!p)
        return;

    for (guard = 0; guard < 100000; ++guard) {
        unsigned long long s = 0, e = 0, x = 0, lst;
        int li;

        if (!g2s_try_u64(p, &b) || !b)
            break;
        fprintf(stderr, "g2s_dfblk tag=%s blk=%#llx w40=%d w60=%d w192=%d"
                " b296=%u b249=%u succ=", tag, b,
                (int)g2s_u32at(b + 40), (int)g2s_u32at(b + 60),
                (int)g2s_u32at(b + 192),
                (unsigned)((g2s_u32at(b + 296) >> 0) & 0xff),
                (unsigned)((g2s_u32at(b + 248) >> 8) & 0xff));
        if (g2s_try_u64(b + 312, &lst) && lst && g2s_try_u64(lst, &e)) {
            int g2;
            for (g2 = 0; e && g2 < 1000; ++g2) {
                if (g2s_try_u64(e + 16, &x))
                    fprintf(stderr, "%s%#llx", g2 ? "," : "", x);
                if (!g2s_try_u64(e, &e))
                    break;
            }
        }
        x = 0;
        g2s_try_u64(b + 272, &x);
        fprintf(stderr, " s272=%#llx", x);
        x = 0;
        g2s_try_u64(b + 280, &x);
        fprintf(stderr, " s280=%#llx\n", x);
        for (li = 0; li < 2; ++li) {
            int off = li ? 80 : 72, g2;
            e = 0;
            g2s_try_u64(b + off, &e);
            for (g2 = 0; e && g2 < 100000; ++g2) {
                unsigned long long node = 0;
                fprintf(stderr, "g2s_dfent blk=%#llx list=%d name=%d"
                        " mask=%#x f76=%u node=", b, off,
                        (int)g2s_u32at(e + 28), (unsigned)g2s_u32at(e + 72),
                        (unsigned)(g2s_u32at(e + 76) & 0xff));
                if (g2s_try_u64(e + 32, &node) && node)
                    fprintf(stderr, "%#x/%#x\n",
                            (unsigned)g2s_u32at(node + 8),
                            (unsigned)g2s_u32at(node + 48));
                else
                    fprintf(stderr, "-\n");
                if (!g2s_try_u64(e, &e))
                    break;
            }
        }
        for (k = 0; k < 4; ++k) {
            char t[32];
            snprintf(t, sizeof t, "%x", sets[k]);
            fprintf(stderr, "g2s_dfset blk=%#llx set=%s", b, t);
            g2s_nibset(b + sets[k], "", NULL, 0);
            fputc('\n', stderr);
        }
        (void)s;
        p = b + 0x120;
    }
}

void g2s_trace_dfin(cpu_t *cpu, unsigned long addr);
void g2s_trace_dfin(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("df"))
        return;
    (void)addr;
    fprintf(stderr, "g2s_df prog=%#llx w2=%u w3=%u\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 16) & 0xffffffff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 24) & 0xffffffff));
    if (!(GST_I64(GUEST_OFF_X0 + 24) & 1))
        g2s_df_dump(GST_I64(GUEST_OFF_X0), "in");
}

void g2s_trace_dfout(cpu_t *cpu, unsigned long addr);
void g2s_trace_dfout(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == 0 || !g2s_want("df"))
        return;
    (void)addr;
    g2s_df_dump(GST_I64(GUEST_OFF_X0 + 8 * 19), "out");
    fprintf(stderr, "g2s_dfend\n");
}

/* g2s_trace_rn* -- the order f_7100036760 numbers the front end's names in
 * (notes/55 §6).  Entry: the block.  0x36780 / 0x367a4: the block[80] /
 * block[72] entry about to be visited (x21 / x20) and its symbol.  0x368b4:
 * a NEW record w0 for symbol x21.
 *
 *   g2s_rnblk blk=<b>
 *   g2s_rnent list=<80|72> ent=<e> sym=<s> name=<sym[64]> rec=<sym[80]>
 *   g2s_rnnew sym=<s> rec=<n>
 */
static int g2s_rn_on(void)
{
    if (g2s_trace_budget == -2) {
        const char *env = getenv("G2S_TRACE");
        g2s_trace_budget = (!env || !*env) ? 0 : -1;
    }
    return g2s_trace_budget != 0 && g2s_want("recnum");
}

void g2s_trace_rnblk(cpu_t *cpu, unsigned long addr);
void g2s_trace_rnblk(cpu_t *cpu, unsigned long addr)
{
    (void)addr;
    if (g2s_rn_on())
        fprintf(stderr, "g2s_rnblk blk=%#llx\n",
                (unsigned long long)GST_I64(GUEST_OFF_X0 + 8));
}

static void g2s_rn_ent(cpu_t *cpu, int list, unsigned long long e)
{
    unsigned long long sym = 0;

    g2s_try_u64(e + 16, &sym);
    fprintf(stderr, "g2s_rnent list=%d ent=%#llx sym=%#llx name=%d rec=%d\n",
            list, e, sym, sym ? (int)g2s_u32at(sym + 64) : -1,
            sym ? (int)g2s_u32at(sym + 80) : -1);
    (void)cpu;
}

void g2s_trace_rn80(cpu_t *cpu, unsigned long addr);
void g2s_trace_rn80(cpu_t *cpu, unsigned long addr)
{
    (void)addr;
    if (g2s_rn_on())
        g2s_rn_ent(cpu, 80, GST_I64(GUEST_OFF_X0 + 8 * 21));
}

void g2s_trace_rn72(cpu_t *cpu, unsigned long addr);
void g2s_trace_rn72(cpu_t *cpu, unsigned long addr)
{
    (void)addr;
    if (g2s_rn_on())
        g2s_rn_ent(cpu, 72, GST_I64(GUEST_OFF_X0 + 8 * 20));
}

void g2s_trace_rnnew(cpu_t *cpu, unsigned long addr);
void g2s_trace_rnnew(cpu_t *cpu, unsigned long addr)
{
    (void)addr;
    if (g2s_rn_on())
        fprintf(stderr, "g2s_rnnew sym=%#llx rec=%d\n",
                (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 21),
                (int)(GST_I64(GUEST_OFF_X0) & 0xffffffff));
}

/* g2s_trace_rnmk -- every f_7100036080 (a new vreg record): the caller and
 * the record count prog[808] before it, i.e. the index it will get.
 *
 *   g2s_rnmk lr=<x30> n=<prog[808]> w1=.. w2=.. w3=..
 */
void g2s_trace_rnmk(cpu_t *cpu, unsigned long addr);
void g2s_trace_rnmk(cpu_t *cpu, unsigned long addr)
{
    (void)addr;
    if (!g2s_rn_on())
        return;
    fprintf(stderr, "g2s_rnmk lr=%#llx n=%d w1=%d w2=%d w3=%d\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30),
            (int)g2s_u32at(GST_I64(GUEST_OFF_X0) + 808),
            (int)(GST_I64(GUEST_OFF_X0 + 8) & 0xffffffff),
            (int)(GST_I64(GUEST_OFF_X0 + 16) & 0xffffffff),
            (int)(GST_I64(GUEST_OFF_X0 + 24) & 0xffffffff));
}

/* g2s_trace_irconst / g2s_trace_exprmk -- notes/64.  The loop head's test is
 * the loop statement's own condition, an IR CONSTANT leaf (operations
 * 0x12..0x1a share the emitter arm at 0xf10c38/0xf10c44, which copies the
 * value from node + 0x30).  irconst prints that value; exprmk prints every
 * cgc EXPRESSION built by f_7100f3b030 (W1 = operator) with its caller, so
 * the reader function that made the condition can be named.
 *
 *   g2s_irconst node=<n> op=<op> w40=<node[40]> v=<node[48]>,<node[52]>...
 *   g2s_exprmk lr=<x30> w1=<op> w2=.. w3=.. x4=.. ret-site
 */
void g2s_trace_irconst(cpu_t *cpu, unsigned long addr);
void g2s_trace_irconst(cpu_t *cpu, unsigned long addr)
{
    unsigned long long n, w = 0;
    unsigned op;
    int k;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("irconst"))
        return;
    (void)addr;
    n = GST_I64(GUEST_OFF_X0 + 8);
    if (!g2s_try_u64(n + 16, &w))
        return;
    op = (unsigned)((w & 0xffffffff) >> 16);
    if (op < 0x12 || op > 0x1a)
        return;
    fprintf(stderr, "g2s_irconst node=%#llx op=%#x lr=%#llx w40=%#x v=", n, op,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30),
            (unsigned)g2s_u32at(n + 40));
    for (k = 0; k < 4; ++k)
        fprintf(stderr, "%s%#x", k ? "," : "", (unsigned)g2s_u32at(n + 48 + 4 * k));
    fputc('\n', stderr);
}

void g2s_trace_exprmk(cpu_t *cpu, unsigned long addr);
void g2s_trace_exprmk(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("exprmk"))
        return;
    (void)addr;
    fprintf(stderr, "g2s_exprmk lr=%#llx w1=%#x w2=%#x w3=%#x x4=%#llx x5=%#llx\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8) & 0xffffffff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 16) & 0xffffffff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 24) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 32),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 40));
}

/* g2s_trace_bealloc / g2s_trace_legal -- notes/64.  The bool normalisation
 * (`TRUNC.U` after a float compare, `MOV.S x, -x` after an integer one) is
 * not built by the cgc emitter: no f_7100f0xxxx builder makes those nodes.
 * The back end's node makers (f_7100053170, f_71000530b0, f_7100053230,
 * f_7100052f80, f_7100055260 ...) all allocate through f_710004d7b4, so its
 * entry names every back-end node with its maker (x30) and the maker's caller
 * ([x29+8], the maker's frame being the one x29 points at on entry).
 *
 *   g2s_bealloc size=<w0> maker=<lr> caller=<[x29+8]> caller2=<[[x29]+8]>
 *
 * f_7100bdefd0 is the MOV legaliser (0x47 -> TRUNC 0x6d mod 4 when the
 * destination type is an integer and the source a float); legal prints the
 * node it is handed, before and after:
 *
 *   g2s_legal node=<n> op=<[8]> w12=<[12]> t24=<[24]> t176=<[176]>
 */
void g2s_trace_bealloc(cpu_t *cpu, unsigned long addr);
void g2s_trace_bealloc(cpu_t *cpu, unsigned long addr)
{
    unsigned long long fp, c1 = 0, up = 0, c2 = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("bealloc"))
        return;
    (void)addr;
    fp = GST_I64(GUEST_OFF_X0 + 8 * 29);
    g2s_try_u64(fp + 8, &c1);
    if (g2s_try_u64(fp, &up))
        g2s_try_u64(up + 8, &c2);
    fprintf(stderr, "g2s_bealloc size=%#x maker=%#llx caller=%#llx caller2=%#llx\n",
            (unsigned)(GST_I64(GUEST_OFF_X0) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30), c1, c2);
}

void g2s_trace_legal(cpu_t *cpu, unsigned long addr);
void g2s_trace_legal(cpu_t *cpu, unsigned long addr)
{
    unsigned long long n;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("legal"))
        return;
    (void)addr;
    n = GST_I64(GUEST_OFF_X0 + 8 * 1);
    if (!n || (n & 7))
        return;
    fprintf(stderr, "g2s_legal f_%lx node=%#llx op=%#x w40=%#x w12=%#x t24=%#x t44=%#x t176=%#x w3=%#x lr=%#llx\n",
            addr, n, (unsigned)g2s_u32at(n + 8), (unsigned)g2s_u32at(n + 40),
            (unsigned)g2s_u32at(n + 12),
            (unsigned)g2s_u32at(n + 24), (unsigned)g2s_u32at(n + 44),
            (unsigned)g2s_u32at(n + 176),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 3) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
}

/* g2s_trace_walk -- f_7100054130(ctx, prog, callback, x3, w4) is the per-node
 * pass walker; w4 reaches the callback as its W3 (0x53e20).  Names which
 * driver runs which callback with which argument (notes/64: the bool
 * representation type handed to f_7100030e20 / f_710005f2d0).
 *
 *   g2s_walk cb=<x2> x3=<x3> w4=<w4> lr=<x30>
 */
void g2s_trace_walk(cpu_t *cpu, unsigned long addr);
void g2s_trace_walk(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("walk"))
        return;
    (void)addr;
    fprintf(stderr, "g2s_walk cb=%#llx x3=%#llx w4=%#x lr=%#llx\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 2),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 3),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 4) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
}

/* g2s_trace_argdump -- generic: caller and the first eight words at X1.
 * notes/64 installs it on f_7100fadca0 (cgc constant -> IR constant leaf) to
 * name who makes the loop statement's constant condition.
 *
 *   g2s_argdump f_<fn> lr=<x30> x1=<x1> w2=.. | [x1+0] [x1+8] ...
 */
void g2s_trace_argdump(cpu_t *cpu, unsigned long addr);
void g2s_trace_argdump(cpu_t *cpu, unsigned long addr)
{
    unsigned long long p, w;
    int k;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("argdump"))
        return;
    p = GST_I64(GUEST_OFF_X0 + 8);
    fprintf(stderr, "g2s_argdump f_%lx lr=%#llx x1=%#llx w2=%#x |", addr,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30), p,
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 2) & 0xffffffff));
    for (k = 0; k < 8 && p && !(p & 7); ++k) {
        w = 0;
        g2s_try_u64(p + 8 * k, &w);
        fprintf(stderr, " %#llx", w);
    }
    fputc('\n', stderr);
}

/* g2s_trace_irnew -- f_7100ee39c0(pool, size) is the front end's bump
 * allocator; a fitting request returns the pool cursor `pool[24]`, so the
 * entry already knows the address it will hand out.  Printed for 0x50-byte
 * requests (IR nodes, notes/34) with the caller and the caller's caller, so
 * an IR node seen later (g2s_irconst) can be traced to its maker (notes/64).
 *
 *   g2s_irnew at=<pool[24]> lr=<x30> up=<[x29+8]>
 */
void g2s_trace_irnew(cpu_t *cpu, unsigned long addr);
void g2s_trace_irnew(cpu_t *cpu, unsigned long addr)
{
    unsigned long long pool, cur = 0, up = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("irnew"))
        return;
    (void)addr;
    if ((GST_I64(GUEST_OFF_X0 + 8) & 0xffffffff) != 0x50)
        return;
    pool = GST_I64(GUEST_OFF_X0);
    g2s_try_u64(pool + 24, &cur);
    g2s_try_u64(GST_I64(GUEST_OFF_X0 + 8 * 29) + 8, &up);
    fprintf(stderr, "g2s_irnew at=%#llx lr=%#llx up=%#llx", cur,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30), up);
    /* and six more frames up the frame-pointer chain */
    {
        unsigned long long fp = GST_I64(GUEST_OFF_X0 + 8 * 29), nfp, ra;
        int k;
        for (k = 0; k < 6; ++k) {
            nfp = 0; ra = 0;
            if (!g2s_try_u64(fp, &nfp) || !nfp || (nfp & 7))
                break;
            if (!g2s_try_u64(nfp + 8, &ra))
                break;
            fprintf(stderr, " %#llx", ra);
            fp = nfp;
        }
    }
    fputc('\n', stderr);
}

/* g2s_trace_regname -- f_710003d0f0(prog, ctx, class, sub, buf, regno, 0),
 * the register namer (notes/23).  notes/64: which record class/sub gives
 * `RC` and which `HC`.
 *
 *   g2s_regname class=<w2> sub=<w3> regno=<w5> lr=<x30>
 */
void g2s_trace_regname(cpu_t *cpu, unsigned long addr);
void g2s_trace_regname(cpu_t *cpu, unsigned long addr)
{
    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("regname"))
        return;
    (void)addr;
    {
        unsigned long long vt = 0, fn = 0;
        if (g2s_try_u64(GST_I64(GUEST_OFF_X0 + 8), &vt))
            g2s_try_u64(vt + 96, &fn);
        fprintf(stderr, "g2s_regname vt96=%#llx ", fn);
    }
    fprintf(stderr, "class=%#x sub=%#x regno=%d lr=%#llx\n",
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 2) & 0xffffffff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 8 * 3) & 0xffffffff),
            (int)(GST_I64(GUEST_OFF_X0 + 8 * 5) & 0xffffffff),
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
}

/* g2s_trace_irtree -- at f_7100f10a30's entry from the statement emitter
 * (lr 0xf15b6c / 0xf1507c / 0xf15e64), dump the IR expression tree: every
 * node's op (node[16] >> 16), the flag half, the words at +24..+72, and,
 * recursively, any word that points at another IR node (class word 0xd).
 * notes/65: what the reader makes of a geometry shader's output store.
 *
 *   g2s_irtree <depth> node=<n> op=<op> f16=<..> w24.. w72
 */
static void g2s_irtree_node(unsigned long long n, int depth)
{
    unsigned long long w[7] = {0};
    unsigned f16, cls;
    int k;

    if (depth > 8 || !n || (n & 7))
        return;
    cls = (unsigned)g2s_u32at(n);
    if (cls > 0x20)
        return;
    f16 = (unsigned)g2s_u32at(n + 16);
    for (k = 0; k < 7; ++k)
        g2s_try_u64(n + 24 + 8 * k, &w[k]);
    fprintf(stderr, "g2s_irtree %*s%d node=%#llx cls=%u op=%#x f16=%#x", depth * 2, "",
            depth, n, cls, f16 >> 16, f16 & 0xffff);
    for (k = 0; k < 7; ++k)
        fprintf(stderr, " %d:%#llx", 24 + 8 * k, w[k]);
    fputc('\n', stderr);
    for (k = 0; k < 7; ++k)
        if (w[k] > 0x10000 && !(w[k] & 7) && w[k] != n
                && g2s_u32at(w[k]) <= 0x20 && g2s_u32at(w[k]) >= 1)
            g2s_irtree_node(w[k], depth + 1);
}

void g2s_trace_irtree(cpu_t *cpu, unsigned long addr);
void g2s_trace_irtree(cpu_t *cpu, unsigned long addr)
{
    unsigned long long lr;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0)
        return;
    (void)addr;
    lr = GST_I64(GUEST_OFF_X0 + 8 * 30);
    if (g2s_want("irtreeall")) {
        /* every call from outside the emitter itself (notes/68: the call
         * and the return statements of cf_call.vert) */
        if (lr >= 0x7100f10a30ULL && lr < 0x7100f13400ULL)
            return;
    } else {
        if (!g2s_want("irtree"))
            return;
        if (lr != 0x7100f15b6cULL && lr != 0x7100f1507cULL
                && lr != 0x7100f15e64ULL)
            return;
    }
    fprintf(stderr, "g2s_irtree stmt lr=%#llx\n", lr);
    g2s_irtree_node(GST_I64(GUEST_OFF_X0 + 8), 0);
}

/* g2s_trace_wstmt -- the statement walker's dispatch (notes/71).
 *
 * `f_7100f13ff0` dispatches every IR statement on its kind word at
 * `L_7100f141e4` (`ldr w2, [x21]`, table 0x11be194).  This label hook prints
 * each statement the walker actually receives -- x21, its kind and, for the
 * pointer fields, the IR node tree -- so a statement that was created by
 * the cgc->IR lowering and never reaches its emitter shows up as ABSENT here.
 *
 *   g2s_wstmt stmt=<x21> kind=<n>   followed by g2s_irtree lines per field
 */
void g2s_trace_wstmt(cpu_t *cpu, unsigned long addr);
void g2s_trace_wstmt(cpu_t *cpu, unsigned long addr)
{
    unsigned long long st, w[8] = {0};
    int k;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("wstmt"))
        return;
    (void)addr;
    st = GST_I64(GUEST_OFF_X0 + 8 * 21);
    if (!st || (st & 7)) {
        fprintf(stderr, "g2s_wstmt stmt=%#llx REJECTED\n", st);
        return;
    }
    for (k = 0; k < 8; ++k)
        g2s_try_u64(st + 8 * k, &w[k]);
    fprintf(stderr, "g2s_wstmt stmt=%#llx kind=%u", st,
            (unsigned)(w[0] & 0xffffffff));
    for (k = 1; k < 8; ++k)
        fprintf(stderr, " %d:%#llx", 8 * k, w[k]);
    /* [64] [68] [72]: the context's counters f_7100fa6d10 & co copy in
     * (ctx[284], ctx[280], ctx[288]); [72] becomes cg[72] -> node[36]. */
    {
        unsigned long long p64 = 0, p72 = 0;
        g2s_try_u64(st + 64, &p64);
        g2s_try_u64(st + 72, &p72);
        fprintf(stderr, " p64=%u p68=%u p72=%u",
                (unsigned)(p64 & 0xffffffff), (unsigned)(p64 >> 32),
                (unsigned)(p72 & 0xffffffff));
    }
    fputc('\n', stderr);
    /* [56] is the condition of an `if` and the expression of the others */
    {
        unsigned long long f = 0;
        if (g2s_try_u64(st + 56, &f) && f > 0x10000 && !(f & 7)
                && g2s_u32at(f) <= 0x20 && g2s_u32at(f) >= 1)
            g2s_irtree_node(f, 1);
    }
}

/* g2s_trace_irloop -- the IR loop statement just built (notes/71).
 *
 * `f_7100fa6d10(ctx, cond, body)` allocates the 0x60-byte loop statement and
 * stores `[80] = cond, [88] = body` (`stp x20, x19, [x0, #80]` at 0xfa6d78).
 * Placed as a label hook at 0xfa6d88, after the last store, x0 is the loop.
 * `G2S_WATCH_LOOP=1` arms the write watch on `[80, 96)` of the first loop, so
 * the pass that replaces its body -- the one that makes a statement the
 * walker never sees -- names itself through the host backtrace.
 *
 *   g2s_irloop loop=<x0> cond=<[80]> body=<[88]> body.kind=<n>
 */
void g2s_trace_irloop(cpu_t *cpu, unsigned long addr);
void g2s_trace_irloop(cpu_t *cpu, unsigned long addr)
{
    unsigned long long lp, cond = 0, body = 0, bk = 0;
    const char *w;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("irloop"))
        return;
    (void)addr;
    lp = GST_I64(GUEST_OFF_X0);
    if (!lp || (lp & 7)) {
        fprintf(stderr, "g2s_irloop loop=%#llx REJECTED\n", lp);
        return;
    }
    g2s_try_u64(lp + 80, &cond);
    g2s_try_u64(lp + 88, &body);
    if (body && !(body & 7))
        g2s_try_u64(body, &bk);
    fprintf(stderr, "g2s_irloop loop=%#llx cond=%#llx body=%#llx "
            "body.kind=%u\n", lp, cond, body, (unsigned)(bk & 0xffffffff));
    w = getenv("G2S_WATCH_LOOP");
    if (w && *w && !g2s_watch_addr && !g2s_watch_hi) {
        g2s_watch_lo = lp + 80;
        g2s_watch_hi = lp + 96;
        fprintf(stderr, "g2s_watch loop lo=%#llx hi=%#llx\n",
                (unsigned long long)g2s_watch_lo,
                (unsigned long long)g2s_watch_hi);
    }
}

/* g2s_trace_cgif -- the cgc `if` statement the SPIR-V reader just built.
 *
 * The OpBranchConditional handler `f_7100fd5a00` builds a cgc if statement
 * with `f_7100f3e120(cg, 1, cond, 0, 0, 0)` at 0xfd5b8c (a selection or loop
 * body with no merge record of its own) and 0xfd5b14; the statement comes
 * back in x0 at the return sites 0xfd5b90 / 0xfd5b18.  Layout (the template
 * `f3e120` fills): `[0]` kind byte (1 = if), `[24]` condition, `[32]` then,
 * `[40]` else.
 *
 * `G2S_WATCH_CGIF=<n>` arms the write watch on `[0, 48)` of the n-th if
 * built, so the pass that disposes of a constant-condition if (notes/71)
 * shows up through the host backtrace.
 *
 *   g2s_cgif n=<n> at=<site> stmt=<x0> cond=<[24]> cond.kind=<byte>
 */
void g2s_trace_cgif(cpu_t *cpu, unsigned long addr);
void g2s_trace_cgif(cpu_t *cpu, unsigned long addr)
{
    static unsigned n;
    unsigned long long st, cond = 0, ck = 0;
    const char *w;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("cgif"))
        return;
    ++n;
    st = GST_I64(GUEST_OFF_X0);
    if (!st || (st & 7)) {
        fprintf(stderr, "g2s_cgif n=%u at=%#lx stmt=%#llx REJECTED\n",
                n, addr, st);
        return;
    }
    g2s_try_u64(st + 24, &cond);
    if (cond && !(cond & 7))
        g2s_try_u64(cond, &ck);
    fprintf(stderr, "g2s_cgif n=%u at=%#lx stmt=%#llx cond=%#llx "
            "cond.kind=%u\n", n, addr, st, cond, (unsigned)(ck & 0xff));
    w = getenv("G2S_WATCH_CGIF");
    if (w && *w && (unsigned)atoi(w) == n && !g2s_watch_addr
            && !g2s_watch_hi) {
        g2s_watch_lo = st;
        g2s_watch_hi = st + 48;
        fprintf(stderr, "g2s_watch cgif lo=%#llx hi=%#llx\n",
                (unsigned long long)g2s_watch_lo,
                (unsigned long long)g2s_watch_hi);
    }
}

/* g2s_trace_cgnew -- `f_7100f3aab0(cg, tmpl, size)`, the cgc node interner.
 *
 * Every cgc statement and expression is built as a stack template and handed
 * here; it looks the template up in the tables chained from `cg[1416]`
 * (`f_7100f2eaa0` per table, 0xf3aaf8..0xf3ab20) and returns an existing
 * node when one matches, else allocates `size` bytes and copies.  Hooked at
 * entry it prints the caller and the template, so a statement that appears
 * at the IR lowering without having come from the SPIR-V reader names the
 * function that built it.  `G2S_CGNEW_KIND=<k>` restricts to kind byte k.
 *
 *   g2s_cgnew lr=<x30> kind=<t[0]> size=<w2> 24:<t[24]> 32:.. 40:..
 */
void g2s_trace_cgnew(cpu_t *cpu, unsigned long addr);
void g2s_trace_cgnew(cpu_t *cpu, unsigned long addr)
{
    unsigned long long t, w0 = 0, a = 0, b = 0, c = 0;
    const char *want;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("cgnew"))
        return;
    (void)addr;
    t = GST_I64(GUEST_OFF_X0 + 8);
    if (!t || (t & 7)) {
        fprintf(stderr, "g2s_cgnew tmpl=%#llx REJECTED\n", t);
        return;
    }
    g2s_try_u64(t, &w0);
    want = getenv("G2S_CGNEW_KIND");
    if (want && *want && (unsigned)atoi(want) != (unsigned)(w0 & 0xff))
        return;
    g2s_try_u64(t + 24, &a);
    g2s_try_u64(t + 32, &b);
    g2s_try_u64(t + 40, &c);
    fprintf(stderr, "g2s_cgnew lr=%#llx kind=%u size=%u 24:%#llx 32:%#llx "
            "40:%#llx\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30),
            (unsigned)(w0 & 0xff),
            (unsigned)(GST_I64(GUEST_OFF_X0 + 16) & 0xffffffff), a, b, c);
    /* G2S_CGNEW_BT=1: the host backtrace, which is the guest call chain
     * (tools/whowrote.py resolves it), for finding the pass behind a
     * rebuild that goes through the copy-on-write pair f3a290/f3a320. */
    want = getenv("G2S_CGNEW_BT");
    if (want && *want) {
        void *bt[32];
        int n = backtrace(bt, 32), i;
        fprintf(stderr, "g2s_write addr=0 width=0 val=0 |");
        for (i = 1; i < n; ++i)
            fprintf(stderr, " %p", bt[i]);
        fputc('\n', stderr);
    }
}

/* g2s_trace_cgtree -- the cgc statement tree between the pass driver's steps.
 *
 * Layouts read from their constructors:
 *   every node   [0] kind byte, [1] bit 0 = interned (f3a290 copies then)
 *   kind 0 seq   f3aa40 -> f3b030(cg, 0, a, b, a[8], 0): [24] a, [32] b
 *   kind 1 if    f3e120(cg, 1, cond, then, else):  [24] cond [32] [40]
 *   kind 3 loop  f3e120(cg, 3, ...): [24] [32] [40] as passed
 * Placed as label hooks in `f_7100eef660` after each step of the chain at
 * 0xeefad8..0xeefb28 (fb0070, vt[544], vt[536], eeffa0, f74bc0), x0 being
 * the step's result; at 0xeefad8 itself x1/x2 are fb0070's inputs.
 *
 *   g2s_cgtree at=<label> reg=<n>
 *   g2s_cg <indent> <node> kind=<k> [cond=<node> ck=<k>]
 */
static void g2s_cgtree_node(unsigned long long nd, int depth)
{
    unsigned long long w0 = 0, a = 0, b = 0, c = 0, ck = 0;
    unsigned k;

    if (depth > 24)
        return;
    if (!nd || (nd & 7) || !g2s_try_u64(nd, &w0)) {
        if (nd)
            fprintf(stderr, "g2s_cg %*s%#llx <unreadable>\n", 2 * depth, "",
                    nd);
        return;
    }
    k = (unsigned)(w0 & 0xff);
    g2s_try_u64(nd + 24, &a);
    g2s_try_u64(nd + 32, &b);
    g2s_try_u64(nd + 40, &c);
    if (k == 1) {
        if (a && !(a & 7))
            g2s_try_u64(a, &ck);
        fprintf(stderr, "g2s_cg %*s%#llx kind=1 cond=%#llx ck=%u/%#x\n",
                2 * depth, "", nd, a, (unsigned)(ck & 0xff),
                (unsigned)((ck >> 8) & 0xffffff));
        g2s_cgtree_node(b, depth + 1);
        if (c)
            fprintf(stderr, "g2s_cg %*selse\n", 2 * depth, "");
        g2s_cgtree_node(c, depth + 1);
        return;
    }
    fprintf(stderr, "g2s_cg %*s%#llx kind=%u w0=%#llx 24:%#llx 32:%#llx "
            "40:%#llx\n", 2 * depth, "", nd, k, w0, a, b, c);
    if (k == 0) {
        g2s_cgtree_node(a, depth + 1);
        g2s_cgtree_node(b, depth + 1);
    } else if (k >= 0x10 && getenv("G2S_CGTREE_EXPR")) {
        /* G2S_CGTREE_EXPR=1: expression operands too -- `[24]`/`[32]` when
         * they are readable nodes with a kind byte below 0x60 (an operator's
         * operands; a constant's `[24]` is its first value word and fails
         * the test or prints as a leaf) */
        unsigned long long ops[2] = { a, b };
        int q;
        for (q = 0; q < 2; ++q) {
            unsigned long long w = 0;
            if (ops[q] > 0x100000 && !(ops[q] & 7)
                    && g2s_try_u64(ops[q], &w) && (w & 0xff) < 0x60)
                g2s_cgtree_node(ops[q], depth + 1);
        }
    } else if (k == 3) {
        g2s_cgtree_node(a, depth + 1);
        g2s_cgtree_node(b, depth + 1);
        g2s_cgtree_node(c, depth + 1);
    }
}

void g2s_trace_cgtree(cpu_t *cpu, unsigned long addr);
void g2s_trace_cgtree(cpu_t *cpu, unsigned long addr)
{
    int r;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("cgtree"))
        return;
    for (r = 0; r < 3; ++r) {
        if (addr != 0x7100eefad8UL && r > 0)
            break;
        if (addr == 0x7100eefad8UL && r == 0)
            continue;
        fprintf(stderr, "g2s_cgtree at=%#lx reg=x%d\n", addr, r);
        g2s_cgtree_node(GST_I64(GUEST_OFF_X0 + 8 * r), 1);
    }
}

/* g2s_trace_shadow -- f_7100f7b3a0, the callback of the output-shadowing
 * pass f_7100f7b230 (notes/65) that makes a shadow for a variable reference
 * whose symbol's binding record `sym[168]` has flags `[12] & 0x2020 == 0x20`
 * and `[80] == 0`.  Prints the symbol, its name atom and those fields.
 *
 *   g2s_shadow sym=<s> atom=<sym[?]> b=<sym[168]> b12=.. b80=.. s96=..
 */
void g2s_trace_shadow(cpu_t *cpu, unsigned long addr);
void g2s_trace_shadow(cpu_t *cpu, unsigned long addr)
{
    unsigned long long n, sym = 0, b = 0, b80 = 0, s96 = 0;
    unsigned f16;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("shadow"))
        return;
    (void)addr;
    n = GST_I64(GUEST_OFF_X0 + 8);
    if (!n || (n & 7))
        return;
    f16 = (unsigned)g2s_u32at(n + 16);
    if (f16 & 0xffff0002u)
        return;
    g2s_try_u64(n + 40, &sym);
    if (!sym || (sym & 7))
        return;
    g2s_try_u64(sym + 168, &b);
    g2s_try_u64(sym + 96, &s96);
    if (b && !(b & 7))
        g2s_try_u64(b + 80, &b80);
    fprintf(stderr, "g2s_shadow sym=%#llx atom=%#x b=%#llx b12=%#x b80=%#llx s96=%#llx\n",
            sym, (unsigned)g2s_u32at(sym + 8), b,
            b && !(b & 7) ? (unsigned)g2s_u32at(b + 12) : 0, b80, s96);
}

/* g2s_trace_blkrec -- the statement walker's block test (notes/65).  At
 * 0xf15ab4 in f_7100f13ff0, X0 is the NAME RECORD f_7100f0dff0 returned for
 * an assignment's destination; the walker opens a new block when
 * `record[40]` is set and the current block already holds a node.
 *
 *   g2s_blkrec rec=<x0> sym=<rec[0]> r40=<rec[40]> r40op=.. r48=.. r56=..
 */
void g2s_trace_blkrec(cpu_t *cpu, unsigned long addr);
void g2s_trace_blkrec(cpu_t *cpu, unsigned long addr)
{
    unsigned long long r, w[13] = {0};
    int k;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("blkrec"))
        return;
    (void)addr;
    r = GST_I64(GUEST_OFF_X0);
    if (!r || (r & 7))
        return;
    for (k = 0; k < 13; ++k)
        g2s_try_u64(r + 8 * k, &w[k]);
    fprintf(stderr, "g2s_blkrec rec=%#llx", r);
    for (k = 0; k < 13; ++k)
        fprintf(stderr, " %d:%#llx", 8 * k, w[k]);
    if (w[5] && !(w[5] & 7))
        fprintf(stderr, " r40op=%#x", (unsigned)g2s_u32at(w[5] + 8));
    fputc('\n', stderr);
}

/* g2s_trace_asgn -- f_7100f0c4e0, the statement walker's store of a value
 * into a NAME RECORD (notes/68: which store is the callee's entry copy of
 * its parameter).  X0 is the record, `record[0]` the symbol, `sym[64]` the
 * name index `g2s_rnent` prints.
 *
 *   g2s_asgn lr=<x30> rec=<x0> sym=<..> name=<sym[64]> x2=.. w7=..
 */
void g2s_trace_asgn(cpu_t *cpu, unsigned long addr);
void g2s_trace_asgn(cpu_t *cpu, unsigned long addr)
{
    unsigned long long r, sym = 0;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("asgn"))
        return;
    (void)addr;
    r = GST_I64(GUEST_OFF_X0);
    if (r && !(r & 7))
        g2s_try_u64(r, &sym);
    fprintf(stderr, "g2s_asgn lr=%#llx rec=%#llx sym=%#llx name=%d x2=%#llx "
            "w7=%d\n", (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30), r,
            sym, (sym && !(sym & 7)) ? (int)g2s_u32at(sym + 64) : -1,
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 16),
            (int)(GST_I64(GUEST_OFF_X0 + 56) & 0xffffffff));
}

/* g2s_trace_regs -- a label hook that prints the guest's x0..x8 and lr at the
 * label (notes/67: the MUL(RCP) -> DIV peephole's profile test, at 0x690a0
 * where x8 is the method and w2 the width, and 0x690a4 where w0 is the
 * answer).
 *
 *   g2s_regs at=<label> x0=.. x8=.. lr=..
 */
void g2s_trace_regs(cpu_t *cpu, unsigned long addr);
void g2s_trace_regs(cpu_t *cpu, unsigned long addr)
{
    int k;

    if (g2s_trace_budget == -2) {
        const char *e = getenv("G2S_TRACE");
        g2s_trace_budget = (!e || !*e) ? 0 : -1;
    }
    if (g2s_trace_budget == 0 || !g2s_want("regs"))
        return;
    fprintf(stderr, "g2s_regs at=%#lx", addr);
    for (k = 0; k <= 8; ++k)
        fprintf(stderr, " x%d=%#llx", k,
                (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * k));
    for (k = 19; k <= 26; ++k)
        fprintf(stderr, " x%d=%#llx", k,
                (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * k));
    fprintf(stderr, " lr=%#llx\n",
            (unsigned long long)GST_I64(GUEST_OFF_X0 + 8 * 30));
}
