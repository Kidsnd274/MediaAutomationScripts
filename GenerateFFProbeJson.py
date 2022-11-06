import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import time

# Command Line Arguments
parser = argparse.ArgumentParser(description="Script to check for corrupted videos")
parser.add_argument('file', type=str, metavar='', help="Video file")
args = parser.parse_args()

current_directory = pathlib.Path(__file__).parent.absolute()

if args.file is None:
    print("No file specified")
    exit(1)
else:
    file_path = pathlib.Path(args.file).absolute()

# Setting ffprobe locations
locations_ffprobe = [current_directory / "ffprobe.exe",
                     current_directory / "ffmpeg" / "ffprobe.exe",
                     current_directory / "ffprobe"]

ffprobe_found = False
for location in locations_ffprobe:
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

def calculate_audio_duration(extracted_audio_duration_string):
    splitted = extracted_audio_duration_string.split('.')
    extract_audio_dur = time.strptime(splitted[0], '%H:%M:%S')
    audio_duration = datetime.timedelta(hours=extract_audio_dur.tm_hour,minutes=extract_audio_dur.tm_min,seconds=extract_audio_dur.tm_sec).total_seconds()
    audio_duration += float("0." + str(splitted[1]))
    return audio_duration

def ffprobe_check(file):
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
    with open(file.stem + ".json", "w") as f:
        f.write(json.dumps(d, indent = 4))
        
    audio_dur_string = d["streams"][1]["tags"]["DURATION"]
    audio_duration = calculate_audio_duration(audio_dur_string)
    duration = float(d["format"]["duration"])
    frames = int(d["streams"][0]["nb_read_frames"])
    framerate = convert_to_float(d["streams"][0]["r_frame_rate"])
    duration_from_frames = frames/framerate
    
    print("  Frame count:", frames)
    print("  Framerate:", framerate)
    print("  Duration:", duration)
    print("  Calculated duration (from frames):", duration_from_frames)
    print("  Audio Duration:", audio_duration)
    
if not ffprobe_check(file_path):
    exit(1)
