# =============================================================================
# RUNBOOK: Cisco IOS — Access Control List (ACL) Audit
# Description : Enumerates all ACLs, hit counters, interface bindings,
#               VTY ACLs, and flags overly permissive permit-any entries.
# Target      : Cisco IOS / IOS-XE devices
# =============================================================================

show ip access-lists
show ip interface | include (Internet address|Inbound|Outbound|access list)
show running-config | section ip access-list
show running-config | include ^access-list
show running-config | section line vty
show running-config | section control-plane
show running-config | include permit any
show ipv6 access-list
show ip access-lists | include (Extended|Standard|permit|deny|matches)