# Startup failsafe (v2). Installed by `tuner.py install failsafe --yes` (or tools/windows/install-failsafe.ps1 for
# the campaign scripts) as a SYSTEM task that runs at every startup. It never decides anything itself; it follows
# files the sweep leaves in -Dir:
#   armed                the sweep wrote a step and rebooted into it (content: the step id)
#   seen                 "<step>|<starts>": how many times Windows started during that step
#   lastgood-values.txt  TOKEN=number per line: the last good values (preferred restore method)
#   lastgood.txt         a prepared SCEWIN script with the last good settings (fallback)
#   failsafe.json        {"scewin": path, "password_file": path or ""}
# First start of a step: count it. A second start of the same step while "armed" still exists (the sweep deletes it
# when the test starts) means that boot failed: restore the last good values, leave "restored" for the sweep, and
# restart once.
# v2: SCEWIN matches questions by their prompt, which contains the live value, so a pre-built script does nothing
# when memory training has fallen back to safe timings. So a fresh export is taken and the numbers are put into it
# by token. Marker and log writes are flushed to disk, and an empty marker still counts (a crash can lose data).
param([Parameter(Mandatory = $true)][string]$Dir)

$log = Join-Path $Dir 'failsafe.log'
function Durable($path, $text, $append) {
  $mode = if ($append) { [IO.FileMode]::Append } else { [IO.FileMode]::Create }
  $f = New-Object IO.FileStream($path, $mode, [IO.FileAccess]::Write, [IO.FileShare]::Read)
  try { $b = [Text.Encoding]::UTF8.GetBytes($text); $f.Write($b, 0, $b.Length); $f.Flush($true) } finally { $f.Dispose() }
}
function Log($m) { Durable $log ("$(Get-Date -f 'yyyy-MM-dd HH:mm:ss') $m`r`n") $true }
function ReadText($p) { try { $t = Get-Content $p -Raw -ErrorAction Stop; if ($t) { ($t -replace "`0", '').Trim() } else { '' } } catch { '' } }

$armed = Join-Path $Dir 'armed'; $seenFile = Join-Path $Dir 'seen'
if (-not (Test-Path $armed)) { Remove-Item $seenFile -ErrorAction SilentlyContinue; exit 0 }
$step = ReadText $armed; if (-not $step) { $step = 'unnamed-step' }
$n = 0
$seen = ReadText $seenFile
if ($seen -match '^(.*)\|(\d+)$' -and $Matches[1] -eq $step) { $n = [int]$Matches[2] }
$n++
Durable $seenFile "$step|$n" $false
if ($n -lt 2) { Log "first boot of step $step"; exit 0 }

Log "step ${step}: Windows started again without the test starting (start $n); restoring the last good settings"
$cfg = Get-Content (Join-Path $Dir 'failsafe.json') -Raw | ConvertFrom-Json
$scewin = $cfg.scewin
$pw = ''
if ($cfg.password_file) { $pw = ReadText $cfg.password_file }
$pwArgs = @(); if ($pw) { $pwArgs = @('/cpwd', $pw) }
function Mask($text) { if ($pw) { $text.Replace($pw, '<password>') } else { $text } }
Set-Location (Split-Path $scewin)

$res = ''
$values = Join-Path $Dir 'lastgood-values.txt'
if (Test-Path $values) {
  $exp = Join-Path $Dir 'fs-export.txt'; $imp = Join-Path $Dir 'fs-import.txt'
  Remove-Item $exp, $imp -ErrorAction SilentlyContinue
  & $scewin /o /s $exp 2>&1 | Out-Null
  if ((Test-Path $exp) -and (Get-Item $exp).Length -gt 10000) {
    $enc = [Text.Encoding]::GetEncoding(28591)   # one byte per character, so every byte survives the round trip
    $lines = [IO.File]::ReadAllText($exp, $enc).Split([char]10)
    $set = 0; $miss = @()
    foreach ($row in [IO.File]::ReadAllLines($values)) {
      if ($row.Trim() -notmatch '^([0-9A-Fa-f]+)=(-?\d+)$') { continue }
      $tok = $Matches[1]; $val = $Matches[2]
      $hits = @(for ($i = 0; $i -lt $lines.Length; $i++) { if ($lines[$i] -match ('^Token\s*=\s*' + $tok + '\s')) { $i } })
      $done = $false
      if ($hits.Count -eq 1) {
        for ($j = $hits[0] + 1; $j -lt [math]::Min($hits[0] + 12, $lines.Length); $j++) {
          if ($lines[$j] -match '^Setup Question') { break }
          if ($lines[$j] -match '^Value\s*=\s*<-?\d+>') { $lines[$j] = ([regex]'<-?\d+>').Replace($lines[$j], "<$val>", 1); $done = $true; break }
        }
      }
      if ($done) { $set++ } else { $miss += $tok }
    }
    [IO.File]::WriteAllText($imp, ($lines -join [string][char]10), $enc)
    $out = & $scewin /i /s $imp @pwArgs 2>&1 | Out-String
    $res = "fresh export, $set values set" + $(if ($miss) { ', not found: ' + ($miss -join ',') } else { '' }) + '; scewin: ' + (Mask (($out -split "`r?`n" | Select-String 'accepted|imported|not modified|Error') -join ' | '))
  } else { $res = 'fresh export failed' }
}
if ($res -notmatch 'imported successfully|not modified') {
  $script = Join-Path $Dir 'lastgood.txt'
  if (Test-Path $script) {
    $out = & $scewin /i /s $script @pwArgs 2>&1 | Out-String
    $res += ' | prepared script: ' + (Mask (($out -split "`r?`n" | Where-Object { $_.Trim() }) -join ' | '))
  } else { $res += ' | no lastgood.txt' }
}
Log $res
Remove-Item $armed, $seenFile -ErrorAction SilentlyContinue
Durable (Join-Path $Dir 'restored') "$step $(Get-Date -f 'yyyy-MM-dd HH:mm:ss')" $false
shutdown.exe /r /t 5 /c "Raptor Lake OC Autopilot failsafe: restored the last good BIOS settings"
