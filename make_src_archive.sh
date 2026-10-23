#!/usr/bin/env bash
# Package the console source for sending to someone, from a commit.
#
#   ./make_src_archive.sh            ->  dist/b360_console_src-<sha>.zip  (HEAD)
#   ./make_src_archive.sh <rev>      ->  the same, from a tag or commit
#
# Built with git archive, so only tracked files go in: build/, gen/, the .spec
# and __pycache__ never do, and the build scripts regenerate them. On top of
# that, EXCLUDE drops the binaries (send the exe and its .sha256 separately)
# and the design record, which stays in-house.
#
# The archive is the commit, not the working tree, so a dirty tree is refused
# rather than quietly leaving uncommitted edits out of what gets sent.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

EXCLUDE=(dist b360_console_plan.md)

REV="${1:-HEAD}"
SHA="$(git rev-parse --short "$REV^{commit}")"

if [ "$REV" = "HEAD" ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no >&2
  echo "Uncommitted changes above would not be in the archive; commit them first." >&2
  exit 1
fi

OUT="dist/b360_console_src-$SHA.zip"
mkdir -p dist
git archive --format=zip --prefix=b360_console_src/ -o "$OUT" "$REV" -- . \
  "${EXCLUDE[@]/#/:!}"

unzip -l "$OUT"
echo "Wrote $OUT  ($(git log -1 --format='%h %s' "$REV"))"
