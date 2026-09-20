# GLASM mnemonic reference (GLSLC 17.24, NV_gpu_program5 family)

Scope: what GLSLC 17.24 prints. **Seen** means the form appears in the saved listings (`--opt-level none`, 120 corpus shaders and 137 probes), and the examples are copied from them verbatim. **Vocab** means the name exists in the compiler's opcode namer (`notes/glasm_opcodes.json`) but no saved listing uses it. For those, the meaning comes from the NV_gpu_program4/5 specs and is not verified against this compiler.

---

## 1. Line syntax

```
MNEMONIC[.TYPE][.CC[n]] dst[.mask], src0[, src1[, src2]];
```

- The mnemonic is left-justified to 5 columns (`%-5s`): `OR.S  R0, ...`, `IF    NE.x;`, `RET   (TR);`. Longer mnemonics run on with a single space.
- Every statement ends with `;`. A label line is `BB<n>:`.
- The listing ends with `END` and then `# <n> instructions, <m> R-regs`.

### Type suffix (depends on the value type)

| type | arithmetic / compare / transcendental | MOV, MIN, MAX, FLR, CEIL, ROUND, TRUNC |
|---|---|---|
| float | `.F32` | `.F` |
| int | `.S` | `.S` |
| uint | `.U` | `.U` |
| bool | stored as uint: `.U` | `.U` |
| int64 / uint64 | `.S64` / `.U64` | `.S64` / `.U64` |
| half | `.F16` | `.F` |

- `MOV` takes its suffix from the **source** type.
- `TRUNC`/`ROUND`/`FLR`/`CEIL` take theirs from the **destination** type: `TRUNC.U R0.x, R0;` converts float to uint.
- `I2F` takes its suffix from the **integer source**: `I2F.S`, `I2F.U`.
- Loads use a width suffix (§7).

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
| outputs | `result.position[.c]`, `result.attrib[n]`, `result_color<n>` (fragment), `result.patch.tessouter[k]` |
| buffers | `buf<n>[byteoff]`, `buf<n>[R0.x + off]`, `sbo_buf<n>[...]` |
| texture handle | `handle(D0.x)` |

### Condition codes

- `.CC` sets the condition register: `MOV.U.CC RC.x, R0;` and `MOV.U.CC1 RC.xy, R10;` (second CC set).
- When the compare itself sets CC, it writes `HC`: `SEQ.U.CC HC.x, a, b;`, `MOV.S.CC HC.x, -R0;`.
- Tests are `NE.x`, `EQ.x` and so on: `IF NE.x;`, `BRK (NE.x);`, `KIL NE.x;`.
- `(TR)` means always true: `RET (TR);`, `CAL BB7 (TR);`.

---

## 2. Data movement and conversion

| mnemonic | does | forms |
|---|---|---|
| `MOV` | copy | Seen. `MOV.F R0, -vertex.attrib[0];` `MOV.S R0.x, {0, 0, 0, 0};` `MOV.U R26.xy, R11;` `MOV.F result.position.x, R0;` |
| `MOV.CC` | copy and set CC | Seen. `MOV.U.CC RC.x, R1;` `MOV.S.CC HC.x, -R0;` |
| `I2F` | int to float | Seen. `I2F.S R11.x, R13;` `I2F.U R16.xyz, R0.zwyw;` |
| `TRUNC` | round toward 0; converts when the destination is int | Seen. `TRUNC.F R0, a;` `TRUNC.U R0.x, R0;` `TRUNC.S R18.xy, R30;` `TRUNC.U.CC HC.x, H0;` |
| `ROUND` | round to nearest | Seen. `ROUND.F R0, vertex.attrib[0];` |
| `FLR` | floor | Seen. `FLR.F R0.x, R3;` |
| `CEIL` | ceiling | Seen. `CEIL.F R0, vertex.attrib[0];` |
| `CVT` | type conversion | Vocab |
| `PK2H PK2US PK4B PK4UB PK64` | pack floats into half / ushort / byte / ubyte, or two 32-bit values into one 64-bit | Vocab |
| `UP2H UP2US UP4B UP4UB UP64` | unpack (the inverses) | Vocab |

