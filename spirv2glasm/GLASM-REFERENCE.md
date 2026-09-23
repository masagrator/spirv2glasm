# GLASM mnemonic reference (GLSLC 17.24, NV_gpu_program5 family)

Scope: what GLSLC 17.24 prints. **Seen** means the form appears in the oracle's listings (`--opt-level none`): 15,598 oracle listings -- the probes, the 120-shader corpus sample and the full corpus. The examples are copied from them verbatim, and the probe named with an example is where the converter's rule for it was measured -- EXCEPT where the example says its listing is in `listings_open/`, which is the ORACLE's listing for a probe the converter does not yet reproduce: the form is measured, the converter's rule for it is not. **Vocab** means the name exists in the compiler's opcode namer (`notes/glasm_opcodes.json`, in `notes.7z`) but no listing uses it. For those, the meaning comes from the NV_gpu_program4/5 specs and is not verified against this compiler.

---

## 1. Line syntax

```
MNEMONIC[.TYPE][.CC[n]] dst[.mask], src0[, src1[, src2]];
```

- The mnemonic is left-justified to 5 columns (`%-5s`): `OR.S  R0, ...`, `IF    NE.x;`, `RET   (TR);`. Longer mnemonics run on with a single space.
- Every statement ends with `;`. A label line is `BB<n>:`.
- The listing ends with `END` and then `# <n> instructions, <m> R-regs`.

### The shape of a mnemonic

Measured over the corpus listings; every part after the name is optional:

```
MNEM [ .<op> ] [ .<type> [ X<width> ] ] [ .CC | .CC1 ]
```

- `.<op>` is the family member, and only the atomics have one:
  `ATOMB.ADD.U32`, `ATOMS.ADD.U32`.
- `.CC` comes AFTER the type, always: `MOV.U.CC`, `MOV.S.CC`, `SEQ.U.CC`,
  `TRUNC.U.CC`, and `MOV.U.CC1` for the second condition register.  There is
  no `MOV.CC` and no `MOV.CC.U`.
- `X<width>` is the vector width, and only the memory family has one:
  `LDC.F32X2`, `LDC.F32X4`, `LDC.U32X4`.

### Type suffix: full or short, and it is per OPCODE

WHICH FORM an opcode takes is READ, not a convention: `py/glslc/glasm.py`'s
`LongSuffixOps` is the list of opcodes that take the FULL suffix for a float
type (`DIV.F32`), and every other opcode takes the short one (`MAX.F`).
Integers are short (`.S`, `.U`) either way, and the `suffix` column of every
table below says which list an opcode is on.

The 35 opcodes on it that have a mnemonic:

`ADD` `COS` `DDX` `DDY` `DIV` `DIVSQ` `DP2` `DP3` `DP4` `DPH` `DST` `EX2` `EXP` `FRC` `IPAC` `IPAO` `IPAS` `LG2` `LIT` `LOG` `MAD` `MOD` `MUL` `POW` `RCP` `RFL` `RSQ` `SGE` `SGT` `SIN` `SLE` `SLT` `SNE` `SUB` `TANH`

(7 more are IR-only and never printed.)

CHECKED over every listing the tree can see: no opcode on the list prints a
short float suffix and no opcode off it prints a full one -- with ONE class
of exception, the memory family (`LDC`, `LDB`, `STB`, `LDS`, `STS`, `ATOMB`,
`ATOMS`, `LOADIM`), which a different printer spells (notes/23) and which
carries the loaded WIDTH instead.  Those rows read `own printer`.

| type | on the long list | off it |
|---|---|---|
| float | `.F32` | `.F` |
| int | `.S` | `.S` |
| uint | `.U` | `.U` |
| bool | stored as uint: `.U` | `.U` |
| int64 / uint64 | `.S64` / `.U64` | `.S64` / `.U64` |
| half | `.F16` | `.F` |

The suffix is chosen from the node's own type code, `node[24]`, through a
byte jump table (`tools/typesuffix.py`, `notes/type_suffix.json`):

| `node[24]` | suffix |
|---|---|
| `0x6` | `.F32` |
| `0x7` | `.F16` |
| `0x8` | `.F16` |
| `0x9` | `.S64` |
| `0xa` | `.U64` |
| `0xb` | `.S32` |
| `0xc` | `.U32` |
| `0xd` | `.S16` |
| `0xe` | `.U16` |
| `0xf` | `.S8` |
| `0x10` | `.U8` |
| `0x13` | `.F64` |
| `0x1b` | `.S64` |

Two opcodes (`0x6c`, `0x6d`) carry no name of their own: the printer
spells them from the rounding mode in `node[12]` -- 1 = `FLR`, 2 = `ROUND`, 3 = `CEIL`, 4 = `TRUNC`
(notes/23).

- `MOV` takes its suffix from the **source** type.
- `TRUNC`/`ROUND`/`FLR`/`CEIL` take theirs from the **destination** type: `TRUNC.U R0.x, R0;` converts float to uint.
- `I2F` takes its suffix from the **integer source**: `I2F.S`, `I2F.U`.
- Loads use a width suffix (§7).

### Operand count, and the flag that decides whether a line is printed

The `operands` column of every table below is `node[153]`, MEASURED, not
counted off an example.  The 8-byte word at `node+152` is two bytes that do
different jobs (notes/29 recorded it as one opaque "flags" field; notes/43
reads it):

| byte | is |
|---|---|
| `node[152]` | flags.  **Bit 0 decides whether the node prints a line of its own or is inlined into its consumer.**  The folding pass `f_7100056fa0` is the only thing that clears it |
| `node[153]` | the number of SOURCE operands; the destination is not among them |

Measured with the oracle's `fold` gate over all 692 probes
(`G2S_TRACE=1 G2S_ONLY=fold`, `tools/arity.py`): **80 opcodes have an
operand count and not one of them varies** -- arity is a constant of the
opcode -- and bit 0 was set on entry to the fold pass on every node, with no
exceptions.  That is what the old observations were: `RET` 0x001 is flag 1
and 0 operands, `MOV` 0x101 one, `POW` 0x201 two, `REP` 0x301 three.

### Destination write mask

| components written | mask |
|---|---|
| 4 | none: `R0` |
| 1 / 2 / 3 | `.x` / `.xy` / `.xyz` |
| single component store | the lane itself: `result.position.y`, `R6.x` |

### Source swizzle

- Omitted when it is the identity for the result width: `a0.xy` into `.xy` prints bare.
- Padded to four by repeating: `.zw` becomes `.zwzw`.
- Collapsed to one letter when all four are equal: `.xxxx` becomes `.x`.
- Scalar ops (`RCP RSQ EX2 LG2 SIN COS POW`) always print the letter, including `.x`: `RSQ.F32 R2.x, R1.x;`.

### Source modifiers

- `-src`: negate. Subtraction prints as `ADD a, -b`.
- `|src|`: absolute value.
- A negated or abs value that gets stored is first materialised: `MOV.F R0, -src;`.

### Operands

| operand | form |
|---|---|
| temporaries | `R<n>` (32-bit), `H<n>` (short), `D<n>` (64-bit, `LONG TEMP`) |
| condition registers | `RC`, `HC` (as destinations of `.CC` ops) |
| constant | `{a, b, c, d}`; a scalar constant read into a wider result prints `{k, 0, 0, 0}.x` |
| vertex input | `vertex.attrib[n]`, `vertex.instanceIndex`, `vertex.vertexIndex` |
| fragment input | `fragment.attrib[n]`, `fragment.position` |
| per-vertex input | `vertex[k].position`, `vertex_position[R1.x]` (indexed alias) |
| tessellation / geometry built-ins | `vertex.tesscoord`, `primitive.invocation` |
| outputs | `result.position[.c]`, `result.attrib[n]`, `result_color<n>` (fragment), `result.patch.tessouter[k]`, `result.depth.z` (`gl_FragDepth`, and the component IS `.z`: `MOV.F result.depth.z, R0.x;`, `0084_fd_a.frag`) |
| buffers | `buf<n>[byteoff]`, `buf<n>[R0.x + off]`, `sbo_buf<n>[...]` |
| local memory | `lmem<n>[byteoff]`, `lmem<n>[R0.x]`, with a component select on a read: `MOV.F R3, lmem0[R0.x].xyzw;` `MOV.F R0.w, lmem0[R0.x].w;` (4,107 corpus listings; `0000_mq_n12.frag`) |
| shared memory | `shared_mem[byteoff]`, `shared_mem[R0.x]` -- read and written by `LDS`/`STS`, never by `MOV` (§7) |
| texture handle | `handle(D0.x)`, and with a component on a gather: `handle(D0.x).y`, `handle(D1.x).z` (§8, `TXG`) |

### Condition codes

- `.CC` sets the condition register: `MOV.U.CC RC.x, R0;` and `MOV.U.CC1 RC.xy, R10;` (second CC set).
- When the compare itself sets CC, it writes `HC`: `SEQ.U.CC HC.x, a, b;`, `MOV.S.CC HC.x, -R0;`.
- Tests are `NE.x`, `EQ.x` and so on: `IF NE.x;`, `BRK (NE.x);`, `KIL NE.x;`.
- `(TR)` means always true: `RET (TR);`, `CAL BB7 (TR);`.

---

**One row per OPCODE NUMBER.**  The first column is the number the compiler
puts in `node[8]`, and every number the namer names has its own row -- the
twelve opcodes spelled `MOV` are twelve rows, not one.  Two further columns
exist for that reason:

* **why this number** -- what separates this opcode from the others with the
  same mnemonic.  Every "measured" claim there is a shader in
  `opcov/opcodes/` compiled and read back with `tools/nodedump.py`, which
  prints the opcode of the node that printed each line.  Where nothing
  separates them that could be read, the cell SAYS SO instead of offering a
  plausible reason.
* **evidence** -- **Emitted** means a line in the 852 traced modules
  carried this number, with the count, the number of modules and a verbatim
  example.  **Named only** means the namer holds the name and no traced line
  carries the number; the mnemonic may still be printed, by a DIFFERENT
  number, so a mnemonic-level "Seen" must not be read as evidence for a
  particular opcode.

