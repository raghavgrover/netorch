# RUNBOOK: Cisco IOS — NTP Configuration Audit & Remediation
# Description : Validates NTP server configuration, reports drift, and remediates if needed.
show ntp associations
show ntp status
show clock detail
show running-config | section ntp
show running-config | include clock timezone

! --- Remediation (uncomment to apply) ---
! configure terminal
!  no ntp server
!  ntp server 10.0.0.10 prefer source Loopback0
!  ntp server 10.0.0.11 source Loopback0
!  ntp update-calendar
!  clock timezone UTC 0 0
!  ntp authenticate
! end
! write memory

! --- Post-check ---
show ntp associations detail
show ntp status