import argparse
import datetime
import json
import math
import pathlib
import subprocess
import sys
import time

# Automated Batch Version that runs on files that ends with "_new" eg. file_new.mkv (for Staxrip)

# Command Line Arguments
parser = argparse.ArgumentParser(description="Script to check for corrupted videos")
parser.add_argument('folder', type=str, metavar='', help="Folder containing the videos")
args = parser.parse_args()


print("-----------------VerifyVideo-----------------")
print("Script to check for corrupted videos")
print("by Kidsnd274")
print("")

video_file_extensions = ['.mkv', '.mp4', '.avi', '.webm'] # Edit this to include whatever extensions you want

current_directory = pathlib.Path(__file__).parent.absolute()

if args.folder is None:
    video_directory = current_directory
else:
    video_directory = pathlib.Path(args.folder).absolute()

print("Folder:", video_directory.resolve())

video_files = []

for i in video_directory.glob('**/*'):
    cond1 = i.is_file()
    cond2 = i.suffix in video_file_extensions
    cond3 = i.stem.endswith("_new")
    if (cond1 and cond2 and cond3): # Remove cond3 to remove "_new" check
        video_files.append(i)

if (not video_files):
    print("No video files found")
    exit()

print("Videos detected:")
for video in video_files:
    print(video.name)
print("")

# Setting ffmpeg and ffprobe locations
locations_ffmpeg = [current_directory / "ffmpeg.exe",
                    current_directory / "ffmpeg" / "ffmpeg.exe",
                    current_directory / "ffmpeg"]

locations_ffprobe = [current_directory / "ffprobe.exe",
                     current_directory / "ffmpeg" / "ffprobe.exe",
                     current_directory / "ffprobe"]

ffmpeg_found = False
for location in locations_ffmpeg:
    if location.exists():
        ffmpeg_exec = location.resolve()
        ffmpeg_found = True
        break
if not ffmpeg_found:
    if sys.platform == 'win32':
        ffmpeg_exec = "ffmpeg.exe"
    else:
        ffmpeg_exec = "ffmpeg"
        
ffprobe_found = False
for location in locations_ffprobe:
    if location.exists():
        ffprobe_exec = location.resolve()
        ffprobe_found = True
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


def calculate_audio_duration(extracted_audio_duration_string):
    splitted = extracted_audio_duration_string.split('.')
    extract_audio_dur = time.strptime(splitted[0], '%H:%M:%S')
    audio_duration = datetime.timedelta(hours=extract_audio_dur.tm_hour,minutes=extract_audio_dur.tm_min,seconds=extract_audio_dur.tm_sec).total_seconds()
    audio_duration += float("0." + str(splitted[1]))
    return audio_duration


def ffmpeg_check(file):
    print("Running ffmpeg check...", end="")
    args = [ffmpeg_exec, 
            "-v", "error", 
            "-i", file.resolve(),
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


def ffprobe_check(file): # ffprobe -show_streams -show_format -threads 8 -v quiet -print_format json (test command to print json)
    print("Running ffprobe check...")

    args = [ffprobe_exec,
        "-show_format",
        # "-select_streams", "v",
        "-show_streams",
        "-count_frames",
        "-threads", "8", # might not want to include this
        "-v", "quiet",
        "-print_format", "json",
        file.resolve()]
    process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    # print(process.stdout)
    
    if process.returncode != 0:
        print(" ERROR")
        print(process.stdout)
        print(process.stderr)
        print("  Couldn't probe file using ffprobe")
        return False
    
    d = json.loads(process.stdout)
    file_duration = float(d["format"]["duration"])
    frames = int(d["streams"][0]["nb_read_frames"])
    framerate = convert_to_float(d["streams"][0]["r_frame_rate"])
    
    duration_from_frames = frames/framerate
    try:
        audio_duration = calculate_audio_duration(d["streams"][1]["tags"]["DURATION"])
    except KeyError: # MKV files with DURATION-eng
        try:
            audio_duration = calculate_audio_duration(d["streams"][1]["tags"]["DURATION-eng"])
        except KeyError: # mp4 files including shadowplay and ffmpeg mp4 output
            try:
                audio_duration = d["streams"][1]["duration"]
            except KeyError:
                print("Skipping audio duration check...")
                audio_duration = duration_from_frames

    
    file_ok = True
    
    # Audio duration check
    if (round(audio_duration) != round(duration_from_frames)):
        print("ERROR")
        print("  Frame count:", frames)
        print("  Framerate:", framerate)
        print("  File Duration:", file_duration)
        print("  Audio Duration:", audio_duration)
        print("  Frames Duration:", duration_from_frames)
        print("Duration mismatch detected, video corrupt!")
        file_ok = False

    if file_ok:
        print("File OK!")
    
    # File duration check
    if (math.floor(duration_from_frames) != math.floor(file_duration)):
        print("WARN: File duration and Frame duration mismatch")
        print("  File Duration:", file_duration)
        print("  Frames Duration:", duration_from_frames)
        
    return file_ok
    

# Running loop to check
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
    print("Corrupted Files:")
    for file in corrupted_files:
        print(file.name)
    
    with open("corrupted_files.txt", "w") as f:
        for file in corrupted_files:
            f.write(file.name)
            f.write("\n")
    exit(1) # Exit with code 1 if corrupted videos found

print("All files checked!")