The attribution comes from `tools/opcodemap.py` over
`notes/opcode_evidence.json`; the hand-written half of the why column is
`notes/glasm_opcode_why.json`.
## 2. Data movement and conversion

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x3b` | `MOV` | 1 | short (prints `.F32`, `.S32`, `.U32`, `.U64`) | copy | The MEMORY opcode, not a move: its MODIFIER picks the spelling. mod=3 prints the constant- and storage-buffer loads (4,462 of its 4,571 lines: `LDC.*`, `LDB.*`), mod=2 prints `MOV`. The namer's `MOV` is never what a mod=3 line prints (notes/23). | **Emitted.** 4571 lines in 313 modules. `LDC.F32 R2.x, buf0[16];` `LDC.F32X4 R15, buf0[736];` -- spelled `LDB.F32`, `LDB.F32X4`, `LDB.U32`, `LDC.F32`, `LDC.F32X2`, `LDC.F32X4`, `LDC.S32`, `LDC.U32`, `LDC.U32X4`, `LDC.U64`, `MOV.F`, `MOV.S`, `MOV.U` |
| `0x3c` | `MOV` | 2 | short | copy | mod 2 and 3, and prints only `MOV` (326 lines in 62 modules). What picks it over `0x47` is NOT READ. | **Emitted.** 326 lines in 62 modules. `MOV.F R1.x, fragment.position.z;` `MOV.F R6.w, R0.x;` -- spelled `MOV.F`, `MOV.S`, `MOV.U` |
| `0x47` | `MOV` | 1 | short (prints `.F64`, `.S64`, `.U64`) | copy | The ordinary move: mod=0, 52,353 lines in 837 of the 866 modules, and the only `MOV` number seen with `.F64`/`.S64`/`.U64` or `.SAT`. | **Emitted.** 52353 lines in 837 modules. `MOV.F R4.w, R3.x;` `MOV.F R4.z, R2.x;` -- spelled `MOV.F`, `MOV.F.SAT`, `MOV.F64`, `MOV.S`, `MOV.S64`, `MOV.U`, `MOV.U.CC`, `MOV.U64` |
| `0x4a` | `MOV` | 1 | short | copy | mod=0 like `0x47`, but 2,192 of its 2,268 lines are `MOV.S`. What picks it over `0x47` is NOT READ. | **Emitted.** 2268 lines in 134 modules. `MOV.S R1.x, R18;` `MOV.S R0.x, R18;` -- spelled `MOV.F`, `MOV.S`, `MOV.U` |
| `0x4b` | `MOV` | -- | short | copy | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x54` | `MOV` | -- | short | copy | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x57` | `MOV` | 2 | short | copy | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x7e` | `MOV` | 1 | short | copy | The CONDITION-CODE move: mod=1, and 4,111 of its 4,618 lines write `RC`/`HC` with `.CC` or `.CC1`. | **Emitted.** 4618 lines in 178 modules. `MOV.U.CC HC.x, R2;` `MOV.U.CC HC.x, R2.y;` -- spelled `MOV.F`, `MOV.S`, `MOV.S.CC`, `MOV.U.CC`, `MOV.U.CC1` |
| `0xa8` | `MOV` | 3 | short | copy | The PREDICATED move: mod=5, and every line carries a guard -- `MOV.U R4.xy(NE1), {1, 1, 0, 0};`. | **Emitted.** 142 lines in 37 modules. `MOV.U R4.xy(NE1), {1, 1, 0, 0};` `MOV.U R3.xy(NE), {1, 1, 0, 0};` -- spelled `MOV.F`, `MOV.U`, `MOV.U.CC` |
| `0xd0` | `MOV` | -- | short | copy | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x204` | `MOV` | -- | short | copy | Observed in 5 modules, all of them `OpImageSparse*` ones. | **Emitted.** 65 lines in 5 modules. `MOV.S R12.x, 0;` `MOV.S R1.x, 0;` -- spelled `MOV.F`, `MOV.S` |
| `0x21b` | `MOV` | -- | short | copy | Not observed. | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. |
| -- | `MOV.<t>.CC / MOV.<t>.CC1` | -- | modifier | copy and set the condition register; `.CC1` sets the second. NOT an opcode of its own -- `.CC` is a modifier on the `MOV` opcodes above, and it follows the type (`MOV.U.CC`, never `MOV.CC.U`) | -- | **Seen.** `MOV.U.CC RC.x, R1;` `MOV.S.CC HC.x, -R0;` |
| `0x70` | `I2F` | 1 | short (prints `.S64`, `.U64`) | int to float | -- | **Emitted.** 386 lines in 103 modules. `I2F.S R1, R0;` `I2F.U R1, R0;` -- spelled `I2F.S`, `I2F.S64`, `I2F.U`, `I2F.U64` |
| -- | `TRUNC` | -- | own printer (prints `.S64`) | round toward 0; converts when the destination is int | -- | **Seen.** `TRUNC.F R0, a;` `TRUNC.U R0.x, R0;` `TRUNC.S R18.xy, R30;` `TRUNC.U.CC HC.x, H0;` |
| -- | `ROUND` | -- | own printer | round to nearest | -- | **Seen.** `ROUND.F R0, vertex.attrib[0];` |
| `0x6e` | `FLR` | 1 | short | floor | -- | **Emitted.** 15 lines in 4 modules. `FLR.F R5.xyz, fragment.attrib[0];` `FLR.F R0, vertex.attrib[0];` |
| `0x65` | `CEIL` | 1 | short | ceiling | -- | **Emitted.** 1 lines in 1 modules. `CEIL.F R0, vertex.attrib[0];` |
| `0x1c0` | `CVT` | -- | short (prints `.F32`, `.F64`, `.S32`, `.S64`, `.U16`, `.U32`, `.U64`) | type conversion, `CVT.<dst>.<src>` | -- | **Emitted.** 64 lines in 8 modules. `CVT.S32.U16 R0.x, R1;` `CVT.F64.S32 D0.x, R0;` -- spelled `CVT.F64.F32.TRUNC`, `CVT.F64.S32`, `CVT.S32.U16`, `CVT.S64.S32.TRUNC`, `CVT.U64.U32`, `CVT.U64.U32.TRUNC` |
| `0xc5` | `UP2H` | 1 | short | unpack two halfs from one 32-bit word | -- | **Emitted.** 55 lines in 9 modules. `UP2H.F H0.xy, R0.x;` `UP2H.F H0.xy, R1.x;` |
| `0xc6` | `UP2US` | 1 | short | unpack two unsigned 16-bit normalized values | -- | **Emitted.** 3 lines in 2 modules. `UP2US.F R6.xy, R1.x;` `UP2US.F R7.xy, R0.x;` |
| `0xc8` | `UP4UB` | 1 | short | unpack four unsigned 8-bit normalized values | -- | **Emitted.** 34 lines in 7 modules. `UP4UB.F H0, R0.x;` `UP4UB.F H0, R4.x;` |
| `0xc0` | `PK2H` | -- | short | pack floats into half / ushort / byte / ubyte, or two 32-bit values into one 64-bit | -- | **Emitted.** 51 lines in 6 modules. `PK2H.F R4.x, fragment.attrib[0].zwzw;` `PK2H.F R5.x, R2;` |
| `0xc1` | `PK2US` | -- | short | pack floats into half / ushort / byte / ubyte, or two 32-bit values into one 64-bit | -- | **Emitted.** 1 lines in 1 modules. `PK2US.F R2.x, fragment.attrib[0];` |
| `0xc2` | `PK4B` | -- | short | pack floats into half / ushort / byte / ubyte, or two 32-bit values into one 64-bit | -- | **Named only.** No line in the traced modules carries this number. |
| `0xc3` | `PK4UB` | -- | short | pack floats into half / ushort / byte / ubyte, or two 32-bit values into one 64-bit | -- | **Emitted.** 31 lines in 4 modules. `PK4UB.F R5.x, fragment.attrib[0];` `PK4UB.F R2.x, R5;` |
| `0x1bb` | `PK64` | -- | short | pack floats into half / ushort / byte / ubyte, or two 32-bit values into one 64-bit | -- | **Emitted.** 21 lines in 3 modules. `PK64.U D0.x, {0, 1073741824, 0, 0};` `PK64.U D2.x, fragment.attrib[2];` |
| `0xc7` | `UP4B` | -- | short | unpack signed bytes / a 64-bit value | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1bc` | `UP64` | -- | short | unpack signed bytes / a 64-bit value | -- | **Emitted.** 1 lines in 1 modules. `UP64.F R2.xy, D1;` |

`UP4UB` and `UP2H` write a SHORT register (`H0`, declared `SHORT TEMP H0;`), which a `MOV.F` then copies to an R register.

---

