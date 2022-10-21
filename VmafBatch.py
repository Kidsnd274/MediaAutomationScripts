import json
import pathlib
import subprocess
import sys

class Vid:
    def __init__(self, reference, distorted):
        self.reference = pathlib.Path(reference)
        self.distorted = pathlib.Path(distorted)

# CONFIGURATION
n_threads = 16 # Number of threads
n_subsample = 2 # Interval for frame sub-sampling, amount of frames processed reduced to 1/N

# List video file names here
videos = [Vid("reference.mkv", "distorted_hevc.mkv"),
          Vid("reference2.mp4", "distorted_av1.mp4"),
          ]





current_directory = pathlib.Path(__file__).parent.absolute()
# Setting ffmpeg and ffprobe locations
locations_ffmpeg = [current_directory / "ffmpeg.exe",
             current_directory / "ffmpeg"]

ffmpeg_found = False
for location in locations_ffmpeg:
    if location.exists():
        ffmpeg_exec = location.resolve()
        found = True
        break
if not ffmpeg_found:
    if sys.platform == 'win32':
        ffmpeg_exec = "ffmpeg.exe"
    else:
        ffmpeg_exec = "ffmpeg"

def run_vmaf(video): # ffmpeg -i "distorted.mkv" -i "reference.mkv" -lavfi libvmaf='n_threads=16:n_subsample=1:log_path=log' -f null -
    print("Reference:", video.reference.name)
    print("Distorted:", video.distorted.name)
    log_f = pathlib.Path(video.distorted.stem + "_log.json")
    args = [ffmpeg_exec, 
            "-i", video.distorted.resolve(), 
            "-i", video.reference.resolve(),
            "-lavfi", "libvmaf='n_threads=" + str(n_threads) + ":n_subsample=" + str(n_subsample) + ":log_path=" + log_f.name + ":log_fmt=json",
            "-hide_banner",
            "-v", "quiet",
            "-stats",
            "-f", "null",
            "-"
            ]
    process = subprocess.Popen(args)
    process.communicate()
    if process.returncode != 0:
        print("ERROR")
        print(process.stdout)
        print(process.stderr)
        print("ffmpeg run failed")
        print("")
        return False
    with open(log_f) as f:
        data = json.load(f)
        vmaf_min = data["pooled_metrics"]["vmaf"]["min"]
        vmaf_max = data["pooled_metrics"]["vmaf"]["max"]
        vmaf_mean = data["pooled_metrics"]["vmaf"]["mean"]
        vmaf_harmonic_mean = data["pooled_metrics"]["vmaf"]["harmonic_mean"]
    print("VMAF Stats:\nMean {0}, Min {1}, Max {2}, Har_Mean {3}\n".format(vmaf_mean, vmaf_min, vmaf_max, vmaf_harmonic_mean))
    return True
    
print("-----------------VMAFBatch Script-----------------")
print("Script to run VMAF easily and on multiple files")
print("by Kidsnd274")
print("")

# Running vmaf
failed = False
for video in videos:
    result = run_vmaf(video)
    if not result:
        failed = True

if failed:
    exit(1)