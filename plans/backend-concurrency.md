# VerifyVideo backend concurrency plan

Date: 2026-11-03

Status: implemented. This document preserves the approved design and acceptance
criteria used for the implementation. See [README.md](../README.md) for current
behavior, [the measurement summary](concurrency-benchmark-results.md), and
[the benchmarking guide](../docs/benchmarking.md) for future agent instructions.
The preview paths below refer to local design experiments; production uses
[verification_ui.py](../verification_ui.py) and does not require those previews.
The fixed defaults below remain requirements, not benchmark-dependent choices.

## 1. Objective and decision record

Improve batch throughput when verification is limited by CPU work, a decoder that
does not use many cores, or periods waiting for local/NAS reads. Concurrency must
remain bounded: additional workers can make a saturated disk or network slower.
The goal is lower total batch time without changing verification outcomes.

### Agreed direction

- Parallelize complete file-verification jobs.
- Keep checks within each job sequential. Do not initially overlap that job's
  FFprobe and FFmpeg passes, its individual stream scans, or its original scan.
- A worker that finishes takes the next queued file immediately, even if an
  earlier file is still processing or its transcript has not been displayed.
- Use separate Python worker processes so Python frame parsing and timestamp
  processing for different files can also run on different CPU cores. Keep a
  direct serial path when the effective file-worker count is one.
- Keep the initial file numbering, final grouped transcript, results table, and
  checked-file failure list deterministic and consistent with the initial list.
- Preserve existing checks, thresholds, warning/error meanings, and flags,
  including `-if`, `-s`, and `--strict-timing`.
- Expose per-file and per-stage progress independently of the UI. The UI should
  be able to show completed details immediately and animate running work.
- Default to `--jobs 1`. Omitted `--jobs` and explicit `--jobs 1` retain the
  current serial terminal appearance, without a live panel or scrollable app UI.
- With `--decode-threads` omitted, preserve current threading behavior: FFprobe
  frame scans request `-threads 8`; FFmpeg receives no threading override.
  The user explicitly chose to retain FFprobe's existing eight-thread default.
- Do not calculate thread budgets, detect storage/CPU bottlenecks, automatically
  select worker counts, or tune resources at startup or during a run.
- For concurrent interactive runs, follow
  [concurrency_preview_v5.py](../previews/concurrency_preview_v5.py): a scrollable
  ordered transcript, pinned live panel, mouse scrollbar, bars/spinners in both
  sections, and the full-width divider above the live panel.
- Benchmark during implementation and report measurements and limitations. The
  user will do further real-world testing; benchmarks do not change the defaults
  or add automatic behavior to the production script.

### Implementation constraints and remaining engineering choices

- Add `--jobs N` for the maximum concurrent file-verification jobs.
- Add `--decode-threads N` to control supported decoder thread settings.
- A thread override is opt-in and independent of `--jobs`; it must work in both
  serial and concurrent modes.
- Keep dependencies in the Python standard library and preserve the README's
  Python 3.7+ / FFmpeg and FFprobe 5.1+ compatibility target unless a change is
  explicitly decided later.

Only the process-pool implementation, IPC details, and platform cleanup helpers
remain engineering choices. There is no application-calculated default thread
budget to implement, and the default file-worker count is fixed at one.

## 2. Current behavior and likely bottlenecks

The current `VerifyVideo.py` discovers and deduplicates inputs, applies
`files = sorted(files)`, prints the numbered selection, and calls `verify()` for
each file in a serial loop. Results are appended to `checked` in that same order.

Within `verify()`, the usual sequence is:

1. FFprobe metadata and stream discovery.
2. A separate FFprobe decoded-frame scan for each selected A/V stream, in stream
   order, followed by timestamp processing.
3. An independent FFmpeg decode of all selected A/V streams, unless `-if` is set.
4. With `-s`, locate and inspect the original's A/V streams, then compare video
   frame counts and timestamps.
5. Check output A/V durations and offsets, using original timing context when
   available, then return a verdict.

Preserve the current early-return and error-continuation rules: for example, an
output with no usable motion-video streams does not become a successful job or
gain an unconditional FFmpeg pass merely because scheduling has changed.

Important performance facts from the code:

