# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
$ErrorActionPreference = "Stop"
Write-Host "Installing AudioDeviceCmdlets for the current user..."
Install-Module -Name AudioDeviceCmdlets -Scope CurrentUser -Force -AllowClobber
Import-Module AudioDeviceCmdlets
Get-AudioDevice -List
Write-Host "Done. Restart NEO."
