#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <sys/wait.h>
#include <time.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <signal.h>
#include <sys/ioctl.h>

#define CONTROL_PORT 5000
#define MAX_PORT 65535
#define MIN_PORT 10000
#define BUFFER_SIZE 1024
#define MAX_SOCKETS 300
#define HISTORY_LEN 6
#define HISTORY_CMD_LEN 24

int port_offset = 0;
FILE* log_file = NULL;
int current_mappings = 0;
static int g_cmd_seq = 0;          // Monotonically increasing command sequence (single-threaded SUT, fully ordered commands)
static int g_verbose_normal = 0;   // 0 = log problems only; 1 = log all debug messages

// Port-to-socket mapping structure
typedef struct {
    int sul_port;
    int main_socket;
    int secondary_socket;
    int created_seq;                                  // Command sequence at which this mapping was created
    int main_close_count;                             // Number of times main_socket was set to -1
    int sec_close_count;                              // Number of times secondary_socket was set to -1
    int history_total;                                // Total commands executed on this mapping
    char history_cmd[HISTORY_LEN][HISTORY_CMD_LEN];   // Ring buffer: command names
    int  history_seq[HISTORY_LEN];                    // Ring buffer: corresponding command sequences
} PortSocketMapping;

// Port-to-socket mapping array
PortSocketMapping mappings[MAX_SOCKETS];

// Emit ordered diagnostics with UTC timestamps and severity.
void log_message(const char* fmt, ...) {
    time_t now = time(NULL);
    struct tm utc;
    char timestamp[32];
    gmtime_r(&now, &utc);
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc);
    const char *level = strncmp(fmt, "Error:", 6) == 0 || strcmp(fmt, "EXIT_FAILURE") == 0
                        ? "ERROR" : (strncmp(fmt, "Warning:", 8) == 0 ? "WARNING" : "INFO");
    FILE *streams[] = {log_file, stderr};
    for (int i = 0; i < 2; i++) {
        if (streams[i] == NULL) continue;
        fprintf(streams[i], "%s %s sut seq=%d ", timestamp, level, g_cmd_seq);
        va_list args;
        va_start(args, fmt);
        vfprintf(streams[i], fmt, args);
        va_end(args);
        fputc('\n', streams[i]);
    }
}

// Verbose logging, enabled only when g_verbose_normal != 0.
#define VLOG(...) do { if (g_verbose_normal) log_message(__VA_ARGS__); } while (0)

// Initialize the mapping array
void initialize_mappings() {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        mappings[i].sul_port = -1;
        mappings[i].main_socket = -1;
        mappings[i].secondary_socket = -1;
        mappings[i].created_seq = -1;
        mappings[i].main_close_count = 0;
        mappings[i].sec_close_count = 0;
        mappings[i].history_total = 0;
        for (int k = 0; k < HISTORY_LEN; k++) {
            mappings[i].history_cmd[k][0] = '\0';
            mappings[i].history_seq[k] = -1;
        }
    }
}

// Append command history to the mapping for sul_port without logging
void record_history(int sul_port, const char* cmd) {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) {
            int idx = mappings[i].history_total % HISTORY_LEN;
            strncpy(mappings[i].history_cmd[idx], cmd, HISTORY_CMD_LEN - 1);
            mappings[i].history_cmd[idx][HISTORY_CMD_LEN - 1] = '\0';
            mappings[i].history_seq[idx] = g_cmd_seq;
            mappings[i].history_total++;
            return;
        }
    }
}