- `scan_stream()` runs FFprobe with `-show_frames`: this is a decoding scan,
  not just a cheap metadata lookup.
- It currently requests eight decoder threads for each scan. That request does
  not establish actual CPU utilization; codec/media support and other work can
  limit scaling. Keep this eight-thread default in both serial and concurrent
  runs unless the user explicitly supplies a different thread value.
- `ffmpeg_check()` does not currently set an explicit input decoder-thread
  budget. Preserve that behavior unless `--decode-threads N` is explicitly set.
- Each stream scan launches a new process and reads/demuxes the input again.
  Adding jobs increases competing reads; it does not remove repeated passes.
- `Stream.add_frame()`, `Stream.finish()`, timestamp checks, and original
  comparison also perform Python CPU work. Simply adding Python threads would
  still share the interpreter lock on the currently supported ordinary runtimes.
- Video timestamp arrays and temporary interval lists consume memory. With `-s`,
  output and original timing data can coexist in a worker.
- `run_streaming()` already consumes stdout incrementally and uses temporary
  stderr files to avoid pipe deadlocks. Retain those protections.

No throughput or utilization measurements have been collected as part of these
UI previews. Do not promise a particular speedup.

## 3. Execution model

Select execution and presentation after discovering valid inputs:

```text
requested jobs = --jobs value, default 1
effective jobs = min(requested jobs, valid selected file count)

0 files: existing no-input/preflight-failure handling; no workers or live panel
1 effective job: direct serial verification + current classic terminal output
2+ effective jobs: bounded Python worker pool + V5 UI on capable terminals
redirected/non-ANSI output: plain logs, retaining the chosen execution concurrency
```

An explicit `--jobs 4` with only one selected file uses the serial appearance.
This cap is input-count validation, not resource detection or automatic tuning.
The serial path should share the verification, subprocess cleanup and command
construction logic, rather than maintain a different set of checks. It need not
create a Python worker pool merely to execute one job.

For concurrent execution:

```text
Parent/controller
  discover -> freeze ordered jobs -> schedule at most K jobs
       |                |                |
       v                v                v
   Python worker 1  Python worker 2  ... worker K
   one job at once  one job at once
       |                |
   one tool child   one tool child
   at a time        at a time
       |                |
       +-- progress, stage records, diagnostics, compact final results --> parent

Parent: aggregate by original index -> render/report -> write one failure report
```

Different jobs can run different kinds of checks simultaneously. For example,
worker 1 may run FFmpeg for file A while worker 2 runs FFprobe for file B. The
restriction is that each individual verification job runs only one tool child at
a time. An original comparison is part of its output's job, not an extra worker.

This gives an upper bound of K active verification tool processes for K workers.
It is not a strict bound on tool threads, RAM use, or bytes being read. The parent
and Python worker processes exist in addition to those tool processes.

### Worker and pool lifecycle

- Use a spawn-compatible design with importable, top-level worker entry points
  and serializable inputs/outputs. Keep process creation under the main guard.
- A worker owns its current file's mutable `Stream` objects and `Result`.
  Workers do not share or mutate one result object.
- Resolve tool locations and run configuration in the parent and pass stable
  values to workers. Preserve the existing executable lookup precedence.
- Prefer a fixed reusable pool to creating a new Python interpreter for every
  file. Keep only K assigned/in-flight jobs rather than eagerly submitting the
  entire selection as an unbounded set of future objects.
- Return compact results and stage records. Do not send timestamp arrays or raw
  per-frame output back to the parent.
- Workers never write terminal output or `corrupted_files.txt`.

A managed pool of explicit spawned workers is a reasonable initial implementation
because child-process ownership and cancellation must be controllable. A
high-level executor is also acceptable if it can meet the same lifecycle and
cleanup requirements on the minimum supported Python version. Do not depend on
recent executor-only APIs that would silently raise that minimum version.

## 4. Scheduling and resource controls

### Required CLI contract

| Setting | Meaning | Required default |
| --- | --- | --- |
| `--jobs N` | Positive integer limiting active file jobs | `1`; preserve classic serial appearance |
| `--decode-threads N` | Positive integer requesting threads for supported input decoders | Unset/`None` preserves existing behavior: FFprobe frame scans use 8; FFmpeg uses its own default |

