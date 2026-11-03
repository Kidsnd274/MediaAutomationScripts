# VerifyVideo implementation handoff

Historical handoff: implemented and validated. For a new benchmarking request,
start with [docs/benchmarking.md](../docs/benchmarking.md). Local preview paths
below describe the original design references; they are not runtime dependencies.

Implement [backend-concurrency.md](backend-concurrency.md) in `VerifyVideo.py`.
Read the entire updated plan and applicable repository instructions first. This
is an implementation request, including testing, UI integration, and reporting
benchmark results; do not stop after another plan or a mock demonstration.

The user's final decisions supersede earlier thread-budget and UI proposals:

1. Add `--jobs N`, default **1**. Keep checks sequential within each file; only
   different files may run concurrently. Use bounded Python worker processes for
   concurrent jobs, refill available slots immediately, and retain scan-list
   numbering and final transcript/result/failure ordering.
2. Add optional `--decode-threads N`, default **unset**. When omitted, preserve
   existing behavior: FFprobe frame scans use `-threads 8`; FFmpeg receives no
   threading override. Keep metadata settings unchanged. The user explicitly
   chose to keep FFprobe's existing eight-thread default. When supplied, apply the value
   appropriately to supported input video decoders, including original scans.
3. Do not add CPU/storage bottleneck detection, calculated thread budgets,
   automatic worker selection, startup benchmarks or adaptive tuning. The tools
   retain the fixed default behavior above regardless of worker count; do not
   calculate or adapt their thread settings.
4. One effective file job uses the current classic terminal appearance, without
   a live panel, alternate screen, app scrollbar or mouse capture. This includes
   default invocation, `--jobs 1`, and a single input selected with a larger limit.
5. Concurrent interactive runs follow
   [concurrency_preview_v5.py](../previews/concurrency_preview_v5.py) and its imported
   helpers: scrollable ordered transcript, pinned live panel, full-width divider,
   mouse wheel and draggable scrollbar, immediate stage/completion details, and
   bars/spinners in both sections. Adapt it to real events; do not import runtime
   code from previews or expose mock simulation controls. Keep plain log fallbacks.
6. Preserve verification semantics and existing flags. Implement reliable
   cancellation and subprocess-tree cleanup, including blocked metadata/scans;
   keep timestamps local to workers and final reporting parent-owned.
7. Run meaningful scheduling/lifecycle tests and existing media/semantic tests.
   Use mocks/fake tools for correctness and real FFmpeg/FFprobe on available real
   videos for performance. Benchmark serial/one/two/four jobs, omitted and explicit
   decoder-thread settings, and `-if`/source comparison where practical. Report
   limitations; do not invent NAS or representative long-file results if those
   inputs/storage are unavailable. The user will do further real-world testing.
8. Write measured results, commands, environment, repeated timings and verdict
   comparisons to `plans/concurrency-benchmark-results.md` and summarize them in
   the final response. Benchmarks do not change the fixed defaults above.

Keep existing preview versions and unrelated user files intact. Update production
documentation and tests as needed. Report what changed, validation outcomes,
measured performance and remaining limitations. This handoff does not request a
commit, deployment, or a new Codex chat.