// Log all mapping diagnostics for the current port when a problem occurs
void dump_mapping_diagnostic(const char* reason, int sul_port) {
    int slot_idx = -1;
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) { slot_idx = i; break; }
    }
    log_message("====== Diagnostics [%s] [seq %d] port %d ======", reason, g_cmd_seq, sul_port);
    log_message("  current_mappings = %d / %d", current_mappings, MAX_SOCKETS);
    if (slot_idx < 0) {
        log_message("  Slot: not found (mapping may have been released by reset or not allocated by port)");
    } else {
        PortSocketMapping* m = &mappings[slot_idx];
        log_message("  Slot %d: main=%d, sec=%d", slot_idx, m->main_socket, m->secondary_socket);
        log_message("  Created at seq %d, main set to -1 %d times, sec set to -1 %d times, total commands %d",
                    m->created_seq, m->main_close_count, m->sec_close_count, m->history_total);
        int show = m->history_total < HISTORY_LEN ? m->history_total : HISTORY_LEN;
        log_message("  Last %d commands (chronological order, oldest -> newest):", show);
        int start = m->history_total - show;
        for (int k = 0; k < show; k++) {
            int idx = (start + k) % HISTORY_LEN;
            log_message("    [seq %d] %s", m->history_seq[idx], m->history_cmd[idx]);
        }
    }
    log_message("====== End diagnostics ======");
}

// Add a new port-to-main-socket mapping
void add_new_mapping(int sul_port, int main_socket) {
    if (current_mappings >= MAX_SOCKETS) {
        log_message("Error: [seq %d] Maximum socket count reached (%d).", g_cmd_seq, MAX_SOCKETS);
        log_message("EXIT_FAILURE");
        exit(EXIT_FAILURE);
    }

    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == -1) {
            mappings[i].sul_port = sul_port;
            mappings[i].main_socket = main_socket;
            mappings[i].secondary_socket = -1;
            mappings[i].created_seq = g_cmd_seq;
            mappings[i].main_close_count = 0;
            mappings[i].sec_close_count = 0;
            mappings[i].history_total = 0;
            for (int k = 0; k < HISTORY_LEN; k++) {
                mappings[i].history_cmd[k][0] = '\0';
                mappings[i].history_seq[k] = -1;
            }
            current_mappings++;
            VLOG("Added mapping: slot %d, port %d, main socket %d.", i, sul_port, main_socket);
            return;
        }
    }

    log_message("Error: [seq %d] Cannot add mapping (current_mappings=%d, no free slots).",
                g_cmd_seq, current_mappings);
    log_message("EXIT_FAILURE");
    exit(EXIT_FAILURE);
}

// Update the main socket mapping
void update_main_socket_mapping(int sul_port, int main_socket) {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) {
            int prev = mappings[i].main_socket;
            mappings[i].main_socket = main_socket;
            if (main_socket == -1 && prev != -1) {
                mappings[i].main_close_count++;
            }
            VLOG("Updated main socket mapping: slot %d, port %d, %d -> %d.", i, sul_port, prev, main_socket);
            return;
        }
    }
    log_message("Warning: [seq %d] Attempted to update a missing main socket mapping: port %d.", g_cmd_seq, sul_port);
}

// Update the secondary socket mapping
void update_secondary_socket_mapping(int sul_port, int secondary_socket) {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) {
            int prev = mappings[i].secondary_socket;
            mappings[i].secondary_socket = secondary_socket;
            if (secondary_socket == -1 && prev != -1) {
                mappings[i].sec_close_count++;
            }
            VLOG("Updated secondary socket mapping: slot %d, port %d, %d -> %d.", i, sul_port, prev, secondary_socket);
            return;
        }
    }
    log_message("Warning: [seq %d] Attempted to update a missing secondary socket mapping: port %d.", g_cmd_seq, sul_port);
}

// Remove a mapping by port number
void remove_mapping_by_port(int sul_port) {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) {
            VLOG("Removed mapping: slot %d, port %d.", i, sul_port);
            mappings[i].sul_port = -1;
            mappings[i].main_socket = -1;
            mappings[i].secondary_socket = -1;
            mappings[i].created_seq = -1;
            mappings[i].main_close_count = 0;
            mappings[i].sec_close_count = 0;
            mappings[i].history_total = 0;
            for (int k = 0; k < HISTORY_LEN; k++) {
                mappings[i].history_cmd[k][0] = '\0';
                mappings[i].history_seq[k] = -1;
            }
            current_mappings--;
            return;
        }
    }
    log_message("Warning: [seq %d] Attempted to remove a missing mapping: port %d.", g_cmd_seq, sul_port);
}