Cap the actual worker count at the number of valid selected files. Validate
arguments before launching workers. Keep the same scheduling policy with and
without `-if`; that flag only removes the independent FFmpeg pass and retains
its existing warning.

Do not infer jobs from CPU count, current load, drive type, or NAS paths. The
previews' two-worker defaults are demonstration settings and must not become the
production default. Invalid values such as zero or negative integers should fail
argument validation; an omitted thread setting is distinct from passing zero.

### Preserved defaults and explicit decoder-thread override

Required behavior:

```text
--decode-threads omitted -> FFprobe frame scans: -threads 8; FFmpeg: no override
--decode-threads N       -> request N for supported input video decoders
```

Remove the previous `min(8, max(1, CPU_count // jobs))` proposal entirely from the
implementation. The default eight-thread FFprobe setting is fixed, not calculated
from CPU count or worker count. Do not substitute `-threads 0` or add a default
FFmpeg thread override. Keep metadata probing's current settings; the eight-thread
default applies to FFprobe frame scans, including original-file scans. All other
verification options and stream maps still apply normally.

- If explicitly supplied, apply the setting consistently to output and original
  FFprobe frame scans and the supported FFmpeg input video decoders. Keep it
  consistent when `-if` removes FFmpeg, and when only one job runs.
- Verify FFmpeg input-option placement and per-stream behavior against the
  supported versions before implementing it. Setting only output encoder threads
  would not control the intended decode work.
- Do not force codec-specific options onto decoders that do not support them.
- Never adjust the explicit value based on other workers starting or finishing.
- Record requested/effective jobs and either the explicit thread value or
  "FFprobe: 8; FFmpeg: tool default" in appropriate diagnostics/benchmark metadata.
  Do not add unnecessary output to the existing serial layout.

The explicit setting is not a strict total-thread or core limit. Multiple
streams, decoder-specific behavior, Python work and tool internals can add work;
some decoders may not use all requested threads. With the flag omitted, FFprobe
still requests eight threads per scanned stream and FFmpeg uses its own defaults,
regardless of how many file workers were explicitly requested. Do not silently
reduce those settings when more files run concurrently.

### Manual workload comparisons, not runtime detection

| Workload | First comparison |
| --- | --- |
| Busy HDD or slow NAS | 1 versus 2 workers |
| Local SSD | 1 versus 2 workers; then 3-4 if resources remain available |
| Fast NAS with spare bandwidth | 1 versus 2 workers; increase only if batch time improves |
| CPU already saturated by one decoder | Compare fewer threads across two jobs against one job |

Do not auto-classify all network paths as slow or all local paths as fast. Do not
set the worker count equal to the CPU count by default. There is no automatic
storage benchmarking or dynamic concurrency tuner in the initial scope.

## 5. Stable job identities and deterministic results

Freeze the complete valid selection before starting verification. Preserve the
current `sorted(files)` path ordering; do not introduce natural sorting,
case-folded sorting, or CLI argument order as an incidental part of this work.

Each job should carry:

- Original zero-based index plus its one-based display number.
- Resolved selected path and parent-computed display name.
- Existing verification flags and resolved decoder settings.

Maintain an indexed results array or equivalent mapping. A finished file writes
to its own slot, rather than being appended in completion order. Final reporting
iterates the original list. Progress events and stored records use the same job
identity; a filename alone is insufficient because different folders can contain
the same filename.

Keep preflight selection problems deterministic as well. Preserve their existing
discovery/input order, followed by checked-file failures in scan-list order, with
the existing deduplication behavior. Do not let worker completion timing reorder
`corrupted_files.txt` or the displayed failure list.

Within each file, retain logical stage order, warning/error ordering, and stream
order. Source and output stages with the same stream index must remain distinct.

### Refill without waiting for display

```text
Queue: 1, 2, 3, 4, 5             K = 2
Start: 1 and 2
2 finishes -> store result[2] -> immediately start 3
3 finishes -> store result[3] -> immediately start 4
1 finishes -> store result[1] -> immediately start the next queued job
Final transcript/table: 1, 2, 3, 4, 5
```

The controller must collect whichever jobs complete, rather than wait on job 1
before noticing job 2. Ordered rendering is a reporting concern, never a worker
dispatch barrier.

## 6. Progress and result contract, independent of the UI

