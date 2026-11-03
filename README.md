# Media Automation Scripts
Just a collection of some scripts that I use to automate my management of media.

## VerifyVideo

Checks video/audio decoding and timing. With `--staxrip`, also compares the output's decoded video frame counts and timestamps with the original.

### Usage

Requires Python 3.7+ and FFmpeg/FFprobe 5.1+ (no extra Python packages). Keep `VerifyVideo.py`, `verification_runtime.py`, and `verification_ui.py` together. Put the tools in PATH, beside the script, or in its `ffmpeg` subfolder. Checks within each file remain sequential.

```text
python VerifyVideo.py -f "Video Folder"
python VerifyVideo.py -f "Video Folder" -r -s
python VerifyVideo.py -f "Video Folder" -s --ignore-ffmpeg
python VerifyVideo.py -f "Video Folder" -s --strict-timing
python VerifyVideo.py --file "Video Folder\Recording_new.mp4" -s
python VerifyVideo.py --file "Video Folder\Recording_new.mp4" -s --strict-timing
python VerifyVideo.py --file "first.mp4" "second.mkv"
python VerifyVideo.py -f "Video Folder" --jobs 2
python VerifyVideo.py -f "Video Folder" --jobs 4 --decode-threads 2

options:
  -h, --help            Show help
  -f, --folder [PATH ...]
                        Folders to check
  --file PATH [PATH ...]
                        Specific video files; may be repeated or combined with --folder
  -r, --recursive       Include subfolders
  -s, --staxrip         Check filenames ending in '_new' and compare originals
  -if, --ignore-ffmpeg  Skip the independent FFmpeg decoding check
  --strict-timing      Require exact timing preservation within time-base rounding;
                       also fail on duplicate video presentation timestamps
  --jobs N             Maximum concurrent file jobs (positive integer; default 1)
  --decode-threads N   Request input video decoder threads (positive integer; optional)
```

Supported extensions are `.mkv`, `.mp4`, `.avi`, and `.webm`, case-insensitively. Edit `video_file_extensions` in the script to change them.

With `--file`, only the named files and any explicitly requested folders are checked; the script's folder is not scanned automatically. You can name multiple files after one `--file`, repeat the option, or combine it with `--folder`. Files selected more than once are checked only once. Without any file or folder paths, the script checks its own folder as before. `--recursive` only applies to folder scans.

Missing paths, directories passed to `--file`, and unsupported extensions are reported as failures; other valid selections are still checked. With `--file -s`, the selected filename must end in `_new` (before its extension), following the original-comparison naming convention below. A non-matching explicit filename is reported as a selection failure rather than silently skipped. Both `-s` and `--strict-timing` work with file selection.

### Status and progress

The Kidsnd274 banner is followed by the selected folders, enabled checks, timing policy, and a numbered list of all selected files (shown relative to their folder). Each file has a `[current/total]` heading and one aligned row per stage: FFprobe metadata, each video/audio stream scan, the independent FFmpeg decode, original comparison, and duration checks. Each row starts with a status icon (`✓` done, `▲` warning, `✗` error, `–` skipped) and shows frame counts, media duration, and elapsed time. Warnings and errors are then listed as a tree under the stages, followed by the file's verdict (`PASSED`, `PASSED WITH WARNINGS`, or `FAILED`). Full commands and routine decoder logs are hidden.

In an interactive terminal, a spinner line with a progress bar updates about four times per second with estimated media-time percentage, processed time, frame count when available, and elapsed time. When duration is unavailable, processed time and frame count are shown without a percentage. Percentages describe the current stage, not the whole batch, and remain below 100% until the stage finishes.

The summary shows a results table (result, error and warning counts, time, and file), the passed / with-warnings / failed totals, total elapsed time, failed paths, and the failure-report location if one was written. "With warnings" counts files that passed with warnings.

Colour is used only in an interactive terminal, using standard ANSI codes (enabled on Windows consoles through `ctypes`, with no extra packages); set `NO_COLOR` to turn it off. Redirected output has no colour or cursor control: stage starts/completions and, for long checks, an update at most once every 30 seconds. If the output encoding cannot represent the Unicode symbols (for example a `cp1252` log file on Windows), plain ASCII equivalents are used instead.

To compare candidate output styles with mock data (no FFmpeg needed), run `python previews/style_preview.py`.

### Concurrent batches

`--jobs` defaults to **1**. The default, explicit `--jobs 1`, and any selection with only one valid input use the classic terminal layout above. A larger limit starts a bounded pool of Python processes, capped at the number of selected files. A freed slot starts the next file immediately. Metadata, individual stream scans, FFmpeg, and original comparisons remain sequential inside each job. File numbering, the final full transcript, summary, and failure report retain the sorted scan-list order.

With `--decode-threads` omitted, **FFprobe frame scans request 8 threads and FFmpeg receives no thread override**, regardless of job count. Metadata probing is unchanged. An explicit value replaces the FFprobe scan setting (including originals) and adds FFmpeg's generic `-threads:v N` input option before `-i`, applying to input video decoders that support threading. It does not change audio or output encoder thread options. This request is not a total CPU/thread limit; decoder implementations and tool internals determine actual usage. There is no resource detection, calculated thread budget, automatic worker selection, startup benchmark, or adaptive tuning.

Concurrent interactive runs use an ordered scrollable transcript above a pinned live panel, following the V5 preview. Both sections show real stage progress and spinners; completed details and diagnostics appear immediately. Use the mouse wheel, drag/click the right-hand scrollbar, or use arrows/Page Up/Page Down. Home browses from the start; End follows completions. Q or Ctrl+C cancels the batch. Only changed rows are rewritten. On exit the terminal modes and cursor are restored, and the full ordered transcript is printed to ordinary scrollback. Redirected output or terminals without the required capabilities use plain completion logs followed by the ordered transcript, with the same concurrency.

