"""Windows process-tree samples for the manual benchmark (stdlib only).

Counters are sampled lower bounds: processes that exit between samples can be
missed. Read bytes include cached I/O and pipes, not physical disk traffic.
"""
import ctypes as c
from ctypes import wintypes as w


class ProcessEntry(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD), ('heap', c.c_size_t),
                ('module', w.DWORD), ('threads', w.DWORD), ('parent', w.DWORD),
                ('priority', w.LONG), ('flags', w.DWORD), ('exe', w.WCHAR * 260)]


class Memory(c.Structure):
    _fields_ = [('size', w.DWORD), ('faults', w.DWORD)] + [(name, c.c_size_t) for name in
                ('peak_rss', 'rss', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]


class IO(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read', 'write', 'other')]


class Metrics:
    def __init__(self, root):
        self.root = root
        self.api = c.WinDLL('kernel32', use_last_error=True)
        self.ps = c.WinDLL('psapi', use_last_error=True)
        self.api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.api.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
        self.api.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(c.c_uint64)] * 4
        self.api.GetProcessIoCounters.argtypes = [w.HANDLE, c.POINTER(IO)]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.ps.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(Memory), w.DWORD]
        self.seen = {}
        self.peak = 0
        self.python_peak = 0
        self.tool_peak = 0
        self.samples = 0

    def sample(self):
        snapshot = self.api.CreateToolhelp32Snapshot(2, 0)
        if snapshot == w.HANDLE(-1).value:
            return
        entries = {}
        try:
            entry = ProcessEntry()
            entry.size = c.sizeof(entry)
            more = self.api.Process32FirstW(snapshot, c.byref(entry))
            while more:
                entries[entry.pid] = (entry.parent, entry.exe)
                more = self.api.Process32NextW(snapshot, c.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        selected = {self.root}
        while True:
            before = len(selected)
            selected.update(pid for pid, (parent, _) in entries.items() if parent in selected)
            if len(selected) == before:
                break
        rss = {'python': 0, 'tool': 0}
        for pid in selected:
            handle = self.api.OpenProcess(0x0400 | 0x0010, False, pid)
            if not handle:
                continue
            try:
                category = 'tool' if entries.get(pid, (0, ''))[1].lower().startswith(('ffmpeg', 'ffprobe')) else 'python'
                created, ended, kernel, user = [c.c_uint64() for _ in range(4)]
                memory, io = Memory(), IO()
                memory.size = c.sizeof(memory)
                if self.api.GetProcessTimes(handle, c.byref(created), c.byref(ended), c.byref(kernel), c.byref(user)):
                    self.api.GetProcessIoCounters(handle, c.byref(io))
                    # Creation time disambiguates PID reuse.
                    self.seen[(pid, created.value)] = (category, (kernel.value + user.value) / 1e7, io.read)
                if self.ps.GetProcessMemoryInfo(handle, c.byref(memory), c.sizeof(memory)):
                    rss[category] += memory.rss
            finally:
                self.api.CloseHandle(handle)
        self.peak = max(self.peak, sum(rss.values()))
        self.python_peak = max(self.python_peak, rss['python'])
        self.tool_peak = max(self.tool_peak, rss['tool'])
        self.samples += 1

    def result(self):
        return dict(sample_count=self.samples, sampled_peak_tree_rss_bytes=self.peak,
                    sampled_peak_python_rss_bytes=self.python_peak, sampled_peak_tool_rss_bytes=self.tool_peak,
                    sampled_python_cpu_seconds=sum(cpu for kind, cpu, _ in self.seen.values() if kind == 'python'),
                    sampled_tool_cpu_seconds=sum(cpu for kind, cpu, _ in self.seen.values() if kind == 'tool'),
                    sampled_tree_read_bytes=sum(read for _, _, read in self.seen.values()))
