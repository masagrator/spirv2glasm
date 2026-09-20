"""lex.py -- the hand-written text scanners the converter uses on its own
lines, in place of regular expressions.

Every helper here states the pattern it replaces and matches it exactly on
ASCII text (the only text the converter writes): the refactor that removed
`re` from the compiler had to leave every listing byte-identical
(tools/samecheck.py).  A "word" character is `[A-Za-z0-9_]`, `\\b` the
boundary between a word and a non-word character, as in `re`.
"""

WS = " \t\n\r\f\v"
DIGITS = "0123456789"
XYZW = "xyzw"
_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789_")


def is_word(c):
    return c in _WORD


def is_digits(s):
    """`\\d+` full: one or more ASCII digits and nothing else."""
    if not s:
        return False
    for c in s:
        if c not in DIGITS:
            return False
    return True


def digit_end(s, i):
    """The end of the run of digits starting at `i` (== i when none)."""
    n = len(s)
    while i < n and s[i] in DIGITS:
        i += 1
    return i


def is_swizzle(s, lo=1, hi=4):
    """`[xyzw]{lo,hi}` full."""
    if not lo <= len(s) <= hi:
        return False
    for c in s:
        if c not in XYZW:
            return False
    return True


def is_numbered(s, head):
    """`<head>\\d+` full, e.g. `#12` for head `#`, `R3` for head `R`."""
    return s.startswith(head) and is_digits(s[len(head):])


def swizzle_suffix(tok, lo=1, hi=None):
    """The `xyzw` run of a trailing `\\.([xyzw]{lo,hi})$`, or None.

    `$` also matches before one final newline; the converter's tokens have
    none, which the callers rely on."""
    k = tok.rfind(".")
    if k < 0:
        return None
    # the run is the LONGEST xyzw tail after a dot: `a.b.xy` -> `xy`
    j = len(tok)
    while j > 0 and tok[j - 1] in XYZW:
        j -= 1
    if j == 0 or tok[j - 1] != "." or j == len(tok):
        return None
    run = tok[j:]
    if len(run) < lo or (hi is not None and len(run) > hi):
        # a longer run than `hi`: `\.[xyzw]{1,4}$` then needs its dot
        # inside the run, which there is none -- no match
        return None
    return run


def keyword_at(line, words, lstrip=True):
    """`^\\s*(w1|w2|...)\\b`: the word, or None."""
    s = line.lstrip(WS) if lstrip else line
    for w in words:
        if s.startswith(w) and (len(s) == len(w) or s[len(w)] not in _WORD):
            return w
    return None


def split_first(line):
    """`^\\s*\\S+\\s*`: (start of the first token, its end, the end of the
    blanks after it), or None when the line is blank."""
    n = len(line)
    i = 0
    while i < n and line[i] in WS:
        i += 1
    j = i
    while j < n and line[j] not in WS:
        j += 1
    if j == i:
        return None
    k = j
    while k < n and line[k] in WS:
        k += 1
    return i, j, k


def split_line(line):
    """`^\\s*(\\S+)\\s+([^,;]+)(?:,\\s*(.*))?;\\s*$` -> (mnemonic, dest,
    rest) with rest '' when there is none, or None.  (sched.py's old
    `_LINE`, backtracking included: `RET  ;` gives the dest ` `.)"""
    got = split_line_at(line)
    return None if got is None else got[:3]


def split_line_at(line):
    """`split_line` and the offset of the rest in `line`: -1 when the line
    has no comma (the group did not take part)."""
    lead = len(line) - len(line.lstrip(WS))
    s = line[lead:].rstrip(WS)
    if not s.endswith(";") or "\n" in s:
        return None
    body = s[:-1]
    n = len(body)
    i = 0
    while i < n and body[i] not in WS:
        i += 1
    if i == 0:
        return None
    j = i
    while j < n and body[j] in WS:
        j += 1
    if j == i:
        return None
    k = j
    while k < n and body[k] != "," and body[k] != ";":
        k += 1
    start = j
    if k == j:
        # the dest may only be empty by giving back one whitespace
        if j - i < 2:
            return None
        start = j - 1
    if k == n:
        return body[:i], body[start:], "", -1
    if body[k] == ";":
        return None
    r = k + 1
    while r < n and body[r] in WS:
        r += 1
    return body[:i], body[start:k], body[r:], lead + r


