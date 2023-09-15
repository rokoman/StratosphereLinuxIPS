from slips_files.common.imports import *
import json
from modules.network_discovery.horizontal_portscan import HorizontalPortscan
from modules.network_discovery.vertical_portscan import VerticalPortscan

class NetworkDiscovery(IModule, multiprocessing.Process):
    """
    A class process to find port scans
    This should be converted into a module that wakesup alone when a new alert arrives
    """
    name = 'Network Discovery'
    description = 'Detect Horizonal, Vertical Port scans, ICMP, and DHCP scans'
    authors = ['Sebastian Garcia', 'Alya Gomaa']

    def init(self):
        self.horizontal_ps = HorizontalPortscan(self.db)
        self.vertical_ps = VerticalPortscan(self.db)
        # Get from the database the separator used to separate the IP and the word profile
        self.fieldseparator = self.db.get_field_separator()
        # To which channels do you wnat to subscribe? When a message arrives on the channel the module will wakeup
        self.c1 = self.db.subscribe('tw_modified')
        self.c2 = self.db.subscribe('new_notice')
        self.c3 = self.db.subscribe('new_dhcp')
        self.channels = {
            'tw_modified': self.c1,
            'new_notice': self.c2,
            'new_dhcp': self.c3,
        }
        # We need to know that after a detection, if we receive another flow
        # that does not modify the count for the detection, we are not
        # re-detecting again only because the threshold was overcomed last time.
        self.cache_det_thresholds = {}
        self.separator = '_'
        # The minimum amount of ports to scan in vertical scan
        self.port_scan_minimum_dports = 5
        self.pingscan_minimum_flows = 5
        self.pingscan_minimum_scanned_ips = 5
        # time in seconds to wait before alerting port scan
        self.time_to_wait_before_generating_new_alert = 25
        # when a client is seen requesting this minimum addresses in 1 tw,
        # slips sets dhcp scan evidence
        self.minimum_requested_addrs = 4

    def shutdown_gracefully(self):
        # alert about all the pending evidence before this module stops
        self.horizontal_ps.combine_evidence()
        self.vertical_ps.combine_evidence()

    def check_icmp_sweep(self, msg, note, profileid, uid, twid, timestamp):
        """
        Use our own Zeek scripts to detect ICMP scans. 
        Threshold is on the scrips and it is 25 icmp flows
        """

        if 'TimestampScan' in note:
            evidence_type = 'ICMP-Timestamp-Scan'
        elif 'ICMPAddressScan' in note:
            evidence_type = 'ICMP-AddressScan'
        elif 'AddressMaskScan' in note:
            evidence_type = 'ICMP-AddressMaskScan'
        else:
            # unsupported notice type
            return False

        hosts_scanned = int(msg.split('on ')[1].split(' hosts')[0])
        # get the confidence from 0 to 1 based on the number of hosts scanned
        confidence = 1 / (255 - 5) * (hosts_scanned - 255) + 1
        threat_level = 'medium'
        category = 'Recon.Scanning'
        attacker_direction = 'srcip'
        # this is the last dip scanned
        attacker = profileid.split('_')[1]
        source_target_tag = 'Recon'
        description = msg
        # this one is detected by zeek so we can't track the uids causing it
        self.db.setEvidence(evidence_type, attacker_direction, attacker, threat_level, confidence, description,
                                 timestamp, category, source_target_tag=source_target_tag, conn_count=hosts_scanned,
                                 profileid=profileid, twid=twid, uid=uid)

    def check_portscan_type3(self):
        """
        ###
        # PortScan Type 3. Direction OUT
        # Considering all the flows in this TW, for all the Dst IP, get the sum of all the pkts send to each dst port TCP No tEstablished
        totalpkts = int(data[dport]['totalpkt'])
        # If for each port, more than X amount of packets were sent, report an evidence
        if totalpkts > 3:
            # Type of evidence
            evidence_type = 'PortScanType3'
            # Key
            key = 'dport' + ':' + dport + ':' + evidence_type
            # Description
            description = 'Too Many Not Estab TCP to same port {} from IP: {}. Amount: {}'.format(dport, profileid.split('_')[1], totalpkts)
            # Threat level
            threat_level = 50
            # Confidence. By counting how much we are over the threshold.
            if totalpkts >= 10:
                # 10 pkts or more, receive the max confidence
                confidence = 1
            else:
                # Between 3 and 10 pkts compute a kind of linear grow
                confidence = totalpkts / 10.0
            self.db.setEvidence(profileid, twid, evidence_type, threat_level, confidence)
            self.print('Too Many Not Estab TCP to same port {} from IP: {}. Amount: {}'.format(dport, profileid.split('_')[1], totalpkts),6,0)
        """

    def check_icmp_scan(self, profileid, twid):
        # Map the ICMP port scanned to it's attack
        port_map = {
            '0x0008': 'AddressScan',
            '0x0013': 'TimestampScan',
            '0x0014': 'TimestampScan',
            '0x0017': 'AddressMaskScan',
            '0x0018': 'AddressMaskScan',
        }

        direction = 'Src'
        role = 'Client'
        type_data = 'Ports'
        protocol = 'ICMP'
        state = 'Established'
        sports = self.db.getDataFromProfileTW(
                    profileid, twid, direction, state, protocol, role, type_data
                )
        for sport, sport_info in sports.items():
            # get the name of this attack
            attack = port_map.get(sport)
            if not attack:
                return

            # get the IPs attacked
            scanned_ips = sport_info['dstips']
            # are we pinging a single IP or ping scanning several IPs?
            amount_of_scanned_ips = len(scanned_ips)

            if amount_of_scanned_ips == 1:
                # how many icmp flows were found?
                for scanned_ip, scan_info in scanned_ips.items():
                    icmp_flows_uids = scan_info['uid']
                    number_of_flows = len(icmp_flows_uids)
                    # how many flows are responsible for this attack
                    # (from this srcip to this dstip on the same port)
                    cache_key = f'{profileid}:{twid}:dstip:{scanned_ip}:{sport}:{attack}'
                    prev_flows = self.cache_det_thresholds.get(cache_key, 0)

                    # We detect a scan every Threshold. So we detect when there
                    # is 5,10,15 etc. scan to the same dstip on the same port
                    # The idea is that after X dips we detect a connection.
                    # And then we 'reset' the counter
                    # until we see again X more.
                    if (
                            number_of_flows % self.pingscan_minimum_flows == 0
                            and prev_flows < number_of_flows
                    ):
                        self.cache_det_thresholds[cache_key] = number_of_flows
                        pkts_sent = scan_info['spkts']
                        timestamp = scan_info['stime']
                        self.set_evidence_icmpscan(
                            amount_of_scanned_ips,
                            timestamp,
                            pkts_sent,
                            protocol,
                            profileid,
                            twid,
                            icmp_flows_uids,
                            attack,
                            scanned_ip=scanned_ip
                        )

            elif amount_of_scanned_ips > 1:
                # this srcip is scanning several IPs (a network maybe)
                # how many dstips scanned by this srcip on this port?
                cache_key = f'{profileid}:{twid}:{attack}'
                prev_scanned_ips = self.cache_det_thresholds.get(cache_key, 0)
                # detect every 5, 10, 15 scanned IPs
                if (
                        amount_of_scanned_ips % self.pingscan_minimum_scanned_ips == 0
                        and prev_scanned_ips < amount_of_scanned_ips
                ):

                    pkts_sent = 0
                    uids = []
                    for scanned_ip, scan_info in scanned_ips.items():
                        # get the total amount of pkts sent to all scanned IP
                        pkts_sent += scan_info['spkts']
                        # get all flows that were part of this scan
                        uids.extend(scan_info['uid'])
                        timestamp = scan_info['stime']

                    self.set_evidence_icmpscan(
                            amount_of_scanned_ips,
                            timestamp,
                            pkts_sent,
                            protocol,
                            profileid,
                            twid,
                            uids,
                            attack
                        )
                    self.cache_det_thresholds[cache_key] = amount_of_scanned_ips


    def set_evidence_icmpscan(
            self,
            number_of_scanned_ips,
            timestamp,
            pkts_sent,
            protocol,
            profileid,
            twid,
            icmp_flows_uids,
            attack,
            scanned_ip=False
    ):
        confidence = self.calculate_confidence(pkts_sent)
        attacker_direction = 'srcip'
        evidence_type = attack
        source_target_tag = 'Recon'
        threat_level = 'medium'
        category = 'Recon.Scanning'
        srcip = profileid.split('_')[-1]
        attacker = srcip

        if number_of_scanned_ips == 1:
            description = (
                            f'ICMP scanning {scanned_ip} ICMP scan type: {attack}. '
                            f'Total packets sent: {pkts_sent} over {len(icmp_flows_uids)} flows. '
                            f'Confidence: {confidence}. by Slips'
                        )
            victim = scanned_ip
        else:
            description = (
                f'ICMP scanning {number_of_scanned_ips} different IPs. ICMP scan type: {attack}. '
                f'Total packets sent: {pkts_sent} over {len(icmp_flows_uids)} flows. '
                f'Confidence: {confidence}. by Slips'
            )
            # not a single victim, there are many
            victim = ''

        self.db.setEvidence(evidence_type, attacker_direction, attacker, threat_level, confidence, description,
                                 timestamp, category, source_target_tag=source_target_tag, conn_count=pkts_sent,
                                 proto=protocol, profileid=profileid, twid=twid, uid=icmp_flows_uids, victim=victim)


    def set_evidence_dhcp_scan(
            self,
            timestamp,
            profileid,
            twid,
            uids,
            number_of_requested_addrs
    ):
        evidence_type = 'DHCPScan'
        attacker_direction = 'srcip'
        source_target_tag = 'Recon'
        srcip = profileid.split('_')[-1]
        attacker = srcip
        threat_level = 'medium'
        category = 'Recon.Scanning'
        confidence = 0.8
        description = (
            f'Performing a DHCP scan by requesting {number_of_requested_addrs} different IP addresses. '
            f'Threat Level: {threat_level}. '
            f'Confidence: {confidence}. by Slips'
        )

        self.db.setEvidence(evidence_type, attacker_direction, attacker, threat_level, confidence, description,
                                 timestamp, category, source_target_tag=source_target_tag,
                                 conn_count=number_of_requested_addrs, profileid=profileid, twid=twid, uid=uids)


    def check_dhcp_scan(self, flow_info):
        """
        Detects DHCP scans, when a client requests 4+ different IPs in the same tw
        """
        # this is the actual zeek flow
        flow = flow_info['flow']
        requested_addr = flow['requested_addr']
        if not requested_addr:
            # we are only interested in DHCPREQUEST flows, where a client is requesting an IP
            return

        profileid = flow_info['profileid']
        twid = flow_info['twid']
        # this is a list of uids
        uids = flow['uids']
        ts = flow['starttime']

        # dhcp_flows format is
        #       { requested_addr: uid,
        #         requested_addr2: uid2... }

        dhcp_flows: dict = self.db.get_dhcp_flows(profileid, twid)

        if dhcp_flows:
            # client was seen requesting an addr before in this tw
            # was it requesting the same addr?
            if requested_addr in dhcp_flows:
                # a client requesting the same addr twice isn't a scan
                return

            # it was requesting a different addr, keep track of it and its uid
            self.db.set_dhcp_flow(profileid, twid, requested_addr, uids)
        else:
            # first time for this client to make a dhcp request in this tw
            self.db.set_dhcp_flow(profileid, twid, requested_addr, uids)
            return


        # TODO if we are not going to use the requested addr, no need to store it
        # TODO just store the uids
        dhcp_flows: dict = self.db.get_dhcp_flows(profileid, twid)

        # we alert every 4,8,12, etc. requested IPs
        number_of_requested_addrs = len(dhcp_flows)
        if number_of_requested_addrs % self.minimum_requested_addrs == 0:
            # get the uids of all the flows where this client was requesting an addr in this tw

            for uids_list in dhcp_flows.values():
                uids.append(uids_list[0])

            self.set_evidence_dhcp_scan(
                ts,
                profileid,
                twid,
                uids,
                number_of_requested_addrs
            )

    def pre_main(self):
        utils.drop_root_privs()
    def main(self):
        if msg:= self.get_msg('tw_modified'):
            # Get the profileid and twid
            profileid = msg['data'].split(':')[0]
            twid = msg['data'].split(':')[1]
            # Start of the port scan detection
            self.print(
                f'Running the detection of portscans in profile '
                f'{profileid} TW {twid}', 3, 0
            )

            # For port scan detection, we will measure different things:

            # 1. Vertical port scan:
            # (single IP being scanned for multiple ports)
            # - 1 srcip sends not established flows to > 3 dst ports in the same dst ip. Any number of packets
            # 2. Horizontal port scan:
            #  (scan against a group of IPs for a single port)
            # - 1 srcip sends not established flows to the same dst ports in > 3 dst ip.
            # 3. Too many connections???:
            # - 1 srcip sends not established flows to the same dst ports, > 3 pkts, to the same dst ip
            # 4. Slow port scan. Same as the others but distributed in multiple time windows

            # Remember that in slips all these port scans can happen for traffic going IN to an IP or going OUT from the IP.

            self.horizontal_ps.check(profileid, twid)
            self.vertical_ps.check(profileid, twid)
            self.check_icmp_scan(profileid, twid)

        if msg:= self.get_msg('new_notice'):
            data = msg['data']
            # Convert from json to dict
            data = json.loads(data)
            profileid = data['profileid']
            twid = data['twid']
            # Get flow as a json
            flow = data['flow']
            # Convert flow to a dict
            flow = json.loads(flow)
            timestamp = flow['stime']
            uid = data['uid']
            msg = flow['msg']
            note = flow['note']
            self.check_icmp_sweep(
                msg, note, profileid, uid, twid, timestamp
            )

        if msg:= self.get_msg('new_dhcp'):
            flow = json.loads(msg['data'])
            self.check_dhcp_scan(flow)

