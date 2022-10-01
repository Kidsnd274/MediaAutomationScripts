# VerifyVideo
Script to check for corrupted videos

## Usage
You need ffmpeg and ffprobe to be in either PATH or in the same directory as the VerifyVideo.py script
Run the command `python VerifyVideo.py` to start verifying videos

You can specify the video folder location using the `-f` flag. If not specified, VerifyVideo will check the videos in the same directory as the VerifyVideo.py script
eg. `python VerifyVideo.py -f "Folder Location"`

Corrupted videos will be listed in a corrupted_files.txt file created in the same directory as the VerifyVideo.py script.
The logs will be displayed in the terminal as well.
