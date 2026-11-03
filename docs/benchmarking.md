# Benchmarking VerifyVideo on a different computer

Use this guide when a user asks which `--jobs` / `--decode-threads` settings suit
their machine and storage. It is also the handoff for a future coding agent.
Measurements are manual experiments, not a reason to change the program defaults.

## Fixed behavior

- Default `--jobs` is **1**.
- Omitted `--decode-threads`: FFprobe frame scans request **8**; FFmpeg uses its
  own default. Metadata probing stays unchanged.
- Explicit `--decode-threads 8` also overrides FFmpeg's input video decoders, so
  it is not equivalent to omitting the flag. Thread requests are not CPU limits.
- Checks within a file stay sequential. Effective jobs are capped by input count;
  four requested jobs on two files cannot measure four active file jobs.
- Do not add CPU/storage detection, calculated thread budgets, startup benchmarks
  or adaptive tuning. Recommend an explicit invocation for that user's workload.

## Agree on inputs and scope

Use only files explicitly selected for this benchmark. Do not pick up new files
that appear in the checkout: they may belong to another person or agent. Confirm
which storage and workload matter, and a reasonable run-time budget before a long
matrix. Existing permission to benchmark those inputs is sufficient; routine
comparisons do not need repeated approval.

Choose several distinct real recordings, ideally at least four for four-job
comparisons. Include representative sizes, codecs, resolutions, audio tracks and
long files when available. Source comparisons need actual `name_new.ext` outputs
with their originals beside them; select only the outputs with `-s`. Keep media
unchanged throughout. Do not manufacture source matches, overwrite media, flush
shared caches, create large copies, or use another agent's files without consent.
Disclose any duplicated inputs/hard links and how they favor cache reuse.

Record Python, FFmpeg and FFprobe versions and executable paths; CPU model, RAM,
OS, storage/device/share, input sizes/durations/codecs and relevant background
activity. Check the repository's executable lookup order (beside the verifier,
its `ffmpeg` subfolder, then PATH). Run the tests when assessing a code change;
do not overlap tests, encoding or another benchmark with timing runs.

## Portable runner

`tools/benchmark_verify_video.py` uses only the standard library and the current
checkout's process-ownership helper. Run it from any working directory with
explicit paths. It creates no media copies, changes no settings, and keeps each
invocation's logs/failure report inside a new results directory. Output locations
must be new or empty, so previous measurements cannot be silently overwritten.
`benchmark-results/` is ignored by Git; other output locations must also stay out
of commits. Local artifacts include full paths and diagnostics; review them
before sharing.

Start with fixed default decoder behavior and one versus two jobs. These relative
paths are placeholders: replace them with the user's agreed corpus. Use at least
three repeats when practical.

```powershell
python tools/benchmark_verify_video.py --file "media/a.mp4" "media/b.mkv" "media/c.mp4" "media/d.mkv" --jobs 1 2 --repeats 3 --output-dir benchmark-results/jobs-1-2
```

If the comparison warrants it, measure four jobs on the same corpus:

```powershell
python tools/benchmark_verify_video.py --file "media/a.mp4" "media/b.mkv" "media/c.mp4" "media/d.mkv" --jobs 1 2 4 --repeats 3 --output-dir benchmark-results/jobs-1-2-4
```

Then compare manual decoder settings, preferably on a small set of promising job
counts. `default` explicitly means omit the production thread flag:

```powershell
python tools/benchmark_verify_video.py --file "media/a.mp4" "media/b.mkv" "media/c.mp4" "media/d.mkv" --jobs 1 2 --decode-threads default 2 4 8 --repeats 3 --output-dir benchmark-results/decoder-settings
```

The runner keeps production flags fixed within one series. Measure the actual
workflow separately: append `--ignore-ffmpeg` for FFprobe-only checks, or use
`--staxrip` / `--strict-timing` as needed. Do not call the skip-FFmpeg workflow
equivalent to full verification; its warning and reduced work are intentional.

```powershell
python tools/benchmark_verify_video.py --file "media/a_new.mkv" "media/b_new.mp4" --staxrip --jobs 1 2 --repeats 3 --output-dir benchmark-results/source-comparison
```