def sub_numbered(line, head, fn, word_end=False, word_start=False):
    """Replace every `<head>(\\d+)` by `fn(digits)`, the digits as text.

    `word_end`: the pattern ends in `\\b` (the digits must not run into a
    word character); `word_start`: it begins with `\\b` (the head must not
    follow a word character -- only meaningful for a word head like `D`)."""
    found = find_numbered(line, head, word_end, word_start)
    if not found:
        return line
    out = []
    pos = 0
    for i, e, digits in found:
        out.append(line[pos:i])
        out.append(fn(digits))
        pos = e
    out.append(line[pos:])
    return "".join(out)


def find_numbered(line, head, word_end=False, word_start=False):
    """The matches of `<head>(\\d+)`, left to right and not overlapping:
    [(start, end, digits as text)]."""
    found = []
    h = len(head)
    n = len(line)
    i = line.find(head)
    while i >= 0:
        e = digit_end(line, i + h)
        ok = e > i + h
        if ok and word_end and e < n and line[e] in _WORD:
            ok = False
        if ok and word_start and i > 0 and line[i - 1] in _WORD:
            ok = False
        if ok:
            found.append((i, e, line[i + h:e]))
            i = line.find(head, e)
        else:
            i = line.find(head, i + 1)
    return found


def not_digit_after(text, e):
    """The default tail for `sub_name`: `(?!\\d)`; the match's end, or -1."""
    return e if e == len(text) or text[e] not in DIGITS else -1


def find_name(text, name, start=0, prev=None, tail=not_digit_after):
    """The first occurrence of `name` at or after `start` that the
    lookbehind set `prev` (a string of characters the one before must be
    among, None for no condition) and `tail` accept: (start, end) or
    None."""
    i = text.find(name, start)
    while i >= 0:
        if prev is None or (i > 0 and text[i - 1] in prev):
            e = tail(text, i + len(name))
            if e >= 0:
                return i, e
        i = text.find(name, i + 1)
    return None


def sub_name(text, name, fn, prev=None, tail=not_digit_after, count=0):
    """`re.sub` of `(?<=[prev])NAME<tail>` by `fn(start, end)`, left to right
    and not overlapping; `count` limits the replacements (0: all)."""
    out, pos, done = [], 0, 0
    at = find_name(text, name, 0, prev, tail)
    while at is not None:
        i, e = at
        out.append(text[pos:i])
        out.append(fn(i, e))
        pos = e
        done += 1
        if count and done >= count:
            break
        at = find_name(text, name, e if e > i else e + 1, prev, tail)
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def has_swizzle_suffix(t):
    """`\\.[xyzw]+$` searched."""
    return swizzle_suffix(t) is not None


def is_attrib(t):
    """`(vertex|fragment)\\.attrib\\[\\d+\\]` in full."""
    for head in ("vertex.attrib[", "fragment.attrib["):
        if t.startswith(head) and t.endswith("]"):
            return is_digits(t[len(head):-1])
    return False


def is_broadcastable(text, braces=True):
    """`-?(#\\d+|(vertex|fragment)\\.attrib\\[\\d+\\]|\\{[^{}]*\\})$`
    matched (without the `{..}` arm when `braces` is false): a scalar that
    a wider op reads through `.x`."""
    t = text[1:] if text.startswith("-") else text
    if is_numbered(t, "#") or is_attrib(t):
        return True
    if braces and len(t) >= 2 and t[0] == "{" and t[-1] == "}":
        inner = t[1:-1]
        return "{" not in inner and "}" not in inner
    return False
