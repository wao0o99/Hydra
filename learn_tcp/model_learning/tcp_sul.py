"""TCP System Under Learning — packet-level interaction with a real SUT.

Classes:
    TCP_SUL      — AALpy SUL interface for TCP
    TCPExample   — low-level packet I/O via scapy + control channel
    ConnectionPool / Connection — per-port TCP connection state
"""

import socket
import random
import time
import threading
import logging

from scapy.all import IP, TCP, conf
from aalpy.base import SUL
from learn_tcp.model_learning.packet_tracker import PacketTracker
from learn_tcp.model_learning.config import (
    SUL_ip,
    my_ip,
    my_port,
    SUL_port_control,
    my_port_control,
    RCV_WAIT_TIME,
    TIME_SLOT,
    interface,
)

MOD32 = 2**32

_SYSCALLS = {
    "LISTEN": "listen",
    "ACCEPT": "accept",
    "RCV": "rcv",
    "SEND": "send",
    "CLOSE": "close",
    "CLOSECONNECTION": "closeconnection",
}


# Connection state
class ConnectionPool:
    def __init__(self):
        self.connections = {}

    def add(self, port):
        self.connections[port] = Connection(port)

    def remove(self, port):
        self.connections.pop(port, None)

    def get(self, port):
        return self.connections[port]


class Connection:
    def __init__(self, port):
        self.dport = port
        self.seq = random.getrandbits(32)
        self.ack = random.getrandbits(32)
        self.tsecr = random.getrandbits(32)
        self.history_packets = []
        self.last_challenge_ack_time = 0
        self.last_send_time = 0

    def update_from_response(self, seq, ack, flags, tsecr, data_length):
        if "R" not in flags:
            self.seq = int(ack)
            self.ack = int(seq) + data_length
        if tsecr is not None:
            self.tsecr = int(tsecr)
        if flags in ("SA", "FA"):
            self.ack += 1


