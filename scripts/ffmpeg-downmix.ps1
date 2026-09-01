# Set input and output folders
$sourceFolder = "C:\Users\Plex_admin\Documents\Media_Shuttle\ffmpegJobs\Encode"
$outputFolder = "C:\Users\Plex_admin\Documents\Media_Shuttle\ffmpegJobs\Done_Encode"

# Subtitle extensions to look for (option 4), in priority order (first match wins)
$subExtensions = @(".srt", ".ass", ".ssa", ".vtt", ".smi")

# Prompt for the operation
Write-Host ""
Write-Host "Select the operation:"
Write-Host "  1) Downmix 7.1 audio -> ADD a 5.1 track and a stereo track (keeps the 7.1)"
Write-Host "  2) Downmix 5.1 audio -> ADD a stereo track (keeps the 5.1)"
Write-Host "  3) Downmix 7.1 audio -> leave ONLY a 5.1 track and a stereo track (drops the 7.1)"
Write-Host "  4) Embed subtitles into files of the same name (no video re-encode)"
Write-Host ""

$choice = $null
while ($choice -notin @("1", "2", "3", "4")) {
    $choice = Read-Host "Enter 1, 2, 3, or 4"
}

switch ($choice) {
    "1" { $operationName = "7.1 -> add 5.1 + stereo" }
    "2" { $operationName = "5.1 -> add stereo" }
    "3" { $operationName = "7.1 -> only 5.1 + stereo" }
    "4" { $operationName = "Embed subtitles" }
}

Write-Host ""
Write-Host "Operation: $operationName"
Write-Host ""

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

    Write-Host "[$currentFile/$totalFiles] Processing: $($file.Name)"

    # -------------------------------------------------------------------------
    # Option 4: embed a matching subtitle as a selectable (off-by-default) track
    # Video and audio are copied untouched (no re-encode).
    # -------------------------------------------------------------------------
    if ($choice -eq "4") {
        $outputPath = Join-Path $outputFolder "$baseName`_subbed$extension"

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
        continue
    }

    # -------------------------------------------------------------------------
    # Options 1-3: build the audio arguments, then run a shared ffmpeg command.
    # -------------------------------------------------------------------------
    $outputPath = Join-Path $outputFolder "$baseName`_encoded$extension"
    $audioArgs = $null

    if ($choice -eq "1") {
        # Keep the 7.1, ADD a 5.1 downmix and a stereo downmix
        $audioArgs = @(
            "-map", "0:a:0", "-c:a:0", "copy",
            "-map", "0:a:0", "-c:a:1", "aac", "-filter:a:1", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR", "-b:a:1", "384k",
            "-map", "0:a:0", "-c:a:2", "aac", "-filter:a:2", "pan=stereo|FL=FL+FC+BL+SL|FR=FR+FC+BR+SR", "-b:a:2", "192k"
        )
    }
    elseif ($choice -eq "2") {
        # Keep the 5.1, ADD a stereo downmix
        $audioArgs = @(
            "-map", "0:a:0", "-c:a:0", "copy",
            "-map", "0:a:0", "-c:a:1", "aac", "-filter:a:1", "pan=stereo|FL=FL+FC+BL|FR=FR+FC+BR", "-b:a:1", "192k"
        )
    }
    elseif ($choice -eq "3") {
        # Inspect the file and end up with ONLY a 5.1 track and a stereo track (drop the 7.1)
        $channels = @(& ffprobe -v error -select_streams a -show_entries stream=channels -of csv=p=0 "$inputPath")

        $idx71 = -1; $idx51 = -1; $idxStereo = -1
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

        # Output order is always: 5.1 as a:0, stereo as a:1.
        if ($idx51 -ge 0 -and $idxStereo -ge 0) {
            Write-Host "  Sub-operation: 7.1 + 5.1 + stereo -> keep 5.1 & stereo, drop 7.1"
            $audioArgs = @(
                "-map", "0:a:$idx51",     "-c:a:0", "copy",
                "-map", "0:a:$idxStereo", "-c:a:1", "copy"
            )
        }
        elseif ($idx51 -ge 0) {
            Write-Host "  Sub-operation: 7.1 + 5.1 -> keep 5.1, downmix 5.1 to stereo, drop 7.1"
            $audioArgs = @(
                "-map", "0:a:$idx51", "-c:a:0", "copy",
                "-map", "0:a:$idx51", "-c:a:1", "aac", "-filter:a:1", "pan=stereo|FL=FL+FC+BL|FR=FR+FC+BR", "-b:a:1", "192k"
            )
        }
        elseif ($idxStereo -ge 0) {
            Write-Host "  Sub-operation: 7.1 + stereo -> downmix 7.1 to 5.1, keep stereo, drop 7.1"
            $audioArgs = @(
                "-map", "0:a:$idx71",     "-c:a:0", "aac", "-filter:a:0", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR", "-b:a:0", "384k",
                "-map", "0:a:$idxStereo", "-c:a:1", "copy"
            )
        }
        else {
            Write-Host "  Sub-operation: 7.1 only -> downmix into new 5.1 + stereo, drop 7.1"
            $audioArgs = @(
                "-map", "0:a:$idx71", "-c:a:0", "aac", "-filter:a:0", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR", "-b:a:0", "384k",
                "-map", "0:a:$idx71", "-c:a:1", "aac", "-filter:a:1", "pan=stereo|FL=FL+FC+BL+SL|FR=FR+FC+BR+SR", "-b:a:1", "192k"
            )
        }
    }

    ffmpeg -i "$inputPath" -map 0:v -c:v copy @audioArgs -map 0:s? -c:s copy "$outputPath"

    Write-Host "Completed: $baseName`_encoded$extension"
    Write-Host "Output location: $outputPath"
    Write-Host ""
}

Write-Host "All $totalFiles file(s) processed!"
