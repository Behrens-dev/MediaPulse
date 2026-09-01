# Goal: every processed file ends up with ONLY a 5.1 track and a stereo track.
#
# For each file that contains a 7.1 (8-channel) track:
#   * 7.1 only               -> downmix 7.1 into a new 5.1 + new stereo, drop the 7.1
#   * 7.1 + existing 5.1      -> keep the 5.1, downmix that 5.1 into stereo, drop the 7.1
#   * 7.1 + existing stereo   -> downmix 7.1 into 5.1, keep the stereo, drop the 7.1
#   * 7.1 + 5.1 + stereo      -> keep the 5.1 and stereo, drop the 7.1
#
# Files without a 7.1 track are skipped.

# Set input and output folders
$sourceFolder = "C:\Users\Plex_admin\Documents\Media_Shuttle\ffmpegJobs\Encode"
$outputFolder = "C:\Users\Plex_admin\Documents\Media_Shuttle\ffmpegJobs\Done_Encode"

# Create output folder if it doesn't exist
if (!(Test-Path -Path $outputFolder)) {
    New-Item -ItemType Directory -Path $outputFolder | Out-Null
}

# Process all MKV and MP4 files in the Encode folder
$mediaFiles = Get-ChildItem -Path $sourceFolder -Filter "*.mkv"
$mediaFiles += Get-ChildItem -Path $sourceFolder -Filter "*.mp4"

$totalFiles = $mediaFiles.Count
$currentFile = 0

Write-Host "Found $totalFiles file(s) to process"
Write-Host ""

foreach ($file in $mediaFiles) {
    $currentFile++
    $inputPath = $file.FullName
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    $extension = $file.Extension
    $outputPath = Join-Path $outputFolder "$baseName`_encoded$extension"

    Write-Host "[$currentFile/$totalFiles] Processing: $($file.Name)"

    # Get the channel count of each audio stream, in audio-stream order (a:0, a:1, ...)
    $channels = @(& ffprobe -v error -select_streams a -show_entries stream=channels -of csv=p=0 "$inputPath")

    # Find the audio-relative index of the first 7.1 (8ch), 5.1 (6ch), and stereo (2ch) tracks
    $idx71 = -1
    $idx51 = -1
    $idxStereo = -1
    for ($i = 0; $i -lt $channels.Count; $i++) {
        $c = 0
        [void][int]::TryParse($channels[$i], [ref]$c)
        if ($c -eq 8 -and $idx71 -lt 0) { $idx71 = $i }
        elseif ($c -eq 6 -and $idx51 -lt 0) { $idx51 = $i }
        elseif ($c -eq 2 -and $idxStereo -lt 0) { $idxStereo = $i }
    }

    if ($idx71 -lt 0) {
        Write-Host "  No 7.1 track found - skipping." -ForegroundColor Yellow
        Write-Host ""
        continue
    }

    # Build the audio arguments. Output order is always: 5.1 as a:0, stereo as a:1.
    if ($idx51 -ge 0 -and $idxStereo -ge 0) {
        $operationName = "7.1 + 5.1 + stereo -> keep 5.1 & stereo, drop 7.1"
        $audioArgs = @(
            "-map", "0:a:$idx51",     "-c:a:0", "copy",
            "-map", "0:a:$idxStereo", "-c:a:1", "copy"
        )
    }
    elseif ($idx51 -ge 0) {
        $operationName = "7.1 + 5.1 -> keep 5.1, downmix 5.1 to stereo, drop 7.1"
        $audioArgs = @(
            "-map", "0:a:$idx51", "-c:a:0", "copy",
            "-map", "0:a:$idx51", "-c:a:1", "aac", "-filter:a:1", "pan=stereo|FL=FL+FC+BL|FR=FR+FC+BR", "-b:a:1", "192k"
        )
    }
    elseif ($idxStereo -ge 0) {
        $operationName = "7.1 + stereo -> downmix 7.1 to 5.1, keep stereo, drop 7.1"
        $audioArgs = @(
            "-map", "0:a:$idx71",     "-c:a:0", "aac", "-filter:a:0", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR", "-b:a:0", "384k",
            "-map", "0:a:$idxStereo", "-c:a:1", "copy"
        )
    }
    else {
        $operationName = "7.1 only -> downmix into new 5.1 + stereo, drop 7.1"
        $audioArgs = @(
            "-map", "0:a:$idx71", "-c:a:0", "aac", "-filter:a:0", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR", "-b:a:0", "384k",
            "-map", "0:a:$idx71", "-c:a:1", "aac", "-filter:a:1", "pan=stereo|FL=FL+FC+BL+SL|FR=FR+FC+BR+SR", "-b:a:1", "192k"
        )
    }

    Write-Host "  Operation: $operationName"

    ffmpeg -i "$inputPath" -map 0:v -c:v copy @audioArgs -map 0:s? -c:s copy "$outputPath"

    Write-Host "Completed: $baseName`_encoded$extension"
    Write-Host "Output location: $outputPath"
    Write-Host ""
}

Write-Host "All $totalFiles file(s) processed!"
