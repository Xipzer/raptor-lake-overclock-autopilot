# Elevated helper for the campaign scripts (scheduled task, see install-tasks.ps1). The scripts in WSL write one
# command to cmd.txt in -Dir; the answer goes to ack.txt. Only the fixed commands below are accepted, with checked
# arguments. Exits on quit or after 10 hours.
#   dimm, dimmlog N, dimmdump, dimmid   DIMM temperatures through dimmtemp.exe (tools/dimmtemp.c) in -Dir
#   hwinfo, hwlog NAME, hwstop, enterproc NAME, shotproc NAME   start/stop HWiNFO (it initialises the SPD hub sensors)
#   chkdsk C, sfc, dism, fw, esp        read-only health checks after a crash
#   dumpoff, dumpsmall                  crash dump mode
#   fsinfo, fsrun                       the startup failsafe task
#   tm5 [MASK], tm5aff MASK, tm5stop    TestMem5 (needs admin), optionally confined to a logical CPU mask (hex)
#   minus N, plus N, apply, shot NAME   ASRock A-Tuning: CPU Core/Cache Voltage -/+ buttons and Apply, by window
#                                       position (measured on one A-Tuning version: check yours before use)
param(
  [string]$Dir = $PSScriptRoot,
  [string]$Tm5Dir = 'C:\Tools\memtest\TM5',
  [string]$HwInfo = 'C:\Program Files\HWiNFO64\HWiNFO64.EXE',
  [string]$FailsafeTask = 'RaptorLakeAutopilot Failsafe'
)
$dir = $Dir
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class W {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
  public static IntPtr Biggest(uint want) {
    IntPtr best = IntPtr.Zero; long area = 0;
    EnumWindows((h, l) => { uint pid; GetWindowThreadProcessId(h, out pid); if (pid != want) return true;
      RECT r; GetWindowRect(h, out r); long a = (long)(r.R - r.L) * (r.B - r.T); if (a > area) { area = a; best = h; } return true; }, IntPtr.Zero);
    return best;
  }
  // A-Tuning's main form: the largest top-level TMainForm window of that process.
  public static IntPtr Find(uint want) {
    IntPtr best = IntPtr.Zero; long area = 0;
    EnumWindows((h, l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (pid != want) return true;
      var sb = new System.Text.StringBuilder(64); GetClassName(h, sb, 64);
      if (sb.ToString() != "TMainForm") return true;
      RECT r; GetWindowRect(h, out r); long a = (long)(r.R - r.L) * (r.B - r.T);
      if (IsIconic(h)) a = 1;
      if (a > area) { area = a; best = h; }
      return true;
    }, IntPtr.Zero);
    return best;
  }
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref POINT p);
  [DllImport("user32.dll")] public static extern IntPtr ChildWindowFromPointEx(IntPtr h, POINT p, uint f);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  // Deepest child under a point given relative to the main window's top-left.
  public static IntPtr Deep(IntPtr main, int x, int y, out int cx, out int cy) {
    RECT r; GetWindowRect(main, out r);
    IntPtr h = main;
    for (int i = 0; i < 20; i++) {
      POINT p = new POINT { X = r.L + x, Y = r.T + y }; ScreenToClient(h, ref p);
      IntPtr c = ChildWindowFromPointEx(h, p, 1 | 4);
      if (c == IntPtr.Zero || c == h) break;
      h = c;
    }
    POINT q = new POINT { X = r.L + x, Y = r.T + y }; ScreenToClient(h, ref q);
    cx = q.X; cy = q.Y; return h;
  }
  public static void Click(IntPtr main, int x, int y) {
    int cx, cy; IntPtr h = Deep(main, x, y, out cx, out cy);
    IntPtr lp = (IntPtr)((cy << 16) | (cx & 0xFFFF));
    PostMessage(h, 0x0200, IntPtr.Zero, lp);
    PostMessage(h, 0x0201, (IntPtr)1, lp);
    PostMessage(h, 0x0202, IntPtr.Zero, lp);
  }
}
"@
# Delphi's process main window is a hidden application window; the real one has the class TMainForm.
# The buttons are found by position, so a minimised window is restored first (without taking the focus).
function Main { $p = Get-Process ATuning -ErrorAction SilentlyContinue | Select -First 1; if (-not $p) { return [IntPtr]::Zero }; $h = [W]::Find([uint32]$p.Id); if ($h -ne [IntPtr]::Zero -and [W]::IsIconic($h)) { [W]::ShowWindow($h, 4) | Out-Null; Start-Sleep -Milliseconds 600 }; $h }
function Shot($name) {
  $h = Main; $r = New-Object W+RECT; [W]::GetWindowRect($h, [ref]$r) | Out-Null
  $b = New-Object Drawing.Bitmap ($r.R - $r.L), ($r.B - $r.T); $g = [Drawing.Graphics]::FromImage($b)
  $dc = $g.GetHdc(); [W]::PrintWindow($h, $dc, 2) | Out-Null; $g.ReleaseHdc($dc)
  $b.Save("$dir\$name.png", [Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $b.Dispose()
}
$end = (Get-Date).AddHours(10)
'ready ' + (Get-Date -f HH:mm:ss) | Out-File "$dir\ack.txt"
while ((Get-Date) -lt $end) {
  if (Test-Path "$dir\cmd.txt") {
    $c = (Get-Content "$dir\cmd.txt" -Raw).Trim(); Remove-Item "$dir\cmd.txt"
    $a = $c -split '\s+'; $msg = 'ok ' + $c
    switch ($a[0]) {
      'minus' { $n = [math]::Min([int]$a[1], 10); for ($i = 0; $i -lt $n; $i++) { [W]::Click((Main), 367, 422); Start-Sleep -Milliseconds 250 } }
      'plus'  { $n = [math]::Min([int]$a[1], 10); for ($i = 0; $i -lt $n; $i++) { [W]::Click((Main), 628, 422); Start-Sleep -Milliseconds 250 } }
      'apply' { [W]::Click((Main), 843, 498); Start-Sleep 3 }
      'shot'  { if ($a[1] -match '^[\w-]+$') { try { Shot $a[1] } catch { $msg = 'shot failed: ' + $_.Exception.Message } } else { $msg = 'bad name' } }
      'dimm'  { $msg = 'ok dimm ' + ((& "$dir\dimmtemp.exe" 2>&1) -join ' ') + ' exit ' + $LASTEXITCODE }
      'dimmdump' { (& "$dir\dimmtemp.exe" dump 2>&1) | Out-File "$dir\dimmdump.txt" }
      'hwlog' { Get-Process HWiNFO64 -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 1; if ($a[1] -match '^[\w-]+$') { Start-Process -FilePath $HwInfo -ArgumentList ('-l' + $dir + '\' + $a[1] + '.csv') -WindowStyle Minimized } }
      'hwinfo' { Get-Process HWiNFO64 -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 1; Start-Process -FilePath $HwInfo }
      'shotproc' { $pp = Get-Process $a[1] -ErrorAction SilentlyContinue | Select -First 1
                   $h = if ($pp) { [W]::Biggest([uint32]$pp.Id) } else { [IntPtr]::Zero }
                   if ($h -ne [IntPtr]::Zero) { [W]::ShowWindow($h, 4) | Out-Null; Start-Sleep -Milliseconds 500; $r = New-Object W+RECT; [W]::GetWindowRect($h, [ref]$r) | Out-Null
                     $b = New-Object Drawing.Bitmap ($r.R - $r.L), ($r.B - $r.T); $g = [Drawing.Graphics]::FromImage($b); $dc = $g.GetHdc(); [W]::PrintWindow($h, $dc, 2) | Out-Null; $g.ReleaseHdc($dc)
                     $b.Save("$dir\proc.png", [Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $b.Dispose() } else { $msg = 'no window' } }
      'enterproc' { $pp = Get-Process $a[1] -ErrorAction SilentlyContinue | ? { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select -First 1
                    if ($pp) { [W]::PostMessage($pp.MainWindowHandle, 0x0100, [IntPtr]0x0D, [IntPtr]0) | Out-Null; [W]::PostMessage($pp.MainWindowHandle, 0x0101, [IntPtr]0x0D, [IntPtr]0) | Out-Null } else { $msg = 'no window' } }
      'hwstop' { Get-Process HWiNFO64 -ErrorAction SilentlyContinue | Stop-Process -Force }
      'dimmid' { (& "$dir\dimmtemp.exe" id 2>&1) | Out-File "$dir\dimmdump.txt" }
      'chkdsk' { if ($a[1] -match '^[CD]$') { Start-Process -WindowStyle Hidden cmd.exe -ArgumentList ('/c chkdsk ' + $a[1] + ': /scan > "' + $dir + '\chkdsk-' + $a[1] + '.txt" 2>&1') } }
      'sfc' { Start-Process -WindowStyle Hidden cmd.exe -ArgumentList ('/c sfc /scannow > "' + $dir + '\sfc.txt" 2>&1') }
      'dumpoff' { Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl -Name CrashDumpEnabled -Value 0 }
      'dumpsmall' { Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl -Name CrashDumpEnabled -Value 3 }
      'fsinfo' { (schtasks /Query /TN $FailsafeTask /V /FO LIST 2>&1) | Out-File "$dir\fsinfo.txt"; (Get-ScheduledTaskInfo $FailsafeTask | fl | Out-String) | Out-File "$dir\fsinfo.txt" -Append }
      'fsrun' { Start-ScheduledTask $FailsafeTask }
      'dimmlog' { $n = [math]::Min([int]$a[1], 7200); Start-Process -WindowStyle Hidden -FilePath "$dir\dimmtemp.exe" -ArgumentList $n, "$dir\dimmlog.csv" }
      'dism' { Start-Process -WindowStyle Hidden cmd.exe -ArgumentList ('/c DISM /Online /Cleanup-Image /ScanHealth > "' + $dir + '\dism.txt" 2>&1') }
      'fw' { (bcdedit /enum firmware 2>&1) | Out-File "$dir\fw.txt" }
      'esp' { $L = @('Y','W','V','U') | ? { -not (Test-Path ($_ + ':\')) } | Select -First 1; $o = @()
              if ($L) { mountvol ($L + ':') /S | Out-Null
                try { $o += Get-ChildItem ($L + ':\EFI') -Directory -ErrorAction SilentlyContinue | % { 'dir EFI\' + $_.Name }
                      $o += Get-ChildItem ($L + ':\EFI') -Recurse -File -Filter *.efi -ErrorAction SilentlyContinue | % { $s = Get-AuthenticodeSignature $_.FullName; 'efi ' + $_.FullName.Substring(3) + ' | ' + $_.Length + ' | ' + $_.LastWriteTime.ToString('yyyy-MM-dd') + ' | ' + $s.Status + ' | ' + $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject.Split(',')[0] } else { 'unsigned' }) }
                      $o += 'hash ESP bootmgfw ' + (Get-FileHash ($L + ':\EFI\Microsoft\Boot\bootmgfw.efi') -Algorithm SHA256).Hash
                      $o += 'hash C:  bootmgfw ' + (Get-FileHash 'C:\Windows\Boot\EFI\bootmgfw.efi' -Algorithm SHA256).Hash
                } finally { mountvol ($L + ':') /D | Out-Null } } else { $o += 'no free drive letter' }
              $o | Out-File "$dir\esp.txt" }
      'tm5' { Get-Process TM5 -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 1; $tp = Start-Process -FilePath (Join-Path $Tm5Dir 'TM5.exe') -WorkingDirectory $Tm5Dir -PassThru
              if ($a.Count -gt 1) { $set = $false; foreach ($i in 1..20) { try { $tp.ProcessorAffinity = [IntPtr][Convert]::ToInt64($a[1], 16); $set = $true; break } catch { Start-Sleep -Milliseconds 100 } }; if (-not $set) { $msg = 'affinity failed' } } }
      'tm5aff' { $n = 0; Get-Process TM5 -ErrorAction SilentlyContinue | % { try { $_.ProcessorAffinity = [IntPtr][Convert]::ToInt64($a[1], 16); $n++ } catch { } }; if ($n -eq 0) { $msg = 'no process' } }
      'tm5stop' { Get-Process TM5 -ErrorAction SilentlyContinue | Stop-Process -Force }
      'quit'  { 'quit' | Out-File "$dir\ack.txt"; exit }
      default { $msg = 'unknown ' + $c }
    }
    Start-Sleep -Milliseconds 500
    $msg + ' ' + (Get-Date -f HH:mm:ss) | Out-File "$dir\ack.txt"
  }
  Start-Sleep -Milliseconds 500
}
