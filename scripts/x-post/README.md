# X posting helpers

These scripts post reviewed clips to X through a signed-in Brave profile with
the [Browse](https://github.com/anthonyrussano/browse) CLI. They are separate
from the engine: the CLI renders clips, and nothing here runs unless you call
`post.sh` with a clip ID. Each call publishes one post. Get the account owner's
explicit approval for the exact clip and text first.

The full procedure, including handle verification, re-encoding, pitfalls and
results, is in the Agent Wiki:
[Posting Rendered Video Clips to X from Brave with Browse](https://agent.wikip.co/posting-rendered-video-clips-to-x-from-brave-with-browse/).

## Files

- `post.sh CLIP_ID` opens the X composer in a new Browse tab, uploads
  `CLIP_ID.mp4`, pastes the approved text, waits until the upload reports
  `Ready`, checks that the text box holds more than 100 characters, then
  clicks Post.
- `mk.py CLIP_ID` prints the Browse `eval` request that pastes the text. It
  never posts.

## Setup

Copy each approved clip into a folder under `/tmp`. Browse uploads only from
its file roots, which were `/tmp` only on Iconium, and accepts at most 50 MiB
(52,428,800 bytes) per upload. `post.sh` refuses larger files.

Write the approved text as a JSON object of plain strings:

```json
{"clip_06": "Post text...\nhttps://youtu.be/VIDEO_ID?t=1478"}
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTS_JSON` | required | Approved post text |
| `BROWSE` | `~/Projects/browse/rust/target/release/browse` | Browse binary |
| `X_POST_CLIP_DIR` | `/tmp/remy-x` | Folder with `CLIP_ID.mp4` |
| `X_POST_TAB_PREFIX` | `remy` | Tab ID prefix. IDs are lowercased and use dashes only |
| `X_POST_DRY_RUN` | `0` | `1` fills the composer and stops before Post |
| `X_POST_SETTLE_SECONDS` | `20` | Wait after clicking Post |

## Use

```bash
export POSTS_JSON=/tmp/remy-x/final_posts.json
X_POST_DRY_RUN=1 scripts/x-post/post.sh clip_06   # check the composer first
scripts/x-post/post.sh clip_06                    # publishes the post
```

After each post, open the profile about 30 seconds later and confirm the text
and video. The script's output is not proof of the final state. Space several
posts a few minutes apart, and check for an earlier post of the same clip
before you retry.