# Low-level TCP I/O
class TCPExample:
    """Scapy-based raw packet sender + SUT control channel."""

    def __init__(self):
        self.tracker = PacketTracker()
        self.packet_template = IP(src=my_ip, dst=SUL_ip) / TCP(sport=my_port)
        self.l3socket = conf.L3socket(iface=interface)

        self.socket_lock = threading.Lock()
        self.control_socket = None
        self._connect_control()

        self.pool = ConnectionPool()

    def close(self):
        self.control_socket.close()
        self.l3socket.close()
        self.tracker.shutdown()

    def _connect_control(self):
        self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.control_socket.bind((my_ip, my_port_control))
        self.control_socket.connect((SUL_ip, SUL_port_control))

    def _recv_line(self):
        buf = b""
        while True:
            chunk = self.control_socket.recv(6)
            buf += chunk
            if b"\n" in chunk:
                break
        return buf

    # Port management
    def get_dst_port(self):
        with self.socket_lock:
            self.control_socket.sendall(b"port\n")
        port = int(self._recv_line().decode())
        self.tracker.add_port(port)
        return port

    def reset(self, port):
        self.tracker.remove_port(int(port))
        with self.socket_lock:
            self.control_socket.sendall(f"reset\n{port}\n".encode())

    # Raw packet path
    def process_letter(self, port, fields):
        """Send a raw TCP packet and return the SUT's response flags."""
        self._wait_rate_limit(port, fields["flags"])
        self.tracker.clear_packets(port)
        self.l3socket.send(self._build_packet(port, fields))
        return self._wait_response(port)

    # Control-channel path
    def action_handler(self, command, port):
        """Send a server-side action (listen/accept/…) and return response."""
        if command == "send":
            self._wait_send_cooldown(port)
        self.tracker.clear_packets(port)
        with self.socket_lock:
            self.control_socket.sendall(f"{command}\n{port}\n".encode())
        return self._wait_response(port, record_send=(command == "send"))

    # Shared response loop
    def _wait_response(self, port, record_send=False):
        total_wait = 0
        while True:
            time.sleep(TIME_SLOT)
            if self.tracker.port_to_packets[port]:
                pkt = self.tracker.port_to_packets[port][0]
                if not self._is_retransmit(pkt):
                    conn = self.pool.get(port)
                    if record_send:
                        conn.last_send_time = time.time()
                    self._update_rate_limit(port, pkt.tcp_flags)
                    conn.update_from_response(
                        pkt.tcp_seq,
                        pkt.tcp_ack,
                        pkt.tcp_flags,
                        pkt.ts_val,
                        pkt.data_length,
                    )
                    return pkt.tcp_flags + pkt.options_flag_str
                else:
                    self.tracker.remove_packet(port)
            total_wait += TIME_SLOT
            if total_wait >= RCV_WAIT_TIME:
                return "timeout"

    # Packet building
    def _build_packet(self, port, fields):
        pkt = self.packet_template.copy()
        conn = self.pool.get(port)

        tcp_seq = (conn.seq + fields["seq"]) % MOD32
        tcp_ack = (conn.ack + fields["ack"]) % MOD32
        tsval = (int(time.time() * 1000) + fields["timestamp_tsval"]) % MOD32
        tsecr = (conn.tsecr + fields["timestamp_tsecr"]) % MOD32

        options = []
        if fields["timestamp_tsval"] != -1:
            options.append(("Timestamp", (tsval + fields["timestamp_tsval"], tsecr)))
        if fields["mss"] != -1:
            options.append(("MSS", fields["mss"]))
        if fields["wscale"] != -1:
            options.append(("WScale", fields["wscale"]))
        if fields["sackok"] != -1:
            options.append(("SAckOK", ""))

        pkt[TCP].dport = port
        pkt[TCP].seq = tcp_seq
        pkt[TCP].ack = tcp_ack
        pkt[TCP].reserved = fields["reserved"]
        pkt[TCP].flags = fields["flags"]
        pkt[TCP].window = fields["window"]
        pkt[TCP].urgptr = fields["urgptr"]
        pkt[TCP].options = options

        if fields["offset"] != 5:
            pkt[TCP].dataofs = fields["offset"]
        if fields["data"] != 0:
            pkt = pkt / (b"\x00" * fields["data"])

        return pkt

    # Retransmit detection
    def _is_retransmit(self, pkt):
        flags = pkt.tcp_flags
        if flags == "FPA":
            return True
        if flags in ("FA", "SA", "PA"):
            sport = pkt.tcp_src_port
            key = (sport, pkt.tcp_dst_port, pkt.tcp_seq, pkt.tcp_ack, flags)
            conn = self.pool.get(sport)
            if key in conn.history_packets:
                return True
            self._record_history(
                conn, sport, pkt.tcp_dst_port, pkt.tcp_seq, pkt.tcp_ack, flags
            )
            return False
        return False

    @staticmethod
    def _record_history(conn, sport, dport, seq, ack, flags):
        if flags == "FA":
            for i in range(5):
                conn.history_packets.append((sport, dport, seq, ack + i, flags))
                conn.history_packets.append((sport, dport, seq + 1, ack + i, flags))
        else:
            conn.history_packets.append((sport, dport, seq, ack, flags))

    # Rate limiting
    def _wait_rate_limit(self, port, flags):
        if any(f in flags for f in "ARSU"):
            elapsed = time.time() - self.pool.get(port).last_challenge_ack_time
            if elapsed < 1:
                time.sleep(1 - elapsed)

    def _update_rate_limit(self, port, flags):
        if "A" in flags:
            self.pool.get(port).last_challenge_ack_time = time.time()

    def _wait_send_cooldown(self, port):
        elapsed = time.time() - self.pool.get(port).last_send_time
        if elapsed < 10:
            time.sleep(10 - elapsed)


# AALpy SUL interface
class TCP_SUL(SUL):
    """AALpy System Under Learning adapter for TCP."""

    def __init__(self):
        super().__init__()
        self.tcp = TCPExample()

    def pre(self):
        port = self.tcp.get_dst_port()
        self.tcp.pool.add(port)
        return port

    def post(self, port):
        self.tcp.reset(port)
        self.tcp.pool.remove(port)
        time.sleep(0.5)

    def step(self, port, letter):
        time.sleep(0.05)
        command = _SYSCALLS.get(letter)
        if command:
            return self.tcp.action_handler(command, port)
        return self.tcp.process_letter(port, self._parse_letter(letter))

    @staticmethod
    def _parse_letter(letter):
        f = letter.split("_")
        return {
            "seq": int(f[0]),
            "ack": int(f[1]),
            "offset": int(f[2]),
            "reserved": int(f[3]),
            "flags": f[4],
            "window": int(f[5]),
            "urgptr": int(f[6]),
            "data": int(f[7]),
            "timestamp_tsval": int(f[8]),
            "timestamp_tsecr": int(f[9]),
            "mss": int(f[10]),
            "wscale": int(f[11]),
            "sackok": int(f[12]),
        }
