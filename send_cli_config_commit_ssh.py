#!/usr/bin/env python3
"""Send raw IOS CLI config with manual commit/confirm flow to Cisco IOS-XE via SSH only.

Flow:
  1. SSH/SFTP - upload commands file to flash:
  2. SSH      - backup running-config to flash: (rollback point)
  3. SSH      - configure EEM applet CONFIG_ROLLBACK as rollback safety net
               (restores backup automatically if no confirmation within CONFIRM_TIMEOUT)
  4. SSH      - copy flash:<commands> into running-config
  5. Prompt   - user must confirm within CONFIRM_TIMEOUT seconds
               (skipped if AUTOCOMMIT is set - auto yes)
  6a. Confirmed / autocommit - remove EEM applet, save config, cleanup, done
  6b. Timeout / No / ^C     - remove EEM applet, rollback, cleanup, exit 1

Rollback-only flow (ROLLBACK_ONLY / --rollback-only):
  No upload, no backup, no EEM configure, no apply.
  1. SSH - remove EEM applet
  2. SSH - configure replace flash:<backup> (rollback)
  3. SSH - delete commands file from flash: (backup file is preserved)

Usage:
    python send_cli_config_commit_ssh.py [commands_file] [-y]
    python send_cli_config_commit_ssh.py --rollback-only

Environment variables (or .env file):
    DEVICE_IP, DEVICE_USERNAME, DEVICE_PASSWORD
    DEVICE_CONFIG_FILE     - path to the commands file
    DEVICE_CONFIRM_TIMEOUT - seconds to wait for confirmation (default: 300)
    AUTOCOMMIT             - set to any non-empty value to skip confirmation
    NO_CLEAN_BACKUP        - set to any non-empty value to keep backup file on flash after run
    NO_ROLLBACK_SCRIPT     - set to any non-empty value to skip EEM applet creation and removal
    ROLLBACK_ONLY          - set to any non-empty value to skip upload/apply and only rollback
"""

import argparse
import os
import sys
import select
import tempfile
import time
import paramiko
from dotenv import load_dotenv
from jinja2 import Environment, StrictUndefined
from scp import SCPClient
from connect_params import get_device_params, env_bool

load_dotenv()

REMOTE_CONFIG_FILE = "automation_cli_push.txt"
REMOTE_BACKUP_FILE = "automation_cli_backup.cfg"
CONFIRM_TIMEOUT = int(os.environ.get("DEVICE_CONFIRM_TIMEOUT", "300"))
EEM_APPLET_NAME = "CONFIG_ROLLBACK"

_CONFIRM_PROMPTS = ("[confirm]", "? [yes/no]", "filename [")


def get_commands_file() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get("DEVICE_CONFIG_FILE") or input("Commands file path: ").strip()


def render_template(path: str) -> tuple[str, bool]:
    """Render path as a Jinja2 template using env vars as context.

    Returns (upload_path, is_temp). If is_temp is True the caller must delete
    the file after use.
    """
    with open(path) as f:
        source = f.read()
    rendered = Environment(undefined=StrictUndefined, keep_trailing_newline=True).from_string(source).render(**os.environ)
    if rendered == source:
        return path, False
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(rendered)
    tmp.close()
    print(f"Template rendered to temporary file: {tmp.name}")
    return tmp.name, True


def _new_ssh(host: str, username: str, password: str) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=22, username=username, password=password)
    return ssh


def upload_file(host: str, username: str, password: str, local_path: str) -> None:
    ssh = _new_ssh(host, username, password)
    try:
        sftp = ssh.open_sftp()
        sftp.put(local_path, REMOTE_CONFIG_FILE)
        sftp.close()
        print("Uploaded via SFTP.")
        return
    except Exception as e:
        print(f"SFTP unavailable ({e}), trying SCP ...")
    finally:
        ssh.close()

    ssh = _new_ssh(host, username, password)
    try:
        with SCPClient(ssh.get_transport()) as scp:
            scp.put(local_path, REMOTE_CONFIG_FILE)
        print("Uploaded via SCP.")
    finally:
        ssh.close()


def _ssh_exec(host: str, username: str, password: str, command: str) -> str:
    """Open a shell channel, send a command, auto-confirm any prompts, return output."""
    ssh = _new_ssh(host, username, password)
    try:
        shell = ssh.invoke_shell()
        time.sleep(1)
        shell.recv(65535)  # drain banner / prompt
        shell.send(command + "\n")
        time.sleep(2)
        output = shell.recv(65535).decode(errors="replace")
        for _ in range(5):
            if any(p in output for p in _CONFIRM_PROMPTS):
                shell.send("\n")
                time.sleep(1)
                output += shell.recv(65535).decode(errors="replace")
            else:
                break
        return output
    finally:
        ssh.close()


def ssh_copy(host: str, username: str, password: str, src: str, dst: str) -> None:
    _ssh_exec(host, username, password, f"copy {src} {dst}")
    print(f"  copy {src} {dst}: done")


def ssh_delete(host: str, username: str, password: str, filename: str) -> None:
    _ssh_exec(host, username, password, f"delete /force flash:{filename}")


