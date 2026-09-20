# spirv2glasm
Work in progress Python GLASM generator targeting SM 5.3 Maxwell SASS.

Main target is to replicate logic behind official glslc package 113 used in Nintendo Switch games.<br>
It will be removed when logic will be good enough to merge it into bigger project.

It's worked out via Claude by analyzing how glslc shipped with Tomb Raider Definitive Edition 1.0.3 works in qemu and via static recompilation by serving it "probes" generated based on shaders shipped with Nintendo Switch games and following functions deciding about multiple factors. We are working only on -O0 level as we target fast conversion, not fully optimized one.
