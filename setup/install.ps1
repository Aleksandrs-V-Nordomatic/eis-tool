<#
Installs the Tender batch downloader on a Windows computer.

    powershell -ExecutionPolicy Bypass -File setup\install.ps1 -LibraryProject "<project folder in the library>"

The Automations library must be synced to this computer by OneDrive (Sync, or Add shortcut to
My files); -LibraryFolder names its local folder when it is not found by itself.

What it does:
  1. clones the repository to %LOCALAPPDATA%\eis-tool\app (or updates it) and makes a
     Python environment there;
  2. writes the settings to %USERPROFILE%\.eis-tool\downloader.json;
  3. asks for the monday.com token and the GitHub token and keeps them in the Windows
     Credential Manager (skip with -NoCredentials);
  4. registers the task "Tender Radar LV download" (daily at 10:00 and at sign-in) and puts
     the shortcut "Tender Radar LV" on the desktop.

Needs Python 3.12 or newer and Git on PATH; 7-Zip for 7z and RAR archives.
#>
param(
    [Parameter(Mandatory = $true)][string]$LibraryProject,
    [string]$LibraryFolder = "",
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

if (-not $LibraryFolder) {
    $places = @()
    if ($env:OneDriveCommercial) {
        $places += Get-ChildItem $env:OneDriveCommercial -Directory -ErrorAction SilentlyContinue
        $organisation = (Split-Path $env:OneDriveCommercial -Leaf) -replace '^OneDrive - ', ''
        $places += Get-ChildItem (Join-Path $env:USERPROFILE $organisation) -Directory -ErrorAction SilentlyContinue
    }
    $found = $places | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName $LibraryProject) } | Select-Object -First 1
    if (-not $found) { throw "The library is not synced here: sync it with OneDrive, or pass -LibraryFolder" }
    $LibraryFolder = $found.FullName
}
if (-not (Test-Path -LiteralPath (Join-Path $LibraryFolder $LibraryProject))) {
    throw "$LibraryFolder does not hold $LibraryProject"
}
Write-Host "Library folder: $LibraryFolder"

$settings_dir = Join-Path $env:USERPROFILE ".eis-tool"
New-Item -ItemType Directory -Force $settings_dir | Out-Null
New-Item -ItemType Directory -Force $Batches | Out-Null
$settings = [ordered]@{
    country = $Country
    library_root = $LibraryFolder
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
}

$run = Join-Path $app "setup\run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
# A run stopped by sleep or a lost network is resumed when the laptop wakes (S3 resume or the
# end of Modern Standby) or joins a network. Most of these runs find nothing open and end at once.
function New-EventTrigger([string]$Log, [string]$Select) {
    $class = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskEventTrigger
    $trigger = New-CimInstance -CimClass $class -ClientOnly
    $trigger.Subscription = "<QueryList><Query Id=`"0`" Path=`"$Log`"><Select Path=`"$Log`">$Select</Select></Query></QueryList>"
    $trigger.Delay = "PT2M"
    $trigger.Enabled = $true
    $trigger
}
$woke = New-EventTrigger "System" ("*[System[(Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1)" +
    " or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=507)]]")
$online = New-EventTrigger "Microsoft-Windows-NetworkProfile/Operational" "*[System[EventID=10000]]"
$triggers = @((New-ScheduledTaskTrigger -Daily -At $At), (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    $woke, $online)
$task_settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Tender Radar LV download" -Action $action -Trigger $triggers `
    -Settings $task_settings -Description "Tender batch downloader: one daily batch of Latvian tenders" -Force | Out-Null

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Tender Radar LV.lnk"))
$link.TargetPath = "powershell.exe"
$link.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`" -Now"
$link.Description = "Download today's batch of Latvian tenders now"
$link.Save()

Write-Host "Installed. Task 'Tender Radar LV download' runs daily at $At, at sign-in, on wake and on joining a network."
