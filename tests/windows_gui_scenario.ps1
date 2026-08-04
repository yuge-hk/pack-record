param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("rapid", "duplicate", "stop-clears", "single", "ime", "stress")]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [string]$RunId
)

$ErrorActionPreference = "Stop"
trap {
    ($_ | Out-String) | Set-Content (Join-Path $PSScriptRoot "scenario-error.txt")
    exit 1
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class TestKeyboard {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte key, byte scan, uint flags, UIntPtr extra);
}
"@

function Activate-Recorder {
    $shell = New-Object -ComObject WScript.Shell
    $targets = Get-CimInstance Win32_Process | Where-Object {
        $_.SessionId -eq 1 -and
        (
            ($_.Name -eq "pythonw.exe" -and $_.CommandLine -match "main.py") -or
            $_.Name -eq "ecom-love-obs.exe"
        )
    }
    foreach ($target in $targets) {
        if ($shell.AppActivate([int]$target.ProcessId)) {
            Start-Sleep -Milliseconds 250
            return
        }
    }
    throw "Recorder window could not be activated"
}

function Send-Scan([string]$Value) {
    Activate-Recorder
    [Windows.Forms.SendKeys]::SendWait("^a")
    [Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
    [Windows.Forms.SendKeys]::SendWait($Value)
    [Windows.Forms.SendKeys]::SendWait("{ENTER}")
}

function Toggle-InputLanguage {
    [TestKeyboard]::keybd_event(0x5B, 0, 0, [UIntPtr]::Zero)
    [TestKeyboard]::keybd_event(0x20, 0, 0, [UIntPtr]::Zero)
    [TestKeyboard]::keybd_event(0x20, 0, 2, [UIntPtr]::Zero)
    [TestKeyboard]::keybd_event(0x5B, 0, 2, [UIntPtr]::Zero)
}

switch ($Scenario) {
    "rapid" {
        Send-Scan "$RunId-A"
        Start-Sleep -Milliseconds 100
        Send-Scan "$RunId-B"
        Start-Sleep -Milliseconds 100
        Send-Scan "$RunId-C"
        Start-Sleep -Seconds 7
        Send-Scan "STOPSTOP"
    }
    "duplicate" {
        Send-Scan "$RunId-DUP"
        Start-Sleep -Milliseconds 100
        Send-Scan "$RunId-DUP"
        Start-Sleep -Seconds 4
        Send-Scan "STOPSTOP"
    }
    "stop-clears" {
        Send-Scan "$RunId-A"
        Start-Sleep -Seconds 2
        Send-Scan "$RunId-PENDING"
        Start-Sleep -Milliseconds 100
        Send-Scan "STOPSTOP"
    }
    "single" {
        Send-Scan $RunId
    }
    "ime" {
        Activate-Recorder
        Toggle-InputLanguage
        Start-Sleep -Milliseconds 500
        Send-Scan "$RunId-IME"
        Start-Sleep -Seconds 4
        Send-Scan "STOPSTOP"
        Activate-Recorder
        Toggle-InputLanguage
    }
    "stress" {
        for ($index = 1; $index -le 20; $index++) {
            Send-Scan ("{0}-S{1:D2}" -f $RunId, $index)
            Start-Sleep -Milliseconds 50
        }
        Start-Sleep -Seconds 8
        Send-Scan "STOPSTOP"
    }
}
