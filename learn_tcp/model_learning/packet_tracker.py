import socket
import time
import logging
from learn_tcp.model_learning.config import SUL_ip, interface
import pcapy  # Provided by pcapy-ng.
import dpkt
from scapy.utils import PcapWriter
from multiprocessing import Process, Event, Queue, Lock, Value, Manager
import sys
import signal
from hydra.runtime import current
from queue import Empty
import threading
from learn_tcp.model_learning import output_dir


class PacketData:
    def __init__(
        self, src_port, dst_port, seq, ack, flags, ts_val, options_flag, data_length
    ):
        self.tcp_src_port = src_port
        self.tcp_dst_port = dst_port
        self.tcp_seq = seq
        self.tcp_ack = ack
        self.tcp_flags = flags
        self.ts_val = ts_val
        self.options_flag_str = options_flag
        self.data_length = data_length


class PacketTracker:
    def __init__(self, max_queue_size=10000, monitor_interval=1, num_processes=8):
        logging.info("init PacketTracker")

        self._closed = False
        self.manager = Manager()
        self.port_to_packets = self.manager.dict()
        self.dict_lock = threading.Lock()

        self.result_queue = Queue(maxsize=max_queue_size)

        self.packet_queue = Queue(maxsize=max_queue_size)

        self.time_lock = Lock()
        self.max_processing_time = Value("d", 0.0)
        self.max_all_time = Value("d", 0.0)
        self.total_processing_time = Value("d", 0.0)
        self.total_all_time = Value("d", 0.0)
        self.processed_packet_count = Value("i", 0)

        self.pcap_writer = PcapWriter(output_dir.PCAP_FILE, append=True, sync=True)

        self.monitor_interval = monitor_interval
        self.monitoring = True

        self.processing_stop_event = Event()
        self.processing_processes = []
        for i in range(num_processes):
            p = Process(
                target=PacketTracker.process_worker,
                name=f"WorkerProcess-{i}",
                args=(self.packet_queue, self.result_queue, self.processing_stop_event),
            )
            p.start()
            self.processing_processes.append(p)

        self.collector_stop_event = threading.Event()
        self.collector_process = Process(
            target=self.result_collector, name="CollectorProcess", args=()
        )
        self.collector_process.start()

        self.monitor_thread = threading.Thread(
            target=self.monitor_queue_size,
            args=(monitor_interval,),
            name="MonitorThread",
            daemon=True,
        )
        self.monitor_thread.start()

        self.stop_event = Event()
        self.sniff_process = Process(
            target=self.sniff_process_func,
            args=(self.packet_queue, self.stop_event),
            daemon=True,
            name="SniffProcess",
        )
        self.sniff_process.start()

    def sniff_process_func(self, packet_queue, stop_event):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            pcap_writer = PcapWriter(output_dir.PCAP_FILE, append=True, sync=True)

            def packet_handler(header, packet):
                try:
                    ts = header.getts()
                    header_data = {
                        "ts_sec": ts[0],
                        "ts_usec": ts[1],
                        "caplen": header.getcaplen(),
                        "len": header.getlen(),
                    }
                    pcap_writer.write(packet)
                    enqueue_time = ts[0] + ts[1] / 1000000.0
                    packet_queue.put_nowait((enqueue_time, header_data, packet))
                except Exception as e:
                    logging.error(f"error in sniff: {e}")

            pcap = pcapy.open_live(interface, 65536, 1, 100)
            pcap.setfilter("tcp dst port 20000")
            logging.info(f"sniff on interface{interface}")
            while not stop_event.is_set():
                pcap.dispatch(1, packet_handler)
        except Exception as e:
            logging.error(f"error {interface}: {e}")
        finally:
            try:
                pcap.close()
            except Exception:
                pass
            pcap_writer.close()

    def add_port(self, port):
        with self.dict_lock:
            if port not in self.port_to_packets:
                self.port_to_packets[port] = []

    def remove_port(self, port):
        with self.dict_lock:
            if port in self.port_to_packets:
                del self.port_to_packets[port]

    def clear_packets(self, port):
        with self.dict_lock:
            if port in self.port_to_packets:
                current_list = self.port_to_packets[port]
                current_list = []
                self.port_to_packets[port] = current_list

    def remove_packet(self, port):
        with self.dict_lock:
            if port in self.port_to_packets:
                current_list = self.port_to_packets[port]
                del current_list[0]
                self.port_to_packets[port] = current_list

    def result_collector(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        logging.info("Result collector process started")
        while not self.collector_stop_event.is_set():
            try:
                port, enqueue_time, end_process_time, packet_info = (
                    self.result_queue.get()
                )

                if port in self.port_to_packets:
                    with self.dict_lock:
                        current_list = self.port_to_packets[port]
                        current_list.append(packet_info)
                        self.port_to_packets[port] = current_list

                    leave_time = time.time()
                    total_processing = end_process_time - enqueue_time
                    total_all = leave_time - enqueue_time

                    if total_processing > self.max_processing_time.value:
                        self.max_processing_time.value = total_processing
                    if total_all > self.max_all_time.value:
                        self.max_all_time.value = total_all
                    self.total_processing_time.value += total_processing
                    self.total_all_time.value += total_all
                    self.processed_packet_count.value += 1
            except Empty:
                continue
            except Exception as e:
                logging.error(f"Result collector thread failed: {e}")

    @staticmethod
    def process_worker(packet_queue, result_queue, processing_stop_event):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        while not processing_stop_event.is_set():
            try:
                enqueue_time, header_data, data = packet_queue.get_nowait()
            except Empty:
                time.sleep(0.001)
                continue
            except Exception as e:
                logging.error(f"[WorkerProcess] : {e}")
                continue

            try:
                eth = dpkt.ethernet.Ethernet(data)
                if not isinstance(eth.data, dpkt.ip.IP):
                    continue
                ip = eth.data
                if socket.inet_ntoa(ip.src) != SUL_ip:
                    continue
                if not isinstance(ip.data, dpkt.tcp.TCP):
                    continue
                tcp = ip.data
                tcp_src_port = tcp.sport
                tcp_dst_port = tcp.dport
                tcp_seq = tcp.seq
                tcp_ack = tcp.ack
                tcp_flags = PacketTracker.parse_tcp_flags(tcp)
                data_length = len(tcp.data)

                ts_val = None
                options_flag = ["0"] * 2
                opts = tcp.opts
                i = 0
                while i < len(opts):
                    kind = opts[i]
                    if kind == dpkt.tcp.TCP_OPT_EOL:
                        break
                    elif kind == dpkt.tcp.TCP_OPT_NOP:
                        i += 1
                        continue
                    else:
                        if i + 1 >= len(opts):
                            break
                        length = opts[i + 1]
                        if kind == dpkt.tcp.TCP_OPT_TIMESTAMP and length >= 10:
                            ts_val = int.from_bytes(
                                opts[i + 2 : i + 10], byteorder="big"
                            )
                            options_flag[0] = "1"
                        elif kind == dpkt.tcp.TCP_OPT_SACK and length >= 2:
                            options_flag[1] = "1"
                        i += length
                options_flag_str = "".join(options_flag)

                end_process_time = time.time()
                result_queue.put_nowait(
                    (
                        tcp_src_port,
                        enqueue_time,
                        end_process_time,
                        PacketData(
                            tcp_src_port,
                            tcp_dst_port,
                            tcp_seq,
                            tcp_ack,
                            tcp_flags,
                            ts_val,
                            options_flag_str,
                            data_length,
                        ),
                    )
                )
            except Exception as e:
                logging.error(f"[WorkerProcess] : {e}")

    @staticmethod
    def parse_tcp_flags(tcp):
        flags = ""
        flags += "F" if (tcp.flags & dpkt.tcp.TH_FIN) else ""
        flags += "S" if (tcp.flags & dpkt.tcp.TH_SYN) else ""
        flags += "R" if (tcp.flags & dpkt.tcp.TH_RST) else ""
        flags += "P" if (tcp.flags & dpkt.tcp.TH_PUSH) else ""
        flags += "A" if (tcp.flags & dpkt.tcp.TH_ACK) else ""
        flags += "U" if (tcp.flags & dpkt.tcp.TH_URG) else ""
        return flags

    def monitor_queue_size(self, monitor_interval):
        while self.monitoring:
            current_packet_size = self.packet_queue.qsize()
            current_result_size = self.result_queue.qsize()
            with self.time_lock:
                processed = self.processed_packet_count.value
                average_processing_time = (
                    self.total_processing_time.value / processed
                    if processed > 0
                    else 0.0
                )
                average_all_time = (
                    self.total_all_time.value / processed if processed > 0 else 0.0
                )
                max_time = self.max_processing_time.value

            pps = processed / monitor_interval
            log_entry = (
                f"Size of result_queue: {current_result_size}, "
                f"Size of packet_queue: {current_packet_size}, "
                f"Max process time: {self.max_processing_time.value * 1000:.3f} ms, "
                f"Max all time: {self.max_all_time.value * 1000:.3f} ms, "
                f"Average processing time: {average_processing_time * 1000:.3f} ms, "
                f"Average all time: {average_all_time * 1000:.3f} ms, "
                f"Number of packets processed last second: {pps:.2f}"
            )
            logging.debug(log_entry)
            current().set(
                packet_queue_depth=current_packet_size,
                result_queue_depth=current_result_size,
                packet_processing_mean_seconds=average_processing_time,
                packet_latency_mean_seconds=average_all_time,
                packet_delivery_rate=pps,
            )

            with self.time_lock:
                self.total_processing_time.value = 0.0
                self.total_all_time.value = 0.0
                self.processed_packet_count.value = 0
                self.max_processing_time.value = 0.0
                self.max_all_time.value = 0.0

            time.sleep(monitor_interval)

    def shutdown(self):
        """Release capture resources after learning has stopped."""
        if self._closed:
            return
        self._closed = True
        self.monitoring = False
        self.processing_stop_event.set()
        self.stop_event.set()
        self.collector_stop_event.set()
        for process in [
            self.sniff_process,
            *self.processing_processes,
            self.collector_process,
        ]:
            process.join(timeout=0.5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
        self.monitor_thread.join(timeout=2)
        self.pcap_writer.close()
        for queue in (self.packet_queue, self.result_queue):
            queue.cancel_join_thread()
            queue.close()
        self.manager.shutdown()


if __name__ == "__main__":
    tracker = PacketTracker(num_processes=4)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        tracker.shutdown()
        sys.exit(0)
