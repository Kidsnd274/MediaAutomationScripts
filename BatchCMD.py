import argparse
import datetime
import json
import math
import pathlib
import subprocess
import sys
import time

# Command Line Arguments
parser = argparse.ArgumentParser(description="Script to run a mkvmerge command on multiple files")
parser.add_argument('-f', '--folder', type=str, metavar='', required=False, help="Folder containing the videos")
parser.add_argument('-r', '--recursive', action='store_true', required=False, help="Recursively check folders and sub-folders")
parser.add_argument('-s', '--staxrip', action='store_true', required=False, help="Only includes videos with filename suffix '_new'")
args = parser.parse_args()


print("-----------------BatchCMD-----------------")
print("Script to run a mkvmerge command on multiple files")
print("by Kidsnd274")
print("")

video_file_extensions = ['.mkv', '.mp4', '.avi', '.webm'] # Edit this to include whatever extensions you want

current_directory = pathlib.Path(__file__).parent.absolute()

if args.folder is None:
    video_directory = current_directory
else:
    video_directory = pathlib.Path(args.folder).absolute()
    
if args.staxrip:
    staxrip_bool = True
else:
    staxrip_bool = False
    
if args.recursive:
    recursive_bool = True
else:
    recursive_bool = False

print("Folder:", video_directory.resolve())

video_files = []

if recursive_bool:
    for i in video_directory.glob('**/*'):
        cond1 = i.is_file()
        cond2 = i.suffix in video_file_extensions
        cond3 = i.stem.endswith("_new") or (not staxrip_bool) # Check Truth Table
        if (cond1 and cond2 and cond3): # Remove cond3 to remove "_new" check
            video_files.append(i)
else: # Check only current folder
    for i in video_directory.iterdir():
        cond1 = i.is_file()
        cond2 = i.suffix in video_file_extensions
        cond3 = i.stem.endswith("_new") or (not staxrip_bool) # Check Truth Table
        if (cond1 and cond2 and cond3):
            video_files.append(i)

if (not video_files):
    print("No video files found")
    exit()
    
print("Videos detected:")
for video in video_files:
    print(video.name)
print("")

def run_command(file):
    command = f'"C:\Program Files\MKVToolNix\mkvmerge.exe" --ui-language en --output ^"{file.parent / file.stem}_final.mkv^" --subtitle-tracks 3 --language 0:und --display-dimensions 0:1920x1080 --chroma-siting 0:1,1 --color-range 0:1 --language 1:ja --track-name ^"1:Japanese 2.0 ^| Opus 160k^" --language 3:en --track-name 3:Dialogue --default-track-flag 3:yes ^"^(^" ^"{str(file.resolve())}^" ^"^)^" --track-order 0:0,0:1,0:3' # Command to remove 1st subtitle track
    print("Command: " + command + "\n")
    process = subprocess.run(command, shell=True)
    print("")
    if process.returncode != 0:
        exit(1)
    

# Running loop to check
for video in video_files:
    print("Running command on: " + video.name)
    run_command(video)