"""Spawn/lifecycle tests use actual Python child processes, not parent-only mocks."""
import contextlib
import io
import json
import multiprocessing
import os
import pathlib
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import VerifyVideo as vv
import verification_runtime as rt
import verification_ui as ui


def fake_worker(connection, progress, cancel, config, parent_pid):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if os.name != 'nt':
        os.setsid()
    if config.get('startup_crash'):
        os._exit(19)
    connection.send(('ready', None))
    with contextlib.suppress(rt.Cancelled), rt.ToolOwner(cancel, parent_pid):
        while not cancel.is_set():
            if not connection.poll(0.05):
                continue
            index, path = connection.recv()
            if index is None:
                return
            sink = vv.EventReporter(index, connection, progress)
            sink.emit('job_start')
            if config.get('crash_progress') and index == 0:
                progress.lock.acquire()
                os._exit(24)
            if config.get('ignore_cancel'):
                while True:
                    time.sleep(0.05)
            if config.get('gate') is not None and index == 0:
                config['gate'].wait(10)
            if config.get('tool') and index in config.get('tool_indices', range(1000)):
                code = ("import os,pathlib,subprocess,sys,time; "
                        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+','+str(child.pid)); "
                        "time.sleep(120)")
                with rt.owned_process([sys.executable, '-c', code, str(path)], stdout=subprocess.PIPE) as process:
                    if config.get('crash') and index == 0:
                        deadline = time.monotonic() + 5
                        while not pathlib.Path(path).exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        os._exit(23)
                    process.stdout.read()  # Intentionally blocks; watcher must break it.
            with vv.Progress(sink, 'FFprobe', 'video stream 0', 10, stream_index=0, stream_kind='video') as p:
                p.updated -= 1
                for _ in range(100):
                    p.updated -= 1
                    p.update(5, 30)
                p.detail = ['30 frames']
            sink.diagnostic('warn', 'test diagnostic')
            sink.emit('job_finish', result=vv.Result(warnings=['test diagnostic']), elapsed=0.1,
                      stages=sink.stages, diagnostics=sink.diagnostics, cancelled=False)


def pid_alive(pid):
    if os.name == 'nt':
        import ctypes as c
        from ctypes import wintypes as w
        api = c.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def all_dead(pids):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(not pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.01)
    return False


