"""Tiny SSH/SFTP helper for the ISMS Raspberry Pi.

Credentials come from backend/.env (PI_SSH_HOST / PI_SSH_USER / PI_SSH_PASS).
Import it, or run a one-off command:  python deploy/pi.py "uname -a"
"""

from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

import paramiko
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "backend" / ".env")

HOST = os.getenv("PI_SSH_HOST", "192.168.1.100")
USER = os.getenv("PI_SSH_USER", "pi")
PASS = os.getenv("PI_SSH_PASS", "")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def connect() -> paramiko.SSHClient:
    if not PASS:
        sys.exit("PI_SSH_PASS is not set — add it to backend/.env (see .env.example).")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASS, timeout=20, banner_timeout=30)
    return client


def run(client: paramiko.SSHClient, cmd: str, sudo: bool = False, timeout: int = 300):
    """Run a command; returns (exit_status, combined_output)."""
    if sudo:
        cmd = f"sudo -S -p '' bash -lc {_q(cmd)}"
    else:
        cmd = f"bash -lc {_q(cmd)}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
    if sudo:
        stdin.write(PASS + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, (out + err).strip()


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts, cur = remote_dir.strip("/").split("/"), ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def put(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    mkdirs(sftp, posixpath.dirname(remote))
    sftp.put(str(local), remote)


def put_text(sftp: paramiko.SFTPClient, text: str, remote: str) -> None:
    mkdirs(sftp, posixpath.dirname(remote))
    with sftp.open(remote, "w") as fh:
        fh.write(text)


def put_script(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    """Upload a shell script with LF endings — bash chokes on CRLF."""
    text = local.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    put_text(sftp, text, remote)


if __name__ == "__main__":
    command = " ".join(sys.argv[1:]) or "uname -a; python3 --version"
    c = connect()
    try:
        code, output = run(c, command)
        print(output)
        sys.exit(code)
    finally:
        c.close()