Separate verification state from formatted strings. Existing `Progress` and
`Reporter` currently write directly to a shared stream; parallel workers must
instead emit structured records to one parent-owned aggregator.

### Suggested event fields

| Field | Purpose |
| --- | --- |
| `job_index` | Stable link to the initial file list |
| `sequence` | Increasing event sequence within that job |
| `kind` | Job/stage start, progress, stage finish, diagnostic, job finish |
| `stage_id` / `stage_order` | Stable stage identity and logical ordering |
| `role` | Output or original |
| `tool`, `stream_index`, `stream_kind` | Identify the check without parsing a label |
| `elapsed_seconds` | Worker-measured stage/job elapsed duration |
| `position_seconds`, `total_seconds`, `frame_count` | Optional progress measurements |
| `state` | Running, done, warning, error, or skipped, as appropriate |
| `details` / diagnostic identifiers | Structured stage details and deduplicated issues |

Unknown totals remain unknown; do not invent percentages. Existing media-time
percentages describe the current stage and stay below 100% until that stage
finishes. A skipped stage is explicit, not a misleading completed decode.

Spinners are a parent-side animation of running state; do not send an event for
every spinner frame. Emit progress at most about four times a second per active
stage, not for every decoded frame. Stage transitions and newly available
diagnostics should be delivered promptly even between progress ticks.

### Transport and backpressure

- Parent-side event collection must continue regardless of UI scrolling, pause,
  rendering cost, or the position of an earlier file in the transcript.
- Keep progress transport bounded. Coalesce or drop obsolete progress updates;
  the newest state matters more than replaying every intermediate percentage.
- Lifecycle events, diagnostics, and final results are reliable. Never drop a
  failure or completion because a progress channel is full.
- A practical split is a bounded best-effort progress channel plus reliable
  control/result transport, with the parent draining both regularly.
- Include complete final stage records and diagnostics in the job result so the
  aggregator can reconcile its state before final reporting.
- Avoid per-frame IPC, timestamp-array serialization, a manager proxy lookup for
  each frame, and blocking worker writes tied to renderer refreshes.
- Define how late progress is rejected after a stage/job is terminal. It must
  never turn a completed row back into a running row.

### Suggested final result payload

Return the job index, existing `Result` warnings/errors and verdict, processing
elapsed time, stable stage records, and any explicit operational/cancellation
state. Results and events must be serializable and free of open handles,
subprocess objects, reporter instances, and timestamp arrays.

Keep the event contract separate from rendering, while implementing the selected
presentation behavior below. Immediate completion data must be available even
when the final grouped transcript will be printed later in file order.

### Required production UI routing and V5 layout

For an effective worker count of one, preserve the current `Reporter`/`Progress`
look: numbered file headings, aligned per-stage rows, current-stage spinner and
bar, diagnostics, verdict, and final summary. Do not enter an alternate screen,
capture mouse input, add a live panel, or introduce application scrolling. This
applies to the omitted `--jobs` default and explicit `--jobs 1`, including `-if`.

For two or more effective workers on a capable interactive terminal, use
[concurrency_preview_v5.py](../previews/concurrency_preview_v5.py) as the visual
and interaction reference. Read its imported shared/pinned preview modules too:
V5 is a small extension of those implementations rather than a standalone UI.

Required concurrent behavior:

- Ordered, scrollable full transcript above a pinned compact live panel.
- Full-width divider labelled "Live progress" separating the two sections.
- Running-stage spinners and media-time progress bars in both the transcript
  and live panel; unknown totals retain honest indeterminate progress.
- Immediate completed stage details and diagnostics, even while an earlier file
  remains active; completed files must not be reduced to an opaque waiting badge.
- Mouse wheel over the transcript, clickable/draggable scrollbar, and the V5
  keyboard navigation (arrows, Page Up/Down, Home/End/follow).
- Only changed rows rewritten in batched output, to avoid whole-panel flashing.
- Scrolling or browsing affects presentation only; it must not block workers,
  IPC collection, queue refill or cancellation.
- Restore terminal/input modes and cursor visibility on normal exit, cancellation
  and failure. Print the final full transcript and results in scan order into
  ordinary terminal scrollback.

