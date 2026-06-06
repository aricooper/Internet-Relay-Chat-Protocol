#!/usr/bin/env python3
"""
server.py

- Listens on TCP port 7734
- Accepts clients, expects a HELLO packet first
- Tracks connected clients and rooms
- Provides helper functions for encoding/decoding packets

"""

import socket
import struct
import threading
import time

# =========================
# Protocol constants
# =========================

PORT = 7734
VERSION_MAGIC = 0xFACE0FF1

# Server limits (for error codes)
MAX_USERS = 100
MAX_ROOMS = 50
MAX_MSG_LEN = 8000  # bytes (after the 20-byte label)

# Opcodes
IRC_OPCODE_ERR             = 0x10000001
IRC_OPCODE_KEEPALIVE       = 0x10000002
IRC_OPCODE_HELLO           = 0x10000003
IRC_OPCODE_LIST_ROOMS      = 0x10000004
IRC_OPCODE_LIST_ROOMS_RESP = 0x10000005
IRC_OPCODE_LIST_USERS_RESP = 0x10000006
IRC_OPCODE_JOIN_ROOM       = 0x10000007
IRC_OPCODE_LEAVE_ROOM      = 0x10000008
IRC_OPCODE_SEND_MSG        = 0x10000009
IRC_OPCODE_TELL_MSG        = 0x10000010
IRC_OPCODE_SEND_PRIV_MSG   = 0x10000011
IRC_OPCODE_TELL_PRIV_MSG   = 0x10000012
IRC_OPCODE_LIST_USERS      = 0x10000013

# Error codes
IRC_ERR_UNKNOWN         = 0x20000001
IRC_ERR_ILLEGAL_OPCODE  = 0x20000002
IRC_ERR_ILLEGAL_LENGTH  = 0x20000003
IRC_ERR_WRONG_VERSION   = 0x20000004
IRC_ERR_NAME_EXISTS     = 0x20000005
IRC_ERR_ILLEGAL_NAME    = 0x20000006
IRC_ERR_ILLEGAL_MESSAGE = 0x20000007
IRC_ERR_TOO_MANY_USERS  = 0x20000008
IRC_ERR_TOO_MANY_ROOMS  = 0x20000009

# Packet header: opcode (uint32), length (uint32) in network byte order
HEADER_STRUCT = struct.Struct("!II")


# =========================
# Utility helpers
# =========================

def encode_label(name: str) -> bytes:
    """
    Encode a user/room label into 20 bytes with RFC :
      - 1..20 printable ASCII chars (0x20-0x7E)
      - no leading/trailing space
      - null-terminated if shorter than 20, rest nulls
    """
    name = name.strip()
    if not (1 <= len(name) <= 20):
        raise ValueError("Label must be 1..20 characters")

    data = name.encode("ascii", errors="replace")
    if len(data) > 20:
        data = data[:20]

    if len(data) < 20:
        data = data + b"\x00" + b"\x00" * (19 - len(data))

    return data


def decode_label(b: bytes) -> str:
    """
    Decode a 20-byte label: interpret up to first null as the name.
    """
    if len(b) != 20:
        raise ValueError("Label must be 20 bytes")
    s = b.split(b"\x00", 1)[0]
    return s.decode("ascii", errors="replace")


