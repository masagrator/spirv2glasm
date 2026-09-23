#!/bin/sh
# mkpackage.sh <out.7z> -- the shipped package, from this working tree.
#
# The package root holds `.github/` (the workflow, from the repository root
# one level up) and `spirv2glasm/`.  To keep it under 100 files, the big
# folders travel as archives next to the code:
#
#   probes.7z  probes/, listings/, listings_open/, opcov/ AND extcov/
#              workflow unpacks it.  `listings_open/` is the oracle's
#              listing for a probe whose rule is read but not yet
#              implemented: evidence, not a gate, and it ships too
#   tools.7z   tools/, all but tools/probecheck.py, which the workflow runs
#              and so ships as a plain file
#   notes.7z   notes/ (no SPIR-V)
#
# Unpack the three with `7z x <name>.7z` in spirv2glasm/ (SETUP.md §0).
set -eu
out=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
name=$(basename "$here")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cd "$here"
rm -f probes.7z tools.7z notes.7z
7z a -t7z -mx=9 probes.7z probes listings listings_open opcov extcov glasmcov stress -xr'!*.spv' > /dev/null
7z a -t7z -mx=9 tools.7z tools -xr'!__pycache__' -x'!tools/probecheck.py' > /dev/null
7z a -t7z -mx=9 notes.7z notes -xr'!*.spv' > /dev/null
mkdir -p "$stage/$name/tools" "$stage/.github"
cp -r "$root/.github/workflows" "$stage/.github/"
cp ./*.md spirv2glasm.py mkpackage.sh probes.7z tools.7z notes.7z \
   "$stage/$name/"
cp tools/probecheck.py "$stage/$name/tools/"
for d in py corpus patches runtime; do
    (tar cf - --exclude=__pycache__ "$d") | (cd "$stage/$name" && tar xf -)
done
rm -f "$out"
(cd "$stage" && 7z a -t7z -mx=9 "$out" .github "$name" > /dev/null)
echo "$out: $(7z l "$out" | tail -1)"