The previews use simulated time and mock results. Replace those with real backend
state; do not ship demo filenames, mock diagnostics, preview version labels or
simulation speed/start/pause CLI flags. The preview's Space-to-pause simulation
is not a requirement to implement pausing verification. Q/Ctrl+C must use the
real cancellation/cleanup path if provided as stop controls.

Production code should not import runtime implementation from `previews/`.
Adapt/extract the necessary production renderer/input support while preserving
the reference previews for comparison. Keep standard-library compatibility.

Redirected output and terminals without the necessary capabilities get readable
plain logs with the existing colour/encoding fallbacks, not mouse or cursor
escape sequences. This fallback does not change the requested concurrency.

## 7. Timing and memory accounting

- File time starts when its worker begins verification and ends after the last
  applicable check. Exclude queue wait and presentation delays.
- Stage time measures that stage's actual processing interval.
- Batch time is wall-clock elapsed for verification, including worker startup
  and final worker cleanup. Preserve/document the boundary relative to discovery
  and rendering; do not present the sum of file times as batch elapsed time.
- Measure durations within the process that owns the work and return durations,
  rather than relying on cross-process clock-origin arithmetic.
- Retain per-frame video timestamps only inside the owning worker for as long as
  its existing checks need them. Original and output data may both be required.
- Raw double timestamp storage alone is approximately eight bytes per video
  frame; Python interval lists and other structures can add substantially more.
  Size tests must include long/high-frame-rate media and `-s` workloads.
- Bound active heavy data by the worker count. Completed out-of-order jobs must
  retain compact results, not their full decoded timing arrays.
- Keep current streaming stdout behavior and bounded reported diagnostics.
  Current temporary stderr files can still grow on severely damaged media;
  concurrency increases simultaneous writers. Note this in resource measurements
  and do not replace it with unbounded captured stderr in RAM.

## 8. Cancellation and subprocess ownership

Cancellation must stop active tools as well as Python workers. Cancelling a
queued future or killing only a worker can leave FFmpeg/FFprobe running.

Required behavior:

1. Parent receives Ctrl+C, marks the run as cancelling, and stops assigning jobs.
2. Unstarted jobs remain cancelled/not checked; they do not become passes or
   corrupt-media failures.
3. Workers receive a shared cancellation signal and stop before launching any
   further stage.
4. The owner terminates its active tool, escalates to a kill after a short bounded
   grace period, reaps it, closes stdout and diagnostic files, and returns any
   available partial records.
5. Parent waits for bounded worker shutdown and force-cleans any survivors and
   their owned tool processes before returning control to the user.
6. Final reporting distinguishes checked results from interrupted/unstarted work.
   A cancelled batch must not print an all-files-checked success message.

Proposed interrupted-run exit code: 130. Preserve ordinary exit codes 0 for
warnings-only/success and 1 for verification or selection failures on a completed
run. The cancellation exit policy must be documented when implemented.

### Important implementation details

- Track every subprocess, including metadata FFprobe. The current metadata
  `subprocess.run()` path needs to participate in cancellation and ownership too.
- A worker can be blocked reading stdout from a stalled disk or tool. Checking a
  flag only inside the stdout-consumer loop is insufficient. Use a cancellation
  watcher or equivalent mechanism able to terminate the registered child while
  the read is blocked.
- Synchronize child registration with cancellation so a race cannot launch a new
  untracked tool after cancellation has already been requested.
- Check cancellation periodically during long Python frame/timestamp/comparison
  loops too, using a cheap throttled/local check rather than adding IPC to every
  frame. Parent-side force cleanup remains the fallback for unresponsive work.
- Parent collection and shutdown must continue while partial results/events are
  being drained. Do not block forever on a full queue during shutdown.
- Avoid joining IPC feeder threads or waiting indefinitely on an executor before
  terminating a hung child.
- Use a tested platform-specific ownership strategy for force cleanup. On
  Windows, evaluate parent-owned per-worker Job Objects with kill-on-close and a
  startup handshake before the worker may spawn children. On POSIX, evaluate
  owned process groups plus explicit cleanup and worker/parent liveness handling.
  Validate host/nested-job constraints and a safe fallback before relying on it.
- A second Ctrl+C may request immediate cleanup escalation; it must not bypass
  handle cleanup or kill unrelated tool instances belonging to the user.

