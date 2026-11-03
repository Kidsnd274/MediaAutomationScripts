"""Manual VerifyVideo benchmarks on explicitly selected, unchanged media.

See docs/benchmarking.md. This tool never changes verification defaults or media.
"""
import argparse
import collections
import hashlib
import json
import os
import pathlib
import platform
import re
import signal
import statistics
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verification_runtime import ToolOwner


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def decoder_threads(value):
    return None if value == 'default' else positive(value)


def signature(output, expected_files):
    """Compare verdicts and diagnostics, excluding timings and ASLR addresses."""
    if 'Final ordered transcript' in output:
        output = output.split('Final ordered transcript', 1)[1]
    rows = re.findall(r'^\s+\d+\s+(PASS|WARN|FAIL)\s+(\d+)\s+(\d+)\s+\S+\s+(.+)$', output, re.M)
    messages = re.findall(r'^\s+(?:\|-|`-|├─|└─)\s+[!x▲✗]\s+(.+)$', output, re.M)
    messages = [re.sub(r' @ (?:0x)?[0-9a-fA-F]+(?=\])', ' @ <address>', x) for x in messages]
    operational = any(re.search(r'Worker (?:exception:|exited unexpectedly)|Could not run FFmpeg:|'
                                r'Could not (?:probe file|scan stream \d+): \[(?:WinError|Errno) ', message)
                      for message in messages)
    return dict(verdicts=rows, diagnostics=messages,
                complete=len(rows) == expected_files and 'INCOMPLETE:' not in output and not operational)


def source_hashes(script):
    paths = [script] + [script.parent / name for name in ('verification_runtime.py', 'verification_ui.py')]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}


def sample(command, folder, resources):
    log = folder / 'stdout.log'
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', NO_COLOR='1')
    started = time.perf_counter()
    metrics = None
    with log.open('w', encoding='utf-8') as output, ToolOwner() as owner:
        with owner.launch(command, cwd=str(folder), stdout=output, stderr=subprocess.STDOUT, env=environment) as process:
            try:
                if resources:
                    from tools.benchmark_metrics import Metrics
                    metrics = Metrics(process.pid)
                    while process.poll() is None:
                        metrics.sample()
                        time.sleep(0.2)
                else:
                    process.wait()
            except KeyboardInterrupt:
                # Let the verifier use its cancellation path before tree cleanup.
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                    pass
                raise
    record = dict(command=command, wall_seconds=time.perf_counter() - started,
                  returncode=process.returncode, log=str(log))
    if metrics:
        record.update(metrics.result())
    return record, log.read_text(encoding='utf-8')


