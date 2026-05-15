# RUNBOOK: Cisco IOS — Interface Status Audit
# Description : Audits all interfaces for status, errors, duplex/speed
#               mismatches, error-disabled ports, and unusual counters.
# Target      : Cisco IOS / IOS-XE devices
# =============================================================================

show interfaces status
show interfaces
show interfaces | include (line protocol|errors|reset|duplex|speed|CRC|input errors|output errors|collisions)
show interfaces | include (is down|protocol is down)
show interfaces status err-disabled
show interfaces | include (drops|ignored|overrun|throttles)
show interfaces switchport | include (Name|Access Mode VLAN|Trunking Native Mode VLAN|Operational Mode)
show interfaces trunk
show interfaces transceiver