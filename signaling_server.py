#!/usr/bin/env python3
"""
Lightweight WebSocket Signaling Server for P2P Kanban
Uses only Python 3.x standard library
"""

import asyncio
import json
import hashlib
import base64
import struct
import socket
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


class WebSocketFrame:
    """WebSocket frame parser and builder"""
    
    @staticmethod
    def parse(data):
        """Parse a WebSocket frame"""
        if len(data) < 2:
            return None
            
        byte1, byte2 = struct.unpack('BB', data[:2])
        
        fin = (byte1 & 0b10000000) >> 7
        opcode = byte1 & 0b00001111
        masked = (byte2 & 0b10000000) >> 7
        payload_len = byte2 & 0b01111111
        
        offset = 2
        
        # Extended payload length
        if payload_len == 126:
            if len(data) < 4:
                return None
            payload_len = struct.unpack('>H', data[2:4])[0]
            offset = 4
        elif payload_len == 127:
            if len(data) < 10:
                return None
            payload_len = struct.unpack('>Q', data[2:10])[0]
            offset = 10
        
        # Masking key
        mask_key = None
        if masked:
            if len(data) < offset + 4:
                return None
            mask_key = data[offset:offset + 4]
            offset += 4
        
        # Payload
        if len(data) < offset + payload_len:
            return None
            
        payload = data[offset:offset + payload_len]
        
        # Unmask if needed
        if masked and mask_key:
            payload = bytes([payload[i] ^ mask_key[i % 4] for i in range(len(payload))])
        
        return {
            'fin': fin,
            'opcode': opcode,
            'payload': payload,
            'length': offset + payload_len
        }
    
    @staticmethod
    def build(payload, opcode=0x1):
        """Build a WebSocket frame"""
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        
        frame = bytearray()
        frame.append(0b10000000 | opcode)  # FIN=1, opcode
        
        payload_len = len(payload)
        if payload_len < 126:
            frame.append(payload_len)
        elif payload_len < 65536:
            frame.append(126)
            frame.extend(struct.pack('>H', payload_len))
        else:
            frame.append(127)
            frame.extend(struct.pack('>Q', payload_len))
        
        frame.extend(payload)
        return bytes(frame)