def write_summary(folder, records):
    groups = collections.OrderedDict()
    for record in records:
        groups.setdefault((record['jobs'], record['decode_threads']), []).append(record)
    reference = next(iter(groups.values()))
    ref_time = statistics.median(r['wall_seconds'] for r in reference)
    lines = ['# VerifyVideo benchmark', '',
             'Speedups use the first requested configuration. Compare only equivalent, complete runs.', '',
             '| Jobs requested/effective | Decode threads | Repeated wall seconds | Median s | Range s | Speedup | Equivalent |',
             '| --- | --- | --- | ---: | --- | ---: | --- |']
    for (jobs, threads), rows in groups.items():
        times = [r['wall_seconds'] for r in rows]
        median = statistics.median(times)
        lines.append('| %s/%s | %s | %s | %.3f | %.3f–%.3f | %.3fx | %s |' %
                     (jobs, rows[0]['effective_jobs'], 'omitted' if threads is None else threads,
                      ', '.join('%.3f' % t for t in times), median, min(times), max(times),
                      ref_time / median, 'yes' if all(r['equivalent'] for r in rows) else 'NO'))
    lines += ['', 'Exit 1 from the verifier can be a consistent media failure. The benchmark itself succeeds only when all runs',
              'complete with equivalent exit codes, ordered verdicts and diagnostics. Raw commands, signatures, hashes,',
              'optional resource samples and logs remain local. Read counters include cached reads and pipes;',
              'they are not physical storage throughput. Resource samples are lower bounds and can perturb timings.']
    (folder / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', nargs='+', required=True, type=pathlib.Path, metavar='PATH')
    parser.add_argument('--output-dir', required=True, type=pathlib.Path, help='New or empty directory for local results')
    parser.add_argument('--script', type=pathlib.Path, default=ROOT / 'VerifyVideo.py')
    parser.add_argument('--jobs', nargs='+', type=positive, default=[1, 2, 4])
    parser.add_argument('--decode-threads', nargs='+', type=decoder_threads, default=[None], metavar='default|N')
    parser.add_argument('--repeats', type=positive, default=3)
    parser.add_argument('-if', '--ignore-ffmpeg', action='store_true')
    parser.add_argument('-s', '--staxrip', action='store_true')
    parser.add_argument('--strict-timing', action='store_true')
    parser.add_argument('--sample-resources', action='store_true', help='Opt-in Windows process-tree CPU/memory/read samples')
    parser.add_argument('--legacy-serial', action='store_true', help='Omit new flags for an older --script; requires --jobs 1 and omitted threads')
    args = parser.parse_args(argv)
    files = sorted(set(path.resolve() for path in args.file))
    script, folder = args.script.resolve(), args.output_dir.resolve()
    if not script.is_file() or any(not path.is_file() for path in files):
        parser.error('the script and every selected media path must be existing files')
    if any(path.suffix.lower() not in ('.mkv', '.mp4', '.avi', '.webm') for path in files):
        parser.error('select supported video files only')
    if args.staxrip and any(not path.stem.endswith('_new') for path in files):
        parser.error('--staxrip requires selected output names ending in _new; originals stay beside them')
    if args.sample_resources and os.name != 'nt':
        parser.error('--sample-resources currently supports Windows only; omit it for portable wall timings')
    if args.legacy_serial and (args.jobs != [1] or args.decode_threads != [None]):
        parser.error('--legacy-serial requires --jobs 1 and omitted --decode-threads')
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        parser.error('--output-dir must be new or empty; previous results are never overwritten')
    folder.mkdir(parents=True, exist_ok=True)
    inputs = [dict(path=str(path), bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns) for path in files]
    hashes = source_hashes(script)
    manifest = dict(python=sys.version, platform=platform.platform(), script=str(script), source_sha256=hashes,
                    inputs=inputs, settings=vars(args))
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2, default=str) + '\n', encoding='utf-8')
    records, reference = [], None
    try:
        for jobs in dict.fromkeys(args.jobs):
            for threads in dict.fromkeys(args.decode_threads):
                for repeat in range(1, args.repeats + 1):
                    if source_hashes(script) != hashes or any(
                            path.stat().st_size != item['bytes'] or path.stat().st_mtime_ns != item['mtime_ns']
                            for path, item in zip(files, inputs)):
                        raise RuntimeError('script or selected media changed during the benchmark; start a fresh series')
                    name = 'jobs%d-threads%s-r%d' % (jobs, 'default' if threads is None else threads, repeat)
                    run_folder = folder / name
                    run_folder.mkdir()
                    command = [sys.executable, '-B', str(script), '--file', *map(str, files)]
                    if not args.legacy_serial:
                        command += ['--jobs', str(jobs)]
                    if threads is not None:
                        command += ['--decode-threads', str(threads)]
                    if args.ignore_ffmpeg:
                        command.append('-if')
                    if args.staxrip:
                        command.append('-s')
                    if args.strict_timing:
                        command.append('--strict-timing')
                    record, output = sample(command, run_folder, args.sample_resources)
                    result = signature(output, len(files))
                    current = dict(returncode=record['returncode'], **result)
                    if reference is None:
                        reference = current
                    record.update(jobs=jobs, effective_jobs=min(jobs, len(files)), decode_threads=threads,
                                  repeat=repeat, signature=result,
                                  equivalent=result['complete'] and record['returncode'] in (0, 1) and current == reference)
                    records.append(record)
                    with (folder / 'results.jsonl').open('a', encoding='utf-8') as output_file:
                        output_file.write(json.dumps(record) + '\n')
                    write_summary(folder, records)
                    print('%s: %.3fs; exit %s; equivalent=%s' %
                          (name, record['wall_seconds'], record['returncode'], record['equivalent']), flush=True)
                    if not record['equivalent']:
                        print('Stopped: incomplete or changed verdicts/diagnostics; inspect the saved log.', file=sys.stderr)
                        return 1
    except KeyboardInterrupt:
        (folder / 'INCOMPLETE.txt').write_text('Benchmark interrupted; partial timings are not a completed series.\n', encoding='utf-8')
        return 130
    except (OSError, RuntimeError) as error:
        (folder / 'INCOMPLETE.txt').write_text(str(error) + '\n', encoding='utf-8')
        print(str(error), file=sys.stderr)
        return 1
    print('Saved ' + str(folder / 'summary.md'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
