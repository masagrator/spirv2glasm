# spirv2glasm
Work in progress Python GLASM generator targeting SM 5.3 Maxwell SASS.

Main target is to replicate logic behind official glslc package 113 used in Nintendo Switch games.<br>
It will be removed when logic will be good enough to merge it into bigger project.

It's worked out via Claude by analyzing how glslc shipped with Tomb Raider Definitive Edition 1.0.3 works in qemu and via static recompilation by serving it "probes" generated based on shaders shipped with Nintendo Switch games and following functions deciding about multiple factors. We are working only on -O0 level as we target fast conversion that can be utilized for generating shaders in real time, not fully optimized one which can take few seconds to finish.

Repository doesn't store original glslc, it stores only GLASM output of shader probes served to original glslc. With updates to glslang glasm output may change, workflow is always using the 16.6.0 glslang.
