! =============================================================================
! RUNBOOK: Cisco IOS — Security Hardening Audit (CIS Benchmark)
! Description : Checks SSH version, AAA, unused services, SNMP, logging,
!               banners, password encryption, and control-plane protection.
! Target      : Cisco IOS / IOS-XE devices
! =============================================================================

show version | include (IOS|Version|uptime|Serial)
show ip ssh
show running-config | include (ip ssh|transport input)
show running-config | section line vty
show running-config | section aaa
show running-config | include username
show running-config | section banner
show running-config | section snmp-server
show snmp
show logging
show running-config | section logging
show running-config | include (no service|service password|ip finger|ip bootp|cdp run|ip http |ip https)
show running-config | include (enable secret|enable password|service password-encryption)
show running-config | section control-plane
show policy-map control-plane
show running-config | include (access-class|ip access-group)
show interfaces | include (is administratively down)
show running-config | include (ntp authenticate|ntp authentication-key|ntp trusted-key)