## 3. Arithmetic

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x83` | `ADD` | 2 | full for float | a + b (subtraction is `ADD a, -b`) | -- | **Emitted.** 13164 lines in 347 modules. `ADD.F32 R5, R7, R4;` `ADD.F32 R5.x, R3, {1, 0, 0, 0};` -- spelled `ADD.F16`, `ADD.F32`, `ADD.F64`, `ADD.S`, `ADD.U` |
| `0xa2` | `SUB` | -- | full for float | a − b | -- | **Named only.** No line in the traced modules carries this number. |
| `0x90` | `MUL` | 2 | full for float | a × b | -- | **Emitted.** 9965 lines in 435 modules. `MUL.F32 R3.x, fragment.attrib[0].w, {5, 0, 0, 0};` `MUL.F32 R2.x, fragment.attrib[0].z, {4, 0, 0, 0};` -- spelled `MUL.F32`, `MUL.F64`, `MUL.S`, `MUL.U` |
| `0x91` | `MUL.HI` | -- | short | high 32 bits of an integer product | -- | **Emitted.** 26 lines in 2 modules. `MUL.HI.S R7.x, R2, R2;` `MUL.HI.U R14.x, R1, R10;` -- spelled `MUL.HI.S`, `MUL.HI.U` |
| `0xac` | `MAD` | -- | full for float | a × b + c | -- | **Emitted.** 1 lines in 1 modules. `MAD.F32 R0.x, fragment.attrib[0], fragment.attrib[0].y, fragment.attrib[0].z;` |
| `0x87` | `DIV` | 2 | full for float | a / b | -- | **Emitted.** 469 lines in 97 modules. `DIV.F32 R0.x, {1, 0, 0, 0}, fragment.position.w;` `DIV.F32 R0.xy, R40, R2.x;` -- spelled `DIV.F32`, `DIV.S`, `DIV.U` |
| `0x8f` | `MOD` | 2 | full for float | integer a mod b | -- | **Emitted.** 317 lines in 22 modules. `MOD.U R0.x, R2, {3, 0, 0, 0}.x;` `MOD.U R1.x, R0, {3, 0, 0, 0}.x;` -- spelled `MOD.S`, `MOD.U` |
| `0x8d` | `MAX` | 2 | short | component-wise min / max | -- | **Emitted.** 271 lines in 87 modules. `MAX.F R1.xy, R38, {0, 0, 0, 0};` `MAX.F R3.x, R0, {0, 0, 0, 0};` -- spelled `MAX.F`, `MAX.S`, `MAX.U` |
| `0x8e` | `MIN` | 2 | short | component-wise min / max | -- | **Emitted.** 326 lines in 88 modules. `MIN.F R0.xy, R34, R38;` `MIN.F R0.x, R4, {7, 0, 0, 0};` -- spelled `MIN.F`, `MIN.S`, `MIN.U` |
| `0xb0` | `SAD` | -- | short | \|a − b\| + c | -- | **Named only.** No line in the traced modules carries this number. |
| `0xab` | `LRP` | -- | short | a·b + (1 − a)·c | -- | **Named only.** No line in the traced modules carries this number. |
| `0x88` | `DP2` | 2 | full for float | dot product of 2, 3 or 4 components, scalar result | -- | **Emitted.** 58 lines in 16 modules. `DP2.F32 R2.x, R4, R4;` `DP2.F32 R1.x, R3, R3;` |
| `0x89` | `DP3` | 2 | full for float | dot product of 2, 3 or 4 components, scalar result | -- | **Emitted.** 310 lines in 69 modules. `DP3.F32 R2.x, vertex.attrib[1], R38;` `DP3.F32 R1.x, vertex.attrib[1], R40;` |
| `0x8a` | `DP4` | 2 | full for float | dot product of 2, 3 or 4 components, scalar result | -- | **Emitted.** 634 lines in 100 modules. `DP4.F32 R15.x, R15, R14;` `DP4.F32 R2.x, R1, R12;` |
| `0xaa` | `DP2A` | -- | short | dp2(a, b) + c.x | -- | **Named only.** No line in the traced modules carries this number. |
| `0x8b` | `DPH` | -- | full for float | dp3(a, b) + b.w | -- | **Named only.** No line in the traced modules carries this number. |
| `0x74` | `LIT` | -- | full for float | distance vector / lighting coefficients (legacy ARB) | -- | **Named only.** No line in the traced modules carries this number. |
| `0x8c` | `DST` | -- | full for float | distance vector / lighting coefficients (legacy ARB) | -- | **Named only.** No line in the traced modules carries this number. |
| `0x78` | `NRM` | -- | short | normalize the xyz components | -- | **Named only.** No line in the traced modules carries this number. |
| `0x94` | `RFL` | -- | full for float | reflect | -- | **Named only.** No line in the traced modules carries this number. |
| `0xa5` | `X2D` | -- | short | 2D coordinate transform | -- | **Named only.** No line in the traced modules carries this number. |
| `0x82` | `SSG` | -- | short | sign: −1, 0 or 1 | -- | **Named only.** No line in the traced modules carries this number. |
| `0x6f` | `FRC` | 1 | full for float | fractional part | -- | **Emitted.** 23 lines in 23 modules. `FRC.F32 R12.xy, R11;` `FRC.F32 R13.xy, R12;` |

---

## 4. Transcendental (scalar, one component per instruction)

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x7b` | `RCP` | 1 | full for float | 1 / x | -- | **Emitted.** 62 lines in 18 modules. `RCP.F32 R8.w, {1023, 1023, 1023, 3}.w;` `RCP.F32 R8.z, {1023, 1023, 1023, 3}.z;` |
| `0x7c` | `RSQ` | 1 | full for float | 1 / √x | -- | **Emitted.** 170 lines in 55 modules. `RSQ.F32 R8.x, R7.x;` `RSQ.F32 R3.x, R1.x;` |
| `0x6a` | `EX2` | 1 | full for float | 2ˣ / log₂ x | -- | **Emitted.** 69 lines in 32 modules. `EX2.F32 R6.x, R7.x;` `EX2.F32 R0.x, R1.x;` |
| `0x72` | `LG2` | 1 | full for float | 2ˣ / log₂ x | -- | **Emitted.** 63 lines in 30 modules. `LG2.F32 R6.x, R8.x;` `LG2.F32 R0.x, R2.x;` |
| `0x6b` | `EXP` | -- | full for float | partial-precision exponent / log (legacy) | -- | **Named only.** No line in the traced modules carries this number. |
| `0x75` | `LOG` | -- | full for float | partial-precision exponent / log (legacy) | -- | **Named only.** No line in the traced modules carries this number. |
| `0x93` | `POW` | 2 | full for float | aᵇ | -- | **Emitted.** 34 lines in 5 modules. `POW.F32 R0.x, vertex.attrib[0].x, vertex.attrib[1].x;` `POW.F32 R3.x, vertex.attrib[0].w, vertex.attrib[1].w;` |
| `0x66` | `COS` | 1 | full for float | sine / cosine | -- | **Emitted.** 43 lines in 11 modules. `COS.F32 R0.x, fragment.attrib[1].x;` `COS.F32 R0.x, vertex.attrib[0].x;` |
| `0x7f` | `SIN` | 1 | full for float | sine / cosine | -- | **Emitted.** 47 lines in 12 modules. `SIN.F32 R0.x, vertex.attrib[0].x;` `SIN.F32 R3.x, vertex.attrib[0].w;` |
| `0x226` | `TANH` | -- | full for float | hyperbolic tangent | -- | **Named only.** No line in the traced modules carries this number. |
| `0x86` | `DIVSQ` | -- | full for float | name only | -- | **Named only.** No line in the traced modules carries this number. |

A vector transcendental is scalarised: one instruction per component, then the components are gathered with `MOV`. `sqrt(x)` becomes `RSQ` followed by `DIV.F32 {1,0,0,0}, rsq`.

---

## 5. Compare (set on condition)

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x95` | `SEQ` | 2 | full for float | ==, !=, <, <=, >, >= | -- | **Emitted.** 1601 lines in 68 modules. `SEQ.U.CC HC.x, {1, 0, 0, 0}, {0, 0, 0, 0};` `SEQ.F H0.x, H0, {0, 0, 0, 0};` -- spelled `SEQ.F`, `SEQ.S`, `SEQ.U`, `SEQ.U.CC` |
| `0x98` | `SGE` | 2 | full for float | ==, !=, <, <=, >, >= | -- | **Emitted.** 94 lines in 58 modules. `SGE.F32 R2, R5.xyxy, R3;` `SGE.U R4.x, R3, R26;` -- spelled `SGE.F32`, `SGE.S`, `SGE.U` |
| `0x99` | `SGT` | 2 | full for float | ==, !=, <, <=, >, >= | -- | **Emitted.** 550 lines in 89 modules. `SGT.F32 R0.x, vertex.attrib[0], {0, 0, 0, 0};` `SGT.F32 R0, vertex.attrib[0], {0, 0, 0, 0};` -- spelled `SGT.F32`, `SGT.S`, `SGT.U` |
| `0x9e` | `SLE` | -- | full for float | ==, !=, <, <=, >, >= | -- | **Emitted.** 23 lines in 5 modules. `SLE.F32 R15.x, R26, R26.y;` `SLE.F32 R4.x, R1, {0, 0, 0, 0};` -- spelled `SLE.F32`, `SLE.S`, `SLE.U` |
| `0x9f` | `SLT` | 2 | full for float | ==, !=, <, <=, >, >= | -- | **Emitted.** 614 lines in 143 modules. `SLT.F32 R0.x, {0, 0, 0, 0}, fragment.attrib[3];` `SLT.F32 R0.x, R6, {0, 0, 0, 0};` -- spelled `SLT.F32`, `SLT.S`, `SLT.U` |
| `0xa0` | `SNE` | 2 | full for float | ==, !=, <, <=, >, >= | -- | **Emitted.** 297 lines in 26 modules. `SNE.F32 R1, fragment.attrib[0], R0.x;` `SNE.F32 R1.xy, fragment.attrib[0], R0.x;` -- spelled `SNE.F32`, `SNE.S`, `SNE.U`, `SNE.U.CC` |

A bool is normalised right after the compare:

- float compare: `TRUNC.U R.x, R;`

- int or uint compare: `MOV.S R.x, -R;` (or `MOV.U`)

The branch then tests it: `MOV.U.CC RC.x, R; IF NE.x;`.

---

## 6. Bit operations

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x84` | `AND` | 2 | short | bitwise | -- | **Emitted.** 777 lines in 31 modules. `AND.U R2.x, R6.z, {15, 0, 0, 0};` `AND.S R0, fragment.attrib[0], fragment.attrib[1];` -- spelled `AND.S`, `AND.U`, `AND.U.CC` |
| `0x92` | `OR` | 2 | short | bitwise | -- | **Emitted.** 574 lines in 90 modules. `OR.S  D0.x, D0, D1;` `OR.U  R1.x, R4, R4.y;` -- spelled `OR.S`, `OR.U`, `OR.U.CC` |
| `0xa3` | `XOR` | 2 | short | bitwise | -- | **Emitted.** 95 lines in 8 modules. `XOR.S R0, fragment.attrib[0], fragment.attrib[1];` `XOR.U R4.x, R0, R10;` -- spelled `XOR.S`, `XOR.U` |
| `0x77` | `NOT` | 1 | short | bitwise not | -- | **Emitted.** 35 lines in 8 modules. `NOT.U R0, fragment.attrib[0];` `NOT.S R1.x, fragment.attrib[0].y;` -- spelled `NOT.S`, `NOT.U` |
| `0x9a` | `SHL` | -- | short | shift left / right (`.S` is arithmetic, `.U` is logical) | Not observed in any of the 866 modules -- yet the IR-operation -> GLASM map read out of the image (`notes/ir_glasm_map.json`) names `0x9a`, not `0x9b`, for all seven variants of the shift family. The two readings disagree and the disagreement is NOT RESOLVED here. | **Named only.** No line in the traced modules carries this number. |
| `0x9b` | `SHL` | 2 | short | shift left / right (`.S` is arithmetic, `.U` is logical) | The shift the compiler emits: 190 lines, `.S` and `.U`, 32- and 64-bit alike. | **Emitted.** 190 lines in 40 modules. `SHL.S R10.x, R2, {5, 0, 0, 0}.x;` `SHL.S R8.x, R6, {2, 0, 0, 0}.x;` -- spelled `SHL.S`, `SHL.U` |
| `0x9c` | `SHR` | -- | short | shift left / right (`.S` is arithmetic, `.U` is logical) | Not observed; same unresolved disagreement with the IR map as `0x9a`. | **Named only.** No line in the traced modules carries this number. |
| `0x9d` | `SHR` | 2 | short | shift left / right (`.S` is arithmetic, `.U` is logical) | The shift the compiler emits: 86 lines, `.S` and `.U`. | **Emitted.** 86 lines in 34 modules. `SHR.S R8.x, R7, {2, 0, 0, 0}.x;` `SHR.S R3.x, R2, {2, 0, 0, 0}.x;` -- spelled `SHR.S`, `SHR.U` |
| `0x1af` | `BFE` | 2 | short | bitfield extract: `BFE dst, {width, offset}, src`; `.S` sign-extends | -- | **Emitted.** 76 lines in 15 modules. `BFE.U R0.x, {11, 0, 0, 0}, R6;` `BFE.U R1.x, {11, 11, 0, 0}, R6;` -- spelled `BFE.S`, `BFE.U` |
| `0x1b0` | `BFI` | 3 | short | bitfield insert: `BFI dst, {width, offset}, insert, base` | -- | **Emitted.** 46 lines in 9 modules. `BFI.S R0.x, {4, 4, 0, 0}, fragment.attrib[0], {0, 0, 0, 0};` `BFI.S R2.x, {4, 12, 0, 0}, fragment.attrib[0].z, {0, 0, 0, 0};` -- spelled `BFI.S`, `BFI.U` |
| `0x1b1` | `BFR` | -- | short | bit reverse | -- | **Emitted.** 13 lines in 2 modules. `BFR.S R8.x, R0;` `BFR.S R1.x, fragment.attrib[1].y;` |
| `0x1b2` | `BTC` | -- | short | population count | -- | **Emitted.** 16 lines in 4 modules. `BTC.U R4.x, R1;` `BTC.U R106.x, R105;` -- spelled `BTC.S`, `BTC.U` |
| `0x1b3` | `BTFL` | -- | short | find lowest / most significant set bit | -- | **Emitted.** 14 lines in 5 modules. `BTFL.S R0.x, fragment.attrib[1];` `BTFL.U R109.x, R105;` -- spelled `BTFL.S`, `BTFL.U` |
| `0x1b4` | `BTFM` | -- | short | find lowest / most significant set bit | -- | **Emitted.** 198 lines in 5 modules. `BTFM.S R2.x, fragment.attrib[1].y;` `BTFM.U R5.x, fragment.attrib[2].z;` -- spelled `BTFM.S`, `BTFM.U` |