---

## 3. Arithmetic

| mnemonic | does | forms |
|---|---|---|
| `ADD` | a + b (subtraction is `ADD a, -b`) | Seen. `ADD.F32 R0, a, b;` `ADD.S R0.x, R3, R10;` `ADD.U R4.x, R3, {1, 0, 0, 0};` |
| `SUB` | a − b | Vocab; always rewritten to `ADD` with `-b` |
| `MUL` | a × b | Seen. `MUL.F32 R1.xyz, R0.x, R36;` `MUL.S R14.x, R0, {128, 0, 0, 0};` `MUL.U` |
| `MUL.HI` | high 32 bits of an integer product | Vocab |
| `MAD` | a × b + c | Vocab |
| `DIV` | a / b | Seen. `DIV.F32 R6.x, {1, 0, 0, 0}, R8.x;` `DIV.S R0.w, a, b.w;` (the integer form is per component) |
| `MOD` | integer a mod b | Vocab |
| `MIN` / `MAX` | component-wise min / max | Seen. `MIN.F R3.x, R2, {1, 0, 0, 0};` `MAX.F R1.xy, R36, {0, 0, 0, 0};` `MIN.U` |
| `SAD` | \|a − b\| + c | Vocab |
| `LRP` | a·b + (1 − a)·c | Vocab |
| `DP2` / `DP3` / `DP4` | dot product of 2, 3 or 4 components, scalar result | Seen. `DP4.F32 R0.x, R35, R37;` |
| `DP2A` | dp2(a, b) + c.x | Vocab |
| `DPH` | dp3(a, b) + b.w | Vocab |
| `DST` `LIT` | distance vector / lighting coefficients (legacy ARB) | Vocab |
| `NRM` | normalize the xyz components | Vocab (the compiler lowers `normalize` to DP / RSQ / MUL) |
| `RFL` | reflect | Vocab |
| `X2D` | 2D coordinate transform | Vocab |
| `SSG` | sign: −1, 0 or 1 | Vocab (`sign()` is inlined as SLT/SGT/TRUNC/ADD) |
| `FRC` | fractional part | Seen. `FRC.F32 R13.xy, R9;` |

---

## 4. Transcendental (scalar, one component per instruction)

| mnemonic | does | forms |
|---|---|---|
| `RCP` | 1 / x | Seen. `RCP.F32 R2.x, R3.x;` |
| `RSQ` | 1 / √x | Seen. `RSQ.F32 R2.x, R1.x;` |
| `EX2` / `LG2` | 2ˣ / log₂ x | Seen. `EX2.F32 R6.x, R7.x;` |
| `EXP` / `LOG` | partial-precision exponent / log (legacy) | Vocab |
| `POW` | aᵇ | Seen. `POW.F32 R0.x, vertex.attrib[0].x, vertex.attrib[1].x;` |
| `SIN` / `COS` | sine / cosine | Seen. `SIN.F32 R6.x, R0.x;` |
| `TANH` | hyperbolic tangent | Vocab |
| `DIVSQ` | name only | Vocab; semantics unknown |

A vector transcendental is scalarised: one instruction per component, then the components are gathered with `MOV`. `sqrt(x)` becomes `RSQ` followed by `DIV.F32 {1,0,0,0}, rsq`.

---

## 5. Compare (set on condition)

| mnemonic | test | forms |
|---|---|---|
| `SEQ` `SNE` `SLT` `SLE` `SGT` `SGE` | ==, !=, <, <=, >, >= | Seen. `SLT.F32 R0.x, a, {0, 0, 0, 0};` `SLT.S R1.x, R0, {4, 0, 0, 0};` `SNE.U` `SEQ.U.CC HC.x, ...` |

A bool is normalised right after the compare:

- float compare: `TRUNC.U R.x, R;`
- int or uint compare: `MOV.S R.x, -R;` (or `MOV.U`)

The branch then tests it: `MOV.U.CC RC.x, R; IF NE.x;`.

---

## 6. Bit operations

| mnemonic | does | forms |
|---|---|---|
| `AND` `OR` `XOR` | bitwise | Seen. `AND.U R1.x, R0.y, {15, 0, 0, 0};` `OR.S  D0.x, D0, D1;` `XOR.S` |
| `NOT` | bitwise not | Vocab |
| `SHL` / `SHR` | shift left / right (`.S` is arithmetic, `.U` is logical) | Seen. `SHL.S R10.x, R2, {5, 0, 0, 0}.x;` `SHR.U R1.x, R6, {1, 0, 0, 0}.x;` Vector shifts are per component, highest first. |
| `BFE` | bitfield extract: `BFE dst, {width, offset}, src` | Vocab |
| `BFI` | bitfield insert: `BFI dst, {width, offset}, insert, base` | Seen. `BFI.S R0.x, {4, 0, 0, 0}, R1, {4080, 0, 0, 0};` |
| `BFR` | bit reverse | Vocab |
| `BTC` | population count | Vocab |
| `BTFL` / `BTFM` | find lowest / most significant set bit | Vocab |

---

## 7. Memory

| mnemonic | does | forms |
|---|---|---|
| `LDC` | constant (uniform) buffer load; the suffix is the loaded width | Seen. `LDC.F32 R0.x, buf0[680];` `LDC.F32X2 R0.xy, buf5[24];` `LDC.F32X4 R0, buf0[0];` `LDC.S32 R0.x, buf1[0];` `LDC.U32X4 R0.yzw, buf4[0];` `LDC.U64 D0.x, buf14[328];` `LDC.F32X4 R26, buf0[R24.x + 480];` |
| `LDB` | storage buffer load | Seen. `LDB.U32 R14.x, sbo_buf15[R14.x + 8];` `LDB.F32X4 R0, sbo_buf0[0];` |
| `STB` | storage buffer store | Seen. `STB.U32 R2, sbo_buf0[0];` |
| `LOADIM` / `STOREIM` | image load / store | Vocab |
| `ATOM.<op>` | atomic on global memory; `<op>` is `ADD AND OR XOR MIN MAX EXCH CSWAP IWRAP DWRAP` | Vocab |
| `ATOMB.<op>` / `ATOMS.<op>` / `ATOMIM.<op>` | atomic on a storage buffer / shared memory / image | Vocab |
| `ATOMCTR.GET` `ATOMCTROP.CSWAP` | atomic counter | Vocab |
| `MEMBAR` | memory barrier | Vocab |
| `LDBB STBB LDMM STMM LDTM STTM LDCB ATTRRD ATTRWR` | name only | Vocab; semantics not established |

Addressing:

- Offsets are in bytes.
- A dynamic index is scaled first: `MUL.S R.x, idx, {stride,0,0,0}; MOV.S R.x, R;`.
- Bindless sampler handles are 64-bit and live in `buf14`: combined samplers at `8·binding`, textures at `328 + 8·binding`, samplers at `1352 + 8·binding`.

---

## 8. Texture

| mnemonic | does | forms |
|---|---|---|
| `TEX` | sample with implicit LOD | Seen. `TEX.F R0, fragment.attrib[0], handle(D0.x), 2D;` |
| `TXL` | explicit LOD, taken from coord `.w` | Seen. `TXL.F R9, R3, handle(D0.x), 2D;` |
| `TXF` | texel fetch (int coord, LOD in `.w`) | Seen. `TXF.F R0, R0, handle(D0.x), 2D;` |
| `TXB` | LOD bias in `.w` | Vocab |
| `TXD` | explicit derivatives: `TXD dst, coord, ddx, ddy, handle, target` | Vocab |
| `TXP` | projective | Vocab |
| `TXFMS` | multisample fetch | Vocab |
| `TXGO` | gather with offsets | Vocab |
| `TXQ` / `TXQS` | size / sample-count query | Vocab |
| `LOD` | query LOD | Vocab |
| `IMQ` / `IMQS` | image size / samples | Vocab |
| `TXA` | name only | Vocab |
| `TEX/TXB/TXL/TXD.FOOTPRINT.FOOTPRINTPRED` | texture footprint query | Vocab |