Cancellation, metadata hangs, worker crashes, and orphan prevention are completion
criteria, not optional follow-up work.

## 9. Errors, isolation, and report ownership

Expected per-file failures continue to produce that file's result while other
files run. Keep output decode errors fatal, original inspection problems warning
as currently defined, and decoder diagnostics significant even on exit code zero.
Do not silently retry media failures or convert incomplete checks into passes.

An unexpected worker exception must become an identified operational failure for
its assigned job, not disappear or leave the parent waiting forever. Clean up its
child before replacing the worker or continuing remaining jobs. Do not re-run its
partially completed job automatically in the initial implementation. A systemic
pool/startup failure should stop new dispatch, clean up, and report incomplete
work explicitly.

Only the parent aggregates counters, returns the batch exit code, and writes the
failure report once. Preserve the current report location (current working
directory), filename-only contents, and write-only-on-failures behavior. Do not
delete an older report on a successful run as an unrelated change. During an
interrupted run, if a report is written for completed failures, clearly mark the
overall run incomplete and exclude cancelled/unstarted files from corruption
claims. Never have multiple workers race to write the report.

## 10. Suggested changes by current code area

| Area | Intended change |
| --- | --- |
| `main()` | Keep discovery/selection; default jobs to 1; route serial versus concurrent execution/UI; add ordered jobs, bounded dispatch, indexed collection and parent-only final reporting |
| `verify()` | Keep check order and verdict semantics; accept execution settings, an event sink and cancellation context |
| `inspect_video()` | Keep stream order and source/output severity; emit structured stage records |
| `scan_stream()` | Keep `-threads 8` as the default; replace 8 with the supplied value when explicitly requested; keep frame parsing and timestamps local |
| `ffmpeg_check()` | Add supported input decoder overrides only when explicitly requested; retain maps, timing options and independent error handling |
| `probe_metadata()` / `run_streaming()` | Centralize tracked subprocess ownership, incremental I/O, cancellation and cleanup |
| `Progress` / `Reporter` | Preserve the current single-job layout; separate event emission from concurrent rendering; implement the V5 layout and plain-output fallback |
| Tests and README | Add concurrency/lifecycle coverage and document resource settings after behavior is implemented |

Keep pure verification logic such as timestamp tolerances, quantization-grid
acceptance, source pairing, and inherited-audio checks unchanged. Avoid mixing
algorithm changes into the concurrency patch.

## 11. Implementation sequence

1. **Capture a baseline.** Run existing tests, record representative serial
   results and timings, and identify the actual Python/tool versions in use.
2. **Define explicit controls and preserve serial output.** Add `--jobs` with
   default 1 and optional `--decode-threads` with default unset. Preserve FFprobe's
   eight-thread frame-scan default and FFmpeg's unmodified threading default.
   Introduce job/stage records and event sinks
   while retaining direct serial execution and its current appearance. Verify
   results, stage order, diagnostics and unchanged default tool threading.
3. **Centralize tool ownership.** Make metadata and streaming tool calls tracked
   and cancellable. Verify blocked-read cancellation and cleanup before scaling.
4. **Add bounded process workers.** Implement spawn-compatible entry points,
   immediate queue refill, indexed aggregation and parent-only report writes.
5. **Validate explicit thread settings.** Check decoder options and supported
   versions. Assert the omitted setting preserves FFprobe 8 / FFmpeg default,
   while explicit values work in both serial and concurrent modes. No budget
   formula, bottleneck detection or runtime tuner is to be implemented.
6. **Exercise failures and backpressure.** Test worker crashes, cancellation,
   parent collection under slow rendering, and high progress/diagnostic volume.
7. **Implement and verify the selected UI.** Use V5 for concurrent interactive
   runs, the existing appearance for one effective job, and plain output where
   needed. Verify mouse/keyboard scrolling, live/transcript bars and spinners,
   immediate details, flicker reduction and terminal cleanup with real events.
8. **Benchmark and report.** Compare one/two/four jobs, preserved default threading and
   selected explicit thread values on available real media. Publish results,
   environment and limitations; do not change the agreed defaults. The user
   will perform further real-world/local/NAS testing.

