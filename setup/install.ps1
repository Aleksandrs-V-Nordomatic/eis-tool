<#
Installs the Tender batch downloader on a Windows computer, or removes it.

    powershell -ExecutionPolicy Bypass -File setup\install.ps1 -LibraryProject "<project folder in the library>" -CoordinatorUrl "<Coordinator API address>"
    powershell -ExecutionPolicy Bypass -File setup\install.ps1 -Uninstall

The Automations library must be synced to this computer by OneDrive (Sync, or Add shortcut to
My files); -LibraryFolder names its local folder when it is not found by itself.

What it does:
  1. clones the repository to %LOCALAPPDATA%\eis-tool\app (or updates it) and makes a
     Python environment there from requirements-laptop.txt;
  2. writes the settings to %USERPROFILE%\.eis-tool\downloader.json, with the coordinator's
     address; the computer is named by its Windows computer name;
  3. asks for each missing credential and keeps it in the Windows Credential Manager under the
     service eis-tool: "monday" (the person's monday.com token) and "coordinator" (this
     computer's Cloudflare Access service token); -NewCredentials asks again for both, and a
     GitHub token stored by an earlier version is removed;
  4. registers the task "Tender Radar LV download" (daily at 10:00 local time, at sign-in, on
     wake and on joining a network, one instance at a time, with no console window) and puts
     the shortcut "Tender Radar LV" on the desktop;
  5. ends with the downloader's doctor, which names everything a run still lacks.

-Uninstall removes the task, the shortcut, the credentials, the settings, the code with its
Python environment and the tender folders. The library is not touched.

Needs Python 3.12 or newer and Git on PATH, 7-Zip, and curl (part of Windows).
#>
[CmdletBinding(DefaultParameterSetName = "Install")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Install")][string]$LibraryProject,
    [Parameter(Mandatory = $true, ParameterSetName = "Install")][string]$CoordinatorUrl,
    [Parameter(ParameterSetName = "Install")][string]$LibraryFolder = "",
    [Parameter(ParameterSetName = "Install")][string]$Country = "LV",
    [Parameter(ParameterSetName = "Install")][string]$Kit = "kit/LV",
    [Parameter(ParameterSetName = "Install")][string]$Repository = "Aleksandrs-V-Nordomatic/eis-tool",
    [Parameter(ParameterSetName = "Install")][string]$Branch = "master",
    [Parameter(ParameterSetName = "Install")][string]$At = "10:00",
    [Parameter(ParameterSetName = "Install")][switch]$NewCredentials,
    [Parameter(Mandatory = $true, ParameterSetName = "Uninstall")][switch]$Uninstall,
    [string]$Tenders = "C:\radar\tenders"
)
$ErrorActionPreference = "Stop"

$task_name = "Tender Radar LV download"
$home_dir = Join-Path $env:LOCALAPPDATA "eis-tool"
$app = Join-Path $home_dir "app"
$venv = Join-Path $home_dir "venv"
$vpy = Join-Path $venv "Scripts\python.exe"
$settings_dir = Join-Path $env:USERPROFILE ".eis-tool"
$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Tender Radar LV.lnk"
$credentials = [ordered]@{
    "monday" = "monday.com token of the person lending this computer"
    "coordinator" = "this computer's Cloudflare Access service token"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task_name -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $shortcut -Force -ErrorAction SilentlyContinue
    if (Test-Path $vpy) {
        foreach ($name in @($credentials.Keys) + @("github")) {
            & $vpy -c "import keyring; keyring.get_password('eis-tool', '$name') and keyring.delete_password('eis-tool', '$name')"
        }
    }
    foreach ($path in @($settings_dir, $home_dir, $Tenders)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    Write-Host "Removed. The library and what it holds are not touched."
    exit 0
}

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
if (-not (Test-Path $vpy)) { & $python -m venv $venv }
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $app "requirements-laptop.txt")

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

New-Item -ItemType Directory -Force $settings_dir | Out-Null
New-Item -ItemType Directory -Force $Tenders | Out-Null
$settings = [ordered]@{
    country = $Country
    library_root = $LibraryFolder
    library_project = $LibraryProject
    kit = $Kit
    tenders = $Tenders
    coordinator_url = $CoordinatorUrl
}
$json = $settings | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $settings_dir "downloader.json"), $json, (New-Object System.Text.UTF8Encoding($false)))

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
foreach ($name in $credentials.Keys) {
    & $vpy -c "import keyring, sys; sys.exit(0 if keyring.get_password('eis-tool', '$name') else 1)"
    if ($LASTEXITCODE -eq 0 -and -not $NewCredentials) { continue }
    if ($name -eq "coordinator") {
        $id = Read-Secret "Client id of $($credentials[$name])"
        $secret = Read-Secret "Client secret of $($credentials[$name])"
        $value = if ($id -and $secret) { "${id}:${secret}" } else { "" }
    } else {
        $value = Read-Secret "The $($credentials[$name])"
    }
    if ($value) {
        # Through standard input, so the secret is never in an environment variable or a command line.
        $value | & $vpy -c "import keyring, sys; keyring.set_password('eis-tool', '$name', sys.stdin.readline().strip())"
    }
}
# The coordinator starts the processing workflow; the computer keeps no GitHub token.
& $vpy -c "import keyring; keyring.get_password('eis-tool', 'github') and keyring.delete_password('eis-tool', 'github')"

$run = Join-Path $app "setup\run.ps1"
# A headless console host: -WindowStyle Hidden still opens a window for a moment, or for good
# when Windows Terminal hosts consoles, and closing that window stops the run.
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$run`""
# A run stopped by sleep or a lost network is resumed when the computer wakes (S3 resume or the
# end of Modern Standby) or joins a network. Most of these runs find no tender left and end at once.
function New-EventTrigger([string]$Log, [string]$Select) {
    $class = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskEventTrigger
    $trigger = New-CimInstance -CimClass $class -ClientOnly
    $trigger.Subscription = "<QueryList><Query Id=`"0`" Path=`"$Log`"><Select Path=`"$Log`">$Select</Select></Query></QueryList>"
    $trigger.Delay = "PT2M"
    $trigger.Enabled = $true
    $trigger
}
$daily = New-ScheduledTaskTrigger -Daily -At $At
# -At stores today's UTC offset, which moves the start by an hour at the clock change; a start
# boundary without an offset is local time.
$daily.StartBoundary = (Get-Date $At).ToString("yyyy-MM-ddTHH:mm:ss")
$woke = New-EventTrigger "System" ("*[System[(Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1)" +
    " or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=507)]]")
$online = New-EventTrigger "Microsoft-Windows-NetworkProfile/Operational" "*[System[EventID=10000]]"
$triggers = @($daily, (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME), $woke, $online)
# The longest run measured so far took 11 hours, on one computer.
$task_settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $task_name -Action $action -Trigger $triggers `
    -Settings $task_settings -Description "Tender batch downloader: the day's Latvian tenders, one at a time, with the coordinator" -Force | Out-Null

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = "conhost.exe"
$link.Arguments = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$run`""
$link.Description = "Take part in today's Latvian tender downloads now"
$link.Save()

Write-Host "Installed on $env:COMPUTERNAME. Task '$task_name' runs daily at $At, at sign-in, on wake and on joining a network."
Push-Location $app
& $vpy download_batch.py --doctor
$code = $LASTEXITCODE
Pop-Location
exit $code