def send_packet(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """
    Send a packet with header + payload.
      header.opcode = opcode
      header.length = len(payload)
    """
    header = HEADER_STRUCT.pack(opcode, len(payload))
    sock.sendall(header + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    Read exactly n bytes from a socket, or raise ConnectionError
    if the connection closes early.
    """
    chunks = []
    total = 0
    while total < n:
        chunk = sock.recv(n - total)
        if not chunk:
            raise ConnectionError("Socket closed during recv")
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def recv_packet(sock: socket.socket):
    """
    Receive one packet from a socket.
    Returns: (opcode, payload_bytes)
    """
    header_bytes = recv_exact(sock, HEADER_STRUCT.size)
    opcode, length = HEADER_STRUCT.unpack(header_bytes)
    payload = recv_exact(sock, length) if length > 0 else b""
    return opcode, payload


def build_error_payload(error_code: int) -> bytes:
    """
    Build the payload for an irc_pkt_error:
      struct irc_pkt_error {
        irc_pkt_header header;  // already sent by send_packet
        uint32_t error_code;
      }
    """
    return struct.pack("!I", error_code)


# =========================
# Server implementation
# =========================

class IRCServer:
    """
    Shared state:
      - clients_by_name: name -> (socket, addr)
      - rooms: room_name -> set(user_names)
    """

    def __init__(self, host="0.0.0.0", port=PORT):
        self.host = host
        self.port = port
        self.server_sock = None

        self.lock = threading.Lock()
        self.clients_by_name = {}  # str -> (socket, addr)
        self.rooms = {}            # str -> set(str)

    def start(self):
        """
        Start listening for incoming client connections and spawn a
        handler thread per client.
        """
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(16)

        print(f"[SERVER] Listening on {self.host}:{self.port}")

        try:
            while True:
                client_sock, addr = self.server_sock.accept()
                print(f"[SERVER] New connection from {addr}")
                t = threading.Thread(target=self.handle_client,
                                     args=(client_sock, addr),
                                     daemon=True)
                t.start()
        except KeyboardInterrupt:
            print("\n[SERVER] Shutting down.")
        finally:
            self.server_sock.close()

    def handle_client(self, sock: socket.socket, addr):
        """
        Handle a single client:
          - Expect HELLO first
          - Register user name
          - Event loop: receive packets and dispatch handlers
        """
        user_name = None

        try:
            # ---- 1. Expect HELLO packet ----
            opcode, payload = recv_packet(sock)
            if opcode != IRC_OPCODE_HELLO:
                print("[SERVER] First packet was not HELLO; closing connection.")
                send_packet(sock, IRC_OPCODE_ERR,
                            build_error_payload(IRC_ERR_ILLEGAL_OPCODE))
                return

            # HELLO payload: uint32_t ver_magic; char chat_name[20];
            if len(payload) != 24:
                print("[SERVER] Invalid HELLO length.")
                send_packet(sock, IRC_OPCODE_ERR,
                            build_error_payload(IRC_ERR_ILLEGAL_LENGTH))
                return

            ver_magic, = struct.unpack("!I", payload[:4])
            chat_name = decode_label(payload[4:24])

            # Check version
            if ver_magic != VERSION_MAGIC:
                print("[SERVER] Wrong protocol version from client.")
                send_packet(sock, IRC_OPCODE_ERR,
                            build_error_payload(IRC_ERR_WRONG_VERSION))
                return

            # Register name (must be unique)
            with self.lock:
                if chat_name in self.clients_by_name:
                    print(f"[SERVER] Name already in use: {chat_name}")
                    send_packet(sock, IRC_OPCODE_ERR,
                                build_error_payload(IRC_ERR_NAME_EXISTS))
                    return
                self.clients_by_name[chat_name] = (sock, addr)

            user_name = chat_name
            print(f"[SERVER] Registered user '{user_name}' from {addr}")

            # ---- 2. Main receive loop ----
            while True:
                opcode, payload = recv_packet(sock)

                if opcode == IRC_OPCODE_KEEPALIVE:
                    continue

                elif opcode == IRC_OPCODE_LIST_ROOMS:
                    self.handle_list_rooms(sock)

                elif opcode == IRC_OPCODE_LIST_USERS:
                    self.handle_list_users(sock, payload)

                elif opcode == IRC_OPCODE_JOIN_ROOM:
                    self.handle_join_room(user_name, payload)

                elif opcode == IRC_OPCODE_LEAVE_ROOM:
                    self.handle_leave_room(user_name, payload)

                elif opcode in (IRC_OPCODE_SEND_MSG, IRC_OPCODE_SEND_PRIV_MSG):
                    self.handle_send_msg(user_name, opcode, payload)

                else:
                    print(f"[SERVER] Unknown opcode {opcode:#x} from {user_name}")
                    self.send_error_to_user(user_name, IRC_ERR_ILLEGAL_OPCODE)
                    break

        except ConnectionError:
            print(f"[SERVER] Connection lost from {addr} (user={user_name})")

        finally:
            # Cleanup on disconnect
            if user_name is not None:
                self.cleanup_user(user_name)
            try:
                sock.close()
            except OSError:
                pass

    # ======= Handlers =======

    def handle_list_rooms(self, sock: socket.socket):
        """
        Respond to IRC_OPCODE_LIST_ROOMS with IRC_OPCODE_LIST_ROOMS_RESP.

        Payload format for response:
          struct irc_pkt_list_resp {
              irc_pkt_header header;
              char identifier[20];       // e.g., "rooms"
              char item_names[][20];     // room labels
          }

        """
        with self.lock:
            room_names = [r for r, members in self.rooms.items() if members]

        identifier = encode_label("rooms")  # arbitrary identifier
        payload = identifier
        for name in room_names:
            payload += encode_label(name)

        send_packet(sock, IRC_OPCODE_LIST_ROOMS_RESP, payload)

    def handle_list_users(self, sock, payload):
        """
        Handle IRC_OPCODE_LIST_USERS.

        Payload format for response:
          struct irc_pkt_list_resp {
              irc_pkt_header header;
              char identifier[20];       // e.g., "rooms"
              char item_names[][20];     // user labels
          }

        Behavior:
          - list members of room
          - Respond with IRC_OPCODE_LIST_USERS_RESP
        """
        if len(payload) != 20:
            print("[SERVER] LIST_USERS: invalid payload length")
            return
        
        room_name = decode_label(payload)

        with self.lock:
            members = list(self.rooms.get(room_name, []))

        payload = encode_label(room_name)
        for u in members:
            payload += encode_label(u)

        send_packet(sock, IRC_OPCODE_LIST_USERS_RESP, payload)


    def handle_join_room(self, user_name: str, payload: bytes):
        """
        Handle IRC_OPCODE_JOIN_ROOM.

        Payload:
          char room_name[20];

        Behavior:
          - add user to room (creating it if needed)
          - send IRC_OPCODE_LIST_USERS_RESP to all users in that room
        """
        if len(payload) != 20:
            # length is wrong => error
            print("[SERVER] JOIN_ROOM: invalid payload length")
            return

        room_name = decode_label(payload)

        if not self.is_valid_name(room_name):
            print(f"[SERVER] JOIN_ROOM: illegal room name {room_name!r}")
            self.send_error_to_user(user_name, IRC_ERR_ILLEGAL_NAME)
            return

        with self.lock:
            # If this is a new room, enforce room limit
            if room_name not in self.rooms and len(self.rooms) >= MAX_ROOMS:
                print("[SERVER] JOIN_ROOM: too many rooms, rejecting creation")
                self.send_error_to_user(user_name, IRC_ERR_TOO_MANY_ROOMS)
                return

        members = self.rooms.setdefault(room_name, set())
        members.add(user_name)

        print(f"[SERVER] {user_name} joined room '{room_name}'")
        self.send_room_membership_update(room_name)

    def handle_leave_room(self, user_name: str, payload: bytes):
        """
        Handle IRC_OPCODE_LEAVE_ROOM.

        Payload:
          char room_name[20];

        Behavior:
          - remove user from room if present
          - drop room if empty
          - send IRC_OPCODE_LIST_USERS_RESP to all remaining users
        """
        if len(payload) != 20:
            print("[SERVER] LEAVE_ROOM: invalid payload length")
            return

        room_name = decode_label(payload)

        with self.lock:
            members = self.rooms.get(room_name)
            if not members or user_name not in members:
                # RFC: ignore leaves for rooms user is not in
                return
            members.remove(user_name)
            if not members:
                del self.rooms[room_name]

        print(f"[SERVER] {user_name} left room '{room_name}'")
        if room_name in self.rooms:
            self.send_room_membership_update(room_name)

    def handle_send_msg(self, user_name: str, opcode: int, payload: bytes):
        """
        Handle:
          - IRC_OPCODE_SEND_MSG (room broadcast)
          - IRC_OPCODE_SEND_PRIV_MSG (private message)

        Payload:
          char target_name[20];
          char msg[LENGTH - 20];

        """
        if len(payload) < 20:
            print("[SERVER] SEND_MSG: invalid payload length")
            return

        target_name = decode_label(payload[:20])
        msg_bytes = payload[20:]

        # Enforce max length
        if len(msg_bytes) > MAX_MSG_LEN:
            print("[SERVER] SEND_MSG: message too long from", user_name)
            self.send_error_to_user(user_name, IRC_ERR_ILLEGAL_MESSAGE)
            return

        # Enforce simple ASCII constraints (printable + newline)
        for b in msg_bytes:
            if not (b == 0x0A or 0x20 <= b <= 0x7E):
                print("[SERVER] SEND_MSG: illegal message character from", user_name)
                self.send_error_to_user(user_name, IRC_ERR_ILLEGAL_MESSAGE)
                return

        if opcode == IRC_OPCODE_SEND_MSG:
            # Message to room
            with self.lock:
                members = list(self.rooms.get(target_name, []))
                sockets = [self.clients_by_name[u][0] for u in members
                        if u in self.clients_by_name]

            print(f"[SERVER] Room message to '{target_name}' from '{user_name}': "
                  f"{msg_bytes.decode(errors='replace')!r}")

            out_payload = encode_label(target_name) + encode_label(user_name) + msg_bytes
            for s in sockets:
                send_packet(s, IRC_OPCODE_TELL_MSG, out_payload)

        else:
            # Private message to user
            with self.lock:
                dest = self.clients_by_name.get(target_name)

            if not dest:
                print(f"[SERVER] Private target '{target_name}' does not exist")
                # You may want to send an error, but RFC doesn't specify a code for this.
                return

            dest_sock, _ = dest
            print(f"[SERVER] Private message to '{target_name}' from '{user_name}': "
                  f"{msg_bytes.decode(errors='replace')!r}")

            out_payload = encode_label(target_name) + encode_label(user_name) + msg_bytes
            send_packet(dest_sock, IRC_OPCODE_TELL_PRIV_MSG, out_payload)

    def send_room_membership_update(self, room_name: str):
        """
        Send IRC_OPCODE_LIST_USERS_RESP to all users in a room whenever
        membership changes.

        Payload:
          char identifier[20];     // room name
          char item_names[][20];   // user names in that room
        """
        with self.lock:
            members = list(self.rooms.get(room_name, []))
            payload = encode_label(room_name)
            for u in members:
                payload += encode_label(u)
            sockets = [self.clients_by_name[u][0] for u in members
                       if u in self.clients_by_name]

        for s in sockets:
            send_packet(s, IRC_OPCODE_LIST_USERS_RESP, payload)

    def cleanup_user(self, user_name: str):
        """
        Remove user from global state and all rooms when they disconnect.
        """
        with self.lock:
            print(f"[SERVER] Cleaning up user '{user_name}'")
            self.clients_by_name.pop(user_name, None)

            for room, members in list(self.rooms.items()):
                if user_name in members:
                    members.remove(user_name)
                    if not members:
                        del self.rooms[room]
                    else:
                        self.send_room_membership_update(room)


    # helpers for validation / errors

    @staticmethod
    def is_valid_name(name: str) -> bool:
        """
        Check if a user/room name is valid according to simple rules:
          - 1..20 characters (encode_label enforces this too)
          - ASCII printable (space .. '~')
        """
        if not (1 <= len(name) <= 20):
            return False
        for ch in name:
            if not (0x20 <= ord(ch) <= 0x7E):
                return False
        return True

    def send_error_to_sock(self, sock: socket.socket, error_code: int):
        """Send an IRC_OPCODE_ERR with the given code to this socket."""
        try:
            send_packet(sock, IRC_OPCODE_ERR, build_error_payload(error_code))
        except OSError:
            pass

    def send_error_to_user(self, user_name: str, error_code: int):
        """
        Look up a user's socket and send an error.
        Safe to call from any handler that only has user_name.
        """
        with self.lock:
            info = self.clients_by_name.get(user_name)
        if not info:
            return
        sock, _ = info
        self.send_error_to_sock(sock, error_code)


def main():
    server = IRCServer()
    server.start()


if __name__ == "__main__":
    main()