Each step must leave a reviewable serial behavior path. Reuse the current checkout
unless implementation work later needs isolation; this document does not request
a branch, worktree, commit, or deployment.

## 12. Validation plan

Use the existing standard-library test suite and media integration fixtures as the
semantic baseline. Test process boundaries with importable fake workers and tiny
fake tool programs where appropriate; parent-side mocks alone do not prove that
spawned workers use the correct code or clean up descendants.

### Scheduling and ordering

- Make job 1 wait on a gate while job 2 finishes; prove job 3 starts before the
  gate is released. Use synchronization rather than brittle sleep-only timing.
- Force reversed/mixed completion order and verify original numbering, final
  transcript/result order, failure-list order and failure-report contents.
- Measure maximum concurrent jobs/tool children and assert it never exceeds K.
- Assert each job has at most one active tool child, including original scans.
- Cover zero files, one file, fewer files than jobs, `--jobs 1`, repeated selections,
  same filenames in different folders, invalid selections, and recursive scans.

### Verification equivalence

- Compare serial and concurrent results on clean, corrupt, unreadable, truncated,
  video-only, audio-first and multiple-A/V-stream inputs.
- Exercise `-if`, `-s`, `--strict-timing`, and their combinations.
- Retain tests for source errors as warnings, zero-exit decoder errors as failures,
  missing/ambiguous originals, variable frame rates, timestamp rounding,
  duplicates/backwards timestamps, and inherited versus new audio discrepancies.
- Verify that subtitle and attached-picture exclusions remain intact.
- Check decoder settings in commands for output and original FFprobe and for
  FFmpeg input decoders, including the minimum supported tool versions.
- Test omission explicitly: FFprobe frame scans retain `-threads 8`, FFmpeg has
  no thread override, and metadata settings remain unchanged. No application-
  calculated value is added when `--decode-threads` is absent. Test explicit
  overrides with `--jobs 1`, `--jobs 2`, `-if` and original comparisons.

### Defaults and presentation acceptance

- Omitted `--jobs` and explicit `--jobs 1` both use one file at a time and retain
  the current terminal layout, without alternate-screen/mouse/live-panel setup.
- More requested jobs than inputs are capped to the valid input count. One
  effective job still uses the classic layout; zero inputs retain preflight logic.
- Concurrent interactive output matches V5, including the full-width divider,
  scrollable ordered transcript, pinned live panel, scrollbar and mouse wheel.
- Bars/spinners exist in both sections and use actual stage progress. A late
  earlier file must not hide the full details of a later completed file.
- Verify mouse-wheel/drag and keyboard browsing, resizing, changed-row rendering,
  fallback logs, final ordered scrollback, and terminal restoration on failure.
- Confirm there is no CPU/storage probing, calculated thread-budget default,
  startup benchmark, or dynamic resource tuning in the normal execution path.

### Events, lifecycle and memory

- Test unknown duration, final stage states, throttling, stable stage identities,
  diagnostic deduplication, late progress, and missing-event reconciliation.
- Slow or pause a fake renderer without blocking worker scheduling or allowing
  unbounded queued progress. Keep real warnings/errors/completions intact.
- Cancel during metadata, stream scanning, FFmpeg, original inspection, and
  Python comparison work. Include a child that emits no stdout and ignores a
  graceful termination request.
- Crash a worker while its fake tool is alive and prove that tool is reaped.
  Inject startup failure and cancellation immediately around child registration.
- Verify remaining jobs continue after isolated file failures and that systemic
  failure/cancellation does not falsely mark remaining files checked.
- Confirm no surviving owned workers/tools, open diagnostic files, or lost final
  failure reports after shutdown; preserve unrelated user FFmpeg processes.
- Test on Windows spawn explicitly, plus another supported platform when
  available. Include non-interactive output and legacy output encodings.
- Use long-file/high-frame-count runs to verify that completed jobs release
  timing arrays and memory scales with active workers rather than batch size.

## 13. Performance measurement and acceptance

Benchmark during implementation with real media and real FFmpeg/FFprobe. Mocks
and fake tools validate scheduling/cancellation; the UI previews are not performance
evidence. Start with the videos already available in the repository, then use
additional representative inputs only where accessible and authorized.