def configure_eem_rollback(host: str, username: str, password: str, timeout_seconds: int) -> None:
    config = "\n".join([
        "configure terminal",
        f"event manager applet {EEM_APPLET_NAME}",
        f" event timer countdown time {timeout_seconds + 30}",
        f' action 1.0 syslog msg "{EEM_APPLET_NAME}: no confirmation received - restoring backup"',
        ' action 2.0 cli command "enable"',
        f' action 3.0 cli command "configure replace flash:{REMOTE_BACKUP_FILE} force"',
        "end",
    ])
    _ssh_exec(host, username, password, config)
    print(f"EEM applet {EEM_APPLET_NAME} configured (fires in {timeout_seconds + 30}s if not confirmed).")


def remove_eem_rollback(host: str, username: str, password: str) -> None:
    _ssh_exec(host, username, password, f"configure terminal\nno event manager applet {EEM_APPLET_NAME}\nend")
    print(f"EEM applet {EEM_APPLET_NAME} removed.")


def confirm_with_timeout(timeout: int) -> bool | None:
    """Prompt for confirmation. Returns True (yes), False (explicit no), None (timeout)."""
    print(f"\nConfiguration applied. Auto-rollback in {timeout}s if not confirmed.")
    print("Confirm changes? [y/N]: ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        answer = sys.stdin.readline().strip()
        return answer.lower() in ("y", "yes")
    print()
    return None


def rollback(host: str, username: str, password: str) -> None:
    print("Rolling back to saved backup ...")
    _ssh_exec(host, username, password, f"configure replace flash:{REMOTE_BACKUP_FILE} force")
    print("Rollback complete.")


def save_config(host: str, username: str, password: str) -> None:
    print("Saving configuration ...")
    _ssh_exec(host, username, password, "copy running-config startup-config")
    print("Configuration saved.")


def cleanup(host: str, username: str, password: str, keep_backup: bool = False) -> None:
    print("Cleaning up temp files from flash: ...")
    ssh_delete(host, username, password, REMOTE_CONFIG_FILE)
    if not keep_backup:
        ssh_delete(host, username, password, REMOTE_BACKUP_FILE)
    else:
        print(f"  keeping flash:{REMOTE_BACKUP_FILE}")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push CLI config to IOS-XE with confirm/rollback.")
    parser.add_argument("commands_file", nargs="?", help="Path to commands file (overrides DEVICE_CONFIG_FILE)")
    parser.add_argument("-y", "--autocommit", action="store_true", help="Skip confirmation, save immediately")
    parser.add_argument("--no-clean-backup", action="store_true", help="Keep backup file on flash after run")
    parser.add_argument("--no-rollback-script", action="store_true", help="Skip EEM applet creation and removal")
    parser.add_argument("--rollback-only", action="store_true", help="Remove EEM applet and rollback to saved backup (no upload/apply)")
    args = parser.parse_args()

    autocommit = args.autocommit or env_bool("AUTOCOMMIT")
    no_clean_backup = args.no_clean_backup or env_bool("NO_CLEAN_BACKUP")
    no_rollback_script = args.no_rollback_script or env_bool("NO_ROLLBACK_SCRIPT")
    rollback_only = args.rollback_only or env_bool("ROLLBACK_ONLY")

    if autocommit and rollback_only:
        parser.error("set exactly one of: -y/AUTOCOMMIT (apply and save) or --rollback-only/ROLLBACK_ONLY (restore backup), not both")

    params = get_device_params()
    host, username, password = params["host"], params["username"], params["password"]

    if rollback_only:
        print("\nRollback-only mode: removing EEM applet and restoring backup ...")
        remove_eem_rollback(host, username, password)
        rollback(host, username, password)
        print("Cleaning up commands file from flash: (backup preserved) ...")
        ssh_delete(host, username, password, REMOTE_CONFIG_FILE)
        print("Done.")
        sys.exit(0)

    commands_file = args.commands_file or os.environ.get("DEVICE_CONFIG_FILE") or input("Commands file path: ").strip()

    upload_path, is_temp = render_template(commands_file)
    try:
        print(f"\nUploading {commands_file!r} to {host}:flash:{REMOTE_CONFIG_FILE} ...")
        upload_file(host, username, password, upload_path)
        print("Upload complete.")
    finally:
        if is_temp:
            os.unlink(upload_path)

    print(f"\nBacking up running-config to flash:{REMOTE_BACKUP_FILE} ...")
    ssh_copy(host, username, password, "running-config", f"flash:{REMOTE_BACKUP_FILE}")
    print("Backup done.")

    if not no_rollback_script:
        configure_eem_rollback(host, username, password, CONFIRM_TIMEOUT)

    print(f"\nApplying flash:{REMOTE_CONFIG_FILE} to running-config ...")
    ssh_copy(host, username, password, f"flash:{REMOTE_CONFIG_FILE}", "running-config")

    if autocommit:
        confirmed = True
        print("\nAutocommit enabled.")
    else:
        try:
            confirmed = confirm_with_timeout(CONFIRM_TIMEOUT)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            confirmed = False

    if confirmed is True:
        print("\nConfirmed.")
        if not no_rollback_script:
            remove_eem_rollback(host, username, password)
        save_config(host, username, password)
        cleanup(host, username, password, keep_backup=no_clean_backup)
    elif confirmed is False:
        print("\nRolling back immediately ...")
        if not no_rollback_script:
            remove_eem_rollback(host, username, password)
        rollback(host, username, password)
        cleanup(host, username, password, keep_backup=no_clean_backup)
        sys.exit(1)
    else:
        print("\nTimeout - rolling back ...")
        if not no_rollback_script:
            remove_eem_rollback(host, username, password)
        rollback(host, username, password)
        cleanup(host, username, password, keep_backup=no_clean_backup)
        sys.exit(1)
