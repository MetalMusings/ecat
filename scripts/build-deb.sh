#!/bin/bash
#
# Build linuxcnc-ecat_*.deb: the ecat HAL component and EtherCAT Setup
# (the ecatsetup command), for the userspace EtherCAT master package
# linuxcnc-ethercat-uspace.
#
# Usage: scripts/build-deb.sh [-w] [-o <dir>]
#
#   -o  directory for the .deb. Default: the current directory.
#   -w  build the working tree, with uncommitted changes, to try them out;
#       the version gets "~dirty" after the date, so that such a build sorts
#       before a real one from the same day and is replaced by it.
#
# Otherwise the committed state of this repository is built, from a fresh
# clone, so uncommitted changes are not packaged. The package is built for the Debian
# release and LinuxCNC that this machine has.
#
# Build dependencies: build-essential git dpkg-dev libyaml-dev
#   linuxcnc-uspace-dev, and linuxcnc-ethercat-uspace 0.5 or newer (its
#   headers in /opt/linuxcnc-ethercat-uspace/include).

set -euo pipefail

PKG=linuxcnc-ecat
BASE_VERSION=0.2
MASTER=/opt/linuxcnc-ethercat-uspace
MASTER_PKG=linuxcnc-ethercat-uspace
MASTER_MIN=0.5
OUTDIR=$PWD
WORKTREE=

while getopts "o:wh" opt; do
    case $opt in
        o) OUTDIR=$(realpath "$OPTARG") ;;
        w) WORKTREE=~dirty ;;
        *) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 1 ;;
    esac
done

if [ ! -f "$MASTER/include/ecrt.h" ]; then
    echo "$MASTER/include/ecrt.h is missing: install $MASTER_PKG $MASTER_MIN or newer." >&2
    exit 1
fi

SRC=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
WORK=$(mktemp -d -t $PKG.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
ROOT=$WORK/pkg
PY=$ROOT/usr/lib/python3/dist-packages
DOC=$ROOT/usr/share/doc/$PKG

step() { echo; echo "=== $*"; }

step "Source"
if [ -n "$WORKTREE" ]; then
    mkdir "$WORK/ecat"
    (cd "$SRC" && git ls-files -co --exclude-standard -z | tar -c --null -T - -f -) \
        | tar -x -C "$WORK/ecat"
    REV=$(git -C "$SRC" rev-parse --short=8 HEAD)
else
    if [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
        echo "Note: $SRC has uncommitted changes; they are not packaged."
    fi
    git clone -q "$SRC" "$WORK/ecat"
    git -C "$WORK/ecat" checkout -q "$(git -C "$SRC" rev-parse HEAD)"
    REV=$(git -C "$WORK/ecat" rev-parse --short=8 HEAD)
fi
echo "ecat $REV"

step "Tests"
(cd "$WORK/ecat" && python3 -m unittest discover -s tests -q) 2>&1 | tail -3
make -s -C "$WORK/ecat/component" USPACE_ETHERCAT=$MASTER check 2>&1 | grep -v -e '^Compiling' -e '^Linking' -e 'warning:' -e 'varning:'

step "Component"
make -s -C "$WORK/ecat/component" USPACE_ETHERCAT=$MASTER \
    >"$WORK/component.log" 2>&1 || { tail -30 "$WORK/component.log"; exit 1; }

step "Package tree"
install -d "$ROOT/usr/lib/linuxcnc/modules" "$ROOT/usr/bin" "$PY/ecatsetup" "$DOC" "$ROOT/DEBIAN"
install -m 644 "$WORK/ecat/component/ecat.so" "$ROOT/usr/lib/linuxcnc/modules/"
strip --strip-unneeded "$ROOT/usr/lib/linuxcnc/modules/ecat.so"
install -m 644 "$WORK/ecat"/ecatsetup/*.py "$PY/ecatsetup/"
cat >"$ROOT/usr/bin/ecatsetup" <<'EOF'
#!/usr/bin/python3
"""EtherCAT Setup: see /usr/share/doc/linuxcnc-ecat/README.md."""
import sys

from ecatsetup.__main__ import main

sys.exit(main())
EOF
chmod 755 "$ROOT/usr/bin/ecatsetup"

install -m 644 "$WORK/ecat/README.md" "$DOC/"
install -d "$DOC/docs"
install -m 644 "$WORK/ecat"/docs/*.md "$DOC/docs/"
install -m 644 "$WORK/ecat/LICENSE" "$DOC/copyright"
echo "ecat: $(git -C "$SRC" rev-parse HEAD)$WORKTREE" >"$DOC/VERSION"

step "Control file"
. /etc/os-release
ARCH=$(dpkg --print-architecture)
# The date is UTC, as on the CI machines, so that a local build late in the
# evening does not get a later date than a release built at the same time.
VERSION=$BASE_VERSION+git$(date -u +%Y%m%d)$WORKTREE.$REV~$ID$VERSION_ID
MAINTAINER="$(git -C "$SRC" log -1 --format='%an <%ae>')"

# Library dependencies of ecat.so, without libethercat.so.2 (from
# linuxcnc-ethercat-uspace, below).
mkdir -p "$WORK/shlibs/debian"
printf 'Source: %s\n\nPackage: %s\nArchitecture: any\n' $PKG $PKG >"$WORK/shlibs/debian/control"
SHLIBS=$(cd "$WORK/shlibs" && dpkg-shlibdeps -O --ignore-missing-info \
    -l"$MASTER/lib" -l/usr/lib "$ROOT/usr/lib/linuxcnc/modules/ecat.so" 2>/dev/null \
    | sed -n 's/^shlibs:Depends=//p')
# The master's library comes from its package, not from a shlibs file.
SHLIBS=$(echo "$SHLIBS" | sed -E 's/(^|, )[^,]*ethercat[^,]*//g; s/^, //')
# Only the upstream version of LinuxCNC counts (see build-uspace-deb.sh in
# linuxcnc-ethercat).
SHLIBS=$(echo "$SHLIBS" | sed -E 's/(linuxcnc-uspace \(>= [^)-]*)-[^)]*\)/\1)/')
case "$SHLIBS" in
    *linuxcnc-uspace*) ;;
    *) SHLIBS="linuxcnc-uspace (>= 1:2.9), $SHLIBS" ;;
esac

cat >"$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Depends: $SHLIBS, $MASTER_PKG (>= $MASTER_MIN), python3, python3-yaml
Section: misc
Priority: optional
Homepage: https://github.com/MetalMusings/ecat
Description: EtherCAT for LinuxCNC configured without XML, experimental
 The ecat HAL component, which runs the userspace IgH EtherCAT master in
 LinuxCNC and is configured from ethercat.yaml, and EtherCAT Setup
 (ecatsetup), which proposes, checks, updates and diagnoses that file from
 the slaves on the bus. See /usr/share/doc/$PKG/README.md.
EOF

step "Package"
mkdir -p "$OUTDIR"
DEB=$OUTDIR/${PKG}_${VERSION}_$ARCH.deb
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
dpkg-deb --info "$DEB" | sed -n '/Package:/,$p'
echo
echo "Built $DEB"
