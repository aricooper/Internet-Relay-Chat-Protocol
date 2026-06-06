# IRC Chat Protocol

A custom IRC-style client/server chat system implemented in Python from scratch, based on a self-authored RFC-style protocol specification. Built for PSU CS 594, Internetworking Protocols, Fall 2025.

## Overview

This project implements a binary TCP messaging protocol and a complete client/server system supporting multi-room chat, private messaging, and connection management. The protocol specification (`RFC.pdf`) was written as part of the project, following IETF Internet-Draft conventions.

## Features

- Multi-user chat server handling concurrent connections via threading
- Named chat rooms — join, leave, list, and broadcast messages
- Private messaging between users
- Keepalive mechanism to detect dropped connections
- Protocol version negotiation via magic number (`0xFACE0FF1`)
- Structured binary packet format (opcode + length header, network byte order)
- Server-side limits: up to 100 users, 50 rooms, 8KB messages
- Error codes for all failure modes (wrong version, name conflicts, capacity limits, etc.)

## Protocol

All communication runs over TCP on port 7734. Packets use an 8-byte header:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           opcode                              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      payload length                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         payload ...                           |
```

User and room names are fixed-width 20-byte ASCII labels, null-padded.

**Opcodes:**

| Opcode | Value | Direction | Description |
|--------|-------|-----------|-------------|
| HELLO | `0x10000003` | C→S | Register with chat name |
| KEEPALIVE | `0x10000002` | C↔S | Connection heartbeat |
| JOIN_ROOM | `0x10000007` | C→S | Join a named room |
| LEAVE_ROOM | `0x10000008` | C→S | Leave a room |
| SEND_MSG | `0x10000009` | C→S | Send message to room |
| TELL_MSG | `0x10000010` | S→C | Deliver room message |
| SEND_PRIV_MSG | `0x10000011` | C→S | Send private message |
| TELL_PRIV_MSG | `0x10000012` | S→C | Deliver private message |
| LIST_ROOMS | `0x10000004` | C→S | Request room list |
| LIST_ROOMS_RESP | `0x10000005` | S→C | Room list response |
| LIST_USERS | `0x10000013` | C→S | Request users in room |
| LIST_USERS_RESP | `0x10000006` | S→C | User list response |
| ERR | `0x10000001` | S→C | Error notification |

Full protocol specification: [RFC.pdf](RFC.pdf)

## Usage

**Start the server:**

```bash
python server.py
```

**Connect a client:**

```bash
python client.py --host 127.0.0.1 --name alice
```

**Client commands:**

```
/join ROOM          join a chat room
/leave ROOM         leave a room
/rooms              list all rooms
/users ROOM         list users in a room
/msg ROOM message   send a message to a room
/priv USER message  send a private message
/quit               disconnect
```

**Example session:**

```
> /join general
> /msg general hello everyone
[ROOM general] <alice> hello everyone
> /priv bob hey, got a sec?
> /rooms
[CLIENT] Rooms: general, random, dev
```

## Requirements

Python 3.8+ — no external dependencies, stdlib only (`socket`, `struct`, `threading`).

## Design notes

The server uses one thread per client connection. Each client is tracked by chat name, and room membership is stored as a set of client references. The keepalive mechanism sends a heartbeat every 5 seconds; the server drops connections that go silent.

Protocol design decisions are documented in `RFC.pdf`, written in IETF Internet-Draft format with RFC 2119 conformance language (MUST, SHOULD, MAY).
