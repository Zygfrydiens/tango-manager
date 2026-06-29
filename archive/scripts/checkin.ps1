# Opens a new PowerShell window in the tango-manager project directory,
# starts Claude Code already running a tango check-in, and plays a sound.
#
# Sound precedence:
#   1. sounds\checkin.wav / .mp3 / .m4a / .wma in the project (drop your own)
#   2. C:\Windows\Media\Alarm01.wav (Windows built-in)
#   3. System Exclamation beep (last resort)
#
# Triggered by Windows Task Scheduler. Manual run:
#   powershell -File scripts\checkin.ps1
#
# Two check-in styles are defined below ($preDancePrompt / $reflectPrompt /
# $weeklyPrompt). schedule.ps1 picks one via the -Kind argument; default is
# pre-dance. Edit the prompts to taste.

param(
    [ValidateSet("predance", "reflect", "weekly")]
    [string]$Kind = "predance"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$logFile = Join-Path $projectDir "scripts\checkin_errors.log"

function Write-CheckinLog {
    param([string]$Message)
    "$(Get-Date -Format o)  $Message" | Out-File -FilePath $logFile -Append -Encoding utf8
}

# -- 1. Locate claude.exe --------------------------------------------------
$claudeExe = $null
$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCmd) {
    $claudeExe = $claudeCmd.Source
} elseif (Test-Path "$env:USERPROFILE\.local\bin\claude.exe") {
    $claudeExe = "$env:USERPROFILE\.local\bin\claude.exe"
} else {
    Write-CheckinLog "Claude CLI not found on PATH or at default install location."
    exit 1
}

# -- 2. Choose the prompt for this check-in kind ---------------------------
$preDancePrompt = "Scheduled pre-dancing check-in. Use the tango-coach skill: read CLAUDE.md and data/weekly_plan.json, tell me what's on tonight (class/practica/milonga/solo), the 1-3 active topics, and send me in with ONE light intention - not a checklist. Keep it short; a milonga is not a test."
$reflectPrompt  = "Scheduled post-dancing reflection. Use the tango-coach skill: open with 'how was it?', listen for the follower's experience, then help me name ONE thing to carry forward. Log the session to data/YYYY-MM-DD.json and append signals to data/observations.jsonl."
$weeklyPrompt   = "Scheduled weekly tango review. Use the tango-coach skill: read this week's session logs and scan observations.jsonl, review topic progress and the week's dancing (trends not single nights), then set next week's 1-3 active topics and plan the week's class/practica/milonga/solo nights. Update state.json, topic_library.json, and weekly_plan.json and tell me what changed."

switch ($Kind) {
    "reflect" { $checkinPrompt = $reflectPrompt }
    "weekly"  { $checkinPrompt = $weeklyPrompt }
    default   { $checkinPrompt = $preDancePrompt }
}

# -- 3. Open the Claude window (non-blocking) ------------------------------
try {
    Start-Process -FilePath "powershell.exe" `
        -WorkingDirectory $projectDir `
        -ArgumentList @("-NoExit", "-NoLogo", "-Command", "& `"$claudeExe`" `"$checkinPrompt`"") `
        -WindowStyle Normal | Out-Null
} catch {
    Write-CheckinLog "Failed to open Claude window: $_"
    exit 1
}

# -- 4. Pick a sound file --------------------------------------------------
$soundFile = $null
$soundsDir = Join-Path $projectDir "sounds"
if (Test-Path $soundsDir) {
    $candidate = Get-ChildItem -Path $soundsDir -Filter "checkin.*" -File -ErrorAction SilentlyContinue |
                 Select-Object -First 1
    if ($candidate) { $soundFile = $candidate.FullName }
}
if (-not $soundFile -and (Test-Path "C:\Windows\Media\Alarm01.wav")) {
    $soundFile = "C:\Windows\Media\Alarm01.wav"
}

# -- 5. Play it ------------------------------------------------------------
if (-not $soundFile) {
    [System.Media.SystemSounds]::Exclamation.Play()
    Start-Sleep -Milliseconds 800
    exit 0
}

$ext = [System.IO.Path]::GetExtension($soundFile).ToLower()
try {
    if ($ext -eq ".wav") {
        $player = New-Object System.Media.SoundPlayer $soundFile
        $player.PlaySync()
    } else {
        # mp3 / m4a / wma — use Windows MCI for blocking playback
        Add-Type @"
using System.Runtime.InteropServices;
using System.Text;
namespace Native {
    public static class WinMm {
        [DllImport("winmm.dll", CharSet=CharSet.Auto)]
        public static extern int mciSendString(string command, StringBuilder buffer, int bufferSize, System.IntPtr hwndCallback);
    }
}
"@ -ErrorAction SilentlyContinue

        $alias = "checkinSnd"
        $sb = New-Object System.Text.StringBuilder 256
        try {
            [Native.WinMm]::mciSendString("open `"$soundFile`" alias $alias", $sb, 256, [System.IntPtr]::Zero) | Out-Null
            [Native.WinMm]::mciSendString("play $alias wait", $sb, 256, [System.IntPtr]::Zero) | Out-Null
        } finally {
            [Native.WinMm]::mciSendString("close $alias", $sb, 256, [System.IntPtr]::Zero) | Out-Null
        }
    }
} catch {
    Write-CheckinLog "Sound playback failed: $_"
}
