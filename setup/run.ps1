<#
One run of the Tender batch downloader: update the code from the repository, then run it.
Started by the scheduled task and by the desktop shortcut. The run itself starts with the
downloader's doctor and stops when something it needs is missing.
#>
# -Now is what the shortcut of an earlier install passes; every run now starts at once.
param([switch]$Now)
$ErrorActionPreference = "Continue"
$home_dir = Join-Path $env:LOCALAPPDATA "eis-tool"
$app = Join-Path $home_dir "app"
$vpy = Join-Path $home_dir "venv\Scripts\python.exe"
$log = Join-Path $home_dir "run.log"

# The output of git, pip and the run stays small: at 1 MB the log moves to run.log.1.
if ((Test-Path $log) -and (Get-Item $log).Length -ge 1MB) { Move-Item -Force $log "$log.1" }

git -C $app pull --ff-only --quiet 2>&1 | Out-File -Append -Encoding utf8 $log
& $vpy -m pip install --quiet -r (Join-Path $app "requirements-laptop.txt") 2>&1 | Out-File -Append -Encoding utf8 $log

Push-Location $app
$env:PYTHONIOENCODING = "utf-8"
& $vpy download_batch.py 2>&1 | Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE
Pop-Location
exit $code
