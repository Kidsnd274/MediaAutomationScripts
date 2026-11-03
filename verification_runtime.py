"""Process ownership, cancellation and bounded file scheduling (stdlib only)."""
import contextlib
import json
import multiprocessing
from multiprocessing.connection import wait
import os
import queue
import signal
import subprocess
import threading
import time


class Cancelled(BaseException):
    pass


class CancelFlag:
    """A shared byte: forced worker exit cannot strand an Event's mutex."""
    def __init__(self, context):
        self.flag = context.RawValue('b', 0)

    def is_set(self):
        return bool(self.flag.value)

    def set(self):
        self.flag.value = 1


class ProgressSlot:
    """One bounded latest update per worker, with nonblocking locking.

    If a worker dies while holding its lock, only its obsolete progress is lost;
    neither the collector nor any other worker waits for that lock. Reliable
    events/results travel on independent control pipes.
    """
    def __init__(self, context):
        self.buffer = context.RawArray('B', 16384)
        self.length = context.RawValue('I', 0)
        self.version = context.RawValue('I', 0)
        self.lock = context.Lock()
        self.seen = 0

    def put_nowait(self, event):
        data = json.dumps(event, ensure_ascii=False).encode('utf-8')
        if len(data) > len(self.buffer) or not self.lock.acquire(False):
            raise queue.Full()
        try:
            self.buffer[:len(data)] = data
            self.length.value = len(data)
            self.version.value = (self.version.value + 1) % 2**32
        finally:
            self.lock.release()

    def get_nowait(self):
        if not self.lock.acquire(False):
            raise queue.Empty()
        try:
            if self.version.value == self.seen:
                raise queue.Empty()
            data = bytes(self.buffer[:self.length.value])
            self.seen = self.version.value
        finally:
            self.lock.release()
        return json.loads(data.decode('utf-8'))


