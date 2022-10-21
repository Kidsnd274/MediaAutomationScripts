@echo off
REM CONFIGURATION
	SET "mkvmergePath=C:\MKVToolNix\mkvmerge.exe"
	SET "ffmpegpath=C:\ffmpeg\bin\ffmpeg.exe"
	SET "originalPath=F:\original"
	SET "outputPath=F:\out"
REM Set last episode number
	SET /A "max=13"
REM Set first episode number
	SET /A "ctr=1"
REM -----------------------------------------------------------------------------------------
:generatenum
if %ctr% LSS 10 goto less_10
if %ctr% GEQ 10 goto more_n_10
:less_10
SET ep=0%ctr%
goto muxit
:more_n_10
SET ep=%ctr%
goto muxit
:muxit
ECHO ===========================================================================
echo KIDSND274 MKVMERGE FFMPEG BATCH SCRIPT
echo Episode %ctr%
ECHO ===========================================================================
REM Configure the mkvmerge settings here
if %ctr% GTR %max% goto end
:start
REM FFMPEG
REM ffmpeg command here if you want to do any preprocessing
REM MKVMERGE
"%mkvmergePath%" --output "%outputPath%\Show %ep% (1080p).mkv" --language 0:und --default-track 0:yes --track-name 1:"AAC Stereo" --default-track 1:yes "%originalPath%\Show %ep% (1080p).mkv"
REM Add ur own command here and replace the episode number with %ep%
SET /A ctr+=1
cls
goto generatenum
:end
cls
set /A ctr-=1
ECHO ===========================================================================
echo KIDSND274 MKVMERGE FFMPEG BATCH SCRIPT
echo Done at episode %ctr%
ECHO ===========================================================================
pause
exit