Aim for CPU-heavy codecs, inexpensive decodes, long files, mixed sizes, multiple
audio streams and source comparisons. Measure local and NAS storage only where
actually available; do not claim NAS results from local runs or infer a drive's
performance from its path. State limitations if only the small local corpus is
available. The user will do further real-world testing on their own workloads.

For each relevant mode, compare:

- Existing serial behavior as a baseline.
- The new backend with one worker and the thread flag omitted (FFprobe 8,
  FFmpeg default).
- Two and four workers (and three where useful), first with that same preserved
  threading behavior, then explicit `--decode-threads` values such as 2/4/8.
- Full checks versus `-if`; include `-s` when that is the real workload.

The old serial baseline and new omitted-flag default should both request eight
threads for FFprobe scans and leave FFmpeg threading unmodified. An explicit
`--decode-threads 8` run also requests eight threads for supported FFmpeg input
decoders, so it is not necessarily identical to the omitted-flag baseline.
Record both tools' command settings and do not attribute thread-setting
differences to concurrency alone.

Record total wall time, throughput, per-file processing time, Python and decoder
CPU usage, per-core utilization where available, process memory, disk/network
throughput, and indicators of I/O contention. Keep the same corpus and flags.
Repeat runs and distinguish warm-cache results from first-read behavior; do not
assume a file reread is physical disk traffic or flush shared NAS caches casually.

Write the results to `plans/concurrency-benchmark-results.md` during implementation
and summarize them for the user. Include exact commands/settings, media sizes,
Python/tool versions, storage/environment details, repeated-run timings, speedups
relative to a stated baseline, verdict equivalence and unmeasured cases. Mark
unavailable metrics honestly. Keep benchmark tooling separate from the production
execution path; no automatic startup/run-time benchmark is requested.

Defaults remain `--jobs 1`, FFprobe frame scans at eight threads, and FFmpeg's own
threading default regardless of the benchmark
winner. Findings may support manual flag recommendations for the user to test,
but must not silently select settings. If extra workers make a workload slower,
report that. Measure and explain serial overhead; assume no target speedup.

## 14. Deferred work and open decisions

Explicitly outside the first implementation:

- Simultaneous FFmpeg and FFprobe checks within one verification job.
- Parallel stream scans or output/original scans within a job.
- GPU/hardware decoding, replacing FFprobe with a library, or reducing the
  independence of the existing FFmpeg check.
- Combining all stream scans into a single FFprobe pass. This might reduce
  repeated reads, but stream selection, excluded types and diagnostic semantics
  need a separate design and validation effort.
- Cross-job original-file caching, per-path locking, and shared timestamp arrays.
  Two different jobs can still read the same physical original independently.
- Automatic CPU/HDD/NAS bottleneck detection, CPU-count-derived thread budgets,
  storage benchmarking at startup, adaptive concurrency, retries/resume, or a
  persistent result cache.
- The normal-scrollback concurrent UI alternative: V5 is the selected concurrent
  UI. The classic serial layout is retained separately.
- Turning preview simulation controls or mock scheduling into production behavior.

Decide before implementation is considered complete:

1. Worker-pool and platform cleanup mechanisms that meet the cancellation tests
   while retaining the intended Python compatibility.
2. Supported-codec handling for explicitly requested decoder overrides.
3. The interrupted-run reporting wording/exit policy and reliable transport shape.

Already decided: default jobs = 1; omitted thread flag = FFprobe frame scans at
eight threads and FFmpeg's own default;
no application resource auto-tuning; classic single-job UI; V5 concurrent UI;
implementation-time real-media benchmarks reported to the user, followed by the
user's own broader real-world tests. Benchmarks do not reopen these defaults.

None of these decisions requires revisiting the agreed principle: bounded
parallel files, sequential checks per file, independent progress collection, and
deterministic final results.

## Repository references

- Current implementation: [VerifyVideo.py](../VerifyVideo.py).
- Existing usage and verification semantics: [README.md](../README.md).
- Existing semantic and media tests: [tests/test_verify_video.py](../tests/test_verify_video.py).
- Selected concurrent UI: [concurrency_preview_v5.py](../previews/concurrency_preview_v5.py),
  plus the shared/pinned modules it imports. These remain reference previews.
- Implementing-agent instructions: [implementation-handoff.md](implementation-handoff.md).
