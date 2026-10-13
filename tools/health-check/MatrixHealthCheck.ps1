<#
.SYNOPSIS
  Matrix device health check for Windows devices. Read-only.

.DESCRIPTION
  Checks the hardware behind failed software updates, random network drops
  and corrupt downloads: disk health (health status, reliability counters,
  disk/NTFS errors, free space, dirty volumes), memory, CPU, hardware (WHEA)
  errors, unexpected shutdowns and blue screens, network adapters (link speed,
  duplex, errors, drops, disconnects, gateway reachability) and update
  readiness (pending reboot, Windows Update and service failures).

  Nothing on the device is changed. Prints a summary and saves an HTML and a
  JSON report. Run it as Administrator for the full check (disk reliability
  counters, dirty-volume and temperature readings need it).

  Remote support (BeyondTrust): copy this file to the device with File
  Transfer, run it from the Shell tab, then copy the report files back.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\MatrixHealthCheck.ps1

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\MatrixHealthCheck.ps1 -OutDir C:\Temp -Days 14
#>
[CmdletBinding()]
param(
    # Where the reports go (default: next to this script, else Public Documents).
    [string]$OutDir = "",
    # How far back to look in the event logs.
    [ValidateRange(1, 365)][int]$Days = 30,
    # Open the HTML report when done (for use on the device's own screen).
    [switch]$Open,
    # Print only the JSON report to stdout (no files).
    [switch]$Json
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # progress bars garble remote shells
$started = Get-Date
$since = $started.AddDays(-$Days)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$deg = [char]0xB0

# ---------------------------------------------------------------- helpers --
$script:Sections = New-Object System.Collections.ArrayList
$script:Notes = New-Object System.Collections.ArrayList

function New-Section([string]$Id, [string]$Title) {
    $s = [ordered]@{ id = $Id; title = $Title; state = 'ok'; group = 'health'; items = (New-Object System.Collections.ArrayList) }
    [void]$script:Sections.Add($s)
    return $s
}

# -Action: the plain next step for a field tech (kept only for warnings/problems).
function Add-Check($Section, [string]$Label, $Value, [string]$State = 'ok', [string]$Detail = '', [string]$Action = '') {
    if ($State -ne 'warn' -and $State -ne 'bad') { $Action = '' }
    [void]$Section.items.Add([ordered]@{ label = $Label; value = [string]$Value; state = $State; detail = $Detail; action = $Action })
}

# 0-100 health scores for the drive and the memory, built from penalties
# (Score-Hit) and ceilings (Score-Cap, "this alone means failing"). Free space
# is not part of them. Same rules as health_check.py (_Score).
function New-Score([string]$Title) {
    [ordered]@{ title = $Title; points = 100.0; ceiling = 100.0
        reasons = (New-Object System.Collections.ArrayList); facts = (New-Object System.Collections.ArrayList)
        missing = (New-Object System.Collections.ArrayList); stats = (New-Object System.Collections.ArrayList) }
}
# Key numbers shown as labelled values (life left, power-on time, cycles...).
function Score-Stat($S, [string]$Label, [string]$Value, [string]$State = 'info', [string]$Hint = '') {
    [void]$S.stats.Add([ordered]@{ label = $Label; value = $Value; state = $State; hint = $Hint })
}
function Score-Hit($S, [double]$Points, [string]$Reason) { $S.points -= $Points; [void]$S.reasons.Add($Reason) }
function Score-Cap($S, [double]$Ceiling, [string]$Reason = '') {
    if ($Ceiling -lt $S.ceiling) { $S.ceiling = $Ceiling }
    if ($Reason) { [void]$S.reasons.Add($Reason) }
}
function Score-Result($S) {
    $n = [int][math]::Round([math]::Max(0, [math]::Min($S.points, $S.ceiling)))
    $grade = if ($n -ge 90) { 'Good' } elseif ($n -ge 70) { 'Fair' } elseif ($n -ge 40) { 'Poor' } else { 'Failing' }
    $state = if ($grade -eq 'Good') { 'ok' } elseif ($grade -eq 'Fair') { 'warn' } else { 'bad' }
    [ordered]@{ title = $S.title; score = $n; grade = $grade; state = $state; reasons = $S.reasons; facts = $S.facts; stats = $S.stats; missing = $S.missing }
}
$diskScore = New-Score 'Drive health'
$memScore = New-Score 'Memory health'
$glance = New-Object System.Collections.ArrayList
function Add-Glance([string]$Label, [string]$Value, [string]$State = 'info') {
    [void]$glance.Add([ordered]@{ label = $Label; value = $Value; state = $State })
}

# Next steps shared by several checks (same wording as health_check.py).
$DO_REPLACE_DISK = 'Replace the disk (or the device). It is failing and will corrupt updates and downloads.'
$DO_FREE_SPACE = 'Free up disk space (old logs, old update files, Downloads, Recycle Bin), then retry the update.'
$DO_CHECK_CABLE = 'Replace the network cable or move it to another switch port, then check again.'
$DO_COOLING = "Check the fans and airflow: clean dust and make sure the vents aren't blocked."
$DO_REPLACE_HW = 'Replace the device: the hardware (CPU, memory or board) reports errors.'
$DO_RESTART = 'Restart the device and check again. If it comes back, export this report and escalate.'
$DO_POWER = 'Check the power supply, power cable and UPS: the device is losing power or resetting.'
$DO_REPLACE_RAM = 'Replace the memory (or the device): a memory module is going bad. This causes crashes and corrupt files.'

function Try-Get([scriptblock]$Block) {
    try { & $Block } catch { $null }
}

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1TB) { return ('{0:N1} TB' -f ($Bytes / 1TB)) }
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Event-Lines($Events, [int]$Max = 8) {
    ($Events | Select-Object -First $Max | ForEach-Object {
        $msg = ''
        try { $msg = ($_.Message -split "`r?`n")[0] } catch { }
        '{0:yyyy-MM-dd HH:mm}  {1} {2}: {3}' -f $_.TimeCreated, $_.ProviderName, $_.Id, $msg
    }) -join "`n"
}

# System log events of interest, one pass. Filtered by Id only (a provider
# name the device doesn't have would make Get-WinEvent fail), then sorted
# into categories by provider below. Id lists are kept short (FilterHashtable
# limit) by splitting into two queries.
$script:EventsReadable = $true
function Get-SystemEvents([int[]]$Ids) {
    try {
        return @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = $Ids; StartTime = $since } -MaxEvents 5000 -ErrorAction Stop)
    } catch {
        if ($_.Exception.Message -notmatch 'No events were found') { $script:EventsReadable = $false }
        return @()
    }
}
$sysEvents = @(Get-SystemEvents @(7, 11, 15, 51, 52, 129, 153, 157, 55, 98, 130, 137, 140, 41, 6008, 1001, 17, 18, 19))
$sysEvents += @(Get-SystemEvents @(47, 2004, 27, 32, 4202, 7000, 7001, 7009, 7011, 7023, 7024, 7031, 7034, 20))
# Windows Memory Diagnostic results (1101 passed, 1102 found errors), any age.
$memTest = @(Try-Get { Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = @(1101, 1102) } -MaxEvents 5 -ErrorAction Stop } |
    Where-Object { $_.ProviderName -match 'MemoryDiagnostics' })
$sysDrive = ($env:SystemDrive).TrimEnd(':')
$sysDisk = Try-Get { (Get-Partition -DriveLetter $sysDrive -ErrorAction Stop).DiskNumber }

function Pick-Events([string]$ProviderRegex, [int[]]$Ids, [switch]$ProblemsOnly) {
    # -ProblemsOnly: Critical/Error/Warning only (e.g. NTFS 98 at Information
    # level just says the volume is healthy).
    @($sysEvents | Where-Object {
        $_.ProviderName -match $ProviderRegex -and $Ids -contains $_.Id -and (-not $ProblemsOnly -or ($_.Level -ge 1 -and $_.Level -le 3))
    } | Sort-Object TimeCreated -Descending)
}

