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
    
    ffmpeg -i "$inputPath" -map 0:v -c:v copy -map 0:a:0 -c:a:0 copy -map 0:a:0 -c:a:1 aac -filter:a:1 "pan=stereo|FL=FL+FC+BL|FR=FR+FC+BR" -b:a:1 192k -map 0:s? -c:s copy "$outputPath"
    
    Write-Host "Completed: $baseName`_encoded$extension"
    Write-Host "Output location: $outputPath"
    Write-Host ""
}

Write-Host "All $totalFiles file(s) processed!"