class WindowsJob:
    """Non-inheritable, kill-on-close job. Failure is fatal before any tool starts."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.api = c.WinDLL('kernel32', use_last_error=True)
        class Basic(c.Structure):
            _fields_ = [('ProcessTime', c.c_int64), ('JobTime', c.c_int64), ('Flags', w.DWORD),
                        ('Min', c.c_size_t), ('Max', c.c_size_t), ('Active', w.DWORD),
                        ('Affinity', c.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in
                        ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class Extended(c.Structure):
            _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', c.c_size_t),
                        ('JobMemory', c.c_size_t), ('PeakProcess', c.c_size_t), ('PeakJob', c.c_size_t)]
        self.api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        class Accounting(c.Structure):
            _fields_ = [('User', c.c_int64), ('Kernel', c.c_int64), ('PeriodUser', c.c_int64),
                        ('PeriodKernel', c.c_int64), ('Faults', w.DWORD), ('Total', w.DWORD),
                        ('Active', w.DWORD), ('Terminated', w.DWORD)]
        self.Accounting = Accounting
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not handle:
            raise self.c.WinError(self.c.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise self.c.WinError(self.c.get_last_error())
        finally:
            self.api.CloseHandle(handle)

    def kill(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            # Termination is asynchronous. Wait for every owned descendant,
            # including tools left behind by a crashed worker.
            deadline = time.monotonic() + 1
            info = self.Accounting()
            while time.monotonic() < deadline:
                if not self.api.QueryInformationJobObject(self.handle, 1, self.c.byref(info),
                                                          self.c.sizeof(info), None) or not info.Active:
                    break
                time.sleep(0.01)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def resume_process(pid):
    """Resume CREATE_SUSPENDED's initial thread after assigning the tool's job."""
    import ctypes as c
    from ctypes import wintypes as w
    api = c.WinDLL('kernel32', use_last_error=True)
    class ThreadEntry(c.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('tid', w.DWORD), ('pid', w.DWORD),
                    ('priority', w.LONG), ('delta', w.LONG), ('flags', w.DWORD)]
    api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    api.CreateToolhelp32Snapshot.restype = w.HANDLE
    api.Thread32First.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
    api.Thread32Next.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
    api.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenThread.restype = w.HANDLE
    api.ResumeThread.argtypes = [w.HANDLE]
    api.ResumeThread.restype = w.DWORD
    api.CloseHandle.argtypes = [w.HANDLE]
    snapshot = api.CreateToolhelp32Snapshot(4, 0)
    if snapshot == w.HANDLE(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        entry = ThreadEntry()
        entry.size = c.sizeof(entry)
        more = api.Thread32First(snapshot, c.byref(entry))
        while more:
            if entry.pid == pid:
                thread = api.OpenThread(2, False, entry.tid)
                if not thread:
                    raise c.WinError(c.get_last_error())
                try:
                    if api.ResumeThread(thread) == 0xffffffff:
                        raise c.WinError(c.get_last_error())
                    return
                finally:
                    api.CloseHandle(thread)
            more = api.Thread32Next(snapshot, c.byref(entry))
        raise OSError('Suspended tool thread not found')
    finally:
        api.CloseHandle(snapshot)


_local = threading.local()


class ToolOwner:
    def __init__(self, cancel=None, parent_pid=None):
        self.cancel = cancel if cancel is not None else threading.Event()
        self.parent_pid = parent_pid
        self.lock = threading.Lock()
        self.process = self.job = None
        self.done = threading.Event()

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def __enter__(self):
        self.previous = getattr(_local, 'owner', None)
        _local.owner = self
        self.watcher = threading.Thread(target=self.watch, daemon=True)
        self.watcher.start()
        return self

    def watch(self):
        while not self.done.wait(0.05):
            if self.parent_pid and os.getppid() != self.parent_pid:
                self.cancel.set()
            if self.cancel.is_set():
                self.stop()

    def stop(self):
        with self.lock:
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        # Give tools a short opportunity to release their own
                        # resources, then enforce tree ownership below.
                        if os.name == 'nt':
                            self.process.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            self.process.terminate()
                        self.process.wait(timeout=0.25)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                if self.parent_pid and os.name != 'nt' and self.cancel.is_set():
                    os.killpg(os.getpid(), signal.SIGKILL)
                kill_tree(self.process, self.job)

    @contextlib.contextmanager
    def launch(self, command, **kwargs):
        with self.lock:
            self.check()
            job = WindowsJob() if os.name == 'nt' else None
            process = None
            try:
                if os.name == 'nt':
                    kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | 4
                else:
                    # Workers already own a session. Keep descendants in that
                    # group so the parent can kill them even after a worker crash.
                    kwargs['start_new_session'] = not bool(self.parent_pid)
                process = subprocess.Popen(command, **kwargs)
                self.process, self.job = process, job
                if job:
                    job.assign(process.pid)
                    self.check()
                    resume_process(process.pid)
            except BaseException:
                if process:
                    kill_tree(process, job)
                    process.wait()
                    if process.stdout:
                        process.stdout.close()
                if job:
                    job.close()
                self.process = self.job = None
                raise
        try:
            yield process
            self.check()
        finally:
            with self.lock:
                kill_tree(process, job)
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if job:
                    job.close()
                self.process = self.job = None

    def __exit__(self, *exc):
        self.done.set()
        self.stop()
        self.watcher.join(timeout=1)
        _local.owner = self.previous


def kill_tree(process, job=None):
    if job:
        job.kill()
    elif os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def check_cancel(iteration=0):
    if iteration % 4096 == 0:
        owner = getattr(_local, 'owner', None)
        if owner:
            owner.check()


@contextlib.contextmanager
def owned_process(command, **kwargs):
    owner = getattr(_local, 'owner', None)
    if owner:
        with owner.launch(command, **kwargs) as process:
            yield process
    else:
        with ToolOwner() as temporary:
            with temporary.launch(command, **kwargs) as process:
                yield process


def worker_entry(connection, progress, cancel, config, parent_pid):
    try:
        run_worker(connection, progress, cancel, config, parent_pid)
    except BaseException as error:
        try:
            connection.send(('worker_error', '%s: %s' % (type(error).__name__, error)))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


def run_worker(connection, progress, cancel, config, parent_pid):
    import VerifyVideo as vv
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if os.name != 'nt':
        os.setsid()
    # Parent assigns Windows job ownership before sending the first assignment.
    connection.send(('ready', None))
    with ToolOwner(cancel, parent_pid):
        while not cancel.is_set():
            if not connection.poll(0.1):
                continue
            index, path = connection.recv()
            if index is None:
                return
            sink = vv.EventReporter(index, connection, progress)
            started = time.monotonic()
            sink.emit('job_start')
            try:
                result = vv.verify(path, reporter=sink, **config)
                payload = dict(result=vv.Result(list(result.warnings), list(result.errors)),
                               elapsed=time.monotonic() - started, stages=sink.stages,
                               diagnostics=sink.diagnostics, cancelled=False)
            except Cancelled:
                payload = dict(result=None, elapsed=time.monotonic() - started,
                               stages=sink.stages, diagnostics=sink.diagnostics, cancelled=True)
            except Exception as error:
                payload = dict(result=vv.Result(warnings=[text for state, text in sink.diagnostics if state == 'warn'],
                                               errors=[text for state, text in sink.diagnostics if state == 'error'] +
                                               ['Worker exception: %s: %s' % (type(error).__name__, error)]),
                               elapsed=time.monotonic() - started, stages=sink.stages,
                               diagnostics=sink.diagnostics, cancelled=False)
            sink.emit('job_finish', **payload)


class FilePool:
    """A collector thread owns dispatch; UI latency cannot hold a worker slot.

    Each worker has a reliable control pipe and a bounded latest-progress slot.
    At most one assigned job per reusable worker.
    """
    def __init__(self, files, jobs, config, accept, target=worker_entry):
        self.files, self.jobs, self.config, self.accept = files, jobs, config, accept
        self.target = target
        self.ctx = multiprocessing.get_context('spawn')
        self.cancel = CancelFlag(self.ctx)
        self.progress = [ProgressSlot(self.ctx) for _ in range(jobs)]
        self.done = threading.Event()
        self.error = None
        self.workers = []
        self.thread = threading.Thread(target=self.run, name='verification-collector', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def run(self):
        next_job = 0
        try:
            for worker_index in range(self.jobs):
                if self.cancel.is_set():
                    break
                parent, child = self.ctx.Pipe()
                process = self.ctx.Process(target=self.target, args=(child, self.progress[worker_index], self.cancel,
                                                                    self.config, os.getpid()))
                record = dict(process=process, pipe=parent, job=None, owner=None, started=time.monotonic(),
                              ready=False, closed=False, exitcode=None)
                self.workers.append(record)
                try:
                    process.start()
                finally:
                    child.close()
                if os.name == 'nt':
                    record['owner'] = WindowsJob()
                    record['owner'].assign(process.pid)
            while self.workers and not self.cancel.is_set():
                # Always drain reliable events before drawing or accepting progress.
                live = [r['pipe'] for r in self.workers]
                for connection in wait(live, timeout=0.025):
                    record = next(r for r in self.workers if r['pipe'] is connection)
                    try:
                        kind, event = connection.recv()
                    except (EOFError, OSError):
                        kind, event = 'dead', None
                    if kind == 'ready':
                        record['ready'] = True
                    elif kind == 'event':
                        self.accept(event)
                        if event['kind'] == 'job_finish':
                            record['job'] = None
                    elif kind == 'dead':
                        self.worker_died(record)
                        continue
                    elif kind == 'worker_error':
                        record['failure'] = event
                        self.worker_died(record)
                        continue
                    if record['ready'] and record['job'] is None and next_job < len(self.files):
                        if not self.cancel.is_set():
                            record['job'] = next_job
                            connection.send((next_job, self.files[next_job]))
                            next_job += 1
                for slot in self.progress:
                    try:
                        self.accept(slot.get_nowait())
                    except queue.Empty:
                        pass
                for record in list(self.workers):
                    if not record['process'].is_alive() and not record['pipe'].poll():
                        self.worker_died(record)
                    elif not record['ready'] and time.monotonic() - record['started'] > 15:
                        raise RuntimeError('Worker startup handshake timed out')
                if next_job == len(self.files) and all(r['job'] is None for r in self.workers):
                    break
            if next_job < len(self.files) and not self.cancel.is_set():
                raise RuntimeError('Worker pool exhausted; remaining files were not checked')
        except BaseException as error:
            self.error = '%s: %s' % (type(error).__name__, error)
            self.cancel.set()
        finally:
            try:
                self.shutdown()
            except BaseException as error:
                self.error = self.error or 'Worker cleanup failed: %s' % error
            finally:
                self.done.set()

    def worker_died(self, record):
        import VerifyVideo as vv
        self.cleanup(record, force=True)
        self.workers.remove(record)
        # Cancellation can arrive while the collector is waiting on a pipe.
        # A worker exiting then is interrupted work, not a checked-file failure.
        if self.cancel.is_set():
            return
        if record['job'] is not None:
            self.accept(dict(job_index=record['job'], sequence=float('inf'), kind='job_finish',
                             result=vv.Result(errors=[record.get('failure') or
                                                      'Worker exited unexpectedly (code %s).' % record['exitcode']]),
                             elapsed=None, stages=None, diagnostics=None, cancelled=False))
        elif not record['ready']:
            raise RuntimeError(record.get('failure') or 'Worker failed before startup handshake')

    def cleanup(self, record, force=False):
        if record['closed']:
            return
        process, owner = record['process'], record['owner']
        if process.pid:
            if os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if force or process.is_alive():
                if owner:
                    owner.kill()
                elif os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.is_alive():
                    process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            record['exitcode'] = process.exitcode
            if not process.is_alive():
                process.close()
        if owner:
            owner.close()
        record['pipe'].close()
        record['closed'] = True

    def shutdown(self):
        for record in self.workers:
            if record['closed']:
                continue
            try:
                record['pipe'].send((None, None))
            except (OSError, BrokenPipeError):
                pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(r['process'].is_alive() for r in self.workers
                                                 if not r['closed'] and r['process'].pid):
            for record in self.workers:
                if record['closed']:
                    continue
                try:
                    while record['pipe'].poll():
                        kind, event = record['pipe'].recv()
                        if kind == 'event':
                            try:
                                self.accept(event)
                            except Exception as error:
                                self.error = self.error or 'Event collection failed: %s' % error
                except (EOFError, OSError):
                    pass
            time.sleep(0.02)
        for record in self.workers:
            try:
                self.cleanup(record)
            except Exception as error:
                self.error = self.error or 'Worker cleanup failed: %s' % error
                if record['owner']:
                    record['owner'].close()
                record['pipe'].close()
        self.progress = []

    def close(self):
        self.cancel.set()
        while not self.done.wait(0.1):
            # A second interrupt escalates via the collector's bounded shutdown.
            pass
        self.thread.join()
