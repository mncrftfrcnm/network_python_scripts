# network_python_scripts

Python scripts for configuring Cisco IOS-XE network devices over SSH.

## Requirements

```
pip install paramiko python-dotenv jinja2 scp
```

## Configuration

Copy `.env.example` to `.env` and fill in your device credentials:

```
DEVICE_IP=192.168.1.1
DEVICE_USERNAME=admin
DEVICE_PASSWORD=secret
DEVICE_PORT=22
DEVICE_CONFIRM_TIMEOUT=300
DEVICE_CONFIG_FILE=commands.txt
AUTOCOMMIT=        # enable to skip confirmation prompt
NO_CLEAN_BACKUP=   # enable to keep backup file on flash after run
NO_ROLLBACK_SCRIPT= # enable to skip EEM applet creation and removal
ROLLBACK_ONLY=     # enable to skip upload/apply and only rollback

# bulk_send_cli_config_commit_ssh.py
DEVICES_FILE=devices_list.txt   # file with device IPs, one per line
NUMBER_AT_ONCE=5                # max concurrent threads
```

If not set, `DEVICE_IP`, `DEVICE_USERNAME`, and `DEVICE_PASSWORD` are prompted at runtime.

Boolean variables accept `1`/`true`/`yes` to enable and `0`/`false`/`no` to disable (case-insensitive). Empty or unset is treated as disabled. Any other value causes a script error.

## Scripts

### send_cli_config_commit_ssh.py

Pushes a CLI config file to a device with a confirm/rollback flow.

**Flow:**
1. Upload commands file to device flash via SFTP/SCP
2. Backup running-config to flash (rollback point)
3. Configure EEM applet `CONFIG_ROLLBACK` as safety net
4. Apply commands file to running-config
5. Prompt user to confirm within `DEVICE_CONFIRM_TIMEOUT` seconds
   - **Confirmed** - remove EEM applet, save config, cleanup
   - **No / Ctrl+C** - immediate rollback via `configure replace`, cleanup
   - **Timeout** - EEM applet triggers rollback automatically, cleanup

**Usage:**
```bash
python send_cli_config_commit_ssh.py [commands_file] [-y]
```

Use `-y` / `--autocommit` (or set `AUTOCOMMIT=1` in `.env`) to skip the confirmation prompt and save immediately. EEM safety net is still active.

Use `--no-clean-backup` (or set `NO_CLEAN_BACKUP=1` in `.env`) to keep `flash:automation_cli_backup.cfg` after the run (useful for manual inspection or recovery).

Use `--no-rollback-script` (or set `NO_ROLLBACK_SCRIPT=1` in `.env`) to skip EEM applet creation and removal entirely.

Use `--rollback-only` (or set `ROLLBACK_ONLY=1` in `.env`) to skip upload/backup/apply entirely and just remove the EEM applet and restore the previously saved backup (`flash:automation_cli_backup.cfg`). The backup file is preserved on flash after the run.

Commands file supports Jinja2 templates with env vars as context:
```
interface GigabitEthernet1
 description {{ UPLINK_DESC }}
```

### bulk_send_cli_config_commit_ssh.py

Runs `send_cli_config_commit_ssh.py` against a list of devices in parallel.

**Usage:**
```bash
python bulk_send_cli_config_commit_ssh.py commands_template.txt --devices-file devices_list.txt -y
python bulk_send_cli_config_commit_ssh.py commands_template.txt --devices-file devices_list.txt --rollback-only
```

Exactly one of `-y`/`--rollback-only` must be set (interactive confirmation is not possible in bulk mode). The script exits with an error before connecting to any device if both or neither are provided.

Output from each device is printed live, prefixed with `[IP]`. A summary table is printed at the end; the process exits 1 if any device failed.

Use `-n` / `--number-at-once` (or set `NUMBER_AT_ONCE`) to control parallelism (default: 5).

`DEVICE_USERNAME` and `DEVICE_PASSWORD` are shared across all devices. `DEVICE_IP` is set per-device automatically.

## Common parameters (connect_params.py)

Shared library used by all scripts. Reads connection params from env / `.env`,
falling back to interactive prompts if not set.
