#!/usr/bin/env python3
"""
client.py

Features:
  - Connects to server on TCP port 7734
  - Sends HELLO with version magic and chat_name
  - Has a simple interactive loop with commands
    (JOIN, LEAVE, LIST, MSG, PRIV, QUIT)
  - Receives and prints server messages

"""

import socket
import struct
import threading
import time
import argparse

# =========================
# Protocol constants
# =========================

PORT = 7734
VERSION_MAGIC = 0xFACE0FF1

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


# Error codes (optional use on client side)
IRC_ERR_UNKNOWN         = 0x20000001
IRC_ERR_ILLEGAL_OPCODE  = 0x20000002
IRC_ERR_ILLEGAL_LENGTH  = 0x20000003
IRC_ERR_WRONG_VERSION   = 0x20000004
IRC_ERR_NAME_EXISTS     = 0x20000005
IRC_ERR_ILLEGAL_NAME    = 0x20000006
IRC_ERR_ILLEGAL_MESSAGE = 0x20000007
IRC_ERR_TOO_MANY_USERS  = 0x20000008
IRC_ERR_TOO_MANY_ROOMS  = 0x20000009

ERROR_MESSAGES = {
    IRC_ERR_UNKNOWN:         "Unknown error",
    IRC_ERR_ILLEGAL_OPCODE:  "Illegal / unsupported opcode",
    IRC_ERR_ILLEGAL_LENGTH:  "Illegal packet length",
    IRC_ERR_WRONG_VERSION:   "Wrong protocol version (VERSION_MAGIC mismatch)",
    IRC_ERR_NAME_EXISTS:     "Chat name already in use",
    IRC_ERR_ILLEGAL_NAME:    "Illegal user or room name",
    IRC_ERR_ILLEGAL_MESSAGE: "Illegal message contents (too long or bad characters)",
    IRC_ERR_TOO_MANY_USERS:  "Server has reached the maximum number of users",
    IRC_ERR_TOO_MANY_ROOMS:  "Server has reached the maximum number of rooms",
}


HEADER_STRUCT = struct.Struct("!II")  # opcode, length


# =========================
# Utility helpers
# =========================

def encode_label(name: str) -> bytes:
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
    if len(b) != 20:
        raise ValueError("Label must be 20 bytes")
    s = b.split(b"\x00", 1)[0]
    return s.decode("ascii", errors="replace")


