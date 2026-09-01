# Goal: embed a matching subtitle file into the video as a SOFT (selectable) text track,
# WITHOUT re-encoding the video. The video and audio are copied byte-for-byte, so there is
# no quality loss and it runs almost instantly. The media server can then direct-play the
# video and render the subtitle client-side instead of transcoding in real time.
#
# For each .mkv / .mp4 in the source folder, it looks for a subtitle file with the SAME
# base name and one of these extensions: .srt .ass .ssa .vtt .smi
#
# The subtitle track is added as a selectable, NON-default track: it is off by default and
# the viewer chooses whether to turn it on from their TV / player. The original video is untouched.

# Set input and output folders
$sourceFolder = "C:\Users\Plex_admin\Documents\Media_Shuttle\ffmpegJobs\Encode"
$outputFolder = "C:\Users\Plex_admin\Documents\Media_Shuttle\ffmpegJobs\Done_Encode"

# Subtitle extensions to look for, in priority order (first match wins)
$subExtensions = @(".srt", ".ass", ".ssa", ".vtt", ".smi")

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
    $outputPath = Join-Path $outputFolder "$baseName`_subbed$extension"

    Write-Host "[$currentFile/$totalFiles] Processing: $($file.Name)"

    # Find a matching subtitle file (same base name)
    $subPath = $null
    $subExt  = $null
    foreach ($ext in $subExtensions) {
        $candidate = Join-Path $sourceFolder "$baseName$ext"
        if (Test-Path -Path $candidate) {
            $subPath = $candidate
            $subExt  = $ext
            break
        }
    }

    if (-not $subPath) {
        Write-Host "  No matching subtitle file found - skipping." -ForegroundColor Yellow
        Write-Host ""
        continue
    }

    Write-Host "  Embedding subtitle: $(Split-Path $subPath -Leaf)"

    # Pick a subtitle codec the output container accepts:
    #   MP4 only supports mov_text for text subtitles.
    #   MKV supports srt/ass/webvtt natively; keep .ass/.ssa as-is to preserve styling,
    #   convert everything else to srt so odd formats (vtt/smi) mux cleanly.
    if ($extension -ieq ".mp4") {
        $subCodec = "mov_text"
    }
    elseif ($subExt -ieq ".ass" -or $subExt -ieq ".ssa") {
        $subCodec = "copy"
    }
    else {
        $subCodec = "srt"
    }

    ffmpeg -i "$inputPath" -i "$subPath" `
        -map 0:v -map 0:a -map 1:0 `
        -c:v copy -c:a copy -c:s $subCodec `
        -metadata:s:s:0 language=eng `
        -disposition:s:0 0 `
        "$outputPath"

    Write-Host "Completed: $baseName`_subbed$extension"
    Write-Host "Output location: $outputPath"
    Write-Host ""
}

Write-Host "All $totalFiles file(s) processed!"
