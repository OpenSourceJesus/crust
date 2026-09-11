#!/bin/bash
# Assemble a stand-in for the macOS SDK's usr/include from the header
# *sources* Apple publishes under the APSL at github.com/apple-oss-distributions,
# so the macOS target's handling of real Apple headers can be developed and
# tested on Linux. The SDK itself is licensed for use on Apple hardware only;
# on a Mac, use the real one: -I "$(xcrun --show-sdk-path)/usr/include".
#
#     tools/macos_proxy_sdk.sh [OUTDIR]        # default /tmp/crust-macos-sdk
#     python3 -m shivyc.main --target arm64 --os macos -S \
#         -I OUTDIR/usr/include prog.c
#
# Needs git, python3, perl, ed and unifdef (apt install unifdef). Only the
# header directories are fetched (shallow, sparse).
#
# Fidelity: the SDK's headers are not the raw sources. This replicates the
# install steps that change what a compiler sees -- Apple's own generators for
# the Availability headers, sys/_symbol_aliasing.h, sys/_posix_availability.h
# and the Libc feature flags; Libc's install-time stripping of
# //Begin-Libc..//End-Libc blocks and its unifdef pass; and leaving out the
# headers Libc marks "Not to be installed". It does not replicate install
# lists (some extra headers are present) or xnu's unifdef of kernel-private
# sections (those are under macros that are never defined outside the kernel,
# so the preprocessor skips them anyway).
set -euo pipefail

OUT=${1:-/tmp/crust-macos-sdk}
SRC=${CRUST_APPLE_SRC:-$OUT.src}
INC=$OUT/usr/include
ORG=https://github.com/apple-oss-distributions

for tool in git python3 perl ed unifdef; do
    command -v $tool >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

fetch() {   # fetch REPO PATH...   (shallow, sparse, reused if present)
    local repo=$1; shift
    if [ ! -d "$SRC/$repo" ]; then
        git clone -q --depth 1 --filter=blob:none --sparse \
            "$ORG/$repo.git" "$SRC/$repo"
    fi
    # Non-cone patterns, anchored at the repo root: cone mode takes only
    # directories, and some of what is needed are root-level files.
    local pats=()
    for p in "$@"; do pats+=("/$p"); done
    (cd "$SRC/$repo" && git sparse-checkout set --no-cone "${pats[@]}")
}

mkdir -p "$SRC"
rm -rf "$OUT/usr"
mkdir -p "$INC"

fetch Libc include xcodescripts Platforms
fetch xnu bsd/sys bsd/arm bsd/machine bsd/net bsd/netinet bsd/netinet6 osfmk/mach \
    libkern/libkern libsyscall/wrappers
fetch libpthread include
fetch libmalloc include
fetch libplatform include
fetch CarbonHeaders TargetConditionals.h
fetch AvailabilityVersions templates availability availability.dsl

# 1. Availability headers, generated from Apple's DSL exactly as its build
#    does (availability --preprocess over each template).
AV=$SRC/AvailabilityVersions
for t in Availability.h AvailabilityInternal.h AvailabilityInternalLegacy.h \
         AvailabilityMacros.h AvailabilityVersions.h; do
    "$AV/availability" --preprocess "$AV/templates/$t" "$INC/$t"
done
mkdir -p "$INC/os"
"$AV/availability" --preprocess "$AV/templates/os_availability.h" \
    "$INC/os/availability.h"

# 2. xnu: sys/, arm/, machine/ and mach/ as the SDK lays them out, plus the two
#    headers xnu generates at build time. Its generators expect to find the
#    availability tool inside an SDK root.
X=$SRC/xnu
for d in sys arm machine net netinet netinet6; do
    mkdir -p "$INC/$d"
    cp -r "$X/bsd/$d/." "$INC/$d/"
done
mkdir -p "$INC/mach" "$INC/libkern"
cp -r "$X/osfmk/mach/." "$INC/mach/"
cp -r "$X/libkern/libkern/." "$INC/libkern/"
cp "$X/libsyscall/wrappers/gethostuuid.h" "$INC/"
FAKE=$SRC/fake-sdkroot
mkdir -p "$FAKE/usr/local/libexec"
ln -sf "$AV/availability" "$FAKE/usr/local/libexec/availability.pl"
ln -sf "$AV/availability.dsl" "$FAKE/usr/local/libexec/availability.dsl"
bash "$X/bsd/sys/make_symbol_aliasing.sh" "$FAKE" "$INC/sys/_symbol_aliasing.h"
bash "$X/bsd/sys/make_posix_availability.sh" "$INC/sys/_posix_availability.h"

# 3. Libc, minus the headers it marks as build-only (its own sys/cdefs.h is a
#    build wrapper around xnu's; the SDK has xnu's). Installed over xnu's so
#    the Libc copy wins where both provide a header, except those.
L=$SRC/Libc
( cd "$L/include"
  find . -name '*.h' | while read -r h; do
      grep -q "Not to be installed" "$h" && continue
      mkdir -p "$INC/$(dirname "$h")"
      cp "$h" "$INC/$h"
  done )

# 4. Libc's install-time rewriting (xcodescripts/headers.sh): strip
#    //Begin-Libc..//End-Libc blocks, then unifdef with the platform's
#    feature flags. unifdef exits 1 when it changed something.
( cd "$L/include"
  find . -name '*.h' | while read -r h; do
      [ -f "$INC/$h" ] || continue
      if grep -q '^//Begin-Libc' "$INC/$h"; then
          ed -s "$INC/$h" < "$L/xcodescripts/strip-header.ed"
      fi
  done )
UNIFDEFARGS=$(cd "$L" && ARCHS=arm64 VARIANT_PLATFORM_NAME=macosx \
              SRCROOT="$L" perl xcodescripts/generate_features.pl --unifdef \
              2>/dev/null)
( cd "$L/include"
  find . -name '*.h' | while read -r h; do
      f=$INC/$h
      [ -f "$f" ] || continue
      grep -q -e UNIFDEF -e OPEN_SOURCE -e _USE_EXTENDED_LOCALES_ "$f" \
          || continue
      cp "$f" "$f.orig"
      # shellcheck disable=SC2086
      unifdef $UNIFDEFARGS "$f.orig" > "$f" || [ $? -ne 2 ]
      rm "$f.orig"
  done )

# 5. The rest of the SDK's usr/include that the common headers reach.
cp -r "$SRC/libpthread/include/." "$INC/"
# libpthread's install-symlinks.sh: the headers live in pthread/, with these
# four linked into usr/include.
for h in pthread.h pthread_impl.h pthread_spis.h sched.h; do
    ln -sf "pthread/$h" "$INC/$h"
done
mkdir -p "$INC/malloc"
cp "$SRC/libmalloc/include/malloc/"*.h "$INC/malloc/"
cp "$SRC/libplatform/include/setjmp.h" "$INC/"
cp "$SRC/CarbonHeaders/TargetConditionals.h" "$INC/"

echo "macOS SDK stand-in: $INC ($(find "$INC" -name '*.h' | wc -l) headers)"