int get_main_socket_by_port(int sul_port) {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) {
            return mappings[i].main_socket;
        }
    }
    log_message("Error: [seq %d] Main socket not found for port %d.", g_cmd_seq, sul_port);
    dump_mapping_diagnostic("get_main_socket_by_port: port not found", sul_port);
    log_message("EXIT_FAILURE");
    exit(EXIT_FAILURE);
    return -1;
}

int get_secondary_socket_by_port(int sul_port) {
    for (int i = 0; i < MAX_SOCKETS; i++) {
        if (mappings[i].sul_port == sul_port) {
            return mappings[i].secondary_socket;
        }
    }
    log_message("Error: [seq %d] Secondary socket not found for port %d.", g_cmd_seq, sul_port);
    dump_mapping_diagnostic("get_secondary_socket_by_port: port not found", sul_port);
    log_message("EXIT_FAILURE");
    exit(EXIT_FAILURE);
    return -1;
}

int generate_port() {
    port_offset++;
    return (port_offset % (MAX_PORT - MIN_PORT + 1)) + MIN_PORT;
}

// Set the socket to nonblocking mode
void set_socket_nonblocking(int socket_fd) {
    if (socket_fd == -1) return;

    int flags = fcntl(socket_fd, F_GETFL, 0);
    if (flags == -1) {
        log_message("Warning: [seq %d] fcntl(F_GETFL) failed, fd %d: %s", g_cmd_seq, socket_fd, strerror(errno));
        return;
    }
    if (fcntl(socket_fd, F_SETFL, flags | O_NONBLOCK) == -1) {
        log_message("Warning: [seq %d] fcntl(F_SETFL) failed, fd %d: %s", g_cmd_seq, socket_fd, strerror(errno));
    } else {
        VLOG("Debug: socket %d set to nonblocking mode.", socket_fd);
    }
}

// Set common socket options
void set_socket_options(int socket_fd) {
    if (socket_fd == -1) return;

    int flag = 1;
    if (setsockopt(socket_fd, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag)) < 0) {
        log_message("Warning: [seq %d] Failed to set TCP_NODELAY, socket %d: %s", g_cmd_seq, socket_fd, strerror(errno));
    }
    if (setsockopt(socket_fd, SOL_SOCKET, SO_REUSEADDR, &flag, sizeof(flag)) < 0) {
        log_message("Warning: [seq %d] Failed to set SO_REUSEADDR, socket %d: %s", g_cmd_seq, socket_fd, strerror(errno));
    }
    if (setsockopt(socket_fd, SOL_SOCKET, SO_KEEPALIVE, &flag, sizeof(flag)) < 0) {
        log_message("Warning: [seq %d] Failed to set SO_KEEPALIVE, socket %d: %s", g_cmd_seq, socket_fd, strerror(errno));
    }
    VLOG("Set socket options, socket %d", socket_fd);
}

// Handle connection acceptance
void handle_accept(int sul_port, int main_socket) {
    if (main_socket == -1) {
        log_message("Warning: [seq %d] Skipped accept, port %d main socket is -1.", g_cmd_seq, sul_port);
        dump_mapping_diagnostic("handle_accept: main_socket == -1", sul_port);
        return;
    }

    set_socket_nonblocking(main_socket);

    VLOG("Port %d: handling accept.", sul_port);

    struct sockaddr_in client_addr;
    socklen_t client_len = sizeof(client_addr);
    int secondary_socket = -1;
    while (1){
        int tmp_socket = accept(main_socket, (struct sockaddr *)&client_addr, &client_len);
        if (tmp_socket == -1){
            break;
        } else {
            secondary_socket = tmp_socket;
        }
    }

    if (secondary_socket == -1) {
        // EAGAIN/EWOULDBLOCK is expected when no connections are pending
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            log_message("Warning: [seq %d] Port %d: accept failed: %s", g_cmd_seq, sul_port, strerror(errno));
        } else {
            VLOG("Port %d: accept found no new connection (EAGAIN/EWOULDBLOCK).", sul_port);
        }
    } else {
        VLOG("Port %d: accept succeeded, secondary socket %d.", sul_port, secondary_socket);

        // Close the previous secondary socket
        int old_secondary = get_secondary_socket_by_port(sul_port);
        if (old_secondary != -1) {
            close(old_secondary);
            VLOG("Debug: closed previous secondary socket %d.", old_secondary);
        }

        // Update the secondary socket mapping
        update_secondary_socket_mapping(sul_port, secondary_socket);

        // Set secondary socket options
        set_socket_options(secondary_socket);
    }
}

