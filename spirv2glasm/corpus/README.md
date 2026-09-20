# The corpus

`mkcorpus.py` turns the 14,706-shader GLSL corpus into SPIR-V.  Its header
comment states every edit it makes and what forces each; the summary is that
glslang rejects all 14,706 out of the box and this gets **10,328** through.

```sh
python3 corpus/mkcorpus.py /path/to/glsl /path/to/spv --jobs 4
```

```
14706 shaders, 10328 compiled to SPIR-V, 4378 rejected
   4358  cannot convert a sampler
     16  cannot convert from ' temp float' to ' temp float16_t'
      3  extension not supported: GL_NV_bindless_texture
      1  must be a multiple of the member's alignment (layout offset
```

**The 4,358 are one cause.**  They construct a sampler from a bindless handle —
`texelFetch(sampler2D(uint64_t(tonemap_param_g)), ...)` — and glslang refuses to
emit SPIR-V for that at all:

    'GL_ARB_bindless_texture' : not allowed when using generating SPIR-V codes

That is a limit of glslang, not of `spirv2glasm`, which compiles bindless SPIR-V
perfectly well when something else produces it; the listings show it as
`LDC.U64 D0.x, buf14[0]` and `TEX.F R0, ..., handle(D0.x), 2D`.  Anything that
can emit SPIR-V for `ARB_bindless_texture` would lift the corpus to ~14,700.

Then verify:

```sh
ls /path/to/spv/*.spv > list.txt
BIN=<port>/build tools/parverify.sh list.txt /tmp/cv 4 --opt-level none --debug-info g2
```

Note the flags: **fat** binaries and `--debug-info g2`, so the compiler stores
its own listing in both forms and the capture can be checked against them.  See
`tools/verify.sh`'s header for why `--output-thin-gpu-binaries --debug-info none`
is the wrong choice for a verification run.
