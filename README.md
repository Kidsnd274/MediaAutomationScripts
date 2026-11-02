# Media Automation Scripts
Just a collection of some scripts that I use to automate my management of media.

## VerifyVideo

Checks video/audio decoding and timing. With `--staxrip`, also compares the output's decoded video frame counts and timestamps with the original.

### Usage

Requires Python 3.7+ and FFmpeg/FFprobe 5.1+ (no extra Python packages). Put the tools in PATH, beside the script, or in its `ffmpeg` subfolder. Both checks remain sequential.

```text
python VerifyVideo.py -f "Video Folder"
python VerifyVideo.py -f "Video Folder" -r -s
python VerifyVideo.py -f "Video Folder" -s --ignore-ffmpeg
python VerifyVideo.py -f "Video Folder" -s --strict-timing
python VerifyVideo.py --file "Video Folder\Recording_new.mp4" -s
python VerifyVideo.py --file "Video Folder\Recording_new.mp4" -s --strict-timing
python VerifyVideo.py --file "first.mp4" "second.mkv"

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
```

Supported extensions are `.mkv`, `.mp4`, `.avi`, and `.webm`, case-insensitively. Edit `video_file_extensions` in the script to change them.

With `--file`, only the named files and any explicitly requested folders are checked; the script's folder is not scanned automatically. You can name multiple files after one `--file`, repeat the option, or combine it with `--folder`. Files selected more than once are checked only once. Without any file or folder paths, the script checks its own folder as before. `--recursive` only applies to folder scans.

Missing paths, directories passed to `--file`, and unsupported extensions are reported as failures; other valid selections are still checked. With `--file -s`, the selected filename must end in `_new` (before its extension), following the original-comparison naming convention below. A non-matching explicit filename is reported as a selection failure rather than silently skipped. Both `-s` and `--strict-timing` work with file selection.

### Status and progress

The original Kidsnd274 banner is followed by the selected folders, enabled checks, timing policy, and a numbered list of all selected files. Each file has a `[current/total]` heading and named stages for FFprobe metadata, each video/audio stream scan, the independent FFmpeg decode, original comparison, and duration checks. Full commands and routine decoder logs are hidden.

In an interactive terminal, a single line updates about twice per second with estimated media-time percentage, processed time, frame count when available, and elapsed time. When duration is unavailable, processed time and frame count are shown without a percentage. Percentages describe the current stage, not the whole batch, and remain below 100% until the stage finishes. A completed stage shows `done`, `issues found`, or `failed`, with available frame counts and media duration.

Redirected output uses ordinary lines without terminal control sequences: stage starts/completions and, for long checks, an update at most once every 30 seconds. Each file ends with its result and error/warning counts; the final summary includes passed files, failures, files with warnings, elapsed time, and the failure-report location if one was written.

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
