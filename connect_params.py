"""Common parameter handling for netconf scripts."""

import os
import sys
import getpass
from dotenv import load_dotenv

load_dotenv()

_TRUTHY = {"1", "true", "yes"}
_FALSY  = {"0", "false", "no"}


def env_bool(name: str) -> bool:
    """Read a boolean env var; exit with an error on any unrecognised value.

    Not set / empty             → False
    1 / true / yes              → True   (case-insensitive)
    0 / false / no              → False  (case-insensitive)
    Anything else               → script error
    """
    val = os.environ.get(name, "")
    if not val:
        return False
    if val.lower() in _TRUTHY:
        return True
    if val.lower() in _FALSY:
        return False
    sys.exit(f"error: {name}={val!r} is not valid. Use 1/true/yes to enable or 0/false/no to disable.")


def get_shared_credentials() -> dict[str, str]:
    """Return username and password from env vars, falling back to interactive prompts."""
    username = os.environ.get("DEVICE_USERNAME") or input("Username: ").strip()
    password = os.environ.get("DEVICE_PASSWORD") or getpass.getpass("Password: ")
    return {"DEVICE_USERNAME": username, "DEVICE_PASSWORD": password}


def get_device_params(require_port: bool = False) -> dict:
    """Return connection params from env vars, falling back to interactive prompts."""
    ip = os.environ.get("DEVICE_IP") or input("Device IP: ").strip()
    username = os.environ.get("DEVICE_USERNAME") or input("Username: ").strip()
    password = os.environ.get("DEVICE_PASSWORD") or getpass.getpass("Password: ")
    port = int(os.environ.get("DEVICE_PORT", "830"))

    params = {
        "host": ip,
        "port": port,
        "username": username,
        "password": password,
        "hostkey_verify": False,
        "device_params": {"name": "iosxe"},
    }

    return params
