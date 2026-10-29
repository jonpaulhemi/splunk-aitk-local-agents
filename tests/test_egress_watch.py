"""The no-cloud-traffic check finds Splunk cloud hosts in a search head's packet capture (#24).

tools/egress_watch.py runs tcpdump on the search head during the acceptance
run, then reads the capture: DNS lookups, TLS server names and new outbound
connections. Any Splunk cloud name fails the check.
"""

import socket
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import egress_watch  # noqa: E402

SEARCH_HEAD = "10.0.0.5"


def _pcap(path, frames, linktype=276):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype))
        for i, frame in enumerate(frames):
            f.write(struct.pack("<IIII", 1793230000 + i, 0, len(frame), len(frame)) + frame)
    return path


def _link(ip_packet, linktype, ipv6=False):
    proto = 0x86DD if ipv6 else 0x0800
    if linktype == 276:  # LINUX_SLL2, what `tcpdump -i any` writes
        return struct.pack(">HHIHBB8s", proto, 0, 2, 1, 4, 6, b"\0" * 8) + ip_packet
    if linktype == 1:  # Ethernet
        return b"\0" * 12 + struct.pack(">H", proto) + ip_packet
    raise ValueError(linktype)


def _ipv4(dst, proto, payload, src=SEARCH_HEAD):
    header = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0, 0, 64, proto, 0,
                         socket.inet_aton(src), socket.inet_aton(dst))
    return header + payload


def _ipv6(dst, proto, payload, src="fd00::5"):
    return (struct.pack(">IHBB", 6 << 28, len(payload), proto, 64)
            + socket.inet_pton(socket.AF_INET6, src) + socket.inet_pton(socket.AF_INET6, dst) + payload)


def _udp(dport, payload, sport=50000):
    return struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload


def _tcp(dport, payload=b"", syn=False, sport=40000):
    flags = 0x02 if syn else 0x18
    return struct.pack(">HHIIBBHHH", sport, dport, 0, 0, 5 << 4, flags, 65535, 0, 0) + payload


def _dns_query(name):
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\0"
    return struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + labels + struct.pack(">HH", 1, 1)


def _client_hello(server_name):
    name = server_name.encode()
    sni = struct.pack(">HBH", len(name) + 3, 0, len(name)) + name
    extensions = struct.pack(">HH", 0, len(sni)) + sni
    body = (b"\x03\x03" + b"\0" * 32 + b"\0" + struct.pack(">H", 2) + b"\x13\x01" + b"\x01\x00"
            + struct.pack(">H", len(extensions)) + extensions)
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake


@pytest.mark.parametrize("linktype", [276, 1])
def test_a_capture_with_only_llm_and_mcp_traffic_passes(tmp_path, linktype):
    frames = [_link(_ipv4("10.0.0.2", 17, _udp(53, _dns_query("bedrock-runtime.us-gov-west-1.amazonaws.com"))), linktype),
              _link(_ipv4("52.61.0.10", 6, _tcp(443, syn=True)), linktype),
              _link(_ipv4("52.61.0.10", 6, _tcp(443, _client_hello("bedrock-runtime.us-gov-west-1.amazonaws.com"))),
                    linktype),
              _link(_ipv4("10.0.0.9", 6, _tcp(8443, _client_hello("mcp.corp.example"))), linktype)]

    report = egress_watch.summarize(_pcap(tmp_path / "run.pcap", frames, linktype))

    assert report["pass"] is True
    assert report["splunk_cloud"] == []
    assert report["names"] == ["bedrock-runtime.us-gov-west-1.amazonaws.com", "mcp.corp.example"]
    assert report["connections"] == ["52.61.0.10:443"]
    assert report["connections_without_a_name"] == []


def test_any_splunk_cloud_name_fails_it(tmp_path):
    frames = [_link(_ipv4("10.0.0.2", 17, _udp(53, _dns_query("tenant.api.scs.splunk.com"))), 276),
              _link(_ipv4("44.0.0.1", 6, _tcp(443, _client_hello("quickdraw.splunk.com"))), 276),
              _link(_ipv6("2600::1", 6, _tcp(443, _client_hello("acme.splunkcloudgc.com"))), 276, ipv6=True),
              _link(_ipv4("10.0.0.2", 17, _udp(53, _dns_query("notsplunk.com.example"))), 276)]

    report = egress_watch.summarize(_pcap(tmp_path / "run.pcap", frames))

    assert report["pass"] is False
    assert report["splunk_cloud"] == ["acme.splunkcloudgc.com", "quickdraw.splunk.com", "tenant.api.scs.splunk.com"]


def test_inbound_and_loopback_traffic_is_left_out(tmp_path):
    # Launchpad's own calls to splunkd arrive from browsers; agent calls reach
    # the runtime over the search head's own address.
    frames = [_link(_ipv4(SEARCH_HEAD, 6, _tcp(8089, syn=True), src="192.168.1.20"), 276),
              _link(_ipv4("127.0.0.1", 6, _tcp(8089, syn=True), src="127.0.0.1"), 276)]

    report = egress_watch.summarize(_pcap(tmp_path / "run.pcap", frames), local_addresses={SEARCH_HEAD})

    assert report["connections"] == []
    assert report["pass"] is True


def test_other_outbound_udp_is_listed(tmp_path):
    # botocore's opt-in client-side monitoring is UDP (docs/appinspect.md).
    frames = [_link(_ipv4("10.0.0.7", 17, _udp(31000, b"{}")), 276)]

    report = egress_watch.summarize(_pcap(tmp_path / "run.pcap", frames))

    assert report["connections"] == ["10.0.0.7:31000/udp"]


def test_hosts_reached_through_an_http_proxy_are_named(tmp_path):
    frames = [_link(_ipv4("10.0.0.3", 6, _tcp(3128, b"CONNECT tenant.api.scs.splunk.com:443 HTTP/1.1\r\n\r\n")), 276)]

    report = egress_watch.summarize(_pcap(tmp_path / "run.pcap", frames))

    assert report["splunk_cloud"] == ["tenant.api.scs.splunk.com"]


def test_connections_whose_name_wasnt_seen_are_listed_for_review(tmp_path):
    # By IP, or with a ClientHello the capture cut short: it could be anything.
    frames = [_link(_ipv4("44.0.0.1", 6, _tcp(443, syn=True)), 276)]

    report = egress_watch.summarize(_pcap(tmp_path / "run.pcap", frames))

    assert report["connections_without_a_name"] == ["44.0.0.1:443"]


def test_a_pcapng_file_is_refused_rather_than_misread(tmp_path):
    path = tmp_path / "run.pcapng"
    path.write_bytes(b"\x0a\x0d\x0d\x0a" + b"\0" * 40)

    with pytest.raises(ValueError, match="isn't a pcap file"):
        egress_watch.summarize(path)
