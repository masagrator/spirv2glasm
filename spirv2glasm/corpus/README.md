# The corpus

`mkcorpus.py` turns the 14,706-shader GLSL corpus into SPIR-V.  Its header
comment states every edit it makes and what forces each; the summary is that
glslang rejects all 14,706 out of the box and this gets **14,630** through
(glslang 15.1.0).

```sh
python3 corpus/mkcorpus.py /path/to/glsl /path/to/spv --jobs 4
```

```
14706 shaders, 14630 compiled to SPIR-V, 76 rejected
     72  cannot convert a sampler
      3  extension not supported: GL_NV_bindless_texture
      1  must be a multiple of the member's alignment (layout offset
```

**The samplers.**  Most of the corpus constructs a sampler from a bindless
handle -- `texelFetch(sampler2D(uint64_t(tonemap_param_g)), ...)` -- which
glslang refuses to emit SPIR-V for at all:

    'GL_ARB_bindless_texture' : not allowed when using generating SPIR-V codes

`mkcorpus.py` rewrites the constructor to `sampler2D(Tex, Smpl)`, pairing the
texture with the sampler declared at its binding (its header states what that
costs).  The 72 left declare no sampler to pair with.  An older version of the
script, without that edit and without the fp16 one, got 10,328 through; the
verification in `RESULTS.md` was run on those.

Then verify:

```sh
ls /path/to/spv/*.spv > list.txt
BIN=<port>/build tools/parverify.sh list.txt /tmp/cv 4 --opt-level none --debug-info g2
```

Note the flags: **fat** binaries and `--debug-info g2`, so the compiler stores
its own listing in both forms and the capture can be checked against them.  See
`tools/verify.sh`'s header for why `--output-thin-gpu-binaries --debug-info none`
is the wrong choice for a verification run.