Cancellation stops dispatch and cleans up owned workers/tools, including blocked metadata reads. Interrupted batches return **130**, identify checked versus unfinished files, and never classify cancelled/unstarted inputs as corrupt. Windows uses parent-owned kill-on-close Job Objects and suspended tool startup before ownership registration. If the host prevents safe Job Object assignment, execution fails before starting an unowned tool. POSIX uses process groups and a worker cancellation/liveness watcher. Abrupt OS termination and terminals/platforms beyond the tested Windows configuration need additional validation.

Batch elapsed time includes worker startup and cleanup, and ends before printing the final concurrent transcript and summary. Per-file times measure processing inside each worker and exclude queue wait. Concurrent work raises memory use and competing reads; start by manually comparing one and two jobs on your storage. Measured local results and limitations are in [plans/concurrency-benchmark-results.md](plans/concurrency-benchmark-results.md); these do not change the fixed defaults.

To find suitable settings on another computer, follow [Benchmarking VerifyVideo](docs/benchmarking.md). It includes instructions for future agents and a reusable runner that accepts explicit media paths; raw results stay local.

### What is checked

- FFprobe decodes each video/audio track and collects frame timestamps and audio sample timing. Stream types are identified explicitly; audio does not have to be the second stream or be present at all. Subtitles and attached cover pictures are excluded from decoding checks.
- The independent FFmpeg pass explicitly decodes every video/audio track with error handling enabled. Reported decoder errors fail verification even when a tool exits with code zero. `--ignore-ffmpeg` skips this additional pass and produces a warning; FFprobe's frame scan and error checks still run.
- Duration checks use decoded timestamps rather than frame count divided by a nominal frame rate, supporting constant- and variable-frame-rate recordings. Audio timing uses decoded sample counts. Missing duration tags do not disable the checks. Unavailable timestamps produce warnings, and any estimate of the final video-frame duration is identified.
- A/V duration differences and start offsets produce warnings. The duration tolerance is the smaller of 50 milliseconds or the median positive video-frame timestamp interval. Container-duration differences also only warn: subtitle overhang, encoder padding, and offsets can legitimately change these values.
- Duplicate video presentation timestamps warn by default and fail with `--strict-timing`. Backwards presentation timestamps in the output always fail. Source timing problems are reported as warnings.

### StaxRip original comparison

For `Recording_new.mkv`, the script removes exactly one trailing `_new` and looks in the same folder for `Recording` with any supported extension, such as `Recording.mp4`. Matching is case-insensitive. If the original is missing or ambiguous, comparison is skipped with a warning; standalone checks still run.

Motion-video streams are paired in their file order. Different video-stream counts or decoded frame counts always fail verification. Corresponding presentation timestamps are first compared after allowing one constant shift shared by all video streams, plus rounding tolerance equal to the sum of the two stream time-base ticks.

By default, larger timestamp differences can produce a warning if **every** paired frame fits nearest-grid rounding: frame counts must match, all paired timestamps must be available, all output timestamps must lie on one fixed grid, and every change must be within half a grid interval (capped at 50 milliseconds), plus time-base tolerance. The grid uses the output's reported frame rate, falling back to its median positive timestamp interval when no rate is available. This recognizes cases such as ShadowPlay timestamp jitter rounded onto a 60 Hz encoder grid. Arbitrary small jitter is not automatically accepted; unexplained timing changes and larger drift still fail and identify the first affected frame.

Use `--strict-timing` to disable that exception and require the original near-exact timing comparison. Duplicate presentation timestamps also fail in this mode, even without an original.

With `--staxrip`, original audio streams are also decoded to give duration warnings source context. An A/V discrepancy is labelled as already present in the original only when the corresponding audio frame counts and start/end timing match, the original already exceeds its tolerance, and any change in the signed duration discrepancy fits a small video-rounding allowance. Newly introduced differences retain their ordinary warnings. These checks do not prove that audio payloads or picture content are identical.

Original-file probe/decoder errors warn that comparison is incomplete. Usable frame counts and timestamps are still compared. Errors in the output always fail; they are never automatically excused as inherited source defects.

This assumes the encode intentionally preserves the frame sequence and timing. Trimming, frame-rate conversion, decimation, and interpolation can legitimately fail this comparison. Frame counts and timestamps do **not** verify picture content: a wrong picture, frozen frame, or visual artifact with unchanged timing can still pass. Lossy encode quality and preservation of existing visual defects require a separate visual comparison.

### Results

- `OK`: enabled checks passed.
- `OK WITH WARNINGS`: no failures, but review warnings or incomplete checks.
- `FAILED`: decoding, source comparison, or a required check failed.

Warnings alone return exit code `0`; failures return `1`. Individual file failures do not stop the batch. Failures are listed in `corrupted_files.txt` in the **current working directory**; this file is only written when failures occur, so an older report can remain after a later successful run. Inputs are not modified.

### Tests

```text
python -m unittest discover -s tests -v
```

Tests use the standard library. Media integration tests require FFmpeg and FFprobe in PATH with the `libx264` encoder and create small temporary synthetic clips.

## VmafBatch
Script to run VMAF easily and on multiple files

### Usage
You need **ffmpeg** to be in either PATH or in the same directory as the script. ffmpeg needs to be build with **--enable-libvmaf**.

Open the script and edit the pair of videos to run VMAF analysis on.
Run the command `python VmafBatch.py` to start.

## mkvmerge_batch
Script to run the same mkvmerge command on files that have similar names but their episode number is different.

Edit the .bat according to the instructions
