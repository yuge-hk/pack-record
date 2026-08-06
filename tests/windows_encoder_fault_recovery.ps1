param(
    [Parameter(Mandatory = $true)]
    [string]$RunId
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$shell = New-Object -ComObject WScript.Shell
foreach ($obsProcess in Get-Process obs64 -ErrorAction SilentlyContinue) {
    if ($shell.AppActivate([int]$obsProcess.Id)) {
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        Start-Sleep -Milliseconds 500
        break
    }
}

$recorderProcessIds = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.SessionId -eq 1 -and
        $_.Name -eq "pythonw.exe" -and
        $_.CommandLine -match "main.py"
    } | ForEach-Object { [int]$_.ProcessId }
)

$root = [System.Windows.Automation.AutomationElement]::RootElement
$windows = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)
$retryButton = $null
foreach ($window in $windows) {
    if ($recorderProcessIds -notcontains [int]$window.Current.ProcessId) { continue }
    $buttons = $window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )
    foreach ($button in $buttons) {
        if ($button.Current.Name -like "*$RunId*") {
            $retryButton = $button
            break
        }
    }
    if ($null -ne $retryButton) { break }
}

if ($null -eq $retryButton) {
    throw "Fault retry button not found for $RunId"
}
$retryButton.GetCurrentPattern(
    [System.Windows.Automation.InvokePattern]::Pattern
).Invoke()
