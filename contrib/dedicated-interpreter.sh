#!/bin/sh
# Build an interpreter used by nothing but this project, so that the Full Disk
# Access grant a scheduled run needs is as narrow as it can be.
#
# The problem this solves. Mail's database is TCC-protected, so an unattended
# run needs Full Disk Access — and macOS grants that per *binary*. The obvious
# binary to point a LaunchAgent at is `uv`, but granting Full Disk Access to
# /opt/homebrew/bin/uv grants it to every project `uv run` ever launches, this
# Mac's other work included. That is a large permission to hand out in order
# to read one mailbox.
#
# So: a copy of the interpreter, at a path only this project uses. Granting
# Full Disk Access to `.venv/bin/mail-triage-python` covers runs of this venv
# and nothing else.
#
# Two details make the copy work:
#
# - The binary loads @executable_path/../lib/libpython3.13.dylib, which a venv
#   does not have, so the library is symlinked into .venv/lib/.
# - CPython finds pyvenv.cfg one directory above the executable, so a copy
#   placed in .venv/bin/ resolves this venv's site-packages exactly as
#   .venv/bin/python3 does. That is why it goes there rather than somewhere
#   tidier.
#
# THE CAVEAT, and it is the reason this is a script rather than a one-off:
# `uv sync` may rebuild .venv and take both artefacts with it. The agent then
# fails loudly — no such file — rather than quietly, but the Full Disk Access
# grant will also need renewing against the fresh binary. Re-run this script
# after any uv sync that recreates the venv, then check the agent still works
# (see README: kickstart a --dry-run copy under a separate label).
#
# If that upkeep is not worth it, the alternative is to point the plist back
# at uv and grant Full Disk Access there instead. That is a deliberate
# widening, not a shortcut, and worth making knowingly.

set -eu

project="$(cd "$(dirname "$0")/.." && pwd)"
venv="$project/.venv"
target="$venv/bin/mail-triage-python"

if [ ! -d "$venv" ]; then
    echo "No .venv at $venv — run 'uv sync' first." >&2
    exit 1
fi

# The real interpreter behind the venv's symlink chain.
real="$(readlink -f "$venv/bin/python3")"
if [ ! -x "$real" ]; then
    echo "Cannot resolve the venv's interpreter from $venv/bin/python3." >&2
    exit 1
fi

cp -f "$real" "$target"
chmod +x "$target"

# The dylib the copy expects to find beside its own prefix.
libdir="$(dirname "$(dirname "$real")")/lib"
for lib in "$libdir"/libpython*.dylib; do
    [ -e "$lib" ] || continue
    ln -sf "$lib" "$venv/lib/$(basename "$lib")"
done

# Prove it before claiming it: a copy that cannot import the package is worse
# than no copy at all, because the failure would surface at half past eight.
if ! "$target" -c 'import mail_triage' 2>/dev/null; then
    echo "Built $target but it cannot import mail_triage — not usable." >&2
    exit 1
fi

echo "Built $target"
echo
echo "Now grant it Full Disk Access:"
echo "  System Settings -> Privacy & Security -> Full Disk Access -> +"
echo "  $target"
echo
echo "(Cmd-Shift-G in the file picker to type that path.)"
