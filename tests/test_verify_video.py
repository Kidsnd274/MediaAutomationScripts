from array import array
import contextlib
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import VerifyVideo as vv


def video(times=(0, 1, 2), index=0, tick=0.001):
    stream = vv.Stream(index, 'video', tick)
    for timestamp in times:
        stream.add_frame({'pts': str(round(timestamp / tick)), 'pkt_duration': str(round(1 / tick))})
    stream.finish([])
    return stream


def audio(duration, index=1, start=0):
    stream = vv.Stream(index, 'audio', 1 / 48000, 48000)
    if duration is not None:
        stream.add_frame({'pts': str(round(start * 48000)), 'nb_samples': str(round(duration * 48000))})
    return stream


class LogicTests(unittest.TestCase):
    def test_five_seconds_mismatch_warns_with_correct_units(self):
        stream = video(range(55))
        result = vv.Result()
        vv.duration_checks([stream, audio(60)], 60, result)
        self.assertEqual(result.status, 'OK WITH WARNINGS')
        self.assertTrue(any('5.000000s > 0.050000s' in w for w in result.warnings))

    def test_one_frame_tolerance_at_60fps(self):
        stream = video((0, 1 / 60, 2 / 60), tick=1 / 60000)
        result = vv.Result()
        vv.duration_checks([stream, audio(stream.duration + 0.020)], None, result)
        self.assertTrue(any('0.016667s tolerance' in w for w in result.warnings))

    def test_missing_audio_duration_is_unavailable(self):
        result = vv.Result()
        vv.duration_checks([video(), audio(None)], 3, result)
        self.assertEqual(result.errors, [])
        self.assertTrue(any('duration check unavailable' in w for w in result.warnings))

    def test_subtitle_overhang_only_warns(self):
        result = vv.Result()
        vv.duration_checks([video(), audio(3)], 10, result)
        self.assertEqual(result.status, 'OK WITH WARNINGS')
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('Container duration', result.warnings[0])

    def test_audio_offsets_warn(self):
        result = vv.Result()
        vv.duration_checks([video(), audio(3, start=0.5)], 3, result)
        self.assertTrue(any('start offset' in w for w in result.warnings))

    def test_video_only_is_valid(self):
        result = vv.Result()
        vv.duration_checks([video()], 3, result)
        self.assertEqual(result.status, 'OK')

    def test_estimate_final_frame_duration(self):
        stream = vv.Stream(0, 'video', 0.001)
        for timestamp in (0, 40, 80):
            stream.add_frame({'pts': str(timestamp)})
        warnings = []
        stream.finish(warnings)
        self.assertAlmostEqual(stream.duration, 0.12)
        self.assertTrue(any('estimated' in w for w in warnings))

    def test_missing_timestamps_are_preserved_as_missing(self):
        stream = vv.Stream(0, 'video', 0.001)
        stream.add_frame({})
        warnings = []
        stream.finish(warnings)
        self.assertEqual(stream.count, 1)
        self.assertIsNone(stream.duration)
        self.assertTrue(any('timing check incomplete' in w for w in warnings))

    def test_vfr_and_constant_shift_pass(self):
        source = video((0, 0.016, 0.050, 0.067))
        output = video((2, 2.016, 2.050, 2.067))
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertEqual(result.status, 'OK')

    def test_timestamp_rounding_passes(self):
        source = video((0, 1 / 60, 2 / 60), tick=1 / 60000)
        output = video((0, 0.017, 0.033), tick=0.001)
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertEqual(result.errors, [])

    def test_changed_frame_count_fails(self):
        result = vv.Result()
        vv.compare_original([video()], [video((0, 1))], result)
        self.assertTrue(any('Frame count changed' in e for e in result.errors))

    def test_duplicate_replacing_missing_frame_changes_timing(self):
        result = vv.Result()
        vv.compare_original([video((0, 1, 2, 3))], [video((0, 1, 1, 3))], result)
        self.assertTrue(any('Frame timing changed' in e and 'frame 3' in e for e in result.errors))

    def test_common_shift_applies_to_all_video_streams(self):
        result = vv.Result()
        vv.compare_original([video(), video(index=2)],
                            [video((5, 6, 7)), video((6, 7, 8), index=2)], result)
        self.assertTrue(any('Frame timing changed' in e for e in result.errors))

    def test_different_video_stream_count_fails(self):
        result = vv.Result()
        vv.compare_original([video(), video(index=2)], [video()], result)
        self.assertTrue(any('Video stream count changed' in e for e in result.errors))

    def test_missing_original_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            output = pathlib.Path(folder) / 'clip_new.mkv'
            output.touch()
            original, warning = vv.find_original(output)
            self.assertIsNone(original)
            self.assertIn('Original not found', warning)

    def test_original_different_extension_and_case(self):
        with tempfile.TemporaryDirectory() as folder:
            output = pathlib.Path(folder) / 'clip_new.MKV'
            original = pathlib.Path(folder) / 'CLIP.MP4'
            original.touch()
            self.assertEqual(vv.find_original(output), (original, None))

    def test_ambiguous_original_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            output = pathlib.Path(folder) / 'clip_new.mkv'
            (pathlib.Path(folder) / 'clip.mp4').touch()
            (pathlib.Path(folder) / 'clip.mkv').touch()
            original, warning = vv.find_original(output)
            self.assertIsNone(original)
            self.assertIn('Multiple possible originals', warning)

    def test_remove_only_one_new_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            output = pathlib.Path(folder) / 'clip_new_new.mkv'
            original = pathlib.Path(folder) / 'clip_new.mp4'
            original.touch()
            self.assertEqual(vv.find_original(output), (original, None))

    @mock.patch.object(vv.subprocess, 'run')
    def test_ffmpeg_errors_with_zero_exit_fail(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stderr='error while decoding MB\n')
        errors = vv.ffmpeg_check(pathlib.Path('clip.mkv'), [audio(3, index=0), video(index=3)], 'ffmpeg')
        self.assertIn('error while decoding MB', errors)
        command = run.call_args.args[0]
        self.assertIn('-xerror', command)
        self.assertIn('0:0', command)
        self.assertIn('0:3', command)

    @mock.patch.object(vv.subprocess, 'run')
    def test_metadata_selects_types_and_excludes_cover_art(self, run):
        data = {'streams': [
            {'index': 0, 'codec_type': 'audio', 'time_base': '1/48000', 'sample_rate': '48000'},
            {'index': 1, 'codec_type': 'subtitle'},
            {'index': 2, 'codec_type': 'video', 'time_base': '1/1000'},
            {'index': 3, 'codec_type': 'video', 'disposition': {'attached_pic': 1}}]}
        run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr='')
        streams, duration, errors = vv.probe_metadata(pathlib.Path('clip.mkv'), 'ffprobe')
        self.assertEqual([(s.index, s.kind) for s in streams], [(0, 'audio'), (2, 'video')])
        self.assertIsNone(duration)
        self.assertEqual(errors, [])

    @mock.patch.object(vv.subprocess, 'run')
    def test_malformed_probe_output_fails(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{oops', stderr='')
        result = vv.Result()
        vv.inspect_video(pathlib.Path('clip.mkv'), 'ffprobe', result)
        self.assertEqual(result.status, 'FAILED')

    @mock.patch.object(vv, 'scan_stream')
    @mock.patch.object(vv, 'probe_metadata')
    def test_source_error_is_warning_even_if_last_stream_succeeds(self, metadata, scan):
        metadata.return_value = ([video(), video(index=1)], 3, [])
        scan.side_effect = [['damaged packet'], []]
        result = vv.Result()
        vv.inspect_video(pathlib.Path('source.mkv'), 'ffprobe', result, source=True)
        self.assertEqual(result.errors, [])
        self.assertTrue(any('comparison is incomplete' in w for w in result.warnings))

    @mock.patch.object(vv, 'ffmpeg_check')
    @mock.patch.object(vv, 'inspect_video')
    def test_ignore_ffmpeg_warns_but_probe_errors_still_fail(self, inspect, ffmpeg):
        def inspect_file(file, probe, result):
            result.errors.append('damaged frame')
            return [video()], 3
        inspect.side_effect = inspect_file
        result = vv.verify(pathlib.Path('clip.mkv'), 'probe', 'ffmpeg', ignore_ffmpeg=True)
        ffmpeg.assert_not_called()
        self.assertEqual(result.status, 'FAILED')
        self.assertTrue(any('skipped' in w for w in result.warnings))

    @mock.patch.object(vv, 'verify')
    def test_batch_continues_and_accepts_uppercase_extensions(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('a.MKV', 'b.mp4'):
                (pathlib.Path(folder) / name).touch()
            verify.side_effect = [vv.Result(errors=['bad']), vv.Result()]
            with mock.patch.object(pathlib.Path, 'write_text'), contextlib.redirect_stdout(io.StringIO()):
                code = vv.main(['-f', folder])
            self.assertEqual(code, 1)
            self.assertEqual(verify.call_count, 2)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe required')
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = pathlib.Path(cls.temp.name)
        cls.ffmpeg = shutil.which('ffmpeg')
        cls.ffprobe = shutil.which('ffprobe')
        cls.clean = cls.folder / 'clean.mkv'
        cls.encode(cls.clean, ['-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=24:duration=2',
                               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2'],
                   ['-c:v', 'libx264', '-preset', 'fast', '-g', '24', '-c:a', 'aac'])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def encode(cls, path, inputs, outputs):
        process = subprocess.run([cls.ffmpeg, '-nostdin', '-v', 'error', *inputs, *outputs, '-y', str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if process.returncode:
            raise RuntimeError(process.stderr.decode(errors='replace'))

    def test_clean_cfr_passes_full_validation(self):
        result = vv.verify(self.clean, self.ffprobe, self.ffmpeg)
        self.assertEqual(result.errors, [], result.errors)

    def corrupted_copy(self, name):
        probe = subprocess.run([self.ffprobe, '-v', 'quiet', '-show_packets', '-show_entries',
                                'packet=codec_type,pos,size', '-of', 'json', str(self.clean)],
                               stdout=subprocess.PIPE, check=True)
        packets = [p for p in json.loads(probe.stdout)['packets'] if p['codec_type'] == 'video']
        packet = packets[len(packets) // 2 + 3]
        data = bytearray(self.clean.read_bytes())
        start = int(packet['pos']) + 30
        stop = min(start + 30, int(packet['pos']) + int(packet['size']) - 10)
        for index in range(start, stop):
            data[index] ^= 0xA5
        corrupted = self.folder / name
        corrupted.write_bytes(data)
        return corrupted

    def test_corruption_is_detected_despite_unchanged_frame_count(self):
        corrupted = self.corrupted_copy('corrupted.mkv')
        clean_streams, _, _ = vv.probe_metadata(self.clean, self.ffprobe)
        damaged_streams, _, _ = vv.probe_metadata(corrupted, self.ffprobe)
        clean_video = next(s for s in clean_streams if s.kind == 'video')
        damaged_video = next(s for s in damaged_streams if s.kind == 'video')
        vv.scan_stream(self.clean, clean_video, self.ffprobe)
        vv.scan_stream(corrupted, damaged_video, self.ffprobe)
        self.assertEqual(clean_video.count, damaged_video.count)
        result = vv.verify(corrupted, self.ffprobe, self.ffmpeg, ignore_ffmpeg=True)
        self.assertTrue(result.errors, 'FFprobe must catch recoverable corruption even when FFmpeg is skipped')

    def test_original_decode_errors_only_warn(self):
        self.corrupted_copy('inherited.mkv')
        output = self.folder / 'inherited_new.mkv'
        shutil.copyfile(self.clean, output)
        with contextlib.redirect_stdout(io.StringIO()):
            result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True)
        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(any('comparison is incomplete' in w for w in result.warnings), result.warnings)

    def test_real_subtitle_overhang_does_not_fail(self):
        subtitles = self.folder / 'overhang.srt'
        subtitles.write_text('1\n00:00:01,000 --> 00:00:07,000\nOverhang\n', encoding='utf-8')
        output = self.folder / 'overhang.mkv'
        self.encode(output, ['-i', str(self.clean), '-i', str(subtitles)],
                    ['-map', '0', '-map', '1', '-c', 'copy'])
        result = vv.verify(output, self.ffprobe, self.ffmpeg)
        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(any('Container duration' in w for w in result.warnings))

    def test_missing_original_warns_without_failing_output(self):
        output = self.folder / 'lonely_new.mkv'
        shutil.copyfile(self.clean, output)
        result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True)
        self.assertEqual(result.status, 'OK WITH WARNINGS')
        self.assertTrue(any('Original not found' in w for w in result.warnings))

    def test_cli_continues_after_unreadable_video_and_writes_report(self):
        folder = self.folder / 'batch'
        folder.mkdir()
        (folder / '00broken.mkv').write_bytes(b'not a video')
        shutil.copyfile(self.clean, folder / '99clean.MKV')
        process = subprocess.run([sys.executable, '-B', vv.__file__, '-f', str(folder)], cwd=folder,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertIn('FAILED: 00broken.mkv', process.stdout)
        self.assertIn('99clean.MKV', process.stdout)
        self.assertIn('2 video(s), 1 failure(s)', process.stdout)
        self.assertEqual((folder / 'corrupted_files.txt').read_text().strip(), '00broken.mkv')

    def test_vfr_remux_and_timestamp_rounding_pass(self):
        original = self.folder / 'vfr.mp4'
        output = self.folder / 'vfr_new.mkv'
        self.encode(original, ['-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=30:duration=1'],
                    ['-vf', r'setpts=if(lt(N\,15)\,N/(30*TB)\,(15/30+(N-15)/20)/TB)',
                     '-fps_mode', 'passthrough', '-enc_time_base:v', '1:60000',
                     '-c:v', 'libx264', '-preset', 'fast'])
        self.encode(output, ['-copyts', '-i', str(original)], ['-map', '0', '-c', 'copy'])
        with contextlib.redirect_stdout(io.StringIO()):
            result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True)
        self.assertEqual(result.errors, [], result.errors)

    def test_audio_first_and_multiple_audio_tracks(self):
        path = self.folder / 'reordered.mkv'
        self.encode(path, ['-i', str(self.clean)],
                    ['-map', '0:a', '-map', '0:v', '-map', '0:a', '-c', 'copy'])
        result = vv.verify(path, self.ffprobe, self.ffmpeg)
        self.assertEqual(result.errors, [], result.errors)

    def test_video_only(self):
        path = self.folder / 'silent.mkv'
        self.encode(path, ['-i', str(self.clean)], ['-map', '0:v', '-c', 'copy'])
        result = vv.verify(path, self.ffprobe, self.ffmpeg)
        self.assertEqual(result.errors, [], result.errors)

    def check_10bit_encode(self, encoder, options):
        encoders = subprocess.run([self.ffmpeg, '-hide_banner', '-encoders'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
        if encoder not in encoders:
            self.skipTest(f'{encoder} encoder unavailable')
        name = encoder.replace('-', '_')
        original = self.folder / (name + '.mkv')
        output = self.folder / (name + '_new.mkv')
        shutil.copyfile(self.clean, original)
        self.encode(output, ['-copyts', '-i', str(original)],
                    ['-map', '0', '-c:v', encoder, '-pix_fmt', 'yuv420p10le',
                     '-fps_mode', 'passthrough', '-enc_time_base:v', '1:1000',
                     *options, '-c:a', 'copy'])
        with contextlib.redirect_stdout(io.StringIO()):
            result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True)
        self.assertEqual(result.errors, [], result.errors)

    def test_10bit_hevc_encode(self):
        self.check_10bit_encode('libx265', ['-preset', 'ultrafast', '-x265-params',
                                           'pools=1:frame-threads=1:log-level=error'])

    def test_10bit_av1_encode(self):
        self.check_10bit_encode('libaom-av1', ['-cpu-used', '8', '-threads', '2', '-crf', '35'])

    def test_truncating_both_audio_and_video_fails_source_comparison(self):
        original = self.folder / 'shortened.mkv'
        output = self.folder / 'shortened_new.mkv'
        shutil.copyfile(self.clean, original)
        self.encode(output, ['-i', str(original)],
                    ['-t', '1', '-c:v', 'libx264', '-preset', 'fast', '-c:a', 'aac'])
        with contextlib.redirect_stdout(io.StringIO()):
            result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True)
        self.assertTrue(any('Frame count changed' in e for e in result.errors), result.errors)


if __name__ == '__main__':
    unittest.main()
