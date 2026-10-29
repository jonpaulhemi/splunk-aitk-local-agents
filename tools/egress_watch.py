#!/usr/bin/env python3
"""No-cloud-traffic check for the release acceptance run (#24). Standard library only.

Run it as root on the search head while tools/acceptance.py runs, then stop
it with Ctrl-C (or give --seconds):

    sudo python3 egress_watch.py --out egress.json
    sudo python3 egress_watch.py --seconds 900 --out egress.json

It captures with tcpdump on every interface, then reports each DNS name
looked up, each TLS server name or proxy CONNECT host, and each new outbound
connection. It fails if any name belongs to Splunk's cloud (Splunk Cloud
Services, Cloud Connect, telemetry). The other names should be only your LLM
and MCP hosts.

Loopback traffic isn't captured, and only each packet's first 1600 bytes are
kept. That is enough for headers and TLS ClientHellos, but anything sent
unencrypted (an http:// LLM endpoint) would be partly in the capture, so the
capture file is deleted afterwards unless --keep-pcap is given.
"""

import argparse
import ipaddress
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

# Splunk-run domains AI Toolkit and splunkd reach: SCS / SLIM, Cloud Connect,
# telemetry (quickdraw), Splunk Cloud (incl. GovCloud) and the mobile/telemetry
# endpoints.
SPLUNK_CLOUD_DOMAINS = ("splunk.com", "splunkcloud.com", "splunkcloudgc.com", "splkmobile.com", "splunkbeta.com")

ETHERNET, LINUX_SLL, LINUX_SLL2 = 1, 113, 276
RAW_IP = (101, 12, 228, 229)
TCP, UDP = 6, 17
LIMITED_BROADCAST = ipaddress.ip_address("255.255.255.255")
PCAP_MAGIC = {0xA1B2C3D4: "<", 0xA1B23C4D: "<", 0xD4C3B2A1: ">", 0x4D3CB2A1: ">"}

# Everything but loopback. (Flag and payload filters would be smaller, but
# libpcap can't apply them to IPv6.)
CAPTURE_FILTER = "(tcp or udp) and not (net 127.0.0.0/8 or host ::1)"


def is_splunk_cloud(name):
    name = name.lower().rstrip(".")
    return any(name == d or name.endswith("." + d) for d in SPLUNK_CLOUD_DOMAINS)


def summarize(pcap_path, local_addresses=()):
    """The report for one capture: names seen, outbound connections, and whether it passes.

    With `local_addresses`, only packets this host sends count; multicast and
    broadcast never do, as they don't leave the LAN.
    """
    local = {ipaddress.ip_address(a) for a in local_addresses}
    names, connections, named_ips = set(), set(), set()
    for src, dst, proto, segment in _ip_packets(pcap_path):
        if dst.is_loopback or dst in local or dst.is_multicast or dst == LIMITED_BROADCAST:
            continue
        if local and src not in local:
            continue
        if proto == UDP and len(segment) >= 8:
            dport = struct.unpack(">H", segment[2:4])[0]
            if dport == 53:
                name = _dns_question(segment[8:])
                if name:
                    names.add(name)
            else:
                connections.add(_address(dst, dport) + "/udp")
        elif proto == TCP and len(segment) >= 20:
            dport = struct.unpack(">H", segment[2:4])[0]
            flags = segment[13]
            payload = segment[(segment[12] >> 4) * 4:]
            if flags & 0x02 and not flags & 0x10:
                connections.add(_address(dst, dport))
            name = _server_name(payload) or _proxy_target(payload)
            if name:
                names.add(name)
                named_ips.add(_address(dst, dport))
    cloud = sorted(n for n in names if is_splunk_cloud(n))
    # A connection whose name wasn't seen (no TLS, or a ClientHello the capture
    # cut short) could be anything, so it's listed for a person to look at.
    unnamed = sorted(c for c in connections if c not in named_ips and not c.endswith("/udp"))
    return {"pass": not cloud, "splunk_cloud": cloud, "names": sorted(names), "connections": sorted(connections),
            "connections_without_a_name": unnamed}


def _address(ip, port):
    return f"{ip}:{port}" if ip.version == 4 else f"[{ip}]:{port}"


def _ip_packets(pcap_path):
    """(source, destination, protocol, transport segment) for each IP packet."""
    with open(pcap_path, "rb") as f:
        header = f.read(24)
        endian = PCAP_MAGIC.get(struct.unpack("<I", header[:4])[0]) if len(header) == 24 else None
        if not endian:
            raise ValueError(f"{pcap_path} isn't a pcap file (pcapng isn't read; use tcpdump -w)")
        linktype = struct.unpack(endian + "I", header[20:24])[0] & 0x0FFFFFFF
        while True:
            record = f.read(16)
            if len(record) < 16:
                return
            captured = struct.unpack(endian + "IIII", record)[2]
            frame = f.read(captured)
            packet = _strip_link(frame, linktype)
            if packet:
                parsed = _ip(packet)
                if parsed:
                    yield parsed


