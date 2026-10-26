<#
One run of the Tender batch downloader: update the code from the repository, then run it.
Started by the scheduled task and by the desktop shortcut.
#>
param([switch]$Now)
$ErrorActionPreference = "Continue"
$home_dir = Join-Path $env:LOCALAPPDATA "eis-tool"
$app = Join-Path $home_dir "app"
$vpy = Join-Path $home_dir "venv\Scripts\python.exe"
$log = Join-Path $home_dir "run.log"

git -C $app pull --ff-only --quiet 2>&1 | Out-File -Append -Encoding utf8 $log
& $vpy -m pip install --quiet -r (Join-Path $app "requirements-laptop.txt") 2>&1 | Out-File -Append -Encoding utf8 $log

Push-Location $app
$env:PYTHONIOENCODING = "utf-8"
$extra = @()
if ($Now) { $extra += "--now" }
& $vpy download_batch.py @extra 2>&1 | Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE
Pop-Location
exit $code
