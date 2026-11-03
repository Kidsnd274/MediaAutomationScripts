import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tools import benchmark_verify_video as bench


FAKE = '''import argparse, os
p = argparse.ArgumentParser()
p.add_argument('--file', nargs='+')
p.add_argument('--jobs')
p.add_argument('--decode-threads')
a = p.parse_args()
count = 2 if a.decode_threads == '2' else 1
for index, path in enumerate(sorted(set(a.file)), 1):
    print('  `- x [decoder @ %016x] fixed diagnostic' % os.getpid())
    print('  %d FAIL %d 0 0:01.00 %s' % (index, count, path))
raise SystemExit(1)
'''


class RunnerTests(unittest.TestCase):
    def test_worker_failure_is_not_a_complete_media_verdict(self):
        output = '  `- x Worker exited unexpectedly (code 23).\n  1 FAIL 1 0 unknown a.mp4\n'
        self.assertFalse(bench.signature(output, 1)['complete'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = pathlib.Path(self.temp.name)
        self.script = self.folder / 'fake.py'
        self.script.write_text(FAKE, encoding='utf-8')
        self.files = [self.folder / 'a.mp4', self.folder / 'b.mkv']
        for path in self.files:
            path.write_bytes(b'fixture')
        self.output = self.folder / 'results'

    def args(self):
        return ['--script', str(self.script), '--file', *map(str, self.files),
                '--output-dir', str(self.output), '--repeats', '1']

    def run_quiet(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bench.main(args)

    def records(self):
        return [json.loads(line) for line in (self.output / 'results.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_consistent_failures_are_valid_and_context_addresses_are_ignored(self):
        self.assertEqual(self.run_quiet(self.args() + ['--jobs', '1', '2']), 0)
        records = self.records()
        self.assertEqual([r['returncode'] for r in records], [1, 1])
        self.assertTrue(all(r['equivalent'] for r in records))
        self.assertTrue(all(r['signature']['complete'] for r in records))
        self.assertIn('<address>', records[0]['signature']['diagnostics'][0])
        self.assertTrue((self.output / 'manifest.json').exists())
        self.assertTrue((self.output / 'summary.md').exists())
        self.assertTrue(all(path.read_bytes() == b'fixture' for path in self.files))

    def test_changed_verdict_stops_recommendation(self):
        self.assertEqual(self.run_quiet(self.args() + ['--jobs', '1', '--decode-threads', 'default', '2']), 1)
        self.assertFalse(self.records()[-1]['equivalent'])
        self.assertIn('NO', (self.output / 'summary.md').read_text(encoding='utf-8'))

    def test_previous_artifacts_are_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / 'keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaises(SystemExit) as error:
            self.run_quiet(self.args())
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')

    def test_legacy_serial_omits_new_flags(self):
        self.assertEqual(self.run_quiet(self.args() + ['--jobs', '1', '--legacy-serial']), 0)
        command = self.records()[0]['command']
        self.assertNotIn('--jobs', command)
        self.assertNotIn('--decode-threads', command)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe required')
    def test_real_serial_and_spawned_verifier_with_optional_metrics(self):
        first, second = self.folder / 'clip1.mkv', self.folder / 'clip2.mkv'
        subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-v', 'error', '-f', 'lavfi',
                        '-i', 'testsrc2=size=96x64:rate=10:duration=0.3', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-y', str(first)], check=True, capture_output=True)
        shutil.copyfile(first, second)
        args = ['--file', str(first), str(second), '--output-dir', str(self.output), '--jobs', '1', '2', '--repeats', '1']
        if os.name == 'nt':
            args.append('--sample-resources')
        self.assertEqual(self.run_quiet(args), 0)
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r['equivalent'] and r['returncode'] == 0 for r in records))
        if os.name == 'nt':
            self.assertGreater(records[1]['sampled_peak_tree_rss_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
