#Requires -RunAsAdministrator
Get-NetFirewallRule -DisplayName 'AGNI Sovereign Block*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName 'AGNI Lockdown Block*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host 'AGNI firewall rules removed.'