// Handle data reception
void handle_receive(int secondary_socket) {
    if (secondary_socket == -1) {
        log_message("Warning: [seq %d] Skipped rcv, secondary socket is -1.", g_cmd_seq);
        return;
    }

    char buffer[1400];
    ssize_t bytes_received = recv(secondary_socket, buffer, sizeof(buffer) - 1, MSG_DONTWAIT);
    if (bytes_received > 0) {
        buffer[bytes_received] = '\0';
        VLOG("Received nonempty data from secondary socket %d: %s", secondary_socket, buffer);
    } else if (bytes_received == 0) {
        VLOG("Received empty data from secondary socket %d.", secondary_socket);
        // Handle connection closure
    } else {
        if (errno != EWOULDBLOCK && errno != EAGAIN) {
            log_message("Warning: [seq %d] Failed to receive data from secondary socket %d: %s", g_cmd_seq, secondary_socket, strerror(errno));
        }
    }
}

// Handle data transmission
void handle_send(int secondary_socket) {
    if (secondary_socket == -1) {
        log_message("Warning: [seq %d] Skipped send, secondary socket is -1.", g_cmd_seq);
        return;
    }

    const char* message = "Hello";

    ssize_t sent_bytes = send(secondary_socket, message, strlen(message), 0);


    if (sent_bytes < 0) {
        log_message("Warning: [seq %d] Failed to send data, secondary socket %d: %s", g_cmd_seq, secondary_socket, strerror(errno));
    } else {
        VLOG("Secondary socket %d sent %ld bytes: '%s'", secondary_socket, sent_bytes, message);
    }


}

// Close the main socket
void close_main_socket(int main_socket, int sul_port) {
    if (main_socket == -1) {
        log_message("Warning: [seq %d] Skipped close, port %d main socket is already -1.", g_cmd_seq, sul_port);
        dump_mapping_diagnostic("close_main_socket: already -1", sul_port);
        return;
    }

    if (close(main_socket) == 0) {
        VLOG("Successfully closed main socket %d.", main_socket);
        update_main_socket_mapping(sul_port, -1);
    } else {
        log_message("Warning: [seq %d] Failed to close main socket %d: %s", g_cmd_seq, main_socket, strerror(errno));
    }
}

// Close the secondary socket
void close_secondary_socket(int secondary_socket, int sul_port) {
    if (secondary_socket == -1) {
        log_message("Warning: [seq %d] Skipped closeconnection, port %d secondary socket is already -1.", g_cmd_seq, sul_port);
        dump_mapping_diagnostic("close_secondary_socket: already -1", sul_port);
        return;
    }

    if (close(secondary_socket) == 0) {
        VLOG("Successfully closed secondary socket %d.", secondary_socket);
        update_secondary_socket_mapping(sul_port, -1);
    } else {
        log_message("Warning: [seq %d] Failed to close secondary socket %d: %s", g_cmd_seq, secondary_socket, strerror(errno));
    }
}

// Reset the connection
void reset_connection(int sul_port, int main_socket, int secondary_socket) {
    if (main_socket != -1) {
        close(main_socket);
    }
    if (secondary_socket != -1) {
        close(secondary_socket);
    }
    remove_mapping_by_port(sul_port);
    VLOG("Reset connection, port %d.", sul_port);
}

