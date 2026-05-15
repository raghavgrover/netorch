# RUNBOOK: Cisco IOS — BGP Neighbor Health Check
# Description : Audits all BGP peers, session states, prefix counts,
#               received/advertised routes, and flapping sessions.
# Target      : Cisco IOS / IOS-XE devices with BGP configured
# =============================================================================

show bgp all summary
show bgp ipv4 unicast summary
show bgp neighbors
show bgp all summary | include (Neighbor|Idle|Active|Connect|OpenSent|OpenConfirm)
show bgp neighbors | include (BGP neighbor|prefixes|MsgRcvd|MsgSent|Up/Down|State)
show bgp all summary | include (BGP router|BGP table|Total number|network entries)
show bgp ipv4 unicast
show running-config | section router bgp
show bgp ipv4 unicast dampened-paths
show bgp ipv4 unicast flap-statistics
show bfd neighbors