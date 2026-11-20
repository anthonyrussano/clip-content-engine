#!/usr/bin/env python3
"""Print the Browse eval request that pastes one post's text into the X composer.

Usage: POSTS_JSON=/path/to/final_posts.json python3 mk.py CLIP_ID

POSTS_JSON must name a JSON object that maps clip IDs to plain-string post
text. The script only builds JSON for ``browse eval``. It does not post.

The text is inserted with a synthetic paste event in the page's main world,
so X does not open its @mention autocomplete the way typed text does.
"""

import json
import os
import sys

PASTE_JS = (
    '(() => { const el=document.querySelector("[role=dialog] [data-testid=tweetTextarea_0]");'
    ' el.focus(); const dt=new DataTransfer(); dt.setData("text/plain", %s);'
    ' el.dispatchEvent(new ClipboardEvent("paste",{clipboardData:dt,bubbles:true,cancelable:true}));'
    ' return "ok"; })()'
)


def build_request(posts: dict, clip_id: str) -> dict:
    if clip_id not in posts:
        raise KeyError(f"{clip_id} is not in POSTS_JSON")
    text = posts[clip_id]
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{clip_id} must map to a nonempty plain string")
    return {"expression": PASTE_JS % json.dumps(text), "world": "main"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: POSTS_JSON=/path/to/final_posts.json mk.py CLIP_ID", file=sys.stderr)
        return 2
    path = os.environ.get("POSTS_JSON")
    if not path:
        print("POSTS_JSON is not set", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as handle:
        posts = json.load(handle)
    if not isinstance(posts, dict):
        print("POSTS_JSON must contain a JSON object", file=sys.stderr)
        return 2
    try:
        request = build_request(posts, argv[1])
    except (KeyError, ValueError) as error:
        print(error.args[0], file=sys.stderr)
        return 1
    print(json.dumps(request))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
