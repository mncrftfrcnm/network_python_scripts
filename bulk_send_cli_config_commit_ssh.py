#!/usr/bin/env python3
"""Run send_cli_config_commit_ssh.py against multiple devices in parallel.

Usage:
    python bulk_send_cli_config_commit_ssh.py commands_template.txt --devices-file devices_list.txt [-y]
    python bulk_send_cli_config_commit_ssh.py commands_template.txt --devices-file devices_list.txt --rollback-only

Environment variables (or .env file):
    DEVICE_USERNAME, DEVICE_PASSWORD  - shared credentials for all devices
    DEVICES_FILE       - path to file with device IP addresses (one per line)
    NUMBER_AT_ONCE     - max concurrent threads (default: 5)
    AUTOCOMMIT         - passed through to each device run
    ROLLBACK_ONLY      - passed through to each device run
    NO_CLEAN_BACKUP    - passed through to each device run
    NO_ROLLBACK_SCRIPT - passed through to each device run
"""

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from connect_params import env_bool, get_shared_credentials

load_dotenv()

SCRIPT = Path(__file__).parent / "send_cli_config_commit_ssh.py"


def load_devices(path: str) -> list[str]:
    devices = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                devices.append(line)
    return devices


def run_device(ip: str, commands_file: str, extra_env: dict) -> tuple[str, int, str]:
    env = {**os.environ, "DEVICE_IP": ip, "DEVICE_CONFIG_FILE": commands_file, **extra_env}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    output = result.stdout
    if result.stderr:
        output += result.stderr
    return ip, result.returncode, output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk push CLI config to multiple IOS-XE devices in parallel.")
    parser.add_argument("commands_file", help="Path to commands/Jinja2 template file")
    parser.add_argument("--devices-file", default=os.environ.get("DEVICES_FILE"),
                        help="File with device IPs, one per line (or set DEVICES_FILE)")
    parser.add_argument("-n", "--number-at-once", type=int,
                        default=int(os.environ.get("NUMBER_AT_ONCE", "5")),
                        help="Max concurrent threads (default: 5)")
    parser.add_argument("-y", "--autocommit", action="store_true", help="Skip confirmation on each device")
    parser.add_argument("--rollback-only", action="store_true", help="Remove EEM applet and rollback on each device")
    parser.add_argument("--no-clean-backup", action="store_true", help="Keep backup file on flash after run")
    parser.add_argument("--no-rollback-script", action="store_true", help="Skip EEM applet creation and removal")
    args = parser.parse_args()

    if not args.devices_file:
        parser.error("--devices-file is required (or set DEVICES_FILE env var)")

    devices = load_devices(args.devices_file)
    if not devices:
        print(f"No devices found in {args.devices_file}")
        sys.exit(1)

    extra_env: dict[str, str] = get_shared_credentials()
    if args.autocommit:
        extra_env["AUTOCOMMIT"] = "1"
    if args.rollback_only:
        extra_env["ROLLBACK_ONLY"] = "1"
    if args.no_clean_backup:
        extra_env["NO_CLEAN_BACKUP"] = "1"
    if args.no_rollback_script:
        extra_env["NO_ROLLBACK_SCRIPT"] = "1"

    autocommit_active = args.autocommit or env_bool("AUTOCOMMIT")
    rollback_only_active = args.rollback_only or env_bool("ROLLBACK_ONLY")
    if autocommit_active and rollback_only_active:
        parser.error("set exactly one of: -y/AUTOCOMMIT (apply and save) or --rollback-only/ROLLBACK_ONLY (restore backup), not both")
    if not autocommit_active and not rollback_only_active:
        parser.error("set exactly one of: -y/AUTOCOMMIT (apply and save) or --rollback-only/ROLLBACK_ONLY (restore backup)")

    print(f"Running against {len(devices)} device(s), {args.number_at_once} at a time ...\n", flush=True)

    results: dict[str, tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=args.number_at_once) as pool:
        futures = {pool.submit(run_device, ip, args.commands_file, extra_env): ip for ip in devices}
        for future in as_completed(futures):
            ip, rc, output = future.result()
            results[ip] = (rc, output)
            prefix = f"[{ip}]"
            for line in output.splitlines():
                print(f"{prefix} {line}", flush=True)
            status = "OK" if rc == 0 else f"FAILED (exit {rc})"
            print(f"{prefix} --- {status} ---\n", flush=True)

    print("=" * 60)
    print("Summary:")
    failed = []
    for ip in devices:
        rc, _ = results.get(ip, (-1, ""))
        status = "OK" if rc == 0 else f"FAILED (exit {rc})"
        print(f"  {ip}: {status}")
        if rc != 0:
            failed.append(ip)
    print("=" * 60)

    sys.exit(1 if failed else 0)