def _strip_link(frame, linktype):
    if linktype == ETHERNET:  # with any VLAN tags
        offset, proto = 14, struct.unpack(">H", frame[12:14])[0]
        while proto in (0x8100, 0x88A8) and len(frame) >= offset + 4:
            proto, offset = struct.unpack(">H", frame[offset + 2:offset + 4])[0], offset + 4
        return frame[offset:] if proto in (0x0800, 0x86DD) else None
    if linktype == LINUX_SLL:
        return frame[16:] if struct.unpack(">H", frame[14:16])[0] in (0x0800, 0x86DD) else None
    if linktype == LINUX_SLL2:
        return frame[20:] if struct.unpack(">H", frame[0:2])[0] in (0x0800, 0x86DD) else None
    if linktype in RAW_IP:
        return frame
    return None


def _ip(packet):
    if not packet:
        return None
    if packet[0] >> 4 == 4 and len(packet) >= 20:
        header = (packet[0] & 0x0F) * 4
        return ipaddress.ip_address(packet[12:16]), ipaddress.ip_address(packet[16:20]), packet[9], packet[header:]
    if packet[0] >> 4 == 6 and len(packet) >= 40:
        return ipaddress.ip_address(packet[8:24]), ipaddress.ip_address(packet[24:40]), packet[6], packet[40:]
    return None


def _dns_question(message):
    if len(message) < 12 or message[2] & 0x80 or struct.unpack(">H", message[4:6])[0] == 0:
        return None
    labels, i = [], 12
    while i < len(message) and message[i]:
        length = message[i]
        if length & 0xC0:
            return None
        labels.append(message[i + 1:i + 1 + length].decode("ascii", "replace"))
        i += 1 + length
    return ".".join(labels).lower() or None


def _proxy_target(payload):
    """The host of an HTTP proxy CONNECT request, or None."""
    if not payload.startswith(b"CONNECT "):
        return None
    target = payload[8:].split(b" ", 1)[0].decode("ascii", "replace")
    return target.rsplit(":", 1)[0].strip("[]").lower() or None


def _server_name(payload):
    """The SNI host name of a TLS ClientHello, or None."""
    try:
        if len(payload) < 43 or payload[0] != 0x16 or payload[5] != 0x01:
            return None
        i = 9 + 2 + 32  # record + handshake headers, version, random
        i += 1 + payload[i]  # session id
        i += 2 + struct.unpack(">H", payload[i:i + 2])[0]  # cipher suites
        i += 1 + payload[i]  # compression methods
        end = i + 2 + struct.unpack(">H", payload[i:i + 2])[0]
        i += 2
        while i + 4 <= min(end, len(payload)):
            kind, length = struct.unpack(">HH", payload[i:i + 4])
            if kind == 0:
                name_length = struct.unpack(">H", payload[i + 7:i + 9])[0]
                return payload[i + 9:i + 9 + name_length].decode("ascii").lower()
            i += 4 + length
    except (IndexError, struct.error, UnicodeDecodeError):
        pass
    return None


def _local_addresses():
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addresses.add(info[4][0].split("%")[0])
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=False).stdout
        addresses.update(out.split())
    except OSError:
        pass
    return addresses


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, help="stop after this many seconds (default: until Ctrl-C)")
    parser.add_argument("--interface", default="any")
    parser.add_argument("--out", help="write the JSON report here as well as printing it")
    parser.add_argument("--keep-pcap", help="keep the capture at this path")
    parser.add_argument("--read", help="summarize an existing capture instead of capturing")
    args = parser.parse_args()

    if args.read:
        pcap = args.read
    else:
        pcap = args.keep_pcap or os.path.join(tempfile.mkdtemp(prefix="egress_watch_"), "run.pcap")
        # -Z root: many builds otherwise drop to a tcpdump user that can't
        # write into this root-only folder.
        tcpdump = subprocess.Popen(["tcpdump", "-i", args.interface, "-s", "1600", "-U", "-n", "-Z", "root",
                                    "-w", pcap, CAPTURE_FILTER], stderr=subprocess.PIPE, text=True)
        started = time.time()
        print(f"capturing on {args.interface}; run the acceptance now, then press Ctrl-C", file=sys.stderr)
        try:
            while tcpdump.poll() is None and (not args.seconds or time.time() - started < args.seconds):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        if tcpdump.poll() is None:
            tcpdump.send_signal(signal.SIGINT)
            try:
                tcpdump.wait(timeout=30)
            except subprocess.TimeoutExpired:
                tcpdump.kill()
                tcpdump.wait()
        if tcpdump.returncode not in (0, -signal.SIGINT, 130) or not os.path.exists(pcap):
            sys.exit(f"tcpdump failed (exit {tcpdump.returncode}): {tcpdump.stderr.read().strip()[-500:]}\n"
                     "Run as root with tcpdump installed.")

    report = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              **summarize(pcap, _local_addresses())}
    if not args.read and not args.keep_pcap:
        os.remove(pcap)
        os.rmdir(os.path.dirname(pcap))
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    sys.exit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
