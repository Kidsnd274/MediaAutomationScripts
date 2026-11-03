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

    @mock.patch.object(vv, 'run_streaming')
    def test_ffmpeg_errors_with_zero_exit_fail(self, run):
        run.return_value = (0, ['error while decoding MB'])
        errors = vv.ffmpeg_check(pathlib.Path('clip.mkv'), [audio(3, index=0), video(index=3)], 'ffmpeg')
        self.assertIn('error while decoding MB', errors)
        command = run.call_args.args[0]
        self.assertIn('-xerror', command)
        self.assertIn('0:0', command)
        self.assertIn('0:3', command)
        self.assertIn('pipe:1', command)

    @mock.patch.object(vv, 'run_streaming')
    def test_metadata_selects_types_and_excludes_cover_art(self, run):
        data = {'streams': [
            {'index': 0, 'codec_type': 'audio', 'time_base': '1/48000', 'sample_rate': '48000'},
            {'index': 1, 'codec_type': 'subtitle'},
            {'index': 2, 'codec_type': 'video', 'time_base': '1/1000'},
            {'index': 3, 'codec_type': 'video', 'disposition': {'attached_pic': 1}}]}
        def metadata(command, consume):
            consume(json.dumps(data))
            return 0, []
        run.side_effect = metadata
        streams, duration, errors = vv.probe_metadata(pathlib.Path('clip.mkv'), 'ffprobe')
        self.assertEqual([(s.index, s.kind) for s in streams], [(0, 'audio'), (2, 'video')])
        self.assertIsNone(duration)
        self.assertEqual(errors, [])

    @mock.patch.object(vv, 'run_streaming')
    def test_malformed_probe_output_fails(self, run):
        def metadata(command, consume):
            consume('{oops')
            return 0, []
        run.side_effect = metadata
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
        def inspect_file(file, probe, result, **kwargs):
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

    def rounded_pair(self):
        # ShadowPlay-style jitter, followed by encoder rounding to a 60 Hz grid.
        source = video((0, 0.016522, 0.033811, 0.050744, 0.066300, 0.083333), tick=1 / 90000)
        output = video(tuple(round(t * 60) / 60 for t in source.timestamps), tick=1 / 15360)
        output.frame_rate = 60
        return source, output

    def test_nearest_frame_grid_rounding_warns_instead_of_failing(self):
        source, output = self.rounded_pair()
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertEqual(result.status, 'OK WITH WARNINGS', result.errors)
        self.assertTrue(any('Frame timing rounded' in w for w in result.warnings))

    def test_strict_timing_rejects_the_same_rounding(self):
        source, output = self.rounded_pair()
        result = vv.Result()
        vv.compare_original([source], [output], result, strict_timing=True)
        self.assertTrue(any('Frame timing changed' in e for e in result.errors))

    def test_small_arbitrary_jitter_is_not_excused_as_grid_rounding(self):
        source, output = self.rounded_pair()
        output.timestamps[2] += 0.001
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertTrue(any('Frame timing changed' in e for e in result.errors))

    def test_progressive_drift_fails_even_if_output_is_on_a_grid(self):
        source = video(tuple(i / 60 for i in range(10)), tick=1 / 60000)
        output = video(tuple(i / 50 for i in range(10)), tick=1 / 60000)
        output.frame_rate = 50
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertTrue(any('Frame timing changed' in e for e in result.errors))

    def test_quantization_does_not_relax_frame_counts(self):
        source, output = self.rounded_pair()
        output.timestamps.pop()
        output.count -= 1
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertTrue(any('Frame count changed' in e for e in result.errors))
        self.assertFalse(any('Frame timing rounded' in w for w in result.warnings))

    def test_rounding_with_a_constant_shift_warns(self):
        source, output = self.rounded_pair()
        output.timestamps = array('d', (t + 5 for t in output.timestamps))
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertEqual(result.status, 'OK WITH WARNINGS', result.errors)

    def test_quantization_collision_warns_and_strict_mode_fails(self):
        source = video((0, 0.027689, 0.033722, 0.050044), tick=1 / 90000)
        output = video((0, 1 / 30, 1 / 30, 0.05), tick=1 / 15360)
        output.frame_rate = 60
        result = vv.Result()
        vv.compare_original([source], [output], result)
        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(any('Duplicate presentation' in w for w in result.warnings))
        strict = vv.Result()
        vv.compare_original([source], [output], strict, strict_timing=True)
        self.assertTrue(any('Duplicate presentation' in e for e in strict.errors))

    def test_backwards_output_timestamps_fail_without_an_original(self):
        result = vv.Result()
        vv.timestamp_checks([video((0, 0.04, 0.02))], result)
        self.assertTrue(any('Backwards presentation' in e for e in result.errors))

    def test_inherited_audio_end_difference_is_identified(self):
        source = [video((0, 1, 2)), audio(2.9)]
        result = vv.Result()
        vv.duration_checks([video((0, 1, 2)), audio(2.9)], None, result, original=source)
        self.assertTrue(any('Already present in original' in w for w in result.warnings))

    def test_new_audio_end_difference_is_not_labelled_inherited(self):
        source = [video((0, 1, 2)), audio(3)]
        result = vv.Result()
        vv.duration_checks([video((0, 1, 2)), audio(2.9)], None, result, original=source)
        self.assertTrue(any('A/V duration mismatch' in w for w in result.warnings))
        self.assertFalse(any('Already present' in w for w in result.warnings))

    def test_larger_video_overhang_is_not_labelled_inherited(self):
        source = [video((0, 1, 2)), audio(2.9)]
        result = vv.Result()
        vv.duration_checks([video((0, 1, 2, 3)), audio(2.9)], None, result, original=source)
        self.assertFalse(any('Already present' in w for w in result.warnings))

    @mock.patch.object(vv, 'verify', return_value=vv.Result())
    def test_cli_banner_file_list_and_strict_option(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            (pathlib.Path(folder) / 'clip_new.mp4').touch()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = vv.main(['-f', folder, '-s', '--strict-timing'])
        text = output.getvalue()
        banner = text.split('\n\n')[0]
        for line in ('VerifyVideo', 'Script to check for corrupted videos', 'by Kidsnd274',
                     'https://github.com/Kidsnd274/MediaAutomationScripts'):
            self.assertIn(line, banner)
        self.assertNotIn('\033[', text, 'redirected output must not contain colour codes')
        self.assertLess(text.index('1 video found'), text.index('[1/1] clip_new.mp4'))
        self.assertIn('clip_new.mp4', text.split('[1/1]')[0])
        self.assertRegex(text, r'1\s+PASS\s+0\s+0\s+\S+\s+clip_new\.mp4')
        self.assertTrue(verify.call_args.kwargs['strict_timing'])
        self.assertEqual(code, 0)

    @mock.patch.object(vv, 'verify', return_value=vv.Result())
    def test_file_selection_only_checks_the_requested_file(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            selected = pathlib.Path(folder) / 'selected video.MP4'
            selected.touch()
            (pathlib.Path(folder) / 'unselected.mp4').touch()
            output = io.StringIO()
            with mock.patch.object(vv, 'current_directory', pathlib.Path(folder)), contextlib.redirect_stdout(output):
                code = vv.main(['--file', str(selected)])
            self.assertEqual(code, 0)
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[0], selected.resolve())
            self.assertIn('1 video found', output.getvalue())
            self.assertNotIn('unselected.mp4', output.getvalue())

    @mock.patch.object(vv, 'verify', return_value=vv.Result())
    def test_files_and_folders_are_combined_and_deduplicated(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            batch = root / 'batch'
            nested = batch / 'nested'
            nested.mkdir(parents=True)
            expected = [batch / 'a.mp4', batch / 'b.MKV', nested / 'c.webm', root / 'extra.avi']
            for file in expected:
                file.touch()
            with contextlib.redirect_stdout(io.StringIO()):
                code = vv.main(['-f', str(batch), '--file', str(expected[0]), str(expected[-1]),
                                '--file', str(expected[-1]), '-r'])
            self.assertEqual(code, 0)
            self.assertEqual([call.args[0] for call in verify.call_args_list], sorted(p.resolve() for p in expected))

    @mock.patch.object(vv, 'verify', return_value=vv.Result())
    def test_file_selection_forwards_comparison_and_decode_options(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            selected = pathlib.Path(folder) / 'clip_new.mp4'
            selected.touch()
            with contextlib.redirect_stdout(io.StringIO()):
                code = vv.main(['--file', str(selected), '-s', '--strict-timing', '-if'])
            self.assertEqual(code, 0)
            self.assertEqual(verify.call_args.args[3:5], (True, True))
            self.assertTrue(verify.call_args.kwargs['strict_timing'])

    @mock.patch.object(vv, 'verify', return_value=vv.Result())
    def test_invalid_file_selections_fail_and_valid_files_continue(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            selected = root / 'valid.mp4'
            selected.touch()
            unsupported = root / 'notes.txt'
            unsupported.touch()
            missing = root / 'missing.mp4'
            output = io.StringIO()
            with mock.patch.object(pathlib.Path, 'write_text') as report, contextlib.redirect_stdout(output):
                code = vv.main(['--file', str(missing), str(unsupported), str(root), str(selected)])
            self.assertEqual(code, 1)
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[0], selected.resolve())
            self.assertIn('Unsupported video extension', output.getvalue())
            self.assertIn('use --folder for directories', output.getvalue())
            self.assertEqual(report.call_args.args[0].splitlines(), ['missing.mp4', 'notes.txt', root.name])

    @mock.patch.object(vv, 'verify')
    def test_staxrip_rejects_an_explicit_file_without_new_suffix(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            selected = pathlib.Path(folder) / 'clip.mp4'
            selected.touch()
            output = io.StringIO()
            with mock.patch.object(pathlib.Path, 'write_text'), contextlib.redirect_stdout(output):
                code = vv.main(['--file', str(selected), '-s'])
            self.assertEqual(code, 1)
            verify.assert_not_called()
            self.assertIn("--staxrip requires a filename ending in '_new'", output.getvalue())

    @mock.patch.object(vv, 'verify')
    def test_missing_file_does_not_trigger_a_default_folder_scan(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / 'unselected.mp4').touch()
            with mock.patch.object(vv, 'current_directory', root), mock.patch.object(pathlib.Path, 'write_text'), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = vv.main(['--file', str(root / 'missing.mp4')])
            self.assertEqual(code, 1)
            verify.assert_not_called()

    @mock.patch.object(vv, 'verify', return_value=vv.Result())
    def test_no_selection_still_defaults_to_script_folder(self, verify):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            selected = root / 'default.mp4'
            selected.touch()
            with mock.patch.object(vv, 'current_directory', root), contextlib.redirect_stdout(io.StringIO()):
                code = vv.main([])
            self.assertEqual(code, 0)
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[0], selected)

    def test_interactive_progress_is_throttled_and_finishes_on_a_new_line(self):
        output = io.StringIO()
        output.isatty = lambda: True
        with mock.patch.object(vv.time, 'monotonic', side_effect=[0, 0.1, 1, 2]):
            with vv.Progress(vv.Reporter(output), 'FFprobe', 'video stream 0', 10) as progress:
                progress.update(1, 60)
                progress.update(5, 300)
        text = output.getvalue()
        self.assertIn('50.0%', text)
        self.assertNotIn('10.0%', text)
        self.assertIn('300 frames', text)
        self.assertTrue(text.endswith('\n'))

    def test_redirected_progress_is_sparse_and_does_not_use_carriage_returns(self):
        output = io.StringIO()
        with mock.patch.object(vv.time, 'monotonic', side_effect=[0, 1, 31, 32]):
            with vv.Progress(vv.Reporter(output), 'FFmpeg', 'decode', 10) as progress:
                progress.update(1)
                progress.update(5)
        text = output.getvalue()
        self.assertNotIn('\r', text)
        self.assertNotIn('10.0%', text)
        self.assertIn('50.0%', text)
        self.assertEqual(len(text.splitlines()), 3)

    def test_missing_duration_shows_processed_time_without_a_percentage(self):
        output = io.StringIO()
        with mock.patch.object(vv.time, 'monotonic', side_effect=[0, 31, 32]):
            with vv.Progress(vv.Reporter(output), 'FFprobe', 'video stream 0') as progress:
                progress.update(5, 300)
        text = output.getvalue()
        self.assertIn('0:05.00 processed', text)
        self.assertNotIn('%', text)

    def test_stage_failures_are_marked_in_the_completed_line(self):
        output = io.StringIO()
        with self.assertRaises(ValueError):
            with vv.Progress(vv.Reporter(output), 'FFprobe', 'original metadata', severity='warn'):
                raise ValueError('bad')
        with vv.Progress(vv.Reporter(output), 'FFmpeg', 'decode all A/V streams') as progress:
            progress.state = 'error'
        lines = output.getvalue().splitlines()
        self.assertRegex(lines[1], r'▲ FFprobe\s+original metadata')
        self.assertIn('see warnings below', lines[1])
        self.assertRegex(lines[3], r'✗ FFmpeg\s+decode all A/V streams')
        self.assertIn('see errors below', lines[3])

    def test_colour_only_on_terminals_and_respects_no_color(self):
        terminal = io.StringIO()
        terminal.isatty = lambda: True
        with mock.patch.object(vv, 'enable_ansi', return_value=True), mock.patch.dict(vv.os.environ, clear=True):
            self.assertIn('\033[32m', vv.Reporter(terminal).paint('ok', vv.GREEN))
            with mock.patch.dict(vv.os.environ, {'NO_COLOR': '1'}):
                self.assertEqual(vv.Reporter(terminal).paint('ok', vv.GREEN), 'ok')
        self.assertEqual(vv.Reporter(io.StringIO()).paint('ok', vv.GREEN), 'ok')

    def test_legacy_encodings_fall_back_to_ascii_symbols(self):
        output = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        reporter = vv.Reporter(output)
        self.assertEqual(reporter.symbols['done'], '+')
        line = reporter.stage('done', 'FFprobe', 'metadata', ['3 A/V streams', '0:01.00'], 0.5)
        line.encode('cp1252')
        self.assertIn('3 A/V streams | 0:01.00', line)

    def test_fit_truncates_visible_text_without_breaking_colour_codes(self):
        text = '\033[32m' + 'a' * 10 + '\033[0m' + 'b' * 10
        fitted = vv.fit(text, 12)
        self.assertEqual(vv.visible_width(fitted), 12)
        self.assertTrue(fitted.startswith('\033[32m' + 'a' * 10 + '\033[0m'))

    @mock.patch.object(vv, 'run_streaming')
    def test_ffmpeg_progress_uses_microseconds_and_keeps_decode_errors(self, run):
        progress = mock.Mock()
        def stream(command, consume):
            for line in ('frame=60', 'out_time_us=1000000', 'progress=continue',
                         'out_time_us=N/A', 'progress=end'):
                consume(line)
            return 1, ['decode error']
        run.side_effect = stream
        errors = vv.ffmpeg_check(pathlib.Path('clip.mp4'), [video()], 'ffmpeg', progress)
        self.assertEqual(progress.update.call_args_list, [mock.call(1.0, 60), mock.call(None, 60)])
        self.assertIn('decode error', errors)
        self.assertTrue(any('exited with code 1' in e for e in errors))

    @mock.patch.object(vv, 'scan_stream', return_value=[])
    @mock.patch.object(vv, 'probe_metadata')
    def test_original_audio_is_scanned_for_inherited_duration_checks(self, metadata, scan):
        metadata.return_value = ([video(), audio(3)], 3, [])
        vv.inspect_video(pathlib.Path('source.mp4'), 'ffprobe', vv.Result(), source=True)
        self.assertEqual([call.args[1].kind for call in scan.call_args_list], ['video', 'audio'])


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
        self.assertRegex(process.stdout, r'FAIL\s+\d+\s+\d+\s+\S+\s+00broken\.mkv')
        self.assertIn('99clean.MKV', process.stdout)
        self.assertIn('1 failed', process.stdout)
        self.assertIn('2 videos in', process.stdout)
        self.assertEqual((folder / 'corrupted_files.txt').read_text().strip(), '00broken.mkv')

    def test_cli_specific_file_compares_original_without_checking_neighbors(self):
        folder = self.folder / 'single_file'
        folder.mkdir()
        original = folder / 'selected.mkv'
        selected = folder / 'selected_new.mkv'
        shutil.copyfile(self.clean, original)
        shutil.copyfile(self.clean, selected)
        (folder / 'unselected_new.mkv').write_bytes(b'not a video')
        process = subprocess.run([sys.executable, '-B', vv.__file__, '--file', str(selected),
                                  '-s', '--strict-timing'], cwd=folder,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertRegex(process.stdout, r'Original\s+selected\.mkv')
        self.assertIn('0 failed', process.stdout)
        self.assertIn('1 video in', process.stdout)
        self.assertNotIn('unselected_new.mkv', process.stdout)
        self.assertFalse((folder / 'corrupted_files.txt').exists())

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

    def test_quantized_encode_warns_and_precise_encode_passes_strict(self):
        original = self.folder / 'jitter.mp4'
        output = self.folder / 'jitter_new.mp4'
        self.encode(original, ['-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=60:duration=1'],
                    ['-vf', r'settb=1/90000,setpts=PTS+if(eq(mod(N\,4)\,1)\,90\,if(eq(mod(N\,4)\,2)\,-90\,0))',
                     '-fps_mode', 'passthrough', '-enc_time_base:v', '1:90000',
                     '-c:v', 'libx264', '-preset', 'fast'])
        self.encode(output, ['-copyts', '-i', str(original)],
                    ['-c:v', 'libx264', '-preset', 'fast', '-fps_mode', 'passthrough'])
        log = io.StringIO()
        result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True, reporter=vv.Reporter(log))
        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(any('Frame timing rounded' in w for w in result.warnings), result.warnings)
        for stage in (r'FFprobe\s+metadata', r'FFprobe\s+video stream', r'FFmpeg\s+decode all',
                      r'FFprobe\s+original metadata', r'Compare\s+frame counts', r'Check\s+A/V durations'):
            self.assertRegex(log.getvalue(), stage)
        strict = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True, strict_timing=True)
        self.assertTrue(any('Frame timing changed' in e for e in strict.errors), strict.errors)
        self.encode(output, ['-copyts', '-i', str(original)],
                    ['-c:v', 'libx264', '-preset', 'fast', '-fps_mode', 'passthrough',
                     '-enc_time_base:v', '-1'])
        precise = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True, strict_timing=True)
        self.assertEqual(precise.errors, [], precise.errors)

    def test_inherited_audio_tail_warning_is_labelled(self):
        original = self.folder / 'audio_tail.mkv'
        output = self.folder / 'audio_tail_new.mkv'
        self.encode(original, ['-i', str(self.clean)],
                    ['-c:v', 'copy', '-af', 'atrim=duration=1', '-c:a', 'aac'])
        self.encode(output, ['-i', str(original)], ['-map', '0', '-c', 'copy'])
        result = vv.verify(output, self.ffprobe, self.ffmpeg, staxrip=True)
        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(any('Already present in original' in w for w in result.warnings), result.warnings)

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