`--sample-resources` optionally records Windows process-tree CPU, memory and logical
read counters, sampled around every 0.2 seconds. Omit it for portable wall timings.
Keep sampling consistent across conditions and consider a separate telemetry pass:
sampling itself costs work. CPU/read totals are lower bounds because short-lived
processes can be missed; sampled summed working sets can miss peaks or count
shared pages repeatedly. Logical reads include cached reads/pipes and do not
measure physical disk or network bandwidth. Per-core utilization, physical I/O
and NAS contention need separate measurements; mark unavailable metrics honestly.

The matrix runs in the listed order. For finalists, repeat with reversed job order
in another directory, and repeat the one-job reference near the end. This helps
expose thermal, cache and desktop-load drift. Do not claim a first read is cold
unless cache state was actually controlled.

For a pre-concurrency verifier, keep a separate baseline checkout and all files it
needs; ensure identical tool versions and media. The old script cannot accept
the new flags:

```powershell
python tools/benchmark_verify_video.py --script "baseline/VerifyVideo.py" --legacy-serial --jobs 1 --file "media/a.mp4" "media/b.mkv" --repeats 3 --output-dir benchmark-results/old-serial
```

Use Ctrl+C to cancel. The runner gives the child a cancellation opportunity and
then uses scoped process ownership for cleanup; it never kills tools by executable
name. Partial series are marked `INCOMPLETE.txt` and must not support a completed
benchmark claim. Windows ownership was exercised locally; POSIX cleanup still
needs platform validation. Preserve other agents' processes and files.

## Inspect correctness before choosing settings

Read `summary.md`, `manifest.json`, `results.jsonl` and each run's `stdout.log`.
The manifest captures source hashes and selected input sizes/modification times.
The runner stops if those change. Keep source-comparison originals unchanged too.

The runner compares completed per-file verdicts, warning/error counts, ordered
diagnostic text and exit code against the first requested configuration. Only
timings and FFmpeg's process-specific context pointer addresses are excluded.
Exit 1 can be an expected media failure: a damaged file must keep failing under
every setting. Consistent failed-media verdicts are valid benchmark observations;
incomplete checks, process failures or changed verdicts are not candidates for a
speed recommendation. An incomplete or mismatched series makes the runner exit 1.
Keep healthy inputs in the timing corpus too: an early rejection of an unreadable
file measures failure handling, not full verification throughput. The runner
rejects recognizable worker/tool-launch failures; still review every diagnostic.
Comparisons across separate series/versions still require the agent to inspect
their signatures and flags; the runner does not silently combine them.

Verify returned file counts and effective jobs, especially after deduplication.
If a result changes, investigate before continuing. Do not suppress diagnostics,
retry media until it passes, weaken checks, or select the fastest incomplete run.
These checks detect regressions; they do not prove picture content or audio
payload equivalence.

## Report and hand off

Give exact commands, corpus/environment, requested/effective jobs, omitted versus
explicit thread settings, all repeated wall times, median/range, throughput and
speedups relative to a clearly identified reference. CLI wall time includes
interpreter startup, discovery, worker setup/cleanup and redirected reporting;
the verifier's displayed batch time uses a narrower boundary. File processing
times exclude queue wait; their sum is not elapsed batch time.

Prefer a lower worker count when an apparent gain is smaller than the observed
spread or costs substantially more RAM/competing I/O. Report slower configurations
as well as faster ones. Recheck finalists under normal background load, and on
the user's actual NAS/HDD/SSD when relevant. A local cached run establishes no
NAS performance claim. State limitations for unavailable codecs, long files,
storage types, operating systems, resource metrics and sample sizes.

Finish with an explicit command the user can try and why it fits the measured
tradeoff. Do not update fixed defaults or install automatic tuning. If asked to
commit benchmark work, include reusable tools, tests, instructions and a reviewed
summary; exclude media, raw logs/JSON, environment dumps and machine-specific
temporary harnesses. Before committing, audit every included file and the commit
diff for real recording titles, personal filenames, usernames, local absolute
paths, share names and credentials. Use neutral recording labels and relative
example paths. Keep hardware/software characteristics needed to interpret results;
check Git author/committer metadata separately if the user wants it anonymized.
The historical implementation summary is in
[plans/concurrency-benchmark-results.md](../plans/concurrency-benchmark-results.md);
its measurements are examples, not universal recommendations.