# ------------------------------------------------------------------ system --
$sec = New-Section 'ident' 'System'
$cs = Try-Get { Get-CimInstance Win32_ComputerSystem }
$os = Try-Get { Get-CimInstance Win32_OperatingSystem }
$bios = Try-Get { Get-CimInstance Win32_BIOS }
Add-Check $sec 'Computer name' $env:COMPUTERNAME 'info'
if ($cs) { Add-Check $sec 'Device' ("{0} {1}" -f $cs.Manufacturer, $cs.Model).Trim() 'info' }
if ($bios) { Add-Check $sec 'Serial number' $bios.SerialNumber 'info' ("BIOS {0}" -f $bios.SMBIOSBIOSVersion) }
if ($os) {
    Add-Check $sec 'OS' ("{0} (build {1})" -f $os.Caption, $os.BuildNumber) 'info' $os.Version
    $up = (Get-Date) - $os.LastBootUpTime
    Add-Check $sec 'Uptime' ("{0}d {1}h {2}m (booted {3:yyyy-MM-dd HH:mm})" -f $up.Days, $up.Hours, $up.Minutes, $os.LastBootUpTime) 'info'
    $upText = ("{0}d {1}h {2}m" -f $up.Days, $up.Hours, $up.Minutes)
}
$matrixProc = @(Get-Process -Name 'arthrex-synergy-matrix' -ErrorAction SilentlyContinue)
if ($matrixProc.Count) {
    Add-Check $sec 'Matrix app' ("running ({0} processes)" -f $matrixProc.Count) 'ok'
} else {
    Add-Check $sec 'Matrix app' 'not running' 'warn' 'No arthrex-synergy-matrix process.' `
        -Action 'Start the Matrix app (or restart the device). If it closes again, export this report and escalate.'
}

$kp = Pick-Events 'Kernel-Power' @(41)
$dirty = Pick-Events '^EventLog$' @(6008)
$bsod = Pick-Events 'BugCheck|WER-SystemErrorReporting' @(1001)
if ($script:EventsReadable) {
    $n = $kp.Count + $dirty.Count
    $state = if ($n -gt 3) { 'bad' } elseif ($n -gt 0) { 'warn' } else { 'ok' }
    Add-Check $sec "Sudden power-offs / hard resets ($Days days)" $n $state ("Power loss or hard resets. They corrupt files being written (updates, downloads).`n" + (Event-Lines ($kp + $dirty))) -Action $DO_POWER
    $state = if ($bsod.Count) { 'bad' } else { 'ok' }
    Add-Check $sec "Crashes (blue screens, $Days days)" $bsod.Count $state (Event-Lines $bsod) `
        -Action 'Windows crashed: restart the device and escalate with this report (often a failing disk, memory or driver).'
}

# ----------------------------------------------------------------- storage --
$sec = New-Section 'disk' 'Storage'
$pdisks = @(Try-Get { Get-PhysicalDisk -ErrorAction Stop })
if ($pdisks.Count) {
    foreach ($d in $pdisks) {
        $problems = New-Object System.Collections.ArrayList
        $warns = New-Object System.Collections.ArrayList
        $state = 'ok'
        switch ([string]$d.HealthStatus) {
            'Healthy' { }
            'Warning' { [void]$warns.Add('health status Warning'); $state = 'warn' }
            default { [void]$problems.Add("health status $($d.HealthStatus)"); $state = 'bad' }
        }
        $ops = @($d.OperationalStatus) -join ', '
        if ($ops -and $ops -ne 'OK') {
            if ($ops -match 'Predictive|Failed|Lost|Error|Degraded') { [void]$problems.Add("status $ops"); $state = 'bad' }
            else { [void]$warns.Add("status $ops"); if ($state -eq 'ok') { $state = 'warn' } }
        }
        $detail = "Media {0}, bus {1}, size {2}, firmware {3}" -f $d.MediaType, $d.BusType, (Format-Size $d.Size), $d.FirmwareVersion
        # Only the disk Windows runs from counts for the drive score (not USB drives).
        $isSysDisk = (($null -ne $sysDisk) -and ([string]$d.DeviceId -eq [string]$sysDisk)) -or (($null -eq $sysDisk) -and $pdisks.Count -eq 1)
        $ds = if ($isSysDisk) { $diskScore } else { New-Score 'other disk' }
        if ($isSysDisk) { [void]$ds.facts.Add(("{0} ({1})" -f $d.FriendlyName, (Format-Size $d.Size))) }
        if ([string]$d.HealthStatus -eq 'Warning') { Score-Hit $ds 30 'Windows rates the disk as Warning' }
        elseif ($d.HealthStatus -and [string]$d.HealthStatus -ne 'Healthy') { Score-Cap $ds 10 "Windows rates the disk as $($d.HealthStatus)" }
        if ($ops -match 'Predictive|Failed|Lost|Error|Degraded') { Score-Cap $ds 15 "the disk reports: $ops" }
        $rc = $null
        $life = $null
        if ($isAdmin) { $rc = Try-Get { $d | Get-StorageReliabilityCounter -ErrorAction Stop } }
        if ($rc) {
            if ($rc.ReadErrorsUncorrected -gt 0) { [void]$problems.Add("$($rc.ReadErrorsUncorrected) uncorrected read errors"); $state = 'bad'; Score-Hit $ds 40 "$($rc.ReadErrorsUncorrected) read errors the disk couldn't fix" }
            if ($rc.WriteErrorsUncorrected -gt 0) { [void]$problems.Add("$($rc.WriteErrorsUncorrected) uncorrected write errors"); $state = 'bad'; Score-Hit $ds 40 "$($rc.WriteErrorsUncorrected) write errors the disk couldn't fix" }
            # Some SSDs never update their wear value (e.g. Phison-based Kingston:
            # 0% used after ~6 years in QA1 OR 6). Don't show that as 100% left.
            if ($null -ne $rc.Wear -and [int]$rc.Wear -eq 0 -and $rc.PowerOnHours -gt 26280) {
                $why = "the disk still reports 0% wear after {0:N1} years, which isn't believable" -f ($rc.PowerOnHours / 8760)
                [void]$ds.missing.Add("life left ($why)")
                Score-Stat $ds 'Life left' 'not reported' 'info' $why
            } elseif ($null -ne $rc.Wear) {
                $life = [math]::Max(0, 100 - [int]$rc.Wear)
                Score-Stat $ds 'Life left' "$life%" $(if ($life -le 10) { 'bad' } elseif ($life -le 50) { 'warn' } else { 'ok' })
                if ($life -lt 50) { Score-Hit $ds ((50 - $life) * 0.6) "only $life% of its rated life left" }
            }
            if ($rc.Wear -ge 90) { [void]$warns.Add("$($rc.Wear)% of rated life used"); if ($state -eq 'ok') { $state = 'warn' } }
            if ($rc.Temperature -ge 70) { [void]$warns.Add("temperature $($rc.Temperature)$($deg)C"); if ($state -eq 'ok') { $state = 'warn' }; Score-Hit $ds 10 "running hot ($($rc.Temperature)$($deg)C)" }
            if ($rc.PowerOnHours) { Score-Stat $ds 'Power-on time' ("{0:N0} h ({1:N1} years)" -f $rc.PowerOnHours, ($rc.PowerOnHours / 8760)) }
            if ($rc.StartStopCycleCount) { Score-Stat $ds 'Power cycles' ("{0:N0}" -f $rc.StartStopCycleCount) }
            if ($rc.Temperature) { Score-Stat $ds 'Temperature' "$($rc.Temperature)$($deg)C" $(if ($rc.Temperature -ge 70) { 'bad' } elseif ($rc.Temperature -ge 60) { 'warn' } else { 'ok' }) }
            $unc = [int]$rc.ReadErrorsUncorrected + [int]$rc.WriteErrorsUncorrected
            Score-Stat $ds 'Uncorrected read/write errors' ("{0:N0}" -f $unc) $(if ($unc) { 'bad' } else { 'ok' })
            $detail += "`nReliability: wear {0}%, temp {1}{2}C (max {3}), read errors total/corrected/uncorrected {4}/{5}/{6}, write errors uncorrected {7}, power-on hours {8}" -f `
                $rc.Wear, $rc.Temperature, $deg, $rc.TemperatureMax, $rc.ReadErrorsTotal, $rc.ReadErrorsCorrected, $rc.ReadErrorsUncorrected, $rc.WriteErrorsUncorrected, $rc.PowerOnHours
        }
        $value = (@($problems) + @($warns)) -join '; '
        if (-not $value) { $value = 'healthy' }
        if ($null -ne $life) { $value += ", $life% life left" }
        if (-not $isSysDisk) { $value += ' (not the Windows disk)' }
        $act = if ($state -eq 'bad') { $DO_REPLACE_DISK } elseif ($value -match 'temperature' -and $warns.Count -eq 1) { $DO_COOLING } else { 'Plan to replace the disk soon: it is wearing out or reporting errors.' }
        Add-Check $sec ("Disk health: {0} ({1})" -f $d.FriendlyName, (Format-Size $d.Size)) $value $state $detail -Action $act
    }
} else {
    foreach ($d in @(Try-Get { Get-CimInstance Win32_DiskDrive })) {
        $state = if ($d.Status -eq 'OK') { 'ok' } else { 'bad' }
        if ($state -eq 'bad' -and [string]$d.Index -eq [string]$sysDisk) { Score-Cap $diskScore 15 "Windows reports the disk status as $($d.Status)" }
        Add-Check $sec ("Disk health: {0}" -f $d.Model) ("status {0}" -f $d.Status) $state ("{0}, {1}" -f $d.InterfaceType, (Format-Size $d.Size)) -Action $DO_REPLACE_DISK
    }
}
if ($isAdmin) {
    $fp = @(Try-Get { Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictStatus -ErrorAction Stop })
    $failing = @($fp | Where-Object { $_.PredictFailure })
    if ($failing.Count) {
        Score-Cap $diskScore 10 'the disk predicts its own failure (SMART)'
        Add-Check $sec 'Disk predicts its own failure (SMART)' ("{0} disk(s)" -f $failing.Count) 'bad' 'The disk itself reports that it is about to fail.' -Action $DO_REPLACE_DISK
    }
    elseif ($fp.Count) { Add-Check $sec 'Disk predicts its own failure (SMART)' 'no' 'ok' }
} else {
    [void]$diskScore.missing.Add('disk wear and error counters (run as Administrator)')
    Add-Check $sec 'Detailed disk wear and error counters' 'skipped (run as Administrator)' 'info'
}

