import json
import os
import socket

from . import SOCKET_PATH


class ClientError(Exception):
    pass


def request(cmd, socket_path=None, **params):
    path = socket_path or os.environ.get("GBCC_SOCKET", SOCKET_PATH)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(path)
            s.sendall(json.dumps(dict(params, cmd=cmd)).encode() + b"\n")
            reply = json.loads(s.makefile("rb").readline())
    except FileNotFoundError:
        raise ClientError("daemon is not running (is services.gigabyte-control-center.enable set?)")
    except PermissionError:
        raise ClientError(f"no permission to use {path}; are you in the allowed group?")
    except (OSError, ValueError) as e:
        raise ClientError(f"cannot talk to daemon: {e}")
    if not reply.get("ok"):
        raise ClientError(reply.get("error", "unknown error"))
    return reply["data"]
