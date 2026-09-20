"""glasmlib -- the converter's output side, one module per concern.

`py/glasm.py` is the public face (`header`, `body`, `declarations`,
`semantic_lines`, `var_lines`, ...); the work is done here:

    common      NotEstablished, the SPIR-V opcode families used everywhere
    header      the profile line, OPTIONs, stage directives, comment header
    types       type codes, component counts, `#var` type spellings
    semantics   built-in semantics, binding kinds and slots
    usage       which ids / members / elements a body touches
    refliveness notes/18's reaching-definition walk (kept for reference)
    blocks      uniform/storage blocks and opaque uniforms
    varblock    the `#semantic` and `#var` blocks
    declare     STORAGE/CBUFFER, ATTRIB/OUTPUT, colour outputs
    operands    constants and interface operands as GLASM text
    chains      output, position, patch and buffer access chains
    text        swizzles, operand text, `_emit`
    alloc       the component allocator (the fallback to py/ifg.py)
    body        the lowered body with its register count and trailer
    boolean     bool normalisation and condition moves
    cflow       structured control flow flattened into markers
    nodes       which GLASM opcode and type code each role uses
    lower/      the body's lowering, one module per instruction family,
                and its scheduling, allocation and rendering (finish.py)

Every SPIR-V number is spelled with its Khronos name (`spvnames`), and
`tools/opnumlint.py` checks that none is written as a literal.  The
compiler's own opcodes and type codes are spelled the same way through
`glasmnames` (the table `py/glslc/glasm.py`, notes/93).
"""
