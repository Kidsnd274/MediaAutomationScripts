# Terminal previews

All previews use fictional media, timings and diagnostics. No videos are opened,
FFmpeg/FFprobe are not run, and no failure reports are written. Python's standard
library is sufficient. Run the commands from the repository root.

## V3A: pinned live progress with mouse scrolling

```powershell
python previews/concurrency_preview_v3_pinned.py
```

The full transcript stays in file order. Completed stages and diagnostics appear
immediately. Running stages have spinners and progress bars both in the transcript
and in the pinned live footer. Only changed screen rows are rewritten.

Scroll over the transcript with the mouse wheel, or click/drag its right-hand
scrollbar. Arrows and Page Up/Down also scroll. Home shows the beginning; End
resumes following completed files. Space pauses/resumes; Q or Ctrl+C stops.
Windows uses native console mouse events; other supporting terminals use SGR
mouse reports. The full transcript returns to normal scrollback on exit.

## V5: V3A with a full-width divider

```powershell
python previews/concurrency_preview_v5.py
```

Same as V3A, except that the "Live progress" rule above the pinned panel spans
the whole window, separating the panel from the transcript. Accepts the same
options as V3.

## V3B: normal terminal scrollback

```powershell
python previews/concurrency_preview_v3_scrollback.py
```

Use the terminal's own scrollbar and mouse wheel. This preview uses the normal
screen and does not capture mouse events. Completed stages and diagnostics append
to history as they finish, labelled with their original file numbers. An animated
active transcript and compact live section follow that history; both show bars and
spinners. Space pauses/resumes; Q or Ctrl+C stops.

Old scrollback remains historical; the active area continues updating at the end.
The final full transcript and results table are printed in original file order.

## Comparing the versions

Both accept these options:

| Option | Effect |
| --- | --- |
| `-if` / `--ignore-ffmpeg` | Skip the mock independent FFmpeg pass |
| `--jobs 3` | Preview three concurrent files (range: 1–5; default: 2) |
| `--speed 1` | Normal simulation speed (default: 2x) |
| `--start-at 10 --paused` | Start paused with completed and running files to inspect |
| `--snapshot 10` | Print a single static snapshot |
| `--no-anim` | Print representative snapshots and the final transcript immediately |

For example, to compare the two scrolling behaviors without rushing:

```powershell
python previews/concurrency_preview_v3_pinned.py --start-at 10 --paused
python previews/concurrency_preview_v3_scrollback.py --start-at 10 --paused
```

Redirected/non-ANSI output automatically uses plain snapshots.

Earlier previews are preserved: `style_preview.py`, `concurrency_preview.py` (v1),
and `concurrency_preview_v2.py`. The v3 entry points share formatting and mock
scheduling through `_concurrency_preview_v3.py`, reusing those earlier previews.
