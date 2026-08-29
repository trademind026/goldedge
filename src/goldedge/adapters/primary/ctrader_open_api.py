import asyncio
import struct
import logging
import json
import ssl
from typing import Optional, Callable
# from OpenApiMessages_pb2 import ProtoMessage ... (assuming protobufs are generated)

logger = logging.getLogger("ctrader_api")

class CTraderOpenAPIClient:
    """
    HFT-Optimized raw asyncio TCP client for cTrader Open API Protocol Buffers.
    Handles the 4-byte big-endian framing requirement.
    """
    def __init__(self, host: str = "live.openapi.ctrader.com", port: int = 5035):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._msg_callbacks = []
        self._loop_task = None

    async def connect(self):
        """Establish SSL TCP connection to cTrader OpenAPI."""
        try:
            ssl_ctx = ssl.create_default_context()
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port, ssl=ssl_ctx
            )
            self._connected = True
            self._loop_task = asyncio.create_task(self._read_loop())
            logger.info(f"Connected to cTrader OpenAPI {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to connect to cTrader OpenAPI: {e}")
            self._connected = False

    async def send_message(self, message):
        """
        Send a serialized Protobuf message prefixed with a 4-byte 
        big-endian length header.
        """
        if not self._connected or not self.writer:
            logger.error("Cannot send message: Not connected")
            return
            
        serialized = message.SerializeToString()
        # 4-byte big-endian integer length prefix
        header = struct.pack("!I", len(serialized))
        
        self.writer.write(header + serialized)
        await self.writer.drain()

    async def _read_loop(self):
        """Continuously read from the TCP socket, unpack frames, and dispatch."""
        while self._connected and self.reader:
            try:
                # Read 4-byte header
                header = await self.reader.readexactly(4)
                if not header:
                    break
                
                msg_len = struct.unpack("!I", header)[0]
                
                # Read payload
                payload = await self.reader.readexactly(msg_len)
                
                # Dispatch payload bytes to registered callbacks for parsing
                for cb in self._msg_callbacks:
                    asyncio.create_task(cb(payload))
                    
            except asyncio.IncompleteReadError:
                logger.warning("cTrader connection closed by remote host.")
                break
            except Exception as e:
                logger.error(f"cTrader Read Loop Error: {e}")
                break
                
        self.disconnect()

    def register_callback(self, callback: Callable):
        """Register an async callback to handle incoming raw protobuf payloads."""
        self._msg_callbacks.append(callback)

    def disconnect(self):
        self._connected = False
        if self.writer:
            self.writer.close()
        logger.info("Disconnected from cTrader OpenAPI.")
