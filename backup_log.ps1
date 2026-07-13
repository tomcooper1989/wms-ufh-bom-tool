# Back up the BOM usage log from the live app.
#
# Reads the dashboard's own /dashboard_data endpoint, so it works against the
# CURRENTLY DEPLOYED code - no redeploy needed (and none wanted: a redeploy
# without a mounted volume is what wipes the log in the first place).
#
#   .\backup_log.ps1 -AppUrl https://your-app.up.railway.app
#
# Saves backups\bom_usage_backup_<timestamp>.json and prints a summary so you
# can sanity-check the entry count against the dashboard before trusting it.
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads a BOM-less .ps1
# as Windows-1252, and a UTF-8 em dash decodes to a curly quote that PowerShell
# treats as a string delimiter - which silently swallows the following lines of
# code into the preceding block instead of raising a parse error.

param(
    [Parameter(Mandatory = $true)][string]$AppUrl,
    [string]$Password,
    [string]$OutDir = "backups"
)

if (-not $Password) {
    $secure = Read-Host "Dashboard password" -AsSecureString
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

$AppUrl = $AppUrl.TrimEnd('/')
$body = @{ password = $Password } | ConvertTo-Json

Write-Host "Fetching usage log from $AppUrl ..."
try {
    $res = Invoke-RestMethod -Uri "$AppUrl/dashboard_data" -Method Post -ContentType 'application/json' -Body $body -ErrorAction Stop
} catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "A 403 means the dashboard password is wrong." -ForegroundColor Yellow
    exit 1
}

if ($null -eq $res.entries) {
    Write-Host "No 'entries' field in the response. Got:" -ForegroundColor Red
    Write-Host ($res | ConvertTo-Json -Compress)
    exit 1
}

$entries = @($res.entries)
if ($entries.Count -eq 0) {
    Write-Host "The log is EMPTY. Nothing to back up." -ForegroundColor Red
    Write-Host "Do not deploy expecting a restore: there is nothing to restore." -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory $OutDir | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$path = Join-Path $OutDir "bom_usage_backup_$stamp.json"

# Save the raw payload verbatim: this is exactly what /import_entries expects back.
$res | ConvertTo-Json -Depth 10 | Out-File -FilePath $path -Encoding utf8

$dates = @($entries | ForEach-Object { $_.ts } | Where-Object { $_ } | Sort-Object)
$users = @($entries | ForEach-Object { $_.user } | Where-Object { $_ } | Select-Object -Unique)

Write-Host ""
Write-Host "Saved $($entries.Count) entries to $path" -ForegroundColor Green
Write-Host "  Oldest entry: $($dates | Select-Object -First 1)"
Write-Host "  Newest entry: $($dates | Select-Object -Last 1)"
Write-Host "  Users:        $($users -join ', ')"
Write-Host ""
Write-Host "Check that count against the dashboard before you deploy anything." -ForegroundColor Yellow