class SchedulingTests(unittest.TestCase):
    def test_worker_exit_collected_after_cancel_is_not_a_failure(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                model = ui.Model(['a'])
                pool = rt.FilePool(['a'], 0, {}, model.accept)
                record = dict(job=0, ready=ready)
                pool.workers.append(record)
                pool.cancel.set()
                with mock.patch.object(pool, 'cleanup'):
                    pool.worker_died(record)
                self.assertIsNone(model.snapshot()[0].result)
                self.assertEqual(pool.workers, [])

    def pool(self, files, config, accept=None):
        model = ui.Model([str(p) for p in files])
        def collect(event):
            model.accept(event)
            if accept:
                accept(event)
        pool = rt.FilePool(files, 2, config, collect, target=fake_worker).start()
        self.addCleanup(pool.close)
        return pool, model

    def test_refills_before_first_job_finishes_and_keeps_final_order(self):
        gate = multiprocessing.get_context('spawn').Event()
        third = threading.Event()
        active, maximum, lock = set(), [0], threading.Lock()
        def collect(event):
            with lock:
                if event['kind'] == 'job_start':
                    active.add(event['job_index'])
                    maximum[0] = max(maximum[0], len(active))
                    if event['job_index'] == 2:
                        third.set()
                elif event['kind'] == 'job_finish':
                    active.discard(event['job_index'])
        pool, model = self.pool(['first', 'same/name', 'other/name', 'last'], {'gate': gate}, collect)
        try:
            self.assertTrue(third.wait(10), pool.error)
            self.assertFalse(model.snapshot()[0].terminal)
        finally:
            gate.set()
        self.assertTrue(pool.done.wait(10))
        self.assertIsNone(pool.error)
        self.assertLessEqual(maximum[0], 2)
        self.assertEqual([j.name for j in model.snapshot()], ['first', 'same/name', 'other/name', 'last'])
        self.assertTrue(all(j.terminal and j.result for j in model.snapshot()))
        self.assertTrue(all(r['closed'] for r in pool.workers))

    def test_blocked_tool_cancel_reaps_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [pathlib.Path(folder) / ('pids%d' % i) for i in range(2)]
            pool, model = self.pool(paths, {'tool': True})
            deadline = time.monotonic() + 10
            while not all(p.exists() for p in paths) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(all(p.exists() for p in paths), pool.error)
            pids = [int(pid) for p in paths for pid in p.read_text().split(',')]
            self.assertTrue(all(pid_alive(pid) for pid in pids))
            started = time.monotonic()
            pool.close()
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(all_dead(pids), pids)
            self.assertTrue(all(j.result is None for j in model.snapshot()))

    def test_worker_crash_cleans_live_descendants(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [pathlib.Path(folder) / ('pids%d' % i) for i in range(3)]
            pool, model = self.pool(paths, {'tool': True, 'crash': True, 'tool_indices': [0]})
            deadline = time.monotonic() + 10
            while not model.snapshot()[0].terminal and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(model.snapshot()[0].terminal, pool.error)
            pids = list(map(int, paths[0].read_text().split(',')))
            self.assertTrue(all_dead(pids))
            self.assertIn('Worker exited unexpectedly', model.snapshot()[0].result.errors[0])
            self.assertTrue(pool.done.wait(10))
            self.assertTrue(all(job.result for job in model.snapshot()))
            self.assertTrue(all(not job.result.errors for job in model.snapshot()[1:]))

    def test_startup_failure_is_incomplete_and_bounded(self):
        pool, model = self.pool(['a', 'b', 'c'], {'startup_crash': True})
        self.assertTrue(pool.done.wait(10))
        self.assertIsNotNone(pool.error)
        self.assertFalse(any(job.result for job in model.snapshot()))

    def test_worker_dies_holding_progress_lock_other_jobs_continue(self):
        pool, model = self.pool(['a', 'b', 'c'], {'crash_progress': True})
        self.assertTrue(pool.done.wait(10))
        self.assertIsNone(pool.error)
        jobs = model.snapshot()
        self.assertTrue(jobs[0].result.errors)
        self.assertTrue(all(job.result and not job.result.errors for job in jobs[1:]))

    def test_force_cleanup_of_unresponsive_workers_preserves_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        try:
            started = threading.Event()
            pool, model = self.pool(['a', 'b', 'unstarted'], {'ignore_cancel': True},
                                    lambda event: started.set() if event['kind'] == 'job_start' else None)
            self.assertTrue(started.wait(10))
            began = time.monotonic()
            pool.close()
            self.assertLess(time.monotonic() - began, 6)
            self.assertTrue(all(r['closed'] for r in pool.workers))
            self.assertIsNone(unrelated.poll())
            self.assertFalse(any(job.result for job in model.snapshot()))
        finally:
            unrelated.terminate()
            unrelated.wait()

    def test_cancellation_before_registration_starts_no_process(self):
        cancel = threading.Event()
        cancel.set()
        with rt.ToolOwner(cancel), mock.patch.object(subprocess, 'Popen') as launch:
            with self.assertRaises(rt.Cancelled):
                with rt.owned_process([sys.executable, '-c', 'pass']):
                    pass
            launch.assert_not_called()

    def test_serial_blocked_read_is_cancellable_and_tree_owned(self):
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'pid'
            def stop():
                deadline = time.monotonic() + 8
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                cancel.set()
            watcher = threading.Thread(target=stop)
            watcher.start()
            with rt.ToolOwner(cancel), self.assertRaises(rt.Cancelled):
                vv.run_streaming([sys.executable, '-c',
                                  'import os,pathlib,time,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)',
                                  str(path)], lambda line: None)
            watcher.join()
            self.assertTrue(all_dead([int(path.read_text())]))

    @unittest.skipUnless(os.name == 'nt', 'Windows ownership registration')
    def test_assignment_failure_never_executes_unowned_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = pathlib.Path(folder) / 'executed'
            with mock.patch.object(rt.WindowsJob, 'assign', side_effect=OSError('injected host job restriction')):
                with self.assertRaises(OSError):
                    vv.run_streaming([sys.executable, '-c',
                                      'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(sentinel)], lambda x: None)
            self.assertFalse(sentinel.exists())

    def test_python_timing_loops_check_cancellation(self):
        from array import array
        stream = vv.Stream(0, 'video', 0.001, timestamps=array('d', range(5000)))
        cancel = threading.Event()
        cancel.set()
        with rt.ToolOwner(cancel):
            with self.assertRaises(rt.Cancelled):
                vv.timestamp_checks([stream], vv.Result())
            with self.assertRaises(rt.Cancelled):
                stream.finish([])
            with self.assertRaises(rt.Cancelled):
                vv.compare_original([stream], [stream], vv.Result())


class ContractTests(unittest.TestCase):
    def test_progress_slot_is_bounded_coalesces_and_never_waits_for_dead_writer(self):
        context = multiprocessing.get_context('spawn')
        slot = rt.ProgressSlot(context)
        for i in range(1000):
            slot.put_nowait(dict(position=i))
        self.assertEqual(slot.get_nowait(), dict(position=999))
        with self.assertRaises(queue.Empty):
            slot.get_nowait()
        slot.lock.acquire()
        try:
            with self.assertRaises(queue.Empty):
                slot.get_nowait()
            with self.assertRaises(queue.Full):
                slot.put_nowait(dict(position=1000))
        finally:
            slot.lock.release()
        with self.assertRaises(queue.Full):
            slot.put_nowait(dict(message='x' * 20000))

    def test_thread_options_and_metadata_omission(self):
        stream = vv.Stream(2, 'video', 0.001)
        for threads in (None, 2, 8):
            with mock.patch.object(vv, 'run_streaming', return_value=(0, [])) as run:
                vv.scan_stream('output', stream, 'probe', decode_threads=threads)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index('-threads') + 1], str(8 if threads is None else threads))
                vv.ffmpeg_check('output', [stream, vv.Stream(0, 'audio', 0.001)], 'ffmpeg', decode_threads=threads)
                command = run.call_args.args[0]
                if threads is None:
                    self.assertNotIn('-threads:v', command)
                    self.assertNotIn('-threads', command)
                else:
                    self.assertLess(command.index('-threads:v'), command.index('-i'))
                    self.assertEqual(command[command.index('-threads:v') + 1], str(threads))
                self.assertEqual(command[command.index('-map') + 1], '0:2')
        with mock.patch.object(vv, 'run_streaming') as run:
            run.side_effect = lambda command, consume: (consume('{"streams":[]}') or 0, [])
            vv.probe_metadata('output', 'probe')
            self.assertNotIn('-threads', run.call_args.args[0])

    def test_explicit_threads_reach_original_and_output(self):
        with mock.patch.object(vv, 'inspect_video', return_value=([vv.Stream(0, 'video', 1)], 1)) as inspect, \
             mock.patch.object(vv, 'ffmpeg_check', return_value=[]) as ffmpeg, \
             mock.patch.object(vv, 'find_original', return_value=(pathlib.Path('source'), None)):
            vv.verify(pathlib.Path('out_new.mp4'), 'probe', 'ffmpeg', staxrip=True, decode_threads=2)
            self.assertEqual(inspect.call_count, 2)
            self.assertTrue(all(call.kwargs['decode_threads'] == 2 for call in inspect.call_args_list))
            self.assertEqual(ffmpeg.call_args.kwargs['decode_threads'], 2)

    def test_invalid_values_fail_before_any_worker(self):
        for flag in ('--jobs', '--decode-threads'):
            for value in ('0', '-1', 'bad'):
                with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(rt, 'FilePool') as pool:
                    with self.assertRaises(SystemExit) as error:
                        vv.main([flag, value])
                    self.assertEqual(error.exception.code, 2)
                    pool.assert_not_called()

    def test_one_effective_job_is_classic_even_when_four_requested(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'one.mp4'
            path.touch()
            for flags in ([], ['--jobs', '1'], ['--jobs', '4']):
                output = io.StringIO()
                with mock.patch.object(vv, 'verify', return_value=vv.Result()), \
                     mock.patch.object(ui, 'run_concurrent') as concurrent, contextlib.redirect_stdout(output):
                    self.assertEqual(vv.main(['--file', str(path), *flags]), 0)
                concurrent.assert_not_called()
                self.assertNotIn('\033[', output.getvalue())
                self.assertNotIn('Live progress', output.getvalue())

    def test_serial_interrupt_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'one.mp4'
            path.touch()
            output = io.StringIO()
            with mock.patch.object(vv, 'verify', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(output):
                self.assertEqual(vv.main(['--file', str(path)]), 130)
            self.assertIn('INCOMPLETE: 0/1', output.getvalue())

    def test_final_reconciliation_and_late_progress(self):
        class Connection:
            def send(self, item):
                model.accept(item[1])
        model = ui.Model(['a'])
        events = queue.Queue(maxsize=1)
        sink = vv.EventReporter(0, Connection(), events)
        sink.emit('job_start')
        with vv.Progress(sink, 'FFprobe', 'original video', 10, role='original', stream_index=3) as p:
            p.updated -= 1
            p.update(5, 60)
        terminal = model.snapshot()[0].stages[0]
        late = events.get_nowait()
        model.accept(late)
        self.assertEqual(model.snapshot()[0].stages[0]['state'], 'done')
        # Deliberately missing intermediate events: final snapshot repairs them.
        repaired = dict(terminal, stage_id=1, stage_order=1, role='output', stream_index=3)
        sink.emit('job_finish', result=vv.Result(errors=['fatal']), elapsed=1,
                  stages=[terminal, repaired], diagnostics=[], cancelled=False)
        model.accept(late)
        job = model.snapshot()[0]
        self.assertEqual(len(job.stages), 2)
        self.assertEqual(job.stages[0]['role'], 'original')
        self.assertEqual(job.diagnostics, [('error', 'fatal')])
        self.assertEqual(job.stages[0]['state'], 'done')

    def test_crash_reconciliation_keeps_partial_diagnostics_and_no_running_rows(self):
        model = ui.Model(['a'])
        stage = dict(stage_id=0, state='running', elapsed_seconds=1, details=[])
        model.accept(dict(job_index=0, sequence=1, kind='stage_start', stage=stage))
        model.accept(dict(job_index=0, sequence=2, kind='diagnostic', state='warn', message='partial warning'))
        model.accept(dict(job_index=0, sequence=3, kind='job_finish', cancelled=False,
                          result=vv.Result(errors=['worker crash']), elapsed=None, stages=None, diagnostics=None))
        job = model.snapshot()[0]
        self.assertEqual(job.stages[0]['state'], 'error')
        self.assertEqual(job.result.warnings, ['partial warning'])
        self.assertEqual(vv.clock_time(job.elapsed), 'unknown')


class RenderingTests(unittest.TestCase):
    def transcript(self):
        r = vv.Reporter(io.StringIO())
        jobs = [ui.JobState('first', started=True), ui.JobState('later', started=True, terminal=True,
                                result=vv.Result(warnings=['immediate detail']), completion=1)]
        stage = dict(stage_id=0, state='running', tool='FFprobe', label='video stream 0', details=[],
                     total_seconds=10, position_seconds=5, frame_count=60, elapsed_seconds=1)
        jobs[0].stages[0] = stage
        return ui.Transcript(r, jobs, 2)

    def test_bars_both_sections_full_rule_and_immediate_details(self):
        t = self.transcript()
        doc = '\n'.join(t.document(1, 109)[0])
        footer = t.footer(1, 109)
        self.assertIn('50.0%', doc)
        self.assertIn('50.0%', '\n'.join(footer))
        self.assertEqual(vv.visible_width(footer[0]), 109)
        self.assertIn('immediate detail', doc)
        self.assertLess(doc.index('first'), doc.index('later'))
        t.files[0].stages[0]['total_seconds'] = None
        self.assertNotIn('%', '\n'.join(t.file_lines(0, 1)))

    def test_navigation_wheel_drag_follow_resize_and_tiny_terminal(self):
        t = self.transcript()
        t.files[1].result.warnings *= 40
        t.frame(1, 80, 18, [{'action': 'home'}])
        self.assertFalse(t.follow)
        self.assertEqual(t.offset, 0)
        t.frame(1, 80, 18, [{'action': 'wheel', 'y': 3, 'lines': 3}])
        self.assertEqual(t.offset, 3)
        t.frame(1, 80, 18, [{'action': 'click', 'x': 78, 'y': 5}, {'action': 'drag', 'x': 78, 'y': 10}])
        self.assertGreater(t.offset, 3)
        t.frame(1, 80, 18, [{'action': 'release'}, {'action': 'end'}])
        self.assertTrue(t.follow)
        for width, height in ((20, 5), (1, 1), (110, 35)):
            lines = t.frame(1, width, height)
            self.assertLessEqual(len(lines), height)

    def test_changed_rows_only_and_restoration_on_error(self):
        output = io.StringIO()
        r = vv.Reporter(output)
        with self.assertRaises(RuntimeError):
            with ui.Screen(r) as screen:
                screen.draw(['alpha', 'beta'], 80, 2)
                before = len(output.getvalue())
                screen.draw(['alpha', 'beta'], 80, 2)
                self.assertEqual(len(output.getvalue()), before)
                screen.draw(['alpha', 'gamma'], 80, 2)
                update = output.getvalue()[before:]
                self.assertNotIn('alpha', update)
                self.assertIn('gamma', update)
                raise RuntimeError()
        self.assertTrue(output.getvalue().endswith('\033[0m\033[?25h\033[?1049l'))

    def test_sgr_partial_sequences_and_no_mock_controls(self):
        events, pending = ui.decode_sgr('\033[<64;10;3M\033[')
        self.assertEqual(events[0]['action'], 'wheel')
        self.assertEqual(pending, '\033[')
        events, pending = ui.decode_sgr(pending + 'A\033[<0;78;5M\033[<32;78;8M\033[<0;78;8mq ')
        self.assertEqual([e['action'] for e in events], ['up', 'click', 'drag', 'release', 'quit'])

    @unittest.skipUnless(os.name == 'nt', 'native Windows input records')
    def test_native_mouse_and_keyboard_records(self):
        Record, _ = ui.console_types()
        record = Record()
        record.EventType = 2
        mouse = record.Event.MouseEvent
        mouse.dwMousePosition.X, mouse.dwMousePosition.Y = 78, 6
        mouse.dwEventFlags, mouse.dwButtonState = 4, (120 << 16)
        self.assertEqual(ui.windows_event(record, (1, 2)), [dict(action='wheel', x=78, y=5, lines=-3)])
        mouse.dwButtonState, mouse.dwEventFlags = 1, 0
        self.assertEqual(ui.windows_event(record)[0]['action'], 'click')
        mouse.dwEventFlags = 1
        self.assertEqual(ui.windows_event(record)[0]['action'], 'drag')
        mouse.dwButtonState = 0
        self.assertEqual(ui.windows_event(record)[0]['action'], 'release')
        record.EventType = 1
        record.Event.KeyEvent.bKeyDown = True
        record.Event.KeyEvent.wVirtualKeyCode = 0x23
        self.assertEqual(ui.windows_event(record), [dict(action='end')])


@unittest.skipUnless(__import__('shutil').which('ffmpeg') and __import__('shutil').which('ffprobe'),
                     'FFmpeg/FFprobe required')
class SpawnMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_verify_video as semantic
        cls.fixture = semantic.MediaTests
        cls.fixture.setUpClass()
        cls.folder, cls.clean = cls.fixture.folder, cls.fixture.clean
        cls.ffmpeg, cls.ffprobe = cls.fixture.ffmpeg, cls.fixture.ffprobe

    @classmethod
    def tearDownClass(cls):
        cls.fixture.tearDownClass()

    def compare(self, paths, **options):
        expected = [vv.verify(path, self.ffprobe, self.ffmpeg, **options) for path in paths]
        model = ui.Model([str(path) for path in paths])
        pool = rt.FilePool(paths, 2, dict(ffprobe=self.ffprobe, ffmpeg=self.ffmpeg, **options), model.accept).start()
        try:
            self.assertTrue(pool.done.wait(25), pool.error)
            self.assertIsNone(pool.error)
        finally:
            pool.close()
        jobs = model.snapshot()
        def normalized(messages):
            # FFmpeg includes ASLR-dependent decoder/context pointer addresses.
            return [re.sub(r' @ (?:0x)?[0-9a-fA-F]+(?=\])', ' @ <address>', text) for text in messages]
        for serial, job in zip(expected, jobs):
            self.assertIsNotNone(job.result)
            self.assertEqual((normalized(job.result.warnings), normalized(job.result.errors)),
                             (normalized(serial.warnings), normalized(serial.errors)))
        return jobs

    def test_spawn_equivalence_clean_corrupt_truncated_unreadable_multistream(self):
        broken = self.folder / 'broken.mp4'
        broken.write_bytes(b'not a video')
        corrupt = self.fixture.corrupted_copy(self, 'corrupt.mp4')
        truncated = self.folder / 'truncated.mkv'
        truncated.write_bytes(self.clean.read_bytes()[:self.clean.stat().st_size // 2])
        reordered = self.folder / 'reordered.mkv'
        self.fixture.encode(reordered, ['-i', str(self.clean)], ['-map', '0:a', '-map', '0:v', '-map', '0:a', '-c', 'copy'])
        multiple = self.folder / 'two_videos.mkv'
        self.fixture.encode(multiple, ['-i', str(self.clean)],
                            ['-map', '0:a', '-map', '0:v', '-map', '0:v', '-c', 'copy'])
        silent = self.folder / 'silent.mkv'
        self.fixture.encode(silent, ['-i', str(self.clean)], ['-map', '0:v', '-c', 'copy'])
        paths = [broken, self.clean, corrupt, truncated, reordered, silent, multiple]
        for ignore in (False, True):
            with self.subTest(ignore_ffmpeg=ignore):
                self.compare(paths, ignore_ffmpeg=ignore, decode_threads=2)

    def test_spawn_source_and_strict_flag_combinations(self):
        import shutil
        for name in ('good', 'bad'):
            shutil.copyfile(self.clean, self.folder / (name + '_new.mkv'))
        shutil.copyfile(self.clean, self.folder / 'good.mkv')
        (self.folder / 'bad.mkv').write_bytes(b'bad original')
        paths = [self.folder / 'good_new.mkv', self.folder / 'bad_new.mkv']
        for strict in (False, True):
            for ignore in (False, True):
                with self.subTest(strict=strict, ignore=ignore):
                    jobs = self.compare(paths, staxrip=True, strict_timing=strict, ignore_ffmpeg=ignore, decode_threads=2)
                    self.assertTrue(any(s['role'] == 'original' for s in jobs[0].stages.values()))

    def test_plain_cli_report_and_transcript_keep_scan_order(self):
        import shutil
        folder = self.folder / 'report'
        folder.mkdir()
        (folder / 'a.mp4').write_bytes(b'bad')
        shutil.copyfile(self.clean, folder / 'b.mkv')
        (folder / 'c.mp4').write_bytes(b'bad')
        command = [sys.executable, '-B', vv.__file__, '--folder', str(folder), '--jobs', '4',
                   '--file', str(folder / 'missing.mp4'), str(folder / 'a.mp4')]
        result = subprocess.run(command, cwd=str(folder), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((folder / 'corrupted_files.txt').read_text().splitlines(), ['missing.mp4', 'a.mp4', 'c.mp4'])
        self.assertNotIn('\033', result.stdout)
        final = result.stdout.split('Final ordered transcript')[1]
        self.assertLess(final.index('[1/3]'), final.index('[2/3]'))
        self.assertLess(final.index('[2/3]'), final.index('[3/3]'))


if __name__ == '__main__':
    unittest.main()