def send_packet(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    header = HEADER_STRUCT.pack(opcode, len(payload))
    sock.sendall(header + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
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
    header_bytes = recv_exact(sock, HEADER_STRUCT.size)
    opcode, length = HEADER_STRUCT.unpack(header_bytes)
    payload = recv_exact(sock, length) if length > 0 else b""
    return opcode, payload


def parse_error_payload(payload: bytes) -> int:
    if len(payload) != 4:
        return 0
    (code,) = struct.unpack("!I", payload)
    return code

# =========================
# Client implementation
# =========================

class IRCClient:
    """
    - Connects to server
    - Sends HELLO with chat_name
    - Spawns:
        - receiver thread (prints server messages)
        - keepalive thread
        - main input loop in main thread
    """

    def __init__(self, host="127.0.0.1", port=PORT, chat_name="user"):
        self.host = host
        self.port = port
        self.chat_name = chat_name
        self.sock = None
        self.running = True

    def connect(self):
        """
        Connect to server, send HELLO, start background threads, and
        run interactive input loop.
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        print(f"[CLIENT] Connected to {self.host}:{self.port}")

        self.send_hello()

        # Start receiver thread
        threading.Thread(target=self.recv_loop, daemon=True).start()
        # Start keepalive thread
        threading.Thread(target=self.keepalive_loop, daemon=True).start()

        # Main interactive loop
        self.input_loop()

    def send_hello(self):
        """
        Send the initial HELLO packet:
        ver_magic (uint32) + chat_name[20]
        """
        try:
            name_bytes = encode_label(self.chat_name)
        except ValueError as e:
            print(f"[CLIENT] Invalid chat name: {e}")
            # Close the socket and stop the client cleanly
            self.running = False
            try:
                if self.sock is not None:
                    self.sock.close()
            except OSError:
                pass
            return

        payload = struct.pack("!I", VERSION_MAGIC) + name_bytes
        send_packet(self.sock, IRC_OPCODE_HELLO, payload)
        print(f"[CLIENT] Sent HELLO as '{self.chat_name}'")


    def keepalive_loop(self):
        """
        Periodically send KEEPALIVE packets to the server.
        According to RFC: at least once every 5 seconds.
        """
        while self.running:
            try:
                send_packet(self.sock, IRC_OPCODE_KEEPALIVE, b"")
            except OSError:
                break
            time.sleep(5)

    def recv_loop(self):
        """
        Receive packets from server and dispatch handlers.
        """
        try:
            while self.running:
                opcode, payload = recv_packet(self.sock)

                if opcode == IRC_OPCODE_ERR:
                    code = parse_error_payload(payload)
                    msg = ERROR_MESSAGES.get(code, "Unrecognized error code")
                    print(f"[CLIENT] ERROR from server: {msg} (0x{code:08x})")
                    # In this simple client, we just stop on any server error:
                    self.running = False

                elif opcode == IRC_OPCODE_LIST_ROOMS_RESP:
                    self.handle_list_rooms_resp(payload)

                elif opcode == IRC_OPCODE_LIST_USERS_RESP:
                    self.handle_list_users_resp(payload)

                elif opcode == IRC_OPCODE_TELL_MSG:
                    self.handle_tell_msg(payload, room=True)

                elif opcode == IRC_OPCODE_TELL_PRIV_MSG:
                    self.handle_tell_msg(payload, room=False)

                else:
                    print(f"[CLIENT] Received opcode {opcode:#x} with {len(payload)} bytes")

        except ConnectionError:
            print("[CLIENT] Connection closed by server.")
        finally:
            self.running = False
            try:
                self.sock.close()
            except OSError:
                pass

    # ===== Handlers for incoming packets =====

    def handle_list_rooms_resp(self, payload: bytes):
        """
        Handle IRC_OPCODE_LIST_ROOMS_RESP:
          identifier[20] (e.g., "rooms")
          item_names[][20]  (room names)
        """
        if len(payload) < 20:
            print("[CLIENT] Malformed LIST_ROOMS_RESP payload")
            return

        identifier = decode_label(payload[:20])
        rooms = []
        rest = payload[20:]
        for i in range(0, len(rest), 20):
            rooms.append(decode_label(rest[i:i+20]))
        rooms = [r for r in rooms if r]

        print(f"[CLIENT] Rooms ({identifier}): {', '.join(rooms) if rooms else '(none)'}")

    def handle_list_users_resp(self, payload: bytes):
        """
        Handle IRC_OPCODE_LIST_USERS_RESP:
          identifier[20] = room name
          item_names[][20] = user names
        """
        if len(payload) < 20:
            print("[CLIENT] Malformed LIST_USERS_RESP payload")
            return

        room_name = decode_label(payload[:20])
        users = []
        rest = payload[20:]
        for i in range(0, len(rest), 20):
            users.append(decode_label(rest[i:i+20]))
        users = [u for u in users if u]

        print(f"[CLIENT] Users in room '{room_name}': {', '.join(users) if users else '(none)'}")

    def handle_tell_msg(self, payload: bytes, room: bool):
        """
        Handle:
          - IRC_OPCODE_TELL_MSG (room messages)
          - IRC_OPCODE_TELL_PRIV_MSG (private messages)

        Payload:
          target_name[20]
          sending_user[20]
          msg[]
        """
        if len(payload) < 40:
            print("[CLIENT] Malformed TELL message")
            return

        target_name = decode_label(payload[:20])
        sending_user = decode_label(payload[20:40])
        msg = payload[40:].decode("ascii", errors="replace")

        if room:
            print(f"[ROOM {target_name}] <{sending_user}> {msg}")
        else:
            print(f"[PRIV from {sending_user}] {msg}")

    # ===== Interactive command loop =====

    def input_loop(self):
        """
        Simple command-line interface:
          /join ROOM
          /leave ROOM
          /users ROOM
          /rooms
          /msg ROOM message...
          /priv USER message...
          /quit
        """
        print("Commands:")
        print("  /join ROOM")
        print("  /leave ROOM")
        print("  /users ROOM")
        print("  /rooms")
        print("  /msg ROOM your message here")
        print("  /priv USER your private message here")
        print("  /quit")

        while self.running:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                break

            if not line:
                continue

            if line.startswith("/"):
                self.handle_command(line)
            else:
                print("Use commands starting with '/'. Example: /join lobby")

        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def handle_command(self, line: str):
        """
        Parse a single command line and send appropriate packets.
        """
        parts = line.strip().split(" ", 2)
        cmd = parts[0].lower()

        if cmd == "/quit":
            self.running = False
            try:
                self.sock.close()
            except OSError:
                pass
            print("[CLIENT] Quitting.")

        elif cmd == "/join" and len(parts) >= 2:
            room = parts[1]
            try:
                payload = encode_label(room)
            except ValueError as e:
                print(f"[CLIENT] Invalid room name: {e}")
                return
            send_packet(self.sock, IRC_OPCODE_JOIN_ROOM, payload)

        elif cmd == "/leave" and len(parts) >= 2:
            room = parts[1]
            try:
                payload = encode_label(room)
            except ValueError as e:
                print(f"[CLIENT] Invalid room name: {e}")
                return
            send_packet(self.sock, IRC_OPCODE_LEAVE_ROOM, payload)
        
        elif cmd == "/users" and len(parts) >= 2:
            room = parts[1]
            try:
                payload = encode_label(room)
            except ValueError as e:
                print(f"[CLIENT] Invalid room name: {e}")
                return
            send_packet(self.sock, IRC_OPCODE_LIST_USERS, payload)

        elif cmd == "/rooms":
            send_packet(self.sock, IRC_OPCODE_LIST_ROOMS, b"")

        elif cmd == "/msg" and len(parts) >= 3:
            room = parts[1]
            msg = parts[2] + "\n"
            try:
                payload = encode_label(room) + msg.encode("ascii", errors="replace")
            except ValueError as e:
                print(f"[CLIENT] Invalid room name: {e}")
                return
            send_packet(self.sock, IRC_OPCODE_SEND_MSG, payload)

        elif cmd == "/priv" and len(parts) >= 3:
            user = parts[1]
            msg = parts[2] + "\n"
            try:
                payload = encode_label(user) + msg.encode("ascii", errors="replace")
            except ValueError as e:
                print(f"[CLIENT] Invalid user name: {e}")
                return
            send_packet(self.sock, IRC_OPCODE_SEND_PRIV_MSG, payload)


        else:
            print("[CLIENT] Unknown or malformed command.")


def main():
    parser = argparse.ArgumentParser(description="IRC RFC client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--name", required=True, help="Chat name (1..20 chars)")
    args = parser.parse_args()

    client = IRCClient(host=args.host, port=args.port, chat_name=args.name)
    try:
        client.connect()
    except ConnectionError as e:
        print(f"[CLIENT] Connection error: {e}")
    except KeyboardInterrupt:
        print("[CLIENT] Interrupted.")


if __name__ == "__main__":
    main()