---

## 7. Memory

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| -- | `LDC` | -- | own printer (prints `.F32`, `.S32`, `.U32`, `.U64`) | constant (uniform) buffer load; the suffix is the loaded width | -- | **Seen.** `LDC.F32 R0.x, buf0[680];` `LDC.F32X2 R0.xy, buf5[24];` `LDC.F32X4 R0, buf0[0];` `LDC.S32 R0.x, buf1[0];` `LDC.U32X4 R0.yzw, buf4[0];` `LDC.U64 D0.x, buf14[328];` `LDC.F32X4 R26, buf0[R24.x + 480];` |
| -- | `LDB` | -- | own printer (prints `.F32`, `.U32`) | storage buffer load | -- | **Seen.** `LDB.U32 R14.x, sbo_buf15[R14.x + 8];` `LDB.F32X4 R0, sbo_buf0[32];` (`0111_hl_b`) |
| -- | `STB` | -- | own printer (prints `.F32`, `.S32`, `.U32`) | storage buffer store: `STB value, buffer[address]`; the suffix is the stored width | -- | **Seen.** `STB.U32 R2, sbo_buf0[0];` `STB.F32 R1, sbo_buf0[0];` (`0111_cb_c`) `STB.F32X4 R0, sbo_buf1[16];` (`0000_st_d`) |
| `0x1b9` | `LOADIM` | 2 | short (prints `.F32`, `.S32`, `.U16`, `.U32`) | image load: `LOADIM dst, coord, handle(Dn.x), target`; the suffix is the raw texel's width, not the image's format (below) | -- | **Emitted.** 35 lines in 25 modules. `LOADIM.U32 R6, {1, 2, 0, 0}, handle(D0.x), 2D;` `LOADIM.U16 R1, {1, 2, 0, 0}, handle(D0.x), 2D;` -- spelled `LOADIM.F32`, `LOADIM.F32X2`, `LOADIM.F32X4`, `LOADIM.S32`, `LOADIM.S32X4`, `LOADIM.SPARSE.F32X4`, `LOADIM.U16`, `LOADIM.U32`, `LOADIM.U32X2`, `LOADIM.U32X4` |
| `0x1ba` | `STOREIM` | 3 | short | image store: `STOREIM handle(Dn.x), texel, coord, target`; the suffix is the texel's type | -- | **Emitted.** 20 lines in 17 modules. `STOREIM.S handle(D0.x), {-3, -3, -3, -3}, {1, 2, 3, 0}, 3D;` `STOREIM.F handle(D0.x), {2, 2, 2, 2}, {5, 0, 0, 0}, 1D;` -- spelled `STOREIM.F`, `STOREIM.S`, `STOREIM.U` |
| `0x1e4` | `ATOMB.ADD` | -- | short (prints `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 22 lines in 6 modules. `ATOMB.ADD.U32 R2.x, {0, 0, 0, 0}, sbo_buf0[0];` `ATOMB.ADD.U32 R3.x, {1, 0, 0, 0}, sbo_buf0[0];` |
| `0x1e5` | `ATOMB.MIN` | -- | short (prints `.S32`, `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 26 lines in 3 modules. `ATOMB.MIN.U32 R6.x, R0, sbo_buf0[4];` `ATOMB.MIN.S32 R13.x, R4, sbo_buf0[512];` -- spelled `ATOMB.MIN.S32`, `ATOMB.MIN.U32` |
| `0x1e6` | `ATOMB.MAX` | -- | short (prints `.S32`, `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 26 lines in 3 modules. `ATOMB.MAX.U32 R7.x, R0, sbo_buf0[8];` `ATOMB.MAX.S32 R17.x, R4, sbo_buf0[516];` -- spelled `ATOMB.MAX.S32`, `ATOMB.MAX.U32` |
| `0x1e7` | `ATOMB.AND` | -- | short (prints `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 13 lines in 2 modules. `ATOMB.AND.U32 R8.x, R0, sbo_buf0[12];` `ATOMB.AND.U32 R10.x, R0, sbo_buf0[12];` |
| `0x1e8` | `ATOMB.OR` | -- | short (prints `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 13 lines in 2 modules. `ATOMB.OR.U32 R9.x, R0, sbo_buf0[16];` `ATOMB.OR.U32 R11.x, R0, sbo_buf0[16];` |
| `0x1e9` | `ATOMB.XOR` | -- | short (prints `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 13 lines in 2 modules. `ATOMB.XOR.U32 R10.x, R0, sbo_buf0[20];` `ATOMB.XOR.U32 R12.x, R0, sbo_buf0[20];` |
| `0x1ea` | `ATOMB.EXCH` | -- | short (prints `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 15 lines in 4 modules. `ATOMB.EXCH.U32 R0.x, {1, 0, 0, 0}, sbo_buf0[0];` `ATOMB.EXCH.U32 R11.x, R0, sbo_buf0[24];` |
| `0x1eb` | `ATOMB.CSWAP` | -- | short (prints `.U32`) | atomic on a storage buffer; returns the old value | -- | **Emitted.** 13 lines in 2 modules. `ATOMB.CSWAP.U32 R12.x, R1, sbo_buf0[28];` `ATOMB.CSWAP.U32 R12.x, R3, sbo_buf0[28];` |
| `0x1f5` | `ATOMB.IWRAP` | -- | short | atomic on a storage buffer; returns the old value | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1f6` | `ATOMB.DWRAP` | -- | short | atomic on a storage buffer; returns the old value | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1cd` | `ATOM.ADD` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1ce` | `ATOM.MIN` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1cf` | `ATOM.MAX` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1d0` | `ATOM.IWRAP` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1d1` | `ATOM.DWRAP` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1d2` | `ATOM.AND` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1d3` | `ATOM.OR` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1d4` | `ATOM.XOR` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1d5` | `ATOM.EXCH` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1d6` | `ATOM.CSWAP` | -- | short | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Not observed, and not reachable from GLSL: atomics through an `NV_shader_buffer_load` pointer are refused -- `argument 1 to atomicAdd needs to be a variable` -- while plain loads and stores through one give `LOAD.U32`/`STORE.U32`. Every atomic that IS expressible has its own family (`ATOMB`, `ATOMS`, `ATOMIM`). | **Named only.** No line in the traced modules carries this number. |
| `0x1ec` | `ATOMS.ADD` | 2 | short | atomic on shared memory | -- | **Emitted.** 13 nodes in 2 modules; nodedump could not pair the printed line. From the listing: `ATOMS.ADD.U32 R1.x, {1, 0, 0, 0}, shared_mem[R1.x + 256];` |
| `0x1ed` | `ATOMS.MIN` | -- | short | atomic on shared memory | -- | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.MIN.U32 R7.x, {2, 0, 0, 0}, shared_mem[R3.x + 256];` |
| `0x1ee` | `ATOMS.MAX` | -- | short | atomic on shared memory | -- | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.MAX.U32 R8.x, {3, 0, 0, 0}, shared_mem[R2.x + 256];` |
| `0x1ef` | `ATOMS.AND` | -- | short | atomic on shared memory | -- | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.AND.U32 R5.x, {4, 0, 0, 0}, shared_mem[R4.x + 256];` |
| `0x1f0` | `ATOMS.OR` | -- | short | atomic on shared memory | -- | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.OR.U32 R6.x, {5, 0, 0, 0}, shared_mem[R2.x + 256];` |
| `0x1f1` | `ATOMS.XOR` | -- | short | atomic on shared memory | -- | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.XOR.U32 R5.x, {6, 0, 0, 0}, shared_mem[R3.x + 256];` |
| `0x1f2` | `ATOMS.EXCH` | -- | short | atomic on shared memory | -- | **Emitted.** 12 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.EXCH.U32 R6.x, {7, 0, 0, 0}, shared_mem[R2.x + 256];` |
| `0x1f3` | `ATOMS.CSWAP` | -- | short | atomic on shared memory | -- | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. From the listing: `ATOMS.CSWAP.U32 R4.x, {1, 2, 0, 0}, shared_mem[R2.x + 256];` |
| `0x1d7` | `ATOMIM.ADD` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 13 lines in 2 modules. `ATOMIM.ADD.U32 R24.x, {1, 0, 0, 0}, {0, 0, 0, 0}, handle(D0.x), 2D;` `ATOMIM.ADD.U32 R22.x, {1, 0, 0, 0}, {1, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x1d8` | `ATOMIM.MIN` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 13 lines in 2 modules. `ATOMIM.MIN.U32 R25.x, R0, {0, 1, 0, 0}, handle(D0.x), 2D;` `ATOMIM.MIN.U32 R4.x, R0, {1, 1, 0, 0}, handle(D0.x), 2D;` |
| `0x1d9` | `ATOMIM.MAX` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 1 lines in 1 modules. `ATOMIM.MAX.U32 R1.x, {2, 0, 0, 0}, {0, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x1da` | `ATOMIM.IWRAP` | -- | short | atomic on an image | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1db` | `ATOMIM.DWRAP` | -- | short | atomic on an image | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1dc` | `ATOMIM.AND` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 1 lines in 1 modules. `ATOMIM.AND.U32 R2.x, {3, 0, 0, 0}, {0, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x1dd` | `ATOMIM.OR` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 1 lines in 1 modules. `ATOMIM.OR.U32 R3.x, {4, 0, 0, 0}, {0, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x1de` | `ATOMIM.XOR` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 1 lines in 1 modules. `ATOMIM.XOR.U32 R4.x, {5, 0, 0, 0}, {0, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x1df` | `ATOMIM.EXCH` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 1 lines in 1 modules. `ATOMIM.EXCH.U32 R5.x, {6, 0, 0, 0}, {0, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x1e0` | `ATOMIM.CSWAP` | -- | short (prints `.U32`) | atomic on an image | -- | **Emitted.** 13 lines in 2 modules. `ATOMIM.CSWAP.U32 R5.x, R3, {0, 2, 0, 0}, handle(D0.x), 2D;` `ATOMIM.CSWAP.U32 R6.x, R3, {1, 2, 0, 0}, handle(D0.x), 2D;` |
| `0x1e1` | `ATOMCTR.GET` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1e2` | `ATOMCTR.INCR` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1e3` | `ATOMCTR.DECR` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x208` | `ATOMCTROP.ADD` | -- | short | atomic counter | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x209` | `ATOMCTROP.ADD` | -- | short | atomic counter | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x20a` | `ATOMCTROP.MIN` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x20b` | `ATOMCTROP.MAX` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x20c` | `ATOMCTROP.AND` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x20d` | `ATOMCTROP.OR` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x20e` | `ATOMCTROP.XOR` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x20f` | `ATOMCTROP.EXCH` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| `0x210` | `ATOMCTROP.CSWAP` | -- | short | atomic counter | -- | **Named only.** No line in the traced modules carries this number. |
| -- | `LDS` | -- | own printer (prints `.F32`, `.U32`) | shared-memory load | -- | **Seen.** `LDS.U32 R1.x, shared_mem[R1.x];` (`0095_sm_a.comp`, listing in `listings_open/`) `LDS.U32 R0.x, shared_mem[4];` (corpus) |
| -- | `STS` | -- | own printer (prints `.F32`, `.U32`) | shared-memory store: `STS value, shared_mem[address]` | -- | **Seen.** `STS.U32 R0, shared_mem[R1.x];` (`0095_sm_a.comp`) |
| `0x1c1` | `MEMBAR` | 0 | short | memory barrier | -- | **Emitted.** 19 lines in 4 modules. `MEMBAR.CTA;` `MEMBAR;` -- spelled `MEMBAR`, `MEMBAR.CTA` |
| `0x213` | `LDCB` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x21c` | `LDBB` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x21d` | `STBB` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x21e` | `ATOMBB.ADD` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x21f` | `ATOMBB.MIN` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x220` | `ATOMBB.MAX` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x221` | `ATOMBB.AND` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x222` | `ATOMBB.OR` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x223` | `ATOMBB.XOR` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x224` | `ATOMBB.EXCH` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x225` | `ATOMBB.CSWAP` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x227` | `ATTRRD` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x228` | `ATTRWR` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x229` | `LDTM` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x22a` | `STTM` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x22b` | `LDMM` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x22c` | `STMM` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |

Addressing:

- Offsets are in bytes.

- A dynamic index is scaled first: `MUL.S R.x, idx, {stride,0,0,0}; MOV.S R.x, R;`.

- Bindless sampler handles are 64-bit and live in `buf14`: combined samplers at `8·binding`, textures at `328 + 8·binding`, samplers at `1352 + 8·binding`, storage images at `256 + 8·binding`.

- Local memory has NO mnemonic of its own: an `lmem<n>[...]` operand is read and written by ordinary `MOV` (`MOV.F lmem0[0], {1, 0.5, 0.25, 0};`). Shared memory is the opposite -- it is only ever `LDS`/`STS`.

- Shared memory is declared by a directive, `SHARED_MEMORY <bytes>;` (`SHARED_MEMORY 512;` in `0095_sm_a.comp`, `SHARED_MEMORY 256;` in `0119_bar_a.comp`), and its variables carry `#semantic TGSM0 : SHARED` with `shared_mem[0]` as the register column.

---

### Image formats and `LOADIM`

The load's width is the texel's storage; the format's own conversion follows
as ordinary instructions (measured on the `ld_*` probes):

| image format | load | then |
|---|---|---|
| `r32f` `rg32f` `rgba32f` | `LOADIM.F32` / `.F32X2` / `.F32X4` | nothing (the full-width formats return the load) |
| `r32i` `rgba32i` / `r32ui` `rgba32ui` | `LOADIM.S32` / `.S32X4` / `.U32` / `.U32X4` | nothing |
| `rgba8` | `LOADIM.U32` | `UP4UB.F H0, R.x` |
| `rgba8_snorm` | `LOADIM.U32` | one `BFE.S {8, 8k}` per lane, then `I2F.S`, `DIV.F32` by 127, `MIN.F` with 1, `MAX.F` with -1 |
| `rgba8i` / `rgba8ui` | `LOADIM.S32` / `LOADIM.U32` | one `BFE.S` / `BFE.U {8, 8k}` per lane |
| `r16f` | `LOADIM.U16` | `CVT.S32.U16`, then `UP2H.F` |
| `rg16f` / `rgba16f` | `LOADIM.U32` / `LOADIM.U32X2` | `UP2H.F` per word |
| `rgba16` | `LOADIM.U32X2` | `UP2US.F` per word |
| `rgba16ui` | `LOADIM.U32X2` | `BFE.U {16, 0}` and `{16, 16}` on each word |
| `rgb10_a2` | `LOADIM.U32` | `BFE.U {10,0} {10,10} {10,20} {2,30}`, `I2F.U`, then a multiply by `RCP {1023, 1023, 1023, 3}` |
| `r11f_g11f_b10f` | `LOADIM.U32` | `BFE.U {11,0} {11,11} {10,22}`, `SHL.U` by 17/17/18 into the float's mantissa, then a multiply by 2¹¹² |

---

## 8. Texture

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0xbc` | `TEX` | 2 | short | sample with implicit LOD | The plain sample. Coordinate in one source. | **Emitted.** 147 lines in 43 modules. `TEX.F R4, R6, handle(D0.x), 2D;` `TEX.F R6, fragment.attrib[1], handle(D0.x), 2D;` -- spelled `TEX.F`, `TEX.F.SPARSE` |
| `0x19f` | `TEX` | 3 | short | sample with implicit LOD | `0xbc` WITH A TEXEL OFFSET -- the line carries a trailing `offset({...})`. Measured: `textureOffset` compiles to `0x19f`, `texture` to `0xbc`. | **Emitted.** 37 lines in 4 modules. `TEX.F R0, fragment.attrib[0], handle(D0.x), 2D, offset({0, 1, 0, 0});` `TEX.F R1, R3, handle(D0.x), 2D, offset({0, 1, 0, 0});` |
| `0x1a7` | `TEX` | -- | short | sample with implicit LOD | The THREE-SOURCE shadow sample: the compare value is its own operand because the coordinate already fills four lanes. Measured on `samplerCubeArrayShadow`: `TEX.F R14, fragment.attrib[0], fragment.attrib[0], handle(D1.x), SHADOWARRAYCUBE;`. Same idea as `0xb2`/`0xb6` for bias and LOD. | **Emitted (opcov/vocab only).** 2 lines in 2 modules. `TEX.F R14, fragment.attrib[0], fragment.attrib[0], handle(D1.x), SHADOWARRAYCUBE;` `TEX.F R1, fragment.attrib[0], fragment.attrib[0], handle(D0.x), SHADOWARRAYCUBE;` |
| `0x1a8` | `TEX` | -- | short | sample with implicit LOD | Not observed; the same gap for `0x1a7` (three-source shadow) + offset. | **Named only.** No line in the traced modules carries this number. |
| `0x1ab` | `TEX` | -- | short | sample with implicit LOD | `0xbc` WITH AN LOD CLAMP -- `TEX.F.LODCLAMP`. Measured from `textureClampARB` (GL_ARB_sparse_texture_clamp); the clamp is a register operand. Works on `2D` and on `ARRAYCUBE`, so the clamp does NOT need a three-source form of its own. | **Emitted (opcov/vocab only).** 3 lines in 2 modules. `TEX.F.LODCLAMP R1, fragment.attrib[0], R0, handle(D0.x), 2D;` `TEX.F.LODCLAMP.SPARSE R3, fragment.attrib[0], R0, handle(D0.x), 2D;` -- spelled `TEX.F.LODCLAMP`, `TEX.F.LODCLAMP.SPARSE` |
| `0x1ad` | `TEX` | -- | short | sample with implicit LOD | `0x1ab` WITH A TEXEL OFFSET -- clamp and offset together. Measured from `textureOffsetClampARB`. | **Emitted (opcov/vocab only).** 2 lines in 1 modules. `TEX.F.LODCLAMP R1, fragment.attrib[0], R1, handle(D0.x), 2D, offset({1, 0, 0, 0});` |
| `0xb6` | `TXL` | -- | short | explicit LOD, taken from coord `.w` | The THREE-SOURCE LOD form, exactly as `0xb2` is for bias. Measured: `textureLod(samplerCubeArray, vec4, lod)` -> `TXL.F R0, a[0], a[0].w, handle(D0.x), ARRAYCUBE;`. | **Emitted (opcov/cover only).** 1 lines in 1 modules. `TXL.F R0, fragment.attrib[0], fragment.attrib[0].w, handle(D0.x), ARRAYCUBE;` |
| `0xb7` | `TXL` | 2 | short | explicit LOD, taken from coord `.w` | The ordinary two-source LOD form. | **Emitted.** 102 lines in 44 modules. `TXL.F R0, R0, handle(D0.x), CUBE;` `TXL.F R3, R0, handle(D0.x), 2D;` -- spelled `TXL.F`, `TXL.F.SPARSE` |
| `0x1a3` | `TXL` | 3 | short | explicit LOD, taken from coord `.w` | `0xb7` WITH A TEXEL OFFSET. Measured from `textureLodOffset`. | **Emitted.** 1 lines in 1 modules. `TXL.F R0, R0, handle(D0.x), 2D, offset({1, 0, 0, 0});` |
| `0x1a4` | `TXL` | -- | short | explicit LOD, taken from coord `.w` | Not observed; the same gap for `0xb6` (three-source LOD) + offset. | **Named only.** No line in the traced modules carries this number. |
| `0xb5` | `TXF` | 2 | short | texel fetch (int coord, LOD in `.w`) | The plain fetch. | **Emitted.** 48 lines in 8 modules. `TXF.F R0, R0, handle(D0.x), 2D;` `TXF.F R3, {1, 0, 0, 0}, handle(D1.x), BUFFER;` -- spelled `TXF.F`, `TXF.F.SPARSE` |
| `0x1a2` | `TXF` | -- | short | texel fetch (int coord, LOD in `.w`) | `0xb5` WITH A TEXEL OFFSET. Measured from `texelFetchOffset`. | **Emitted (opcov/cover only).** 1 lines in 1 modules. `TXF.F R0, R0, handle(D0.x), 2D, offset({1, 0, 0, 0});` |
| `0xb2` | `TXB` | -- | short | LOD bias in `.w` | The THREE-SOURCE bias form: the bias is its own operand because the coordinate already fills four lanes. Measured: `texture(samplerCubeArray, vec4, bias)` -> `TXB.F R0, a[0], a[0].w, handle(D0.x), ARRAYCUBE;` at `0xb2`. | **Emitted (opcov/cover only).** 1 lines in 1 modules. `TXB.F R0, fragment.attrib[0], fragment.attrib[0].w, handle(D0.x), ARRAYCUBE;` |
| `0xb3` | `TXB` | -- | short | LOD bias in `.w` | The ordinary two-source bias form (`texture(s, coord, bias)` on every target whose coordinate leaves a lane free, cube and array included). | **Emitted.** 1 lines in 1 modules. `TXB.F R0, R0, handle(D0.x), 2D;` |
| `0x1a0` | `TXB` | -- | short | LOD bias in `.w` | `0xb3` WITH A TEXEL OFFSET. Measured from `textureOffset(s, c, off, bias)`. | **Emitted (opcov/cover only).** 1 lines in 1 modules. `TXB.F R0, R0, handle(D0.x), 2D, offset({1, 0, 0, 0});` |
| `0x1a1` | `TXB` | -- | short | LOD bias in `.w` | Not observed, and STRUCTURALLY UNREACHABLE from GLSL: by the pattern the rest of the block follows it is `0xb2` (three-source bias) with a texel offset, and a three-source form only arises on a cube-array target, which takes no offset. Every other cell of that grid IS measured, which is why this reads as a gap rather than a guess. | **Named only.** No line in the traced modules carries this number. |
| `0x1ac` | `TXB` | -- | short | LOD bias in `.w` | `0xb3` WITH AN LOD CLAMP -- `TXB.F.LODCLAMP`. Measured from `textureClampARB(..., bias)`. | **Emitted (opcov/vocab only).** 2 lines in 1 modules. `TXB.F.LODCLAMP R2, R1, R0, handle(D0.x), 2D;` `TXB.F.LODCLAMP R1, fragment.attrib[0], R0, handle(D0.x), ARRAYCUBE;` |
| `0x1ae` | `TXB` | -- | short | LOD bias in `.w` | `0x1ac` WITH A TEXEL OFFSET. Measured from `textureOffsetClampARB(..., bias)`. | **Emitted (opcov/vocab only).** 1 lines in 1 modules. `TXB.F.LODCLAMP R2, R2, R0, handle(D0.x), 2D, offset({1, 0, 0, 0});` |
| `0xb4` | `TXD` | -- | short | explicit derivatives: `TXD dst, coord, ddx, ddy, handle, target` | Explicit-gradient sample. NOTE: the offset form is NOT a second number -- `textureGradOffset` also compiles to `0xb4`, unlike every other member of the family. | **Emitted.** 1 lines in 1 modules. `TXD.F R0, fragment.attrib[0], fragment.attrib[0], fragment.attrib[0].zwzw, handle(D0.x), 2D;` |
| `0x205` | `TXD` | -- | short | explicit derivatives: `TXD dst, coord, ddx, ddy, handle, target` | `0xb4` WITH AN LOD CLAMP -- `TXD.F.LODCLAMP`. Measured from `textureGradClampARB`. TXD keeps its habit of not changing number for an offset: `textureGradOffsetClampARB` is `0x205` too. | **Emitted (opcov/vocab only).** 3 lines in 1 modules. `TXD.F.LODCLAMP R1, fragment.attrib[0], fragment.attrib[0], R0, handle(D0.x), 2D;` `TXD.F.LODCLAMP R1, fragment.attrib[0], fragment.attrib[0], R1, handle(D0.x), 2D, (1);` |
| `0xb8` | `TXP` | -- | short | projective | The plain projective sample, shadow targets included (`TXP.F R9, R8, handle(D0.x), SHADOW2D;`). | **Emitted.** 14 lines in 3 modules. `TXP.F R9, R8, handle(D0.x), SHADOW2D;` `TXP.F R6, R8, handle(D0.x), 2D;` |
| `0x1a5` | `TXP` | -- | short | projective | `0xb8` WITH A TEXEL OFFSET. Measured from `textureProjOffset`. | **Emitted (opcov/cover only).** 1 lines in 1 modules. `TXP.F R3, R0, handle(D0.x), 2D, offset({1, 0, 0, 0});` |
| `0x19d` | `TXFMS` | -- | short | multisample fetch | -- | **Emitted.** 7 lines in 2 modules. `TXFMS.F R3, R3, handle(D0.x), 2DMS;` `TXFMS.F R4, R4, handle(D0.x), 2DMS;` |
| `0x19c` | `TXG` | -- | short | gather (`textureGather`): the component gathered is a SWIZZLE ON THE HANDLE, not an operand, and it is a REQUIRED operand of the SPIR-V op (notes/120 §1) | The plain gather. Gather has no number in the first texture block. | **Emitted.** 61 lines in 7 modules. `TXG.F R1, R2, handle(D0.x), 2D;` `TXG.F R1, R2, handle(D0.x).y, 2D;` -- spelled `TXG.F`, `TXG.F.SPARSE` |
| `0x1a6` | `TXG` | -- | short | gather (`textureGather`): the component gathered is a SWIZZLE ON THE HANDLE, not an operand, and it is a REQUIRED operand of the SPIR-V op (notes/120 §1) | `0x19c` WITH A TEXEL OFFSET. Measured from `textureGatherOffset`. | **Emitted.** 1 lines in 1 modules. `TXG.F R0, fragment.attrib[0], handle(D0.x), 2D, offset({1, 1, 0, 0});` |
| `0x1a9` | `TXG` | -- | short | gather (`textureGather`): the component gathered is a SWIZZLE ON THE HANDLE, not an operand, and it is a REQUIRED operand of the SPIR-V op (notes/120 §1) | The THREE-SOURCE gather, for the same reason. Measured: `textureGather(samplerCubeArrayShadow, vec4, refZ)` -> `TXG.F R2, fragment.attrib[0], fragment.attrib[0], handle(D0.x), SHADOWARRAYCUBE;`. | **Emitted (opcov/vocab only).** 1 lines in 1 modules. `TXG.F R2, fragment.attrib[0], fragment.attrib[0], handle(D0.x), SHADOWARRAYCUBE;` |
| `0x1aa` | `TXG` | -- | short | gather (`textureGather`): the component gathered is a SWIZZLE ON THE HANDLE, not an operand, and it is a REQUIRED operand of the SPIR-V op (notes/120 §1) | Not observed; the same gap for `0x1a9` (three-source gather) + offset. | **Named only.** No line in the traced modules carries this number. |
| `0x19e` | `TXGO` | -- | short | gather with offsets | -- | **Emitted (opcov/cover only).** 1 lines in 1 modules. `TXGO.F R0, fragment.attrib[0], {0, 1, 2, 3}, {0, 1, 2, 3}, handle(D0.x), 2D;` |
| `0xbb` | `TXQ` | 2 | short | texture size query (`textureSize`): `TXQ dst, lod, handle, target` | -- | **Emitted.** 10 lines in 4 modules. `TXQ   R1, {0, 0, 0, 0}, handle(D0.x), CUBE;` `TXQ   R0, {0, 0, 0, 0}, handle(D0.x), 2D;` |
| `0x207` | `TXQS` | -- | short | sample-count query | -- | **Emitted.** 4 lines in 2 modules. `TXQS  R5.x, handle(D0.x), 2DMS;` `TXQS  R1.x, handle(D1.x), 2DMS;` |
| `0x19b` | `LOD` | -- | short | query LOD | -- | **Emitted.** 4 lines in 2 modules. `LOD.F R5.xy, R3, handle(D0.x), 2D;` `LOD.F R7.xy, fragment.attrib[0], handle(D0.x), 2D;` |
| `0x1f4` | `IMQ` | -- | short | image size query (`imageSize`); needs `OPTION ARB_shader_image_size` | -- | **Emitted.** 3 lines in 1 modules. `IMQ   R6, handle(D0.x), 2D;` |
| `0x206` | `IMQS` | -- | short | image sample count | -- | **Emitted.** 1 lines in 1 modules. `IMQS  R0.x, handle(D0.x), 2DMS;` |
| `0xb1` | `TXA` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x214` | `TEX.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | The footprint query itself. `textureFootprintNV` emits `TEX.FOOTPRINT.FOOTPRINTPRED.U R0.xzw, coord, {0, <granularity>, 0, 0}, handle(D0.x), 2D;` -- targets `2D` and `3D`, and `coarse=true` adds `.COARSELEVEL`. Which of `0x214`/`0x218` it is was NOT traced. | **Emitted.** 1 nodes in 1 modules; nodedump could not pair the printed line. |
| `0x215` | `TXB.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | The bias form, from `textureFootprintNV(..., bias)`; 2D only. | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `TXB.FOOTPRINT.FOOTPRINTPRED.U R0.xzw, fragment.attrib[0], {0, 2.80259693e-45, 0, 0}, handle(D0.x), 2D;` -- opcov/vocab/RESULTS.md |
| `0x216` | `TXL.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | The explicit-LOD form, from `textureFootprintLodNV`; `2D` and `3D`. It is the ONE footprint name the namer does not duplicate, and GLSL has no `LodClamp` overload -- the two absences line up. | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `TXL.FOOTPRINT.FOOTPRINTPRED.U R0.xzw, fragment.attrib[0].xyzz, {0, 2.80259693e-45, 0, 0}, handle(D0.x), 3D;` -- opcov/vocab/RESULTS.md |
| `0x217` | `TXD.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | The explicit-gradient form, from `textureFootprintGradNV`; 2D only. | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `TXD.FOOTPRINT.FOOTPRINTPRED.U R0.xzw, R1.xyzz, fragment.attrib[0], fragment.attrib[0].zwzw, handle(D0.x), 2D;` -- opcov/vocab/RESULTS.md |
| `0x218` | `TEX.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | Almost certainly `0x214` with `.LODCLAMP` -- the `...ClampNV` overload, which passes the clamp in a register. Not traced: the GLSL path that reaches it has no node tracer. The namer duplicates exactly the three names that have a clamp overload (`TEX`, `TXB`, `TXD`) and not `TXL`, which has none. | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `TEX.FOOTPRINT.FOOTPRINTPRED.U.LODCLAMP R0.xzw, fragment.attrib[0], R2, handle(D0.x), 3D;` -- opcov/vocab/RESULTS.md |
| `0x219` | `TXB.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | Almost certainly `0x215` with `.LODCLAMP`; not traced. | **Named only.** No line in the traced modules carries this number. |
| `0x21a` | `TXD.FOOTPRINT.FOOTPRINTPRED` | -- | short | texture footprint query | Almost certainly `0x217` with `.LODCLAMP`; not traced. | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `TXD.FOOTPRINT.FOOTPRINTPRED.U.LODCLAMP R0.xzw, R1.xyzz, fragment.attrib[0], fragment.attrib[0].zwzw, handle(D0.x), 2D;` -- opcov/vocab/RESULTS.md |

- Targets, MEASURED rather than taken from the spec: one shader per target family in `opcov/vocab/` (`tgt_a` the plain float samplers, `tgt_b` the shadow ones, `tgt_c` the integer samplers and the images) prints sixteen target keywords -- `1D`, `2D`, `3D`, `CUBE`, `ARRAY1D`, `ARRAY2D`, `ARRAYCUBE`, `BUFFER`, `2DMS`, `ARRAY2DMS`, `SHADOW1D`, `SHADOW2D`, `SHADOWCUBE`, `SHADOWARRAY1D`, `SHADOWARRAY2D`, `SHADOWARRAYCUBE`.  The shadow spelling is a PREFIX and the array spelling sits between it and the dimension (`SHADOWARRAYCUBE`, not `ARRAYSHADOWCUBE` or `SHADOWCUBEARRAY`).  `LOADIM` takes the same keywords as `TEX` -- `LOADIM.F32X4 ... ARRAYCUBE;` -- so the target set is the sampler's, not the instruction's.  Listings in `opcov/vocab/tgt_*.frag.glasm`.

- Result suffix: `.F` for a float result, `.S`/`.U` for integer textures (`TXF.U R3, R3, handle(D0.x), 2D;`).

- The coordinate is built in a register when components must be placed, for example the LOD into `.w`.

- A constant texel offset is a TRAILING OPERAND, after the target: `TEX.F R0, fragment.attrib[0], handle(D0.x), 2D, offset({0, 1, 0, 0});` (`0088_io_b.frag`) and `TXL.F R0, R0, handle(D0.x), 2D, offset({1, 0, 0, 0});` (`0088_io_a.frag`). It is printed for an explicit `ConstOffset`, including the all-zero one the corpus has (`offset({0, 0, 0, 0})`).

- The FOOTPRINT family is real, fully shaped, and the compiler CANNOT ASSEMBLE ANY OF IT.  `opcov/vocab/v_footprint_all.frag` calls every overload the extension has on both targets and gets nine distinct instructions out: the modifier chain is `.FOOTPRINT.FOOTPRINTPRED[.U][.COARSELEVEL][.LODCLAMP]`, the targets are `2D` and `3D` ONLY (no cube, array or buffer overload exists), `TXB` and `TXD` are 2D-only while `TEX` and `TXL` take both, and the second operand carries the granularity in its `.y` lane as an integer printed through the float formatter -- granularity 2 is `{0, 2.80259693e-45, 0, 0}`, 4 is `5.60519386e-45`, 11 is `1.54142831e-44`, which are the bit patterns of 2, 4 and 11.  Every one of the nine is then rejected by the compiler's own assembler with `unknown opcode modifier`, and each `(SINGLELOD)` guard below it with `invalid condition code mask rule`.  Which NUMBER printed which line is not measured -- that path has no node tracer (opcov/vocab/RESULTS.md).

---

## 9. Derivatives and fragment

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x68` | `DDX` | 1 | full for float | screen-space derivative | -- | **Emitted.** 102 lines in 16 modules. `DDX.F32 R2.x, R0;` `DDX.F32 R1.x, R5;` -- spelled `DDX.COARSE.F32`, `DDX.F32`, `DDX.FINE.F32` |
| `0x69` | `DDY` | 1 | full for float | screen-space derivative | -- | **Emitted.** 101 lines in 15 modules. `DDY.F32 R0.x, R0;` `DDY.F32 R1.x, R5;` -- spelled `DDY.COARSE.F32`, `DDY.F32`, `DDY.FINE.F32` |
| `0xca` | `KIL` | 1 | short | discard if the condition holds | -- | **Emitted.** 10 lines in 6 modules. `KIL   NE.x;` |
| `0xdd` | `DEMOTE` | -- | short | demote to helper invocation | -- | **Emitted.** 1 lines in 1 modules. `DEMOTE NE.x;` |
| `0x1b5` | `IPAC` | -- | full for float | interpolate at centroid / offset / sample | -- | **Emitted.** 1 lines in 1 modules. `IPAC.F32 R0.x, fragment.attrib[0];` |
| `0x1b6` | `IPAO` | -- | full for float | interpolate at centroid / offset / sample | -- | **Emitted.** 1 lines in 1 modules. `IPAO.F32 R3.x, fragment.attrib[0].z, {0.5, 0.5, 0, 0};` |
| `0x1b7` | `IPAS` | -- | full for float | interpolate at centroid / offset / sample | -- | **Emitted.** 1 lines in 1 modules. `IPAS.F32 R1.x, fragment.attrib[0].y, {0, 0, 0, 0}.x;` |
| `0x1c2` | `FSIB` | -- | short | fragment shader interlock begin / end | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `FSIB;` -- opcov/vocab/RESULTS.md |
| `0x1c3` | `FSIE` | -- | short | fragment shader interlock begin / end | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `FSIE;` -- opcov/vocab/RESULTS.md |

---

## 10. Control flow

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x1a` | `IF` | 2 | short | conditional block on CC | -- | **Emitted.** 2696 lines in 154 modules. `IF    NE.x;` |
| `0x1b` | `ELSE` | 1 | short | conditional block on CC | -- | **Emitted.** 1147 lines in 140 modules. `ELSE;` |
| `0x1c` | `ENDIF` | 1 | short | conditional block on CC | -- | **Emitted.** 2696 lines in 154 modules. `ENDIF;` |
| `0xf` | `REP` | 3 | short | loop (`REP.S ;` with no count means infinite) | -- | **Emitted.** 333 lines in 54 modules. `REP.S ;` |
| `0x13` | `ENDREP` | 1 | short | loop (`REP.S ;` with no count means infinite) | -- | **Emitted.** 333 lines in 54 modules. `ENDREP;` |
| `0x14` | `BRK` | 1 | short | conditional break out of `REP` | -- | **Emitted.** 1764 lines in 54 modules. `BRK   (NE.x);` |
| `0x15` | `BREAK` | -- | short | break | -- | **Named only.** No line in the traced modules carries this number. |
| `0x16` | `CONT` | -- | short | continue | -- | **Named only.** No line in the traced modules carries this number. |
| `0x10` | `LOOP` | -- | short | counted loop (`LOOP`/`ENDLOOP`) | -- | **Named only.** No line in the traced modules carries this number. |
| `0xa` | `BRA` | -- | short | branch to label | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0xc` | `BRA` | -- | short | branch to label | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0xd` | `BRA` | -- | short | branch to label | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x1d` | `CAL` | 1 | short | call subroutine | -- | **Emitted.** 71 lines in 8 modules. `CAL   BB7 (TR);` `CAL   BB8 (TR);` |
| `0x1e` | `CALI` | -- | short | indirect call / call through a subroutine table | The INDIRECT call. GLSL subroutines are the only construct that reaches it, and they exist only in the GLSL front end -- SPIR-V has no subroutine, so the oracle can never emit this. `opcov/vocab/v_subroutine.frag`. | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `CALI program_subroutine_0;` -- opcov/vocab/RESULTS.md |
| `0x1f` | `PCALL` | -- | short | indirect call / call through a subroutine table | -- | **Named only.** No line in the traced modules carries this number. |
| `0x18` | `RET` | 0 | short | return | The one `RET` the compiler emits (`RET   (TR);`, 934 lines). | **Emitted.** 934 lines in 852 modules. `RET   (TR);` |
| `0x19` | `RET` | -- | short | return | Not observed. | **Named only.** No line in the traced modules carries this number. |
| `0x20` | `FUNC` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| -- | `BB<n>:` | -- | own printer | label; `<n>` is the subroutine's first block number | -- | **Seen.** |

Structure:

- `if`/`else` becomes compare, normalise, `MOV.U.CC`, `IF NE.x`.

- `while`/`for` becomes `REP`, the head test, then `IF cond; body; ELSE; MOV.U.CC RC.x,{1,0,0,0}; BRK (NE.x); ENDIF;`, then `ENDREP`.

- `switch` becomes an IF chain. Each case test is `SEQ.S R0.x, sel, {k,...}; MOV.S.CC HC.x, -R0; IF NE.x;`.

- An INDIRECT call is a whole sub-language, and only the GLSL front end reaches it (SPIR-V has no subroutine).  A `subroutine uniform` declares a type and a slot, and each implementation becomes a labelled block: `SUBROUTINETYPE I0 { BB3, BB5 };` / `SUBROUTINE I0 program_subroutine_0 = program.subroutine[0];` / `CALI program_subroutine_0;`, with the bodies opening as `BB3 SUBROUTINENUM(0):` and each ending `RET (TR);` (`opcov/vocab/v_subroutine.frag`).

---

## 11. Geometry and synchronisation

| opcode | mnemonic | operands | suffix | does | why this number | evidence |
|---|---|---|---|---|---|---|
| `0x22` | `EMIT` | 0 | short | emit vertex / emit to stream | -- | **Emitted.** 16 lines in 12 modules. `EMIT;` |
| `0x1b8` | `EMITS` | -- | short | emit vertex / emit to stream | -- | **Emitted.** 5 lines in 2 modules. `EMITS.S {0, 0, 0, 0}.x;` |
| `0x23` | `ENDPRIM` | 0 | short | end primitive | -- | **Emitted.** 21 lines in 13 modules. `ENDPRIM;` |
| `0x3d` | `BAR` | 0 | short | workgroup barrier | -- | **Emitted.** 6 lines in 3 modules. `BAR ;` |
| -- | `GROUP_SIZE` | -- | own printer | compute workgroup size (a directive) | -- | **Seen.** `GROUP_SIZE 8 8;` `GROUP_SIZE 64 2 3;` (trailing 1s are dropped) |
| -- | `SHARED_MEMORY` | -- | own printer | shared-memory size in bytes (a directive) | -- | **Seen.** `SHARED_MEMORY 512;` (`0095_sm_a.comp`) `SHARED_MEMORY 256;` (`0119_bar_a.comp`) |
| `0x1bd` | `TGALL` | -- | short | thread-group vote / ballot | -- | **Emitted.** 9 lines in 6 modules. `TGALL.U R9.x, R7;` `TGALL.U R102.x, R101;` |
| `0x1be` | `TGANY` | -- | short | thread-group vote / ballot | -- | **Emitted.** 6 lines in 5 modules. `TGANY.U R10.x, R8;` `TGANY.U R102.x, R101;` |
| `0x1bf` | `TGEQ` | -- | short | thread-group vote / ballot | -- | **Emitted.** 1 lines in 1 modules. `TGEQ.U R1.x, R0;` |
| `0x1f9` | `TGBALLOT` | -- | short | thread-group vote / ballot | -- | **Emitted.** 115 lines in 8 modules. `TGBALLOT.U R11.x, R7;` `TGBALLOT.U R105.x, R104;` |
| `0x1fa` | `SHFUP` | -- | short | warp shuffle | -- | **Emitted.** 93 lines in 3 modules. `SHFUP.U R103.xy, R0, {1, 0, 0, 0}, {0, 0, 0, 0};` `SHFUP.U R103.xy, R100, {1, 0, 0, 0}, {0, 0, 0, 0};` -- spelled `SHFUP.F`, `SHFUP.U` |
| `0x1fb` | `SHFDOWN` | -- | short | warp shuffle | -- | **Emitted.** 3 lines in 2 modules. `SHFDOWN.U R104.xy, R0, {1, 0, 0, 0}, {31, 0, 0, 0};` `SHFDOWN.U R56.xy, R0, {1, 0, 0, 0}, {31, 0, 0, 0};` |
| `0x1fc` | `SHFXOR` | -- | short | warp shuffle | -- | **Emitted.** 246 lines in 3 modules. `SHFXOR.U R104.xy, R0, {1, 0, 0, 0}, {31, 0, 0, 0};` `SHFXOR.U R106.xy, R100, {1, 0, 0, 0}, {31, 0, 0, 0};` -- spelled `SHFXOR.F`, `SHFXOR.U` |
| `0x1fd` | `SHFIDX` | -- | short | warp shuffle | -- | **Emitted.** 163 lines in 7 modules. `SHFIDX.F R29.xy, R26, {0, 0, 0, 0}, {31, 0, 0, 0};` `SHFIDX.U R108.xy, R0, R103, {31, 0, 0, 0};` -- spelled `SHFIDX.F`, `SHFIDX.U` |
| `0x211` | `MATCH.ALL` | -- | short | warp match | -- | **Named only.** No line in the traced modules carries this number. |
| `0x212` | `MATCH.ANY` | -- | short | warp match | -- | **Named only.** No line in the traced modules carries this number. |
| `0x1fe` | `QSWZ0` | -- | short | quad swizzle | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `QSWZ0.F R0.x, fragment.attrib[0].x, {0, 0, 0, 0}.x;` -- opcov/vocab/RESULTS.md |
| `0x1ff` | `QSWZ1` | -- | short | quad swizzle | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `QSWZ1.F R0.y, fragment.attrib[0].y, {0, 0, 0, 0}.x;` -- opcov/vocab/RESULTS.md |
| `0x200` | `QSWZ2` | -- | short | quad swizzle | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `QSWZ2.F R0.z, fragment.attrib[0].z, {0, 0, 0, 0}.x;` -- opcov/vocab/RESULTS.md |
| `0x201` | `QSWZ3` | -- | short | quad swizzle | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `QSWZ3.F R0.y, fragment.attrib[0].w, {0, 0, 0, 0}.x;` -- opcov/vocab/RESULTS.md |
| `0x202` | `QSWZX` | -- | short | quad swizzle | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `QSWZX.F R0.z, fragment.attrib[0].x, {0, 0, 0, 0}.x;` -- opcov/vocab/RESULTS.md |
| `0x203` | `QSWZY` | -- | short | quad swizzle | -- | **Printed, but only through the GLSL front end** (not traced, so the number is inferred from the mnemonic): `QSWZY.F R0.y, fragment.attrib[0].y, {0, 0, 0, 0}.x;` -- opcov/vocab/RESULTS.md |
| `0x4f` | `ARA` | -- | short | address-register add | -- | **Named only.** No line in the traced modules carries this number. |
| `0x21` | `PROT` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |
| `0x7a` | `RCC` | -- | short | name only | -- | **Named only.** No line in the traced modules carries this number. |

- A PASSTHROUGH geometry shader (GL_NV_geometry_shader_passthrough) has no body for the varyings it forwards: it emits `OPTION NV_geometry_shader_passthrough;` and one `PASSTHROUGH result.attrib[0];` / `PASSTHROUGH result.position;` per forwarded varying (`opcov/vocab/v_passthrough.geom`).

---

## 12. Store shapes at `--opt-level none`

- **Vector store to `gl_Position`**: scalarised. The `.x` component is written straight from the value; `.y`, `.z` and `.w` each go through a scratch `.x` lane:

  ```
  MOV.F result.position.x, R0;
  MOV.F R0.x, R0.y;
  MOV.F result.position.y, R0.x;
  ```

- **Colour output**: one vector `MOV`. If the source is a register it is followed by a self-move:

  ```
  MOV.F result_color0, R0;
  MOV.F R0, R0;
  ```

- **Vertex location output**: `MOV.F result.attrib[0].xy, R0; MOV.F R0.xy, R0;`
- **Constant into a component**: `MOV.F result_color0.y, {1, 0, 0, 0}.x;`
- **Shader-level directives**, not instructions: `PRIMITIVE_IN`, `PRIMITIVE_OUT`, `VERTICES_OUT`, `INVOCATIONS`, `TESS_MODE`, `TESS_SPACING`, `TESS_VERTEX_ORDER`, `TESS_POINT_MODE`, `GROUP_SIZE` (compute).
