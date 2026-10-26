<#
Installs the Tender batch downloader on a Windows computer.

    powershell -ExecutionPolicy Bypass -File setup\install.ps1 -LibraryProject "<project folder in the library>"

What it does:
  1. clones the repository to %LOCALAPPDATA%\eis-tool\app (or updates it) and makes a
     Python environment there;
  2. writes the settings to %USERPROFILE%\.eis-tool\downloader.json;
  3. asks for the monday.com token and the GitHub token and keeps them in the Windows
     Credential Manager (skip with -NoCredentials);
  4. opens the browser once to sign in to the SharePoint gateway;
  5. registers the task "Tender Radar LV download" (daily at 10:00 and at sign-in) and puts
     the shortcut "Tender Radar LV" on the desktop.

Needs Python 3.12 or newer and Git on PATH; 7-Zip for 7z and RAR archives.
#>
param(
    [Parameter(Mandatory = $true)][string]$LibraryProject,
    [string]$Country = "LV",
    [string]$Kit = "kit/LV",
    [string]$Batches = "C:\radar\batches",
    [string]$Repository = "Aleksandrs-V-Nordomatic/eis-tool",
    [string]$Branch = "master",
    [string]$At = "10:00",
    [switch]$NoCredentials
)
$ErrorActionPreference = "Stop"

$home_dir = Join-Path $env:LOCALAPPDATA "eis-tool"
$app = Join-Path $home_dir "app"
New-Item -ItemType Directory -Force $home_dir | Out-Null

if (Test-Path (Join-Path $app ".git")) {
    git -C $app fetch origin $Branch
    git -C $app checkout -q $Branch
    git -C $app pull --ff-only origin $Branch
} else {
    git clone --branch $Branch "https://github.com/$Repository.git" $app
}

$python = (Get-Command python -ErrorAction Stop).Source
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 or newer is needed" }
$venv = Join-Path $home_dir "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & $python -m venv $venv }
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $app "requirements-laptop.txt")

if (-not (Test-Path "C:\Program Files\7-Zip\7z.exe") -and -not (Get-Command 7z -ErrorAction SilentlyContinue)) {
    Write-Warning "7-Zip is not installed: 7z and RAR archives will be uploaded whole"
}

$settings_dir = Join-Path $env:USERPROFILE ".eis-tool"
New-Item -ItemType Directory -Force $settings_dir | Out-Null
New-Item -ItemType Directory -Force $Batches | Out-Null
$settings = [ordered]@{
    country = $Country
    library_project = $LibraryProject
    kit = $Kit
    batches = $Batches
    github_repo = $Repository
    workflow = "process-batch.yml"
    ref = $Branch
}
$json = $settings | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $settings_dir "downloader.json"), $json, (New-Object System.Text.UTF8Encoding($false)))

if (-not $NoCredentials) {
    foreach ($name in @("monday", "github")) {
        $secure = Read-Host -AsSecureString "Token for $name (Enter keeps the stored one)"
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if ($plain) {
            $env:EIS_SECRET_VALUE = $plain
            & $vpy -c "import keyring, os; keyring.set_password('eis-tool', '$name', os.environ['EIS_SECRET_VALUE'])"
            Remove-Item Env:\EIS_SECRET_VALUE
        }
    }
    Push-Location $app
    & $vpy gateway.py sign-in
    Pop-Location
}

$run = Join-Path $app "setup\run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
$triggers = @((New-ScheduledTaskTrigger -Daily -At $At), (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME))
$task_settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Tender Radar LV download" -Action $action -Trigger $triggers `
    -Settings $task_settings -Description "Tender batch downloader: one daily batch of Latvian tenders" -Force | Out-Null

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Tender Radar LV.lnk"))
$link.TargetPath = "powershell.exe"
$link.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
$link.Description = "Download today's batch of Latvian tenders now"
$link.Save()

Write-Host "Installed. Task 'Tender Radar LV download' runs daily at $At and at sign-in."