// Read the message byte by byte until a newline
char* receive_message(int control_socket) {
    static char buffer[BUFFER_SIZE];
    size_t message_length = 0;
    ssize_t bytes_received;

    while (message_length < BUFFER_SIZE - 1) {
        bytes_received = recv(control_socket, &buffer[message_length], 1, 0);
        if (bytes_received <= 0) {
            break;
        }
        if (buffer[message_length] == '\n') {
            message_length++;
            break;
        }
        message_length++;
    }

    buffer[message_length] = '\0';

    char *dynamic_buffer = (char *)malloc((message_length + 1) * sizeof(char));
    if (dynamic_buffer == NULL) {
        log_message("Error: [seq %d] Memory allocation failed.", g_cmd_seq);
        log_message("EXIT_FAILURE");
        exit(EXIT_FAILURE);
    }
    strcpy(dynamic_buffer, buffer);
    return dynamic_buffer;
}

// Handle control commands
void handle_control_command(int control_socket) {
    g_cmd_seq++;

    VLOG("---------------------------------------------------------------------------------");

    char* command = receive_message(control_socket);
    VLOG("[seq %d] Received command: %s", g_cmd_seq, command);
    VLOG("Current mapping count: %d", current_mappings);

    if (strcmp(command, "port\n") == 0) {
        VLOG("[seq %d] Processing command: port", g_cmd_seq);

        int attempts = 0;
        while (attempts < 10) {
            attempts++;
            int sul_port = generate_port();
            int main_socket = socket(AF_INET, SOCK_STREAM, 0);

            if (main_socket < 0) {
                log_message("Warning: [seq %d] Failed to create main socket: %s", g_cmd_seq, strerror(errno));
                continue;
            }

            set_socket_options(main_socket);

            struct sockaddr_in server_addr;
            memset(&server_addr, 0, sizeof(server_addr));
            server_addr.sin_family = AF_INET;
            server_addr.sin_addr.s_addr = INADDR_ANY;
            server_addr.sin_port = htons(sul_port);

            if (bind(main_socket, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
                log_message("Warning: [seq %d] Failed to bind port: %d, error: %s", g_cmd_seq, sul_port, strerror(errno));
                close(main_socket);
                continue;
            }

            add_new_mapping(sul_port, main_socket);
            VLOG("Bound port %d, main socket %d.", sul_port, main_socket);

            char port_str[16];
            snprintf(port_str, sizeof(port_str), "%d\n", sul_port);
            send(control_socket, port_str, strlen(port_str), 0);
            VLOG("Sent new port number: %d.", sul_port);
            break;
        }

        if (attempts == 10) {
            log_message("Error: [seq %d] No port numbers available.", g_cmd_seq);
            log_message("EXIT_FAILURE");
            exit(EXIT_FAILURE);
        }

    } else {
        // Handle other commands
        char* port_str = receive_message(control_socket);
        if (strlen(port_str) > 0) {
            port_str[strlen(port_str) - 1] = '\0'; // Remove the newline
        }
        int sul_port = atoi(port_str);
        int main_socket = get_main_socket_by_port(sul_port);
        int secondary_socket = get_secondary_socket_by_port(sul_port);

        VLOG("[seq %d] Port %d, main socket %d, secondary socket %d", g_cmd_seq, sul_port, main_socket, secondary_socket);

        if (strcmp(command, "listen\n") == 0) {
            VLOG("[seq %d] Processing command: listen", g_cmd_seq);
            if (listen(main_socket, 1) == 0) {
                VLOG("Port %d is listening.", sul_port);
            } else {
                log_message("Error: [seq %d] listen failed, port %d, fd %d: %s",
                            g_cmd_seq, sul_port, main_socket, strerror(errno));
                dump_mapping_diagnostic("listen failed", sul_port);
            }
            record_history(sul_port, "listen");

        } else if (strcmp(command, "accept\n") == 0) {
            VLOG("[seq %d] Processing command: accept, port %d", g_cmd_seq, sul_port);
            handle_accept(sul_port, main_socket);
            VLOG("Port %d: accept completed.", sul_port);
            record_history(sul_port, "accept");

        } else if (strcmp(command, "rcv\n") == 0) {
            VLOG("[seq %d] Processing command: rcv, port %d", g_cmd_seq, sul_port);
            handle_receive(secondary_socket);
            VLOG("Port %d: rcv completed.", sul_port);
            record_history(sul_port, "rcv");

        } else if (strcmp(command, "send\n") == 0) {
            VLOG("[seq %d] Processing command: send, port %d", g_cmd_seq, sul_port);
            handle_send(secondary_socket);
            VLOG("Port %d: send completed.", sul_port);
            record_history(sul_port, "send");

        } else if (strcmp(command, "close\n") == 0) {
            VLOG("[seq %d] Processing command: close, port %d", g_cmd_seq, sul_port);
            close_main_socket(main_socket, sul_port);
            VLOG("Port %d: close completed.", sul_port);
            record_history(sul_port, "close");

        } else if (strcmp(command, "closeconnection\n") == 0) {
            VLOG("[seq %d] Processing command: closeconnection, port %d", g_cmd_seq, sul_port);
            close_secondary_socket(secondary_socket, sul_port);
            VLOG("Port %d: closeconnection completed.", sul_port);
            record_history(sul_port, "closeconnection");

        } else if (strcmp(command, "reset\n") == 0) {
            VLOG("[seq %d] Processing command: reset, port %d", g_cmd_seq, sul_port);
            record_history(sul_port, "reset");
            reset_connection(sul_port, main_socket, secondary_socket);
            VLOG("Port %d: reset completed.", sul_port);

        } else {
            log_message("Warning: [seq %d] Received unknown command: %s", g_cmd_seq, command);
        }

        free(port_str);
    }

    free(command);
}

int main(int argc, char **argv) {
    const char *log_path = "sut.log";
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--verbose") == 0) g_verbose_normal = 1;
        else if (strcmp(argv[i], "--log") == 0 && i + 1 < argc) log_path = argv[++i];
        else if (strcmp(argv[i], "--help") == 0) {
            puts("Usage: hydra-sut [--log PATH] [--verbose]");
            return 0;
        } else { fprintf(stderr, "Unknown or incomplete option: %s\n", argv[i]); return 2; }
    }
    int server_socket, control_socket;
    struct sockaddr_in server_addr, client_addr;
    socklen_t client_len = sizeof(client_addr);

    log_file = fopen(log_path, "a");
    if (log_file == NULL) {
        perror("Cannot open SUT log");
        return EXIT_FAILURE;
    }
    VLOG("Logging initialized.");

    initialize_mappings();
    VLOG("Socket mappings initialized.");

    server_socket = socket(AF_INET, SOCK_STREAM, 0);
    if (server_socket < 0) {
        log_message("Error: Failed to create socket: %s", strerror(errno));
        log_message("EXIT_FAILURE");
        fclose(log_file);
        exit(EXIT_FAILURE);
    }

    set_socket_options(server_socket);

    memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sin_family = AF_INET;
    server_addr.sin_addr.s_addr = INADDR_ANY;
    server_addr.sin_port = htons(CONTROL_PORT);

    if (bind(server_socket, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
        log_message("Error: Bind failed: %s", strerror(errno));
        close(server_socket);
        log_message("EXIT_FAILURE");
        fclose(log_file);
        exit(EXIT_FAILURE);
    }

    if (listen(server_socket, 5) < 0) {
        log_message("Error: Listen failed: %s", strerror(errno));
        close(server_socket);
        log_message("EXIT_FAILURE");
        fclose(log_file);
        exit(EXIT_FAILURE);
    }

    log_message("Server is listening on control port %d.", CONTROL_PORT);


    control_socket = accept(server_socket, (struct sockaddr *)&client_addr, &client_len);
    if (control_socket < 0) {
        log_message("Error: Failed to accept connection: %s", strerror(errno));
        close(server_socket);
        log_message("EXIT_FAILURE");
        fclose(log_file);
        exit(EXIT_FAILURE);
    }

    log_message("Connected to client.");


    while (1) {
        clock_t start_time = clock(); // Record the start time

        handle_control_command(control_socket);

        clock_t end_time = clock(); // Record the end time
        double time_spent = (double)(end_time - start_time) / CLOCKS_PER_SEC;
        VLOG("Elapsed time: %.6f seconds", time_spent);

    }

    close(control_socket);
    close(server_socket);
    fclose(log_file);

    return 0;
}
