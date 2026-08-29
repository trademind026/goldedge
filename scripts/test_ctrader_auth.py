import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from twisted.internet import reactor


# ============================================================
# LOAD .ENV
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


CLIENT_ID = os.getenv("CTRADER_CLIENT_ID")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET")
ENVIRONMENT = os.getenv("CTRADER_ENV", "demo")


if not CLIENT_ID:
    print("Missing CTRADER_CLIENT_ID")
    sys.exit(1)

if not CLIENT_SECRET:
    print("Missing CTRADER_CLIENT_SECRET")
    sys.exit(1)


# ============================================================
# ENDPOINT
# ============================================================

if ENVIRONMENT.lower() == "live":
    HOST = EndPoints.PROTOBUF_LIVE_HOST
else:
    HOST = EndPoints.PROTOBUF_DEMO_HOST


# ============================================================
# ERROR
# ============================================================

def onError(failure):
    print()
    print("ERROR:")
    print(failure)
    print()

    if reactor.running:
        reactor.stop()


# ============================================================
# CONNECTED
# ============================================================

def onConnected(client):

    print()
    print("CONNECTED")
    print(f"Host: {HOST}")
    print(f"Client ID: {CLIENT_ID[:20]}...")
    print()

    request = ProtoOAApplicationAuthReq()

    request.clientId = CLIENT_ID
    request.clientSecret = CLIENT_SECRET

    print("Sending application authentication...")

    # IMPORTANT:
    # Use the official SDK behavior.
    # Do NOT manually set clientMsgId here.
    deferred = client.send(request)

    deferred.addErrback(onError)


# ============================================================
# MESSAGE
# ============================================================

def onMessageReceived(client, message):

    if message.payloadType == ProtoOAApplicationAuthRes().payloadType:

        print()
        print("=" * 60)
        print("APPLICATION AUTH SUCCESS")
        print("=" * 60)
        print()

        reactor.stop()

        return


    if message.payloadType == ProtoOAErrorRes().payloadType:

        error = Protobuf.extract(message)

        print()
        print("=" * 60)
        print("CTRADER ERROR")
        print("=" * 60)
        print(f"Code       : {error.errorCode}")
        print(f"Description: {error.description}")
        print("=" * 60)
        print()

        reactor.stop()

        return


# ============================================================
# DISCONNECTED
# ============================================================

def onDisconnected(client, reason):

    print()
    print("Disconnected:", reason)

    if reactor.running:
        reactor.stop()


# ============================================================
# CLIENT
# ============================================================

client = Client(
    HOST,
    EndPoints.PROTOBUF_PORT,
    TcpProtocol
)

client.setConnectedCallback(onConnected)
client.setMessageReceivedCallback(onMessageReceived)
client.setDisconnectedCallback(onDisconnected)


print("=" * 60)
print("cTrader Application Authentication Test")
print("=" * 60)
print(f"Environment: {ENVIRONMENT}")
print(f"Host       : {HOST}")
print(f"Client ID  : {CLIENT_ID[:20]}...")
print("=" * 60)


client.startService()

reactor.run()