$vols = @(Try-Get { Get-Volume -ErrorAction Stop | Where-Object { $_.DriveType -eq 'Fixed' -and $_.DriveLetter } } | Sort-Object { [string]$_.DriveLetter -ne $sysDrive }, DriveLetter)
if ($vols.Count) {
    foreach ($v in $vols) {
        $free = [double]$v.SizeRemaining; $size = [double]$v.Size
        if ($size -le 0) { continue }
        $pct = [math]::Round(100 * $free / $size)
        $state = 'ok'
        if ($pct -lt 5 -or $free -lt 2GB) { $state = 'bad' } elseif ($pct -lt 15 -or $free -lt 5GB) { $state = 'warn' }
        $detail = ''
        if ($state -ne 'ok') { $detail = 'A nearly full drive makes updates fail and downloads end up incomplete or corrupt.' }
        $hs = [string]$v.HealthStatus
        if ($hs -and $hs -ne 'Healthy') { $state = 'bad'; $detail = "Volume health: $hs. $detail" }
        $label = "Free space on drive {0}:" -f $v.DriveLetter
        if ([string]$v.DriveLetter -eq $sysDrive) { $label += ' (Windows drive)' }
        $act = if ($hs -and $hs -ne 'Healthy') { $DO_REPLACE_DISK } else { $DO_FREE_SPACE }
        $isSysVol = [string]$v.DriveLetter -eq $sysDrive
        if ($hs -and $hs -ne 'Healthy' -and $isSysVol) { Score-Hit $diskScore 25 "Windows rates drive $($v.DriveLetter): as $hs" }
        $freeText = "{0} free of {1} ({2}% free)" -f (Format-Size $free), (Format-Size $size), $pct
        $spaceState = if ($pct -lt 5 -or $free -lt 2GB) { 'bad' } elseif ($pct -lt 15 -or $free -lt 5GB) { 'warn' } else { 'ok' }
        if ($glance.Count -lt 3) { Add-Glance ("Drive {0}" -f $v.DriveLetter) $freeText $spaceState }
        Add-Check $sec $label $freeText $state ("$detail File system {0}, label '{1}'." -f $v.FileSystem, $v.FileSystemLabel).Trim() -Action $act
        if ($isAdmin) {
            $q = Try-Get { & fsutil.exe dirty query "$($v.DriveLetter):" 2>$null }
            if ($q -match 'is Dirty') {
                if ($isSysVol) { Score-Hit $diskScore 25 'Windows found file system damage on the Windows drive' }
                Add-Check $sec ("Drive {0}: marked as damaged" -f $v.DriveLetter) 'yes' 'bad' 'Windows found file system corruption on this drive (dirty bit); it will run chkdsk at the next boot.' `
                    -Action 'Restart the device so Windows repairs the drive (let the disk check finish). If it comes back, replace the disk.'
            }
        }
    }
} else {
    foreach ($v in @(Try-Get { Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' })) {
        $free = [double]$v.FreeSpace; $size = [double]$v.Size
        if ($size -le 0) { continue }
        $pct = [math]::Round(100 * $free / $size)
        $state = 'ok'
        if ($pct -lt 5 -or $free -lt 2GB) { $state = 'bad' } elseif ($pct -lt 15 -or $free -lt 5GB) { $state = 'warn' }
        if ($glance.Count -lt 3) { Add-Glance ("Drive {0}" -f $v.DeviceID.TrimEnd(':')) ("{0} free of {1} ({2}% free)" -f (Format-Size $free), (Format-Size $size), $pct) $state }
        Add-Check $sec ("Free space on drive {0}" -f $v.DeviceID) ("{0} free of {1} ({2}% free)" -f (Format-Size $free), (Format-Size $size), $pct) $state -Action $DO_FREE_SPACE
    }
}
$tempFree = Try-Get { (Get-PSDrive -Name ([IO.Path]::GetPathRoot($env:TEMP).TrimEnd(':\'))).Free }

if ($script:EventsReadable) {
    # Disk events name the disk ("\Device\Harddisk0\DR0"): errors on the
    # system disk are what break updates; others are often USB sticks/drives.
    $diskErr = Pick-Events '^disk$' @(7, 11, 15, 51, 52, 153, 157) -ProblemsOnly
    $byDisk = @{}
    foreach ($e in $diskErr) {
        $num = 'unknown'
        try { if ($e.Message -match 'Harddisk(\d+)|\bDisk (\d+)') { $num = @($Matches[1], $Matches[2]) | Where-Object { $_ } | Select-Object -First 1 } } catch { }
        if (-not $byDisk.ContainsKey($num)) { $byDisk[$num] = New-Object System.Collections.ArrayList }
        [void]$byDisk[$num].Add($e)
    }
    foreach ($num in ($byDisk.Keys | Sort-Object)) {
        $evs = @($byDisk[$num])
        $isSys = ($null -ne $sysDisk) -and ([string]$sysDisk -eq $num)
        $hard = @($evs | Where-Object { @(7, 11, 15, 51, 52) -contains $_.Id })
        $state = 'warn'
        if ($isSys -and $hard.Count) { $state = 'bad'; Score-Hit $diskScore 30 "$($hard.Count) read/write errors on the Windows disk in $Days days" }
        elseif ($isSys) { Score-Hit $diskScore 10 "$($evs.Count) retried reads/writes on the Windows disk in $Days days" }
        $what = if ($isSys) { "Windows disk (disk $num)" } elseif ($num -eq 'unknown') { 'unknown disk' } else { "disk $num (not the Windows disk, e.g. a USB drive)" }
        $counts = ($evs | Group-Object Id | Sort-Object Name | ForEach-Object { "{0}x id {1}" -f $_.Count, $_.Name }) -join ', '
        $act = if ($isSys) { $DO_REPLACE_DISK } else { 'Unplug or replace that extra drive (USB stick or external disk); the device itself is not affected.' }
        Add-Check $sec "Disk read/write errors ($Days days): $what" $evs.Count $state `
            ("Event ids: $counts. 7 = bad block, 11 = controller error, 15 = device not ready, 51 = error during paging, 153 = I/O retried.`n" + (Event-Lines $evs 10)) -Action $act
    }
    if (-not $diskErr.Count) { Add-Check $sec "Disk read/write errors ($Days days)" 0 'ok' }
    $ntfs = Pick-Events 'Ntfs' @(55, 98, 130, 137, 140) -ProblemsOnly
    $state = if ($ntfs.Count) { 'bad' } else { 'ok' }
    if ($ntfs.Count) { Score-Hit $diskScore 25 "$($ntfs.Count) file system damage reports in $Days days" }
    Add-Check $sec "File system damage ($Days days)" $ntfs.Count $state (Event-Lines $ntfs 10) `
        -Action 'Restart the device so Windows repairs the file system. If it keeps coming back, replace the disk.'
    # Resets from USB storage drivers (UASPStor, USBSTOR) are external drives, not the device's disk.
    $resets = @(Pick-Events 'stor|nvme|ahci|iaStor|^disk$|LSI|amdsata' @(129) -ProblemsOnly | Where-Object { $_.ProviderName -notmatch 'UASP|USB' })
    $state = if ($resets.Count -gt 5) { 'warn' } elseif ($resets.Count) { 'info' } else { 'ok' }
    if ($resets.Count -gt 5) { Score-Hit $diskScore 10 "the disk stopped responding $($resets.Count) times in $Days days" }
    Add-Check $sec "Disk stopped responding ($Days days)" $resets.Count $state ("Reset to device (129): the disk stopped answering for a while.`n" + (Event-Lines $resets 8)) `
        -Action 'Plan to replace the disk (it stops responding now and then).'
} else {
    [void]$diskScore.missing.Add('the disk error history (run as Administrator)')
}

# ------------------------------------------------------------------ memory --
$sec = New-Section 'mem' 'Memory'
if ($os) {
    $totalMB = [math]::Round($os.TotalVisibleMemorySize / 1KB)
    $freeMB = [math]::Round($os.FreePhysicalMemory / 1KB)
    $state = 'ok'
    if ($freeMB -lt 256) { $state = 'bad' } elseif ($freeMB -lt 512 -or $freeMB * 10 -lt $totalMB) { $state = 'warn' }
    if ($state -eq 'bad') { Score-Hit $memScore 20 'almost no free memory right now' } elseif ($state -eq 'warn') { Score-Hit $memScore 10 'low on free memory right now' }
    Score-Stat $memScore 'Installed' ("{0:N1} GB" -f ($totalMB / 1024))
    Score-Stat $memScore 'Free right now' ("{0:N1} GB" -f ($freeMB / 1024)) $state
    $memGlance = "{0:N1} GB free of {1:N1} GB" -f ($freeMB / 1024), ($totalMB / 1024)
    Add-Check $sec 'Free memory' ("{0} MB free of {1} MB" -f $freeMB, $totalMB) $state -Action $DO_RESTART
    $memGlanceState = $state
    $cTotal = [double]$os.TotalVirtualMemorySize; $cFree = [double]$os.FreeVirtualMemory
    if ($cTotal -gt 0) {
        $state = if ($cFree * 10 -lt $cTotal) { 'warn' } else { 'ok' }
        if ($state -eq 'warn') { Score-Hit $memScore 10 'memory plus page file nearly used up' }
        Add-Check $sec 'Free memory incl. page file' ("{0} MB free of {1} MB" -f [math]::Round($cFree / 1KB), [math]::Round($cTotal / 1KB)) $state 'Windows commit limit (RAM + page file).' -Action $DO_RESTART
    }
}
$dimms = @(Try-Get { Get-CimInstance Win32_PhysicalMemory })
if ($dimms.Count) {
    Score-Stat $memScore 'Modules' ("{0}" -f $dimms.Count)
    Add-Check $sec 'Memory modules' (($dimms | ForEach-Object { "{0} {1}" -f (Format-Size $_.Capacity), $_.DeviceLocator }) -join ', ') 'info'
}
if ($script:EventsReadable) {
    # Memory errors reported by the hardware (WHEA): 47 = corrected memory
    # error, 18 = fatal hardware error, 19 = corrected machine check.
    $whea = Pick-Events 'WHEA' @(17, 18, 19, 47)
    $memErr = @($whea | Where-Object { $_.Id -eq 47 })
    $fatalHw = @($whea | Where-Object { $_.Id -eq 18 })
    $mceHw = @($whea | Where-Object { $_.Id -eq 19 })
    $state = 'ok'
    if ($fatalHw.Count -or $memErr.Count -ge 10) { $state = 'bad' } elseif ($memErr.Count) { $state = 'warn' }
    if ($memErr.Count) { Score-Hit $memScore $(if ($memErr.Count -lt 10) { 30 } else { 50 }) "$($memErr.Count) corrected memory errors (a module is going bad)" }
    if ($fatalHw.Count) { Score-Cap $memScore 20 "$($fatalHw.Count) serious hardware errors (CPU, memory or board)" }
    if ($mceHw.Count -gt 10) { Score-Hit $memScore 20 "$($mceHw.Count) corrected CPU/memory hardware errors" }
    $val = if ($memErr.Count -or $fatalHw.Count) { "{0} memory, {1} serious" -f $memErr.Count, $fatalHw.Count } else { 'none' }
    Score-Stat $memScore "Memory errors ($Days days)" ("{0:N0}" -f ($memErr.Count + $fatalHw.Count)) $state
    Add-Check $sec "Memory errors reported by the hardware ($Days days)" $val $state `
        ("Windows WHEA events 47 (corrected memory error) and 18 (fatal hardware error).`n" + (Event-Lines (@($fatalHw) + @($memErr)))) -Action $DO_REPLACE_RAM
    $low = Pick-Events 'Resource-Exhaustion' @(2004)
    $state = if ($low.Count) { 'warn' } else { 'ok' }
    if ($low.Count) { Score-Hit $memScore $(if ($low.Count -gt 5) { 25 } else { 15 }) "ran out of memory $($low.Count) time(s)" }
    Add-Check $sec "Ran out of memory ($Days days)" $low.Count $state (Event-Lines $low) -Action $DO_RESTART
} else {
    [void]$memScore.missing.Add('the memory error history (run as Administrator)')
}
# The built-in Windows Memory Diagnostic (if anyone ever ran it).
if ($memTest.Count) {
    $last = $memTest | Sort-Object TimeCreated -Descending | Select-Object -First 1
    if ($last.Id -eq 1102) {
        Score-Cap $memScore 10 ("Windows Memory Diagnostic found errors ({0:yyyy-MM-dd})" -f $last.TimeCreated)
        Score-Stat $memScore 'Memory test' ("FAILED {0:yyyy-MM-dd}" -f $last.TimeCreated) 'bad'
        Add-Check $sec 'Windows memory test (last run)' ("found errors on {0:yyyy-MM-dd}" -f $last.TimeCreated) 'bad' 'Windows Memory Diagnostic result (event 1102).' -Action $DO_REPLACE_RAM
    } else {
        Score-Stat $memScore 'Memory test' ("passed {0:yyyy-MM-dd}" -f $last.TimeCreated) 'ok'
        Add-Check $sec 'Windows memory test (last run)' ("passed on {0:yyyy-MM-dd}" -f $last.TimeCreated) 'ok' 'Windows Memory Diagnostic result (event 1101).'
    }
} else {
    Score-Stat $memScore 'Memory test' 'never run' 'info' 'Run mdsched.exe for a full test'
    Add-Check $sec 'Windows memory test (last run)' 'never run' 'info' 'For a full memory test, run Windows Memory Diagnostic (mdsched.exe). It restarts the device and takes 10-20 minutes.'
}

# --------------------------------------------------------- CPU / hardware --
$sec = New-Section 'cpu' 'CPU & hardware'
$cpu = @(Try-Get { Get-CimInstance Win32_Processor })
if ($cpu.Count) {
    $c = $cpu[0]
    Add-Check $sec 'CPU' ("{0} ({1} threads)" -f $c.Name.Trim(), $c.NumberOfLogicalProcessors) 'info'
    $load = ($cpu | Measure-Object -Property LoadPercentage -Average).Average
    if ($null -ne $load) {
        $state = if ($load -ge 90) { 'warn' } else { 'ok' }
        Add-Check $sec 'CPU busy (right now)' ("{0:N0}%" -f $load) $state 'One sample; check Task Manager if it stays high.' -Action $DO_RESTART
    }
}
if ($isAdmin) {
    $tz = @(Try-Get { Get-CimInstance -Namespace root\wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop })
    $temps = @($tz | ForEach-Object { [math]::Round($_.CurrentTemperature / 10 - 273.15) } | Where-Object { $_ -gt 0 -and $_ -lt 130 })
    if ($temps.Count) {
        $max = ($temps | Measure-Object -Maximum).Maximum
        $state = 'ok'
        if ($max -ge 90) { $state = 'bad' } elseif ($max -ge 75) { $state = 'warn' }
        Add-Check $sec 'Temperature' (($temps | ForEach-Object { "$_$($deg)C" }) -join ', ') $state 'ACPI thermal zones (not every board reports the CPU itself).' -Action $DO_COOLING
    }
}
if ($script:EventsReadable) {
    # Memory errors (47) and fatal errors (18) are scored under Memory; here
    # only the CPU (19) and the internal bus (17, usually harmless).
    $whea = Pick-Events 'WHEA' @(17, 19)
    $mce = @($whea | Where-Object { $_.Id -eq 19 })
    $pcie = @($whea | Where-Object { $_.Id -eq 17 })
    $state = 'ok'
    if ($mce.Count -gt 10) { $state = 'warn' } elseif ($whea.Count) { $state = 'info' }
    Add-Check $sec "Hardware error reports ($Days days)" ("{0} CPU, {1} minor (internal bus)" -f $mce.Count, $pcie.Count) $state `
        ("Windows WHEA events. Corrected CPU errors (19); corrected PCIe errors (17) are common and usually harmless.`n" + (Event-Lines (@($mce) + @($pcie)))) -Action $DO_REPLACE_HW
}

# ----------------------------------------------------------------- network --
$sec = New-Section 'net' 'Network'
$adapters = @(Try-Get { Get-NetAdapter -Physical -ErrorAction Stop })
$gwRoute = Try-Get { Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object RouteMetric | Select-Object -First 1 }
foreach ($a in $adapters) {
    if ($a.Status -eq 'Not Present') { continue }   # removed USB/virtual-dock adapters
    $st = Try-Get { Get-NetAdapterStatistics -Name $a.Name -ErrorAction Stop }
    $problems = New-Object System.Collections.ArrayList
    $state = 'ok'
    $wifi = ([string]$a.NdisPhysicalMedium -match '802\.11') -or ($a.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11')
    $isGw = $gwRoute -and $gwRoute.ifIndex -eq $a.ifIndex
    if ($a.Status -ne 'Up') {
        if ($isGw) { [void]$problems.Add('this is the main network port'); $state = 'bad' }
        else { $state = 'info' }
    } else {
        $bps = [double]$a.ReceiveLinkSpeed
        if (-not $wifi -and $bps -gt 0 -and $bps -lt 1e9) {
            [void]$problems.Add("running at only $($a.LinkSpeed) (should be 1 Gbps or more)"); $state = 'warn'
        }
        # Wi-Fi is always half duplex; only a wired port running half duplex is a fault.
        if ($a.FullDuplex -eq $false -and -not $wifi) { [void]$problems.Add('half duplex (cable or switch port problem)'); $state = 'warn' }
    }
    $detail = "{0}, MAC {1}" -f $a.InterfaceDescription, $a.MacAddress
    if ($st) {
        $errs = [double]$st.ReceivedPacketErrors + [double]$st.OutboundPacketErrors
        $drops = [double]$st.ReceivedDiscardedPackets + [double]$st.OutboundDiscardedPackets
        $pkts = [double]$st.ReceivedUnicastPackets + [double]$st.SentUnicastPackets
        # A handful of bad packets among millions is normal (same rule as health_check.py).
        if ($errs -gt 1000 -or ($pkts -gt 0 -and $errs / $pkts -gt 0.0001)) {
            [void]$problems.Add("$errs damaged packets")
            if ($state -eq 'ok' -or $state -eq 'info') { $state = 'warn' }
            if ($pkts -gt 0 -and $errs / $pkts -gt 0.001) { $state = 'bad' }
        } elseif ($errs -gt 0) {
            [void]$problems.Add("$errs damaged packets (normal level)")
        }
        if ($drops -gt 1000 -and $pkts -gt 0 -and $drops / $pkts -gt 0.01) {
            [void]$problems.Add("$drops dropped packets")
            if ($state -eq 'ok' -or $state -eq 'info') { $state = 'warn' }
        }
        $detail += "`nErrors rx/tx {0}/{1}, discarded rx/tx {2}/{3}, unicast packets rx/tx {4}/{5} (since the adapter last started)" -f `
            $st.ReceivedPacketErrors, $st.OutboundPacketErrors, $st.ReceivedDiscardedPackets, $st.OutboundDiscardedPackets, $st.ReceivedUnicastPackets, $st.SentUnicastPackets
    }
    $value = if ($a.Status -eq 'Up') { "connected at $($a.LinkSpeed)" } else { 'not connected' }
    if ($problems.Count) { $value = $value + ' - ' + (@($problems) -join '; ') }
    if ($isGw) { $detail += "`nCarries the default route." }
    $act = if ($wifi) { 'Check the Wi-Fi connection, or use a network cable instead.' } else { $DO_CHECK_CABLE }
    Add-Check $sec ("Network port '{0}'" -f $a.Name) $value $state $detail -Action $act
    if ($a.Status -eq 'Up' -and ((-not $netGlance) -or $isGw)) { $netGlance = @(("Network '{0}'" -f $a.Name), "connected at $($a.LinkSpeed)", $state) }
}
if (-not $adapters.Count) {
    foreach ($a in @(Try-Get { Get-CimInstance Win32_NetworkAdapter -Filter 'PhysicalAdapter=True' })) {
        $state = if ($a.NetEnabled) { 'ok' } else { 'info' }
        Add-Check $sec ("Network port '{0}'" -f $a.NetConnectionID) ("{0} {1}" -f $(if ($a.NetEnabled) { 'connected' } else { 'not connected' }), $(if ($a.Speed) { "at $([math]::Round($a.Speed / 1e6)) Mbps" })) $state $a.Name
    }
}
if ($gwRoute) {
    $gw = $gwRoute.NextHop
    $ok = 0; $rtts = @()
    $ping = New-Object System.Net.NetworkInformation.Ping
    for ($i = 0; $i -lt 4; $i++) {
        try { $r = $ping.Send($gw, 1000); if ($r.Status -eq 'Success') { $ok++; $rtts += $r.RoundtripTime } } catch { }
    }
    $loss = [math]::Round(100 * (4 - $ok) / 4)
    $state = if ($loss -gt 0) { 'warn' } else { 'ok' }
    $avg = if ($rtts.Count) { ', {0:N0} ms' -f (($rtts | Measure-Object -Average).Average) } else { '' }
    $detail = "Gateway $gw, 4 pings."
    if ($loss -eq 100) { $detail += ' No reply at all. Some routers ignore ping, so check the other results too.' }
    Add-Check $sec 'Reaches the router (ping)' ("{0}% lost{1}" -f $loss, $avg) $state $detail -Action $DO_CHECK_CABLE
} else {
    Add-Check $sec 'Route to the outside network' 'none' 'warn' 'No default route: the device cannot reach anything outside its own network.' `
        -Action "Check the device's network settings (gateway) and that the cable is plugged in."
}
if ($script:EventsReadable) {
    $flaps = @($sysEvents | Where-Object { ($_.Id -eq 27 -and $_.ProviderName -notmatch 'Microsoft-Windows') -or ($_.Id -eq 4202 -and $_.ProviderName -match 'Tcpip') })
    $state = 'ok'
    # Every restart or replug also drops the link once, so only a lot counts.
    if ($flaps.Count -gt 100) { $state = 'bad' } elseif ($flaps.Count -gt 30) { $state = 'warn' } elseif ($flaps.Count) { $state = 'info' }
    Add-Check $sec "Network disconnects ($Days days)" $flaps.Count $state ("The network link went down. A few come from restarts and replugging; dozens point at the cable, port or switch.`n" + (Event-Lines ($flaps | Sort-Object TimeCreated -Descending))) `
        -Action 'Replace the network cable (or fiber module) and try another switch port: the link keeps dropping.'
}

# ------------------------------------------------------- update readiness --
$sec = New-Section 'svc' 'Updates & services'
$pending = @()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pending += 'Windows servicing' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pending += 'Windows Update' }
$pfro = Try-Get { (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction Stop).PendingFileRenameOperations }
if ($pfro) { $pending += 'pending file renames' }
if ($pending.Count) {
    Add-Check $sec 'Waiting for a restart' 'yes' 'warn' ("Pending: {0}. Installs can fail until the device is restarted." -f ($pending -join ', ')) `
        -Action 'Restart the device before installing the update.'
} else { Add-Check $sec 'Waiting for a restart' 'no' 'ok' }
if ($null -ne $tempFree) {
    $state = if ($tempFree -lt 2GB) { 'warn' } else { 'ok' }
    Add-Check $sec 'Space for downloads' (Format-Size $tempFree) $state "TEMP folder: $env:TEMP" -Action $DO_FREE_SPACE
}
foreach ($name in @('BITS', 'wuauserv', 'TrustedInstaller')) {
    $s = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($s) {
        $state = if ($s.StartType -eq 'Disabled') { 'info' } else { 'ok' }
        Add-Check $sec $s.DisplayName ("{0} (starts {1})" -f $s.Status, $s.StartType) $state "Windows service $name. Stopped is normal when idle; Disabled blocks downloads/updates."
    }
}
if ($script:EventsReadable) {
    $wu = Pick-Events 'WindowsUpdateClient' @(20)
    $state = if ($wu.Count) { 'warn' } else { 'ok' }
    Add-Check $sec "Windows updates that failed ($Days days)" $wu.Count $state (Event-Lines $wu) `
        -Action 'Restart the device and free up disk space. If updates keep failing, check the disk results above and escalate.'
    $scm = Pick-Events 'Service Control Manager' @(7000, 7001, 7009, 7011, 7023, 7024, 7031, 7034)
    $state = 'ok'
    if ($scm.Count -gt 20) { $state = 'warn' } elseif ($scm.Count) { $state = 'info' }
    Add-Check $sec "Background services that failed or crashed ($Days days)" $scm.Count $state (Event-Lines $scm 12) -Action $DO_RESTART
} else {
    [void]$script:Notes.Add('The event log could not be read, so the error history (disk, shutdowns, hardware, network drops) is missing. Run as Administrator.')
}
if (-not $isAdmin) {
    [void]$script:Notes.Add('Not run as Administrator, so some disk checks and temperatures were skipped. For the full check, run PowerShell as Administrator.')
}

# ------------------------------------------------------------------ report --
# Drives and memory first: they're what the scores are about.
$order = @{ disk = 0; mem = 1; cpu = 2; net = 3; svc = 4; ident = 5 }
$script:Sections = @($script:Sections | Sort-Object { $order[$_.id] })
# The device's health (verdict, issues, What to do) = drive, memory, CPU/temperature/power
# and network. Software and system info go under "Other details" (same as health_check.py).
$identSec = $script:Sections | Where-Object { $_.id -eq 'ident' }
$cpuSec = $script:Sections | Where-Object { $_.id -eq 'cpu' }
$cpuSec.title = 'CPU, temperature & power'
foreach ($it in @($identSec.items | Where-Object { $_.label -match '^(Sudden power-offs|Crashes)' })) {
    [void]$cpuSec.items.Add($it)
    $identSec.items.Remove($it)
}
($script:Sections | Where-Object { $_.id -eq 'svc' }).title = 'Software, updates & services'
foreach ($s in $script:Sections) { $s.group = if (@('disk', 'mem', 'cpu', 'net') -contains $s.id) { 'health' } else { 'other' } }
$scores = [ordered]@{ disk = (Score-Result $diskScore); memory = (Score-Result $memScore) }
if ($memGlance) { Add-Glance 'Free memory' $memGlance $memGlanceState }
if ($netGlance) { Add-Glance $netGlance[0] $netGlance[1] $netGlance[2] }
# "Device" summary at the top: what this box is (same fields as health_check.build_device).
$device = New-Object System.Collections.ArrayList
function Add-Device([string]$Label, $Value) { if ("$Value".Trim()) { [void]$device.Add([ordered]@{ label = $Label; value = ("$Value".Trim() -replace '\s+', ' ') }) } }
Add-Device 'Name' $env:COMPUTERNAME
if ($cs) { Add-Device 'Model' ("{0} {1}" -f $cs.Manufacturer, $cs.Model) }
if ($bios -and $bios.SerialNumber -notmatch '^(Default string|To be filled.*|None|0+|System Serial Number)?$') { Add-Device 'Serial number' $bios.SerialNumber }
if ($gwRoute) {
    $ips = @(Try-Get { Get-NetIPAddress -InterfaceIndex $gwRoute.ifIndex -AddressFamily IPv4 -ErrorAction Stop } | ForEach-Object { $_.IPAddress })
    Add-Device 'IP address' ($ips -join ', ')
}
$matrixExe = Try-Get { ($matrixProc | Where-Object { $_.Path } | Select-Object -First 1).Path }
if ($matrixExe) { Add-Device 'Matrix software' (Try-Get { (Get-Item $matrixExe).VersionInfo.ProductVersion }) }
if ($os) { Add-Device 'Operating system' ("{0} (build {1})" -f $os.Caption, $os.BuildNumber) }
if ($cpu.Count) { Add-Device 'CPU' ("{0}, {1} threads" -f $cpu[0].Name.Trim(), $cpu[0].NumberOfLogicalProcessors) }
if ($totalMB) { Add-Device 'Memory' ("{0:N1} GB" -f ($totalMB / 1024)) }
$sysPd = @($pdisks | Where-Object { [string]$_.DeviceId -eq [string]$sysDisk }) | Select-Object -First 1
if ($sysPd) { Add-Device 'Disk' ("{0} ({1})" -f $sysPd.FriendlyName, (Format-Size $sysPd.Size)) }
if ($upText) { Add-Device 'Running for' $upText }
$rank = @{ ok = 0; info = 0; warn = 1; bad = 2 }
$issues = New-Object System.Collections.ArrayList
$worstAll = 0
foreach ($s in $script:Sections) {
    $w = 0
    $health = $s.group -eq 'health'
    foreach ($it in $s.items) {
        $r = $rank[$it.state]
        if ($r -gt $w) { $w = $r }
        if ($health -and ($it.state -eq 'bad' -or $it.state -eq 'warn')) { [void]$issues.Add(("{0}: {1} - {2}" -f $s.title, $it.label, $it.value)) }
    }
    $s.state = @('ok', 'warn', 'bad')[$w]
    if ($health -and $w -gt $worstAll) { $worstAll = $w }
}
# "What to do": each distinct next step once, problems first (same as health_check.report_actions).
$actions = New-Object System.Collections.ArrayList
$byText = @{}
foreach ($level in @('bad', 'warn')) {
    foreach ($s in @($script:Sections | Where-Object { $_.group -eq 'health' })) {
        foreach ($it in $s.items) {
            if ($it.state -eq $level -and $it.action) {
                if (-not $byText.ContainsKey($it.action)) {
                    $a = [ordered]@{ text = $it.action; state = $level; because = (New-Object System.Collections.ArrayList) }
                    $byText[$it.action] = $a
                    [void]$actions.Add($a)
                }
                [void]$byText[$it.action].because.Add($it.label)
            }
        }
    }
}
$report = [ordered]@{
    state      = @('healthy', 'warning', 'error')[$worstAll]
    label      = $env:COMPUTERNAME
    target     = [ordered]@{ host = $env:COMPUTERNAME; port = 0 }
    platform   = 'windows'
    checked_at = $started.ToString('yyyy-MM-ddTHH:mm:ss')
    sudo       = $isAdmin
    duration_s = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    days       = $Days
    device     = $device
    scores     = $scores
    glance     = $glance
    issues     = $issues
    actions    = $actions
    notes      = $script:Notes
    sections   = $script:Sections
}
$jsonText = $report | ConvertTo-Json -Depth 8

if ($Json) {
    Write-Output $jsonText
    return
}

$healthItems = @($script:Sections | Where-Object { $_.group -eq 'health' } | ForEach-Object { $_.items })
$nBad = @($healthItems | Where-Object { $_.state -eq 'bad' }).Count
$nWarn = @($healthItems | Where-Object { $_.state -eq 'warn' }).Count
$verdict = switch ($report.state) {
    'healthy' { 'HEALTHY - no hardware problems found' }
    'warning' { "NEEDS ATTENTION - $nWarn warning(s), no serious problems" }
    default { "PROBLEMS FOUND - $nBad problem(s), $nWarn warning(s)" }
}
$tag = @{ ok = '[ OK ]'; warn = '[WARN]'; bad = '[FAIL]'; info = '[INFO]' }
$lines = New-Object System.Collections.ArrayList
[void]$lines.Add(('=' * 70))
[void]$lines.Add("Matrix device health check: $env:COMPUTERNAME")
[void]$lines.Add("RESULT: $verdict")
[void]$lines.Add(('=' * 70))
foreach ($dv in $device) { [void]$lines.Add(("{0,-17} {1}" -f ($dv.label + ':'), $dv.value)) }
[void]$lines.Add(('-' * 70))
foreach ($key in @('disk', 'memory')) {
    $sc = $scores[$key]
    $extra = @($sc.facts) -join ', '
    [void]$lines.Add(("{0,-14} {1,3}/100  {2,-8} {3}" -f ($sc.title.ToUpper() + ':'), $sc.score, $sc.grade, $extra))
    foreach ($st in $sc.stats) { [void]$lines.Add(("                 {0,-30} {1}" -f ($st.label + ':'), $st.value)) }
    foreach ($r in $sc.reasons) { [void]$lines.Add("                 - $r") }
    foreach ($m in $sc.missing) { [void]$lines.Add("                 (not checked: $m)") }
}
[void]$lines.Add('')
[void]$lines.Add('AT A GLANCE:')
foreach ($g in $glance) { [void]$lines.Add(("  {0} {1}: {2}" -f $tag[$g.state], $g.label, $g.value)) }
if ($actions.Count) {
    [void]$lines.Add('')
    [void]$lines.Add('WHAT TO DO (most important first):')
    $i = 0
    foreach ($a in $actions) {
        $i++
        [void]$lines.Add(("  {0}. {1}" -f $i, $a.text))
        [void]$lines.Add(("     because: {0}" -f (@($a.because) -join '; ')))
    }
}
foreach ($n in $script:Notes) { [void]$lines.Add(''); [void]$lines.Add("NOTE: $n") }
[void]$lines.Add('')
[void]$lines.Add(("All checks ({0:yyyy-MM-dd HH:mm}, last {1} days of history, {2}, {3}s):" -f $started, $Days, $(if ($isAdmin) { 'as Administrator' } else { 'NOT as Administrator' }), $report.duration_s))
foreach ($grp in @('health', 'other')) {
    if ($grp -eq 'other') {
        [void]$lines.Add('')
        [void]$lines.Add('OTHER DETAILS (software and system info, not part of the health result):')
    }
    foreach ($s in @($script:Sections | Where-Object { $_.group -eq $grp })) {
        [void]$lines.Add('')
        [void]$lines.Add("--- $($s.title) ---")
        foreach ($it in $s.items) { [void]$lines.Add(("  {0} {1}: {2}" -f $tag[$it.state], $it.label, $it.value)) }
    }
}

# Save next to the script (easy to pull back with BeyondTrust File Transfer).
$candidates = @()
if ($OutDir) { $candidates += $OutDir }
if ($PSScriptRoot) { $candidates += $PSScriptRoot }
$candidates += (Join-Path $env:PUBLIC 'Documents\MatrixHealthCheck')
$candidates += (Join-Path $env:TEMP 'MatrixHealthCheck')
$saveDir = $null
foreach ($dir in $candidates) {
    try {
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null }
        $probe = Join-Path $dir ('.write-test-{0}' -f [guid]::NewGuid())
        Set-Content -Path $probe -Value 'x' -ErrorAction Stop
        Remove-Item $probe -Force
        $saveDir = $dir
        break
    } catch { }
}

function Encode-Html([string]$s) { [System.Net.WebUtility]::HtmlEncode($s) }
$pill = @{ ok = '<span class="p good">OK</span>'; warn = '<span class="p warn">Warning</span>'; bad = '<span class="p bad">Problem</span>'; info = '' }
$secHtml = foreach ($s in @($script:Sections | Where-Object { $_.group -eq 'health' }) + @('OTHER') + @($script:Sections | Where-Object { $_.group -eq 'other' })) {
    if ($s -is [string]) { '<details class="other"><summary>Other details (software and system info, not part of the health result)</summary>'; continue }
    $rows = foreach ($it in $s.items) {
        $d = if ($it.detail) { "<details><summary>details</summary><pre>$(Encode-Html $it.detail)</pre></details>" } else { '' }
        $todo = if ($it.action) { "<div class=`"todo`">What to do: $(Encode-Html $it.action)</div>" } else { '' }
        "<tr class=`"$($it.state)`"><td>$(Encode-Html $it.label)</td><td>$(Encode-Html $it.value)$todo$d</td><td>$($pill[$it.state])</td></tr>"
    }
    "<section class=`"card $($s.state)`"><h2>$(Encode-Html $s.title) $($pill[$s.state])</h2><table>$($rows -join '')</table></section>"
}
$todoHtml = if ($actions.Count) {
    '<h2>What to do</h2><ol>' + (($actions | ForEach-Object {
        "<li class=`"$($_.state)`"><b>$(Encode-Html $_.text)</b><div class=`"because`">Because: $(Encode-Html (@($_.because) -join '; '))</div></li>"
    }) -join '') + '</ol>'
} else { '<p>Nothing to do. If the device still misbehaves, the cause is probably not its hardware.</p>' }
$noteHtml = ($script:Notes | ForEach-Object { "<p class=`"note`">$(Encode-Html $_)</p>" }) -join ''
$scoreHtml = ($scores.Values | ForEach-Object {
    $list = if ($_.reasons.Count) { '<ul>' + ((@($_.reasons) | ForEach-Object { "<li>$(Encode-Html $_)</li>" }) -join '') + '</ul>' } else { '<p class="none">No problems found.</p>' }
    $miss = if ($_.missing.Count) { "<p class=`"miss`">Not checked: $(Encode-Html (@($_.missing) -join '; '))</p>" } else { '' }
    $statHtml = if ($_.stats.Count) { '<div class="stats">' + ((@($_.stats) | ForEach-Object {
        $h = if ($_.hint) { "<em>$(Encode-Html $_.hint)</em>" } else { '' }
        "<div class=`"stat $($_.state)`"><span>$(Encode-Html $_.label)</span><b>$(Encode-Html $_.value)</b>$h</div>" }) -join '') + '</div>' } else { '' }
    "<div class=`"score $($_.state)`"><div class=`"st`">$(Encode-Html $_.title)</div><div class=`"sv`">$($_.score)<small>/100</small> <span>$(Encode-Html $_.grade)</span></div>" +
    "<div class=`"bar`"><i style=`"width:$($_.score)%`"></i></div><div class=`"sf`">$((@($_.facts) | ForEach-Object { Encode-Html $_ }) -join ' &middot; ')</div>$statHtml$list$miss</div>"
}) -join ''
$deviceHtml = ($device | ForEach-Object { "<div class=`"dv`"><span>$(Encode-Html $_.label)</span>$(Encode-Html $_.value)</div>" }) -join ''
$glanceHtml = ($glance | ForEach-Object { "<div class=`"g $($_.state)`"><span>$(Encode-Html $_.label)</span><b>$(Encode-Html $_.value)</b></div>" }) -join ''
$html = @"
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Health check - $(Encode-Html $env:COMPUTERNAME)</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;margin:24px;background:#f5f6f8;color:#1d2330}
h1{font-size:20px;margin:0 0 4px}.sub{color:#3b4856;font-size:13px;margin-bottom:16px}
.card{background:#fff;border:1px solid #dde1e7;border-top:3px solid #9aa3b0;border-radius:8px;padding:12px 16px;margin-bottom:14px}
.card.ok{border-top-color:#10a37f}.card.warn{border-top-color:#d98e04}.card.bad{border-top-color:#d93a3a}
h2{font-size:15px;margin:0 0 8px}table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:6px 8px;border-top:1px solid #eef0f3;vertical-align:top}td:first-child{width:32%;font-weight:600}td:last-child{width:90px;text-align:right}
tr.bad td:nth-child(2){color:#c02626}tr.warn td:nth-child(2){color:#a86b00}
.p{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px}.good{background:#dff5ee;color:#0b7a5e}.warn .p.warn,.p.warn{background:#fdf1d8;color:#a86b00}.p.bad{background:#fde2e2;color:#c02626}
.summary.healthy{border-top-color:#10a37f}.summary.warning{border-top-color:#d98e04}.summary.error{border-top-color:#d93a3a}
pre{white-space:pre-wrap;font-size:12px;background:#f5f6f8;padding:8px;border-radius:6px}summary{cursor:pointer;color:#3b4856;font-size:12px}
.note{color:#a86b00;font-size:13px}
.verdict{font-size:22px;font-weight:700;margin:4px 0 10px}.summary.healthy .verdict{color:#0b7a5e}.summary.warning .verdict{color:#a86b00}.summary.error .verdict{color:#c02626}
ol{margin:0;padding-left:22px;font-size:14px}ol li{margin-bottom:8px}ol li.bad b{color:#c02626}ol li.warn b{color:#a86b00}.because{color:#3b4856;font-size:12px}
.todo{font-size:12px;color:#1d2330;margin-top:3px}h3{font-size:13px;color:#3b4856;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.4px}
.device{background:#fff;border:1px solid #dde1e7;border-radius:8px;padding:10px 16px;margin-bottom:14px;font-size:13px;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:4px 20px}
.device .st{grid-column:1/-1;margin-bottom:2px}.dv span{display:block;color:#3b4856;font-size:11px}
.scores{display:grid;grid-template-columns:1fr;gap:10px;margin-bottom:14px}details.other>summary{font-size:14px;font-weight:600;color:#1d2330;margin:18px 0 10px}.score{background:#fff;border:1px solid #dde1e7;border-radius:8px;padding:14px 16px}
.st{font-size:12px;text-transform:uppercase;letter-spacing:.4px;color:#3b4856;font-weight:700}.sv{font-size:34px;font-weight:800}.sv small{font-size:15px;color:#3b4856;font-weight:600}.sv span{font-size:16px;margin-left:6px}
.score.ok .sv{color:#0b7a5e}.score.warn .sv{color:#a86b00}.score.bad .sv{color:#c02626}.bar{height:8px;background:#eef0f3;border-radius:99px;margin:6px 0 8px;overflow:hidden}.bar i{display:block;height:100%}
.score.ok .bar i{background:#10a37f}.score.warn .bar i{background:#d98e04}.score.bad .bar i{background:#d93a3a}.sf{font-size:12px;color:#3b4856}.score ul{margin:6px 0 0;padding-left:18px;font-size:13px}.none{font-size:13px;color:#0b7a5e;margin:6px 0 0}.miss{font-size:12px;color:#a86b00;margin:6px 0 0}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px;margin:10px 0 4px}.stat{border:1px solid #dde1e7;border-radius:6px;padding:6px 10px}
.stat span{display:block;font-size:11.5px;font-weight:600;color:#1d2330}.stat b{display:block;font-size:17px;color:#1d2330}.stat em{display:block;font-style:normal;font-size:11.5px;color:#1d2330}
.stat.ok b{color:#0b7a5e}.stat.warn{border-color:#d98e04}.stat.warn b{color:#a86b00}.stat.bad{border-color:#d93a3a}.stat.bad b{color:#c02626}.sf{color:#1d2330;font-weight:600}
.glance{background:#fff;border:1px solid #dde1e7;border-radius:8px;padding:8px 16px;margin-bottom:14px;font-size:13px}.g{padding:3px 0}.g span{color:#3b4856;display:inline-block;min-width:170px}
.g::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:#9aa3b0;margin-right:8px}.g.ok::before{background:#10a37f}.g.warn::before{background:#d98e04}.g.bad::before{background:#d93a3a}
</style></head><body>
<h1>Matrix device health check: $(Encode-Html $env:COMPUTERNAME)</h1>
<div class="sub">Checked $($started.ToString('yyyy-MM-dd HH:mm')) &middot; last $Days days of history &middot; $(if ($isAdmin) { 'as Administrator' } else { 'not as Administrator' }) &middot; read-only, nothing was changed</div>
<div class="device"><div class="st">Device</div>$deviceHtml</div>
<div class="scores">$scoreHtml</div>
<div class="glance">$glanceHtml</div>
<section class="card summary $($report.state)"><div class="verdict">$(Encode-Html $verdict)</div>$todoHtml$noteHtml</section>
<h3>All checks</h3>
$($secHtml -join "`n")
</details>
</body></html>
"@

$lines | ForEach-Object { Write-Output $_ }
if ($saveDir) {
    $stamp = $started.ToString('yyyyMMdd-HHmm')
    $base = Join-Path $saveDir ("health-{0}-{1}" -f $env:COMPUTERNAME, $stamp)
    [IO.File]::WriteAllText("$base.html", $html, (New-Object System.Text.UTF8Encoding $false))
    [IO.File]::WriteAllText("$base.json", $jsonText, (New-Object System.Text.UTF8Encoding $false))
    Write-Output ''
    Write-Output 'Reports saved (copy them back with BeyondTrust File Transfer and attach them to the ticket):'
    Write-Output "  $base.html   <- open in any browser"
    Write-Output "  $base.json   <- Matrix Facility Manager > Health Check > Open report file"
    if ($Open) { Start-Process "$base.html" }
} else {
    Write-Output ''
    Write-Output 'Could not save the report files (no writable folder). Use -OutDir.'
}
if ($report.state -eq 'error') { exit 2 } elseif ($report.state -eq 'warning') { exit 1 } else { exit 0 }
