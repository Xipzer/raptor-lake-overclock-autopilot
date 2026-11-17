# Runs SCEWIN elevated on request, for the campaign scripts (scheduled task, started on demand; see install-tasks.ps1).
# Reads <Stage>\request.txt:
#   export   -> <Stage>\export.txt
#   import   -> imports <Stage>\import.txt with the BIOS admin password, then exports <Stage>\after.txt
# Everything SCEWIN prints goes to <Stage>\result.txt, ending with a "done" line. Only this script and SCEWIN run
# elevated. The password is read from -PasswordFile (readable by administrators only), never stored in a script.
param(
  [Parameter(Mandatory = $true)][string]$Scewin,         # full path to SCEWIN_64.exe
  [Parameter(Mandatory = $true)][string]$Stage,          # exchange folder, writable by your user
  [string]$PasswordFile = ''                             # BIOS admin password file (ASRock needs one for imports)
)
Set-Location (Split-Path $Scewin)
$req = (Get-Content "$Stage\request.txt" -Raw -ErrorAction SilentlyContinue)
if ($req) { $req = $req.Trim() }
Remove-Item "$Stage\request.txt" -ErrorAction SilentlyContinue
$pw = ''
if ($PasswordFile -and (Test-Path $PasswordFile)) { $pw = (Get-Content $PasswordFile -Raw).Trim() }
$pwArgs = @(); if ($pw) { $pwArgs = @('/cpwd', $pw) }
$out = @("request: $req  at $(Get-Date -f 'yyyy-MM-dd HH:mm:ss')")
switch ($req) {
  'export' { $out += (& $Scewin /o /s "$Stage\export.txt" 2>&1) }
  'import' { $out += (& $Scewin /i /s "$Stage\import.txt" @pwArgs 2>&1); $out += "exit $LASTEXITCODE"
             $out += (& $Scewin /o /s "$Stage\after.txt" 2>&1) }
  default  { $out += 'unknown request' }
}
$out += "done $(Get-Date -f 'HH:mm:ss')"
$text = ($out | Out-String)
if ($pw) { $text = $text.Replace($pw, '<password>') }
$text | Out-File "$Stage\result.txt" -Encoding utf8