- Targets seen: `2D`, `3D`, `CUBE`, `SHADOWARRAY2D`. By spec, `1D`, `ARRAY2D`, `SHADOW2D` and `ARRAYCUBE` also exist.
- Result suffix: `.F` for a float result, `.S`/`.U` for integer textures.
- The coordinate is built in a register when components must be placed, for example the LOD into `.w`.

---

## 9. Derivatives and fragment

| mnemonic | does | forms |
|---|---|---|
| `DDX` / `DDY` | screen-space derivative | Seen. `DDX.F32 R6.x, R15;` `.COARSE`/`.FINE` variants also enable `OPTION ARB_derivative_control` |
| `KIL` | discard if the condition holds | Seen. `MOV.U.CC RC.x, {1,0,0,0}; KIL NE.x;` |
| `DEMOTE` | demote to helper invocation | Vocab |
| `IPAC` / `IPAO` / `IPAS` | interpolate at centroid / offset / sample | Vocab |
| `FSIB` / `FSIE` | fragment shader interlock begin / end | Vocab |

---

## 10. Control flow

| mnemonic | does | forms |
|---|---|---|
| `IF` / `ELSE` / `ENDIF` | conditional block on CC | Seen. `IF    NE.x;` `ELSE;` `ENDIF;` |
| `REP` / `ENDREP` | loop (`REP.S ;` with no count means infinite) | Seen. Loop head: `SEQ.U.CC HC.x, {1,0,0,0}, {0,0,0,0}; BRK   (NE.x);` |
| `BRK` | conditional break out of `REP` | Seen. `BRK   (NE.x);` |
| `BREAK` | break | Vocab |
| `CONT` | continue | Vocab |
| `LOOP` | counted loop (`LOOP`/`ENDLOOP`) | Vocab |
| `BRA` | branch to label | Vocab |
| `CAL` | call subroutine | Seen. `CAL   BB7 (TR);` Arguments are copied through registers beforehand. |
| `CALI` / `PCALL` | indirect call / call through a subroutine table | Vocab |
| `RET` | return | Seen. `RET   (TR);` (the end of `main`, and each subroutine) |
| `FUNC` | name only | Vocab |
| `BB<n>:` | label; `<n>` is the subroutine's first block number | Seen |

Structure:

- `if`/`else` becomes compare, normalise, `MOV.U.CC`, `IF NE.x`.
- `while`/`for` becomes `REP`, the head test, then `IF cond; body; ELSE; MOV.U.CC RC.x,{1,0,0,0}; BRK (NE.x); ENDIF;`, then `ENDREP`.
- `switch` becomes an IF chain. Each case test is `SEQ.S R0.x, sel, {k,...}; MOV.S.CC HC.x, -R0; IF NE.x;`.

---

## 11. Geometry and synchronisation

| mnemonic | does | forms |
|---|---|---|
| `EMIT` / `EMITS` | emit vertex / emit to stream | Seen. `MOV.F result.position, R; EMIT;` |
| `ENDPRIM` | end primitive | Seen |
| `BAR` | workgroup barrier | Vocab |
| `TGALL TGANY TGEQ TGBALLOT` | thread-group vote / ballot | Vocab |
| `SHFIDX SHFUP SHFDOWN SHFXOR` | warp shuffle | Vocab |
| `MATCH.ANY` / `MATCH.ALL` | warp match | Vocab |
| `QSWZ0..3 QSWZX QSWZY` | quad swizzle | Vocab |
| `ARA` | address-register add | Vocab |
| `RCC` `PROT` | name only | Vocab |

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
