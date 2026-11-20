#!/bin/bash
# Post one reviewed clip to X through the signed-in Brave profile, using the
# Browse CLI. Running this script with a clip ID publishes a post. Get the
# account owner's explicit approval for the exact text and clip first.
#
# Usage: POSTS_JSON=/path/to/final_posts.json scripts/x-post/post.sh CLIP_ID
#
# Environment (defaults match the November 20, 2026 run on Iconium):
#   BROWSE          Browse binary  (~/Projects/browse/rust/target/release/browse)
#   POSTS_JSON      JSON object mapping clip IDs to plain-string post text (required)
#   X_POST_CLIP_DIR folder holding CLIP_ID.mp4  (/tmp/remy-x). Browse uploads
#                   only from its file roots, which were /tmp only.
#   X_POST_TAB_PREFIX tab ID prefix (remy). Tab IDs must use lowercase
#                   letters, digits and dashes.
#   X_POST_DRY_RUN  set to 1 to stop before clicking Post
#   X_POST_SETTLE_SECONDS wait after clicking Post (20)
#
# The script does not confirm the published post. Check the profile afterward.
set -u

BROWSE="${BROWSE:-$HOME/Projects/browse/rust/target/release/browse}"
CLIP_DIR="${X_POST_CLIP_DIR:-/tmp/remy-x}"
TAB_PREFIX="${X_POST_TAB_PREFIX:-remy}"
MAX_BYTES=52428800 # Browse accepts at most 50 MiB per upload call.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

die() { echo "$CLIP $1" >&2; exit 1; }

[ $# -eq 1 ] || { echo "usage: POSTS_JSON=FILE $0 CLIP_ID" >&2; exit 2; }
CLIP=$1
: "${POSTS_JSON:?POSTS_JSON must name the approved post text file}"
export POSTS_JSON
[ -x "$BROWSE" ] || die "browse-not-found: $BROWSE"

# Tab IDs accept only lowercase letters, digits and dashes.
TAB=$(printf '%s-%s' "$TAB_PREFIX" "$CLIP" | tr '[:upper:]_' '[:lower:]-' | tr -c 'a-z0-9-' '-')
FILE="$CLIP_DIR/$CLIP.mp4"
[ -f "$FILE" ] || die "missing-file: $FILE"
SIZE=$(stat -c %s "$FILE")
[ "$SIZE" -le "$MAX_BYTES" ] || die "too-large: $SIZE bytes, limit $MAX_BYTES; re-encode first"
case "$(realpath "$FILE")" in /tmp/*) ;; *) echo "$CLIP warning: $FILE is outside /tmp; Browse may refuse it" >&2 ;; esac

# Build the paste request first, so a bad POSTS_JSON stops before any browser work.
REQUEST=$(python3 "$HERE/mk.py" "$CLIP") || die "bad-post-text"

run() { local out; out=$("$BROWSE" "$@" 2>&1) || { echo "$out" >&2; return 1; }; }

run tabs --json "{\"action\":\"new\",\"id\":\"$TAB\",\"url\":\"https://x.com/compose/post\"}" || die "tab-failed"
run wait_for --tab "$TAB" --json '{"selector":"[role=dialog] [data-testid=tweetTextarea_0]","timeout":30000}' || die "composer-missing"
run upload --tab "$TAB" --selector '[role=dialog] input[data-testid=fileInput]' --files "$FILE" --timeout 180000 || die "upload-failed"
run eval --tab "$TAB" --json "$REQUEST" || die "paste-failed"

st=""
for _ in $(seq 1 60); do
  st=$("$BROWSE" eval --tab "$TAB" --expression 'document.querySelector("[role=dialog] [data-testid=attachments]")?.innerText||""' 2>/dev/null | tail -1)
  echo "$st" | grep -q 'Ready' && break
  sleep 5
done
echo "$st" | grep -q 'Ready' || die "not-ready: $st"

len=$("$BROWSE" eval --tab "$TAB" --expression 'document.querySelector("[role=dialog] [data-testid=tweetTextarea_0]").innerText.length' 2>/dev/null | tail -1)
[ "${len:-0}" -gt 100 ] 2>/dev/null || die "text-missing: length ${len:-unknown}"

if [ "${X_POST_DRY_RUN:-0}" = 1 ]; then
  echo "$CLIP dry-run: video ready and text filled in tab $TAB; Post was not clicked"
  exit 0
fi

run click --tab "$TAB" --selector '[role=dialog] [data-testid=tweetButton]' || die "click-failed"
sleep "${X_POST_SETTLE_SECONDS:-20}"
echo "$CLIP posted-click $(date +%T)"
