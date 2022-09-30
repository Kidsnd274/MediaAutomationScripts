import json
import math
import pathlib
import subprocess
import sys

print("-----------------VerifyVideo-----------------")
print("Script to check for corrupted videos")
print("by Kidsnd274")
print("")

video_file_extensions = ['.mkv', '.mp4', '.avi', '.webm']

current_directory = pathlib.Path(__file__).parent.absolute()
video_files = [x for x in current_directory.iterdir() if (x.is_file() and x.suffix in video_file_extensions)]

# Check if ffmpeg in current directory
locations_ffmpeg = [current_directory / "ffmpeg.exe",
             current_directory / "ffmpeg"]

locations_ffprobe = [current_directory / "ffprobe.exe",
                     current_directory / "ffprobe"]

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
        
ffprobe_found = False
for location in locations_ffmpeg:
    if location.exists():
        ffprobe_exec = location.resolve()
        found = True
        break
if not ffprobe_found:
    if sys.platform == 'win32':
        ffprobe_exec = "ffprobe.exe"
    else:
        ffprobe_exec = "ffprobe"

def convert_to_float(frac_str):
    try:
        return float(frac_str)
    except ValueError:
        num, denom = frac_str.split('/')
        try:
            leading, num = num.split(' ')
            whole = float(leading)
        except ValueError:
            whole = 0
        frac = float(num) / float(denom)
        return whole - frac if whole < 0 else whole + frac

def ffmpeg_check(file):
    print("Running ffmpeg check...", end="")
    args = [ffmpeg_exec, 
            "-v", "error", 
            "-i", file.name,
            "-f", "null",
            "-"
            ]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.communicate()
    if process.returncode != 0:
        print(" ERROR")
        print(process.stdout)
        print(process.stderr)
        print("ffmpeg check failed")
        return False
    print(" OK!")
    return True


def ffprobe_check(file):
    print("Running ffprobe check...", end="")

    args = [ffprobe_exec,
        "-show_format",
        "-select_streams", "v",
        "-show_streams",
        "-count_frames",
        "-threads", "8", # might not want to include this
        "-v", "quiet",
        "-print_format", "json",
        file.name]
    process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    # print(process.stdout)
    
    if process.returncode != 0:
        print(" ERROR")
        print(process.stdout)
        print(process.stderr)
        print("  Couldn't probe file using ffprobe")
        return False
    
    d = json.loads(process.stdout)
    duration = float(d["format"]["duration"])
    frames = int(d["streams"][0]["nb_read_frames"])
    framerate = convert_to_float(d["streams"][0]["r_frame_rate"])
    
    duration_from_frames = frames/framerate
    
    if (math.floor(duration_from_frames) != math.floor(duration)):
        print(" ERROR")
        print("  Frame count:", frames)
        print("  Framerate:", framerate)
        print("  Duration:", duration)
        print("  Calculated duration:", duration_from_frames)
        print("Duration mismatch detected, video corrupt!")
        return False
    
    print("  OK!")
    return True
    
corrupted_files = []
for video in video_files:
    print("Checking: " + video.name)
    pass1 = ffmpeg_check(video)
    pass2 = ffprobe_check(video)
    
    if (pass1 and pass2):
        print("OK!")
    else:
        print("WARNING: " + video.name + " is corrupt!")
        corrupted_files.append(video)
    print("")

if corrupted_files:
    with open("corrupted_files.txt", "w") as f:
        for file in corrupted_files:
            f.write(file.name)
            f.write("\n")
            
            
            
# args = [ffprobe_exec,
#         "-show_entries", "stream=r_frame_rate,nb_read_frames,duration",
#         "-select_streams", "v",
#         "-count_frames",
#         "-of", "compact=p=1:nk=1",
#         "-threads", "8",
#         "-v", "quiet",
#         "-print_format", "json",
#         file.name]
