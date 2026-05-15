# Capture Rocket League Stats API events to a .bin file (raw bytes) and
# print a summary at the end. Pure PowerShell, no installs needed.
#
# Usage:
#   1. Make sure Rocket League is FULLY CLOSED.
#   2. Launch Rocket League and load to the main menu.
#   3. In a PowerShell window in this folder, run:
#        .\capture.ps1
#      (If you get a script-execution error, run instead:
#        powershell -ExecutionPolicy Bypass -File .\capture.ps1 )
#   4. Play 1-2 matches.
#   5. Press Ctrl+C to stop. Send the .bin file from the captures\ folder.

$ErrorActionPreference = 'Stop'

$Host_      = '127.0.0.1'
$Port       = 49123
$OutDir     = Join-Path $PSScriptRoot 'captures'
$Stamp      = Get-Date -Format 'yyyyMMdd_HHmmss'
$RawPath    = Join-Path $OutDir ("rl_{0}.bin"   -f $Stamp)
$JsonlPath  = Join-Path $OutDir ("rl_{0}.jsonl" -f $Stamp)

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

Write-Host "connecting to ${Host_}:${Port} ..."

$client = $null
for ($i = 1; $i -le 30; $i++) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect($Host_, $Port)
        break
    } catch {
        Write-Host ("  [{0}/30] waiting for RL socket..." -f $i)
        Start-Sleep -Seconds 1
        $client = $null
    }
}

if ($null -eq $client -or -not $client.Connected) {
    Write-Host "could not connect. Make sure Rocket League is running and PacketSendRate > 0 in DefaultStatsAPI.ini."
    exit 1
}

Write-Host "connected."
Write-Host "  raw   -> $RawPath"
Write-Host "  jsonl -> $JsonlPath"
Write-Host "press Ctrl+C to stop."
Write-Host ""

$stream  = $client.GetStream()
$buffer  = New-Object byte[] 65536
$rawFs   = [System.IO.File]::Open($RawPath,   [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
$jsonlFs = [System.IO.File]::Open($JsonlPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$jsonlW  = New-Object System.IO.StreamWriter($jsonlFs, $utf8NoBom)

$textBuf     = ''
$eventCount  = 0
$typeCounts  = @{}

try {
    while ($true) {
        $n = $stream.Read($buffer, 0, $buffer.Length)
        if ($n -le 0) { Write-Host ""; Write-Host "socket closed by RL."; break }

        $rawFs.Write($buffer, 0, $n)
        $rawFs.Flush()

        $textBuf += [System.Text.Encoding]::UTF8.GetString($buffer, 0, $n)

        # Pull off as many complete JSON objects as we can. Simple brace-depth
        # scanner that respects strings + escapes. RL emits concatenated JSON
        # objects with no delimiter, so we have to find the boundaries.
        while ($true) {
            $start = -1
            for ($k = 0; $k -lt $textBuf.Length; $k++) {
                $c = $textBuf[$k]
                if ($c -eq ' ' -or $c -eq "`r" -or $c -eq "`n" -or $c -eq "`t") { continue }
                $start = $k
                break
            }
            if ($start -lt 0) { $textBuf = ''; break }
            if ($textBuf[$start] -ne '{') {
                # garbage we can't parse - skip one char so we don't loop forever
                $textBuf = $textBuf.Substring($start + 1)
                continue
            }

            $depth = 0
            $inStr = $false
            $esc   = $false
            $end   = -1
            for ($k = $start; $k -lt $textBuf.Length; $k++) {
                $c = $textBuf[$k]
                if ($inStr) {
                    if ($esc)            { $esc = $false }
                    elseif ($c -eq '\')  { $esc = $true }
                    elseif ($c -eq '"')  { $inStr = $false }
                } else {
                    if     ($c -eq '"') { $inStr = $true }
                    elseif ($c -eq '{') { $depth++ }
                    elseif ($c -eq '}') { $depth--; if ($depth -eq 0) { $end = $k; break } }
                }
            }
            if ($end -lt 0) { break }  # incomplete - wait for more bytes

            $objStr = $textBuf.Substring($start, $end - $start + 1)
            $textBuf = $textBuf.Substring($end + 1)

            $jsonlW.WriteLine($objStr)
            $jsonlW.Flush()

            $eventCount++

            $name = '?'
            try {
                $obj = $objStr | ConvertFrom-Json -ErrorAction Stop
                if     ($obj.event) { $name = [string]$obj.event }
                elseif ($obj.Event) { $name = [string]$obj.Event }
                elseif ($obj.name)  { $name = [string]$obj.name }
                else {
                    $k0 = ($obj.PSObject.Properties | Select-Object -First 1).Name
                    if ($k0) { $name = $k0 }
                }
            } catch {}

            if ($typeCounts.ContainsKey($name)) { $typeCounts[$name]++ } else { $typeCounts[$name] = 1 }

            if ($name -ne 'UpdateState' -or ($typeCounts[$name] % 30) -eq 1) {
                Write-Host ("  #{0,-6} {1}" -f $eventCount, $name)
            }
        }
    }
} catch [System.Management.Automation.PipelineStoppedException] {
    Write-Host ""; Write-Host "stopped."
} catch {
    Write-Host ""; Write-Host ("error: {0}" -f $_.Exception.Message)
} finally {
    if ($jsonlW)  { $jsonlW.Close()  }
    if ($jsonlFs) { $jsonlFs.Close() }
    if ($rawFs)   { $rawFs.Close()   }
    if ($stream)  { $stream.Close()  }
    if ($client)  { $client.Close()  }
}

Write-Host ""
Write-Host ("captured {0} events." -f $eventCount)
if ($typeCounts.Count -gt 0) {
    Write-Host "event type counts:"
    $typeCounts.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object {
        Write-Host ("  {0,6}  {1}" -f $_.Value, $_.Key)
    }
}
Write-Host ""
Write-Host "files written:"
Write-Host "  $RawPath"
Write-Host "  $JsonlPath"
