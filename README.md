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

options:
  -h, --help            Show help
  -f, --folder [PATH ...]
                        Folders to check; defaults to the script's folder
  -r, --recursive       Include subfolders
  -s, --staxrip         Check filenames ending in '_new' and compare originals
  -if, --ignore-ffmpeg  Skip the independent FFmpeg decoding check
```

Supported extensions are `.mkv`, `.mp4`, `.avi`, and `.webm`, case-insensitively. Edit `video_file_extensions` in the script to change them.

### What is checked

- FFprobe decodes each video/audio track and collects frame timestamps and audio sample timing. Stream types are identified explicitly; audio does not have to be the second stream or be present at all. Subtitles and attached cover pictures are excluded from decoding checks.
- The independent FFmpeg pass explicitly decodes every video/audio track with error handling enabled. Reported decoder errors fail verification even when a tool exits with code zero. `--ignore-ffmpeg` skips this additional pass and produces a warning; FFprobe's frame scan and error checks still run.
- Duration checks use decoded timestamps rather than frame count divided by a nominal frame rate, supporting constant- and variable-frame-rate recordings. Audio timing uses decoded sample counts. Missing duration tags do not disable the checks. Unavailable timestamps produce warnings, and any estimate of the final video-frame duration is identified.
- A/V duration differences and start offsets produce warnings. The duration tolerance is the smaller of 50 milliseconds or the median positive video-frame timestamp interval. Container-duration differences also only warn: subtitle overhang, encoder padding, and offsets can legitimately change these values.

### StaxRip original comparison

For `Recording_new.mkv`, the script removes exactly one trailing `_new` and looks in the same folder for `Recording` with any supported extension, such as `Recording.mp4`. Matching is case-insensitive. If the original is missing or ambiguous, comparison is skipped with a warning; standalone checks still run.

Motion-video streams are paired in their file order. Different video-stream counts or decoded frame counts fail verification. Corresponding presentation timestamps must match after allowing one constant shift shared by all video streams, plus rounding tolerance equal to the sum of the two stream time-base ticks. Internal timing changes fail and identify the first affected frame. Audio-track preservation is not compared against the original.

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