# VerifyVideo concurrency implementation and measurements

Date: 2026-11-03.

This is the reviewed implementation summary. Raw logs, JSON, environment dumps,
media and the machine-specific original harness are local artifacts excluded
from version control. For new measurements on another computer, use
[the benchmarking guide](../docs/benchmarking.md) and its portable runner.

## Delivered behavior

`--jobs N` bounds reusable spawned Python workers; checks inside a file remain
sequential. The collector refills free slots independently of rendering and file
order. Final transcript, result numbering and failure report use scan order.
Workers retain frame/timestamp arrays locally and return compact results/stages.

Defaults remain **jobs = 1**, **FFprobe frame scans = 8 threads**, **FFmpeg = tool
default**. No automatic resource selection, detection, budgets or tuning were
added. `--decode-threads N` replaces the FFprobe scan setting, including originals,
and supplies FFmpeg's generic `-threads:v N` **before `-i`**, for input video
decoders supporting threading. Metadata commands are unchanged. This is not a
total-thread limit. Explicit 8 also overrides FFmpeg, so it differs from omission.
Option placement/semantics were checked with Context7's official FFmpeg reference
and exercised on 5.1.2, including multiple input video streams. See the
[FFmpeg codec options](https://ffmpeg.org/ffmpeg-all.html#Codec-Options).

One effective job retains the classic terminal layout. Concurrent capable
terminals use the V5 layout with real events: ordered scrollable transcript,
pinned live panel, full usable-width divider, bars/spinners in both sections,
immediate details, wheel/drag/keyboard navigation and changed-row writes. Plain
logs retain concurrency. Production does not import the previews.

Windows ownership uses kill-on-close jobs: the parent assigns each worker before
dispatch; individual tools start suspended, enter ownership, then resume. A
watcher can stop blocked metadata/stdout reads. Cancellation stops dispatch,
allows a short tool shutdown grace, then enforces bounded tree cleanup. Exit 130
and INCOMPLETE distinguish interrupted/unstarted files from checked media.
Unexpected worker failures become identified operational errors, with partial
diagnostics retained and unknown durations labelled honestly. Safe ownership
registration failures stop execution before an unowned tool executes.

Progress uses one 16 KiB latest-update slot per worker with nonblocking access;
control/results use independent reliable pipes. A worker dying with its progress
lock held cannot block the collector or other workers. The cancellation flag is
a shared byte, avoiding a process-shared Event mutex during forced termination.

## Environment and corpus

- Windows 11 Pro 10.0.26300, x64; Python 3.13.2.
- AMD Ryzen 7 5800X: 8 cores, 16 logical processors; approximately 31.9 GiB RAM.
- The benchmark volume is NTFS on **Samsung SSD 860 EVO 2TB, SATA**, confirmed
  with volume, partition and disk queries. No NAS or HDD benchmark was performed.
- FFmpeg and FFprobe: **5.1.2-full_build-www.gyan.dev**, GCC 12.1.0,
  resolved from PATH. Exact local executable paths remain only in the ignored
  `environment.json` artifact.
- Original serial baseline: commit
  `3fd6f2af724d6ecdb344fe61f0c0e5f58bd2751c`'s `VerifyVideo.py`.
  The saved pre-edit script matched that baseline commit after newline normalization; normalized
  SHA-256 `2a19b88b6f4fa1232ac189049bc6b0c7e9c69e8120ebc5a8845b91233858faef`.

| Anonymized recording | Video | Dimensions | Duration | Bytes |
| --- | --- | --- | ---: | ---: |
| Original recording | H.264 | 2560×1440 | 84.200 s | 404,463,814 |
| Encoded output A | AV1 | 2560×1440 | 84.200 s | 120,275,658 |
| Encoded output B | AV1 | 2560×1440 | 84.200 s | 120,312,178 |

Each has video and two AAC streams. Output frame scans report 5,052 video frames.
The batch corpus uses benchmark-only hard links: `clip0` = original, `clip1` = A,
`clip2` = B, `clip3` = A again. This gives **four actual file jobs but only three
unique recordings**, 765,327,308 logical selected bytes and 336.8 media seconds.
This deliberate reread/cache reuse is a limitation, not a four-recording corpus.
The original media files were left intact. Recording labels and corpus aliases
are neutral identifiers; personal filenames and local absolute paths are not
included in this summary.

For `-s`, two output hard links `source0_new`/`source1_new` map to A/B, each with a
same-stem original hard link to the H.264 recording. There are only **two source
comparison jobs**, so requested jobs 4 is capped to effective jobs 2. Selected
output bytes total 240,587,836; original reads and all repeated passes add work.

## Reproduction and methodology

The reusable runner now accepts explicitly selected files. See
[docs/benchmarking.md](../docs/benchmarking.md) for setup, source comparison,
thread matrices, optional Windows telemetry and legacy baselines. For example:

```powershell
python tools/benchmark_verify_video.py --file "media/a.mp4" "media/b.mkv" "media/c.mp4" "media/d.mkv" --jobs 1 2 4 --repeats 3 --output-dir benchmark-results/jobs
```

The original implementation used a machine-specific harness and an ignored copy
of the pre-edit verifier from commit `3fd6f2af724d6ecdb344fe61f0c0e5f58bd2751c`.
Those historical helper scripts are not a reusable interface and are not shipped.
Local artifacts under `plans/benchmark-data/` contain the expanded commands,
per-file times, signatures and samples: `baseline.jsonl`, `current.jsonl`,
`final.jsonl`, `baseline-final.jsonl`, and one `.log` per run.

The matrix was old serial, new jobs 1/2/4 with omitted threads, and jobs 2 with
explicit threads 2/8, for full checks, `-if`, and `-s`, with two repetitions per
condition. A later old-serial full-check pair assessed drift. Explicit thread
settings also ran through serial and spawned media equivalence tests.

Example expanded production calls (all four clip paths are passed explicitly):

```powershell
python -B VerifyVideo.py --file plans\benchmark-data\corpus\clip0.mp4 plans\benchmark-data\corpus\clip1.mp4 plans\benchmark-data\corpus\clip2.mp4 plans\benchmark-data\corpus\clip3.mp4 --jobs 2
python -B VerifyVideo.py --file plans\benchmark-data\corpus\clip0.mp4 plans\benchmark-data\corpus\clip1.mp4 plans\benchmark-data\corpus\clip2.mp4 plans\benchmark-data\corpus\clip3.mp4 -if --jobs 2 --decode-threads 2
python -B VerifyVideo.py --file plans\benchmark-data\corpus\source0_new.mp4 plans\benchmark-data\corpus\source1_new.mp4 -s --jobs 2 --decode-threads 8
```

CLI wall times below include interpreter startup, discovery, pool startup/cleanup,
and redirected reporting. The CLI's displayed batch time starts after discovery
and ends after worker cleanup, before the final concurrent transcript/summary.
File times are worker-local processing durations, excluding queue wait. They must
not be summed and presented as batch time.

Runs reuse the OS cache; no cache flush was attempted, and even the first baseline
is not a proven cold read. Early `current` measurements are exploratory: brief
correctness/terminal tests and lifecycle hardening overlapped parts of that series.
Later final confirmation runs use frozen production source hashes and run without
those overlapping tests. Do not interpret small differences as statistically
established wins; there are only two repeats on a normal desktop.

## Measured results

Final full-check confirmation, with decoder threads omitted:

| File jobs | Repeat 1 s | Repeat 2 s | Mean s | vs new jobs 1 | Files/min |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 98.099 | 111.742 | 104.920 | 1.000× | 2.287 |
| 2 | 80.661 | 76.184 | 78.422 | 1.338× | 3.060 |
| 4 | 82.022 | 81.810 | 81.916 | 1.281× | 2.930 |

Two jobs improved this batch by about **25.3%** relative to the new one-job mean.
Four jobs were about **4.5% slower than two**, with substantially more memory.
These are two-repeat observations, not statistically established rankings.

The old serial full-check mean was **94.477 s early** (92.328 / 96.625) and
**109.387 s late** (108.765 / 110.009). The early new serial mean was 96.468 s,
about 2.1% slower than the early baseline. The final new serial mean, 104.920 s,
was about 4.1% faster than the late old baseline. This drift also affects the old
code, so the data do **not** isolate a precise serial-overhead percentage. Process
ownership, watchers and periodic cancellation checks have costs; this desktop
series cannot separate those from changing execution conditions. No hardware or
background-load cause was established.

Exploratory flag comparisons (same short corpus and original semantics):

- `-if`: new jobs 1/2/4 means **48.552 / 36.595 / 37.791 s**; old serial mean
  51.655 s. Jobs 2 improved over the new serial path by about 24.6%.
- `-s`: new requested jobs 1/2/4 means **72.406 / 52.495 / 54.378 s**; old serial
  mean 75.366 s. Requested 4 still means only 2 effective jobs here. Its difference
  from requested 2 illustrates timing noise, not a different scheduling limit.
- Full checks, jobs 2: explicit threads 2 averaged **112.333 s**, explicit 8
  **68.579 s**, omission **71.853 s**. Lower thread requests were substantially
  slower on this corpus. Explicit 8 changes FFmpeg too, so its small advantage
  cannot be attributed to file concurrency alone or assumed to generalize.
- Jobs 2 with explicit threads 2 also slowed the `-if` and `-s` samples; the
  source-comparison pair varied 112.191 / 84.610 s while tests overlapped parts of
  that series. Treat those exploratory values with particular caution.

Sampled final full-check peak aggregate working sets were about **510 MiB / 890
MiB / 1,597 MiB** for jobs 1/2/4. Exact repeats and Python/tool CPU/read counters
remain in the local raw artifacts. More workers increased memory and aggregate CPU work.
There are no physical disk, network or per-core measurements to diagnose a
bottleneck from these results.

All original-corpus benchmark runs returned 0 and preserved ordered per-file
verdicts, error/warning counts, and diagnostic text. Full checks warn about the
existing A/V duration differences; `-if` additionally warns that FFmpeg was
skipped; source comparisons preserve inherited-audio and frame-rounding warnings.
Warnings are not counted as corrupt media. The local analysis asserted these
comparisons and verified final-series source hashes, allowing only removal of one
extra renderer EOF newline after measurement.

Commit preparation subsequently fixed a cancellation-only collector race: a
worker exit collected after cancellation is interrupted work, not a checked-file
failure. A deterministic regression test covers it. The completed-run timing
path and fixed resource settings are unchanged; this cancellation fix was tested
but the throughput matrix was not repeated after it.

Manual takeaway: compare one and two jobs on the user's real storage first.
Four workers did not improve the final measured batch. Keep the agreed defaults;
do not extrapolate a throughput guarantee or automatic setting from this corpus.

## Correctness and lifecycle validation

The original suite passed **67 tests** before implementation. The expanded suite
passed **93 tests**, including a run with `ResourceWarning` promoted to an error
(56.957 seconds; no resource warnings in the log). It
adds real Windows-spawn scheduling/lifecycle tests, bounded progress/dead-lock
isolation, source/strict/skip combinations, actual media equivalence and renderer
tests. The implementation validation log is `validation.log` (local only).

Commit preparation added the portable benchmark runner, its integration tests,
and the cancellation-race regression. The complete suite then passed **100 tests
in 35.335 seconds** with `ResourceWarning` promoted to an error. The local log is
`commit-validation.log`; raw validation logs are not committed.

Coverage includes immediate refill while job 1 is gated, at most K assigned jobs,
ordered results/report, same-name identities, no-input/single-input routing,
invalid positive-integer arguments, blocked stdout, child/grandchild cleanup,
worker crash with a live tool, startup failure, ownership-registration rejection,
unresponsive-worker force cleanup while an unrelated process survives, timing-loop
cancellation, late-progress rejection and final snapshot reconciliation.

Media tests compare serial/spawn warnings and errors in order, normalizing only
FFmpeg's ASLR-dependent pointer addresses. They include clean, corrupt, truncated,
unreadable, audio-first, multiple audio/video streams, video-only and bad-original
inputs. Existing tests retain VFR, strict timestamps, grid rounding, inherited
audio, zero-exit decoder errors, subtitles and cover-art exclusions, plus short
10-bit HEVC/AV1 fixtures.

The real-event Windows ConPTY test at 80×24 exercised Home, wheel scrolling,
scrollbar click/drag/release, End/follow, Q/exit 130, completion details and terminal
restoration. See `terminal-smoke.md` (local only).
Changed-row writes, tiny layouts, resize, unknown totals, native Windows input
records and SGR parsing also have deterministic tests.

Python 3.7 grammar parsing and `git diff --check` were checked. Only Python 3.13.2
and FFmpeg/FFprobe 5.1.2 were actually executed; a grammar check is not a 3.7 runtime
certification. POSIX process-group/liveness paths were implemented but could not
be executed on this Windows host.

## Measurement limitations

- The measured corpus is the original short recording set; additional files
  belonging to another agent are excluded.
  No representative hours-long files, NAS, slow-storage load, large unique batch
  or broad codec benchmark was measured.
  Repeated hard links favor cached reads and are not independent physical data.
- Windows process-tree sampling runs approximately every 0.2 s. CPU and logical
  read totals are **lower bounds**: short-lived tools and work after the last
  sample can be missed. Sampled working-set peaks can miss shorter peaks and sum
  shared pages across processes. The early old baseline has wall times but no
  process telemetry.
- Logical process read counters include cached reads and pipes. They are not
  physical disk throughput or NAS traffic. Physical disk/network counters,
  per-core utilization, cache hit rates and direct I/O-contention measurements
  were not captured. No bottleneck diagnosis is claimed.
- Compact final payloads and active-worker memory were exercised, but sustained
  multi-million-frame timestamp-array release/scaling was not measured. Long
  source comparisons can retain both arrays and temporary Python interval lists.
  Severely damaged files can also grow temporary stderr files on disk.
- Physical mouse/drag behavior and visual flicker in other terminal hosts,
  other Windows Job Object nesting policies, Python 3.7 itself, POSIX cleanup and
  abrupt parent/OS termination need broader real-world validation. Tested host
  ownership registration succeeds; injected rejection fails safely.

Defaults remain fixed irrespective of benchmark winners. These measurements are
evidence for the user's manual workload comparisons, not a runtime tuning policy.