class SignalingServer:
    """WebSocket signaling server for P2P connections"""
    
    def __init__(self, host='0.0.0.0', port=8765):
        self.host = host
        self.port = port
        self.clients = {}  # websocket -> client_info
        self.rooms = {}    # room_id -> set of websockets
        
    async def handle_client(self, reader, writer):
        """Handle a WebSocket client connection"""
        client_id = None
        websocket = None
        
        try:
            # HTTP handshake
            request_line = await reader.readline()
            if not request_line:
                return
            
            headers = {}
            while True:
                line = await reader.readline()
                if line == b'\r\n':
                    break
                if b':' in line:
                    key, value = line.decode('utf-8').strip().split(':', 1)
                    headers[key.strip().lower()] = value.strip()
            
            # WebSocket handshake
            if headers.get('upgrade', '').lower() != 'websocket':
                writer.close()
                await writer.wait_closed()
                return
            
            websocket_key = headers.get('sec-websocket-key', '')
            accept_key = self.generate_accept_key(websocket_key)
            
            response = (
                b'HTTP/1.1 101 Switching Protocols\r\n'
                b'Upgrade: websocket\r\n'
                b'Connection: Upgrade\r\n'
                b'Sec-WebSocket-Accept: ' + accept_key.encode() + b'\r\n'
                b'\r\n'
            )
            
            writer.write(response)
            await writer.drain()
            
            print(f"WebSocket connection established from {writer.get_extra_info('peername')}")
            
            # Store client
            websocket = writer
            client_id = id(writer)
            self.clients[websocket] = {
                'id': client_id,
                'reader': reader,
                'writer': writer,
                'rooms': set()
            }
            
            # Handle messages
            buffer = b''
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                
                buffer += data
                
                while buffer:
                    frame = WebSocketFrame.parse(buffer)
                    if not frame:
                        break
                    
                    buffer = buffer[frame['length']:]
                    
                    # Handle frame
                    if frame['opcode'] == 0x8:  # Close
                        await self.close_connection(websocket)
                        return
                    elif frame['opcode'] == 0x9:  # Ping
                        pong = WebSocketFrame.build(frame['payload'], opcode=0xA)
                        writer.write(pong)
                        await writer.drain()
                    elif frame['opcode'] == 0x1:  # Text
                        await self.handle_message(websocket, frame['payload'].decode('utf-8'))
        
        except Exception as e:
            print(f"Error handling client: {e}")
        finally:
            if websocket:
                await self.close_connection(websocket)
    
    def generate_accept_key(self, key):
        """Generate WebSocket accept key"""
        magic = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
        sha1 = hashlib.sha1((key + magic).encode()).digest()
        return base64.b64encode(sha1).decode()
    
    async def handle_message(self, websocket, message):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')
            
            if msg_type == 'join':
                # Join a room
                room_id = data.get('room', 'default')
                if room_id not in self.rooms:
                    self.rooms[room_id] = set()
                self.rooms[room_id].add(websocket)
                self.clients[websocket]['rooms'].add(room_id)
                print(f"Client joined room: {room_id}")
                
                # Send confirmation
                await self.send_to_client(websocket, {
                    'type': 'joined',
                    'room': room_id
                })
                
            elif msg_type in ['presence', 'offer', 'answer', 'ice-candidate']:
                # Broadcast to room
                room_id = data.get('room', 'default')
                if room_id in self.rooms:
                    await self.broadcast_to_room(room_id, data, exclude=websocket)
                    
        except json.JSONDecodeError:
            print(f"Invalid JSON received: {message}")
        except Exception as e:
            print(f"Error handling message: {e}")
    
    async def send_to_client(self, websocket, data):
        """Send message to a specific client"""
        try:
            message = json.dumps(data)
            frame = WebSocketFrame.build(message)
            websocket.write(frame)
            await websocket.drain()
        except Exception as e:
            print(f"Error sending to client: {e}")
    
    async def broadcast_to_room(self, room_id, data, exclude=None):
        """Broadcast message to all clients in a room"""
        if room_id not in self.rooms:
            return
        
        message = json.dumps(data)
        frame = WebSocketFrame.build(message)
        
        for client_ws in self.rooms[room_id]:
            if client_ws != exclude and client_ws in self.clients:
                try:
                    client_ws.write(frame)
                    await client_ws.drain()
                except Exception as e:
                    print(f"Error broadcasting to client: {e}")
    
    async def close_connection(self, websocket):
        """Close a client connection"""
        if websocket not in self.clients:
            return
        
        client_info = self.clients[websocket]
        
        # Remove from rooms
        for room_id in client_info['rooms']:
            if room_id in self.rooms:
                self.rooms[room_id].discard(websocket)
                if not self.rooms[room_id]:
                    del self.rooms[room_id]
        
        # Remove client
        del self.clients[websocket]
        
        try:
            websocket.close()
            await websocket.wait_closed()
        except:
            pass
        
        print(f"Client disconnected")
    
    async def start(self):
        """Start the signaling server"""
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        print(f"Signaling server listening on {addr[0]}:{addr[1]}")
        print(f"Clients should connect to: ws://{self.get_local_ip()}:{addr[1]}")
        
        async with server:
            await server.serve_forever()
    
    def get_local_ip(self):
        """Get local IP address"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return self.host


async def main():
    """Main entry point"""
    import sys
    
    host = '0.0.0.0'
    port = 8765
    
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    
    server = SignalingServer(host, port)
    
    try:
        await server.start()
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == '__main__':
    print("=" * 60)
    print("P2P Kanban Signaling Server")
    print("=" * 60)
    asyncio.run(main())
