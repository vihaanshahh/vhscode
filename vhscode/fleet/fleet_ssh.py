"""Ship the small frontend and attach a PTY through one OpenSSH connection."""
import hashlib
import io
import json
import os
import stat
from functools import cache
from pathlib import Path
import shlex
import subprocess
import tempfile
import zipfile

# Extract under a content-addressed name. Concurrent connects publish only complete runtimes.
INSTALL = """
import hashlib, io, os, pathlib, shutil, sys, tempfile, zipfile
if sys.version_info < (3, 10):
    sys.exit("vhscode fleet requires Python 3.10+ on this host")
digest = sys.argv[1]
data = sys.stdin.buffer.read()
if hashlib.sha256(data).hexdigest() != digest:
    sys.exit("Incomplete workbench transfer")
base = pathlib.Path.home() / ".cache/vhscode/fleet"
base.mkdir(mode=0o700, parents=True, exist_ok=True)
dest = base / digest
if not dest.exists():
    tmp = pathlib.Path(tempfile.mkdtemp(dir=base))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(tmp)
        try:
            tmp.rename(dest)
        except OSError:
            if not dest.is_dir():
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
"""


def destination(host):
    if not host or host.startswith("-") or any(c.isspace() or not c.isprintable() for c in host):
        raise ValueError("Use an SSH host alias or user@hostname.")
    return host


@cache
def bundle():
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
            info = zipfile.ZipInfo(path.name)
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    data = bundle.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


def ssh_options(host, batch):
    # Control sockets are credentials. Reuse them only within a private, owned directory.
    directory = Path(tempfile.gettempdir()) / f"vhs-ssh-{os.getuid()}"
    if len(os.fsencode(directory)) > 48:
        directory = Path("/tmp") / directory.name
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"SSH socket directory must be private and owned by you: {directory}")
    alias = hashlib.sha256(host.encode()).hexdigest()[:10]
    options = ["ssh", "-o", "ControlMaster=auto", "-o", "ControlPersist=60",
               "-S", str(directory / (alias + "-%C")), "-o", "ServerAliveInterval=15"]
    if batch:
        options.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
    return options


def remote_python(arguments):
    # macOS can expose an older system Python over SSH while the installed app ships a newer one.
    return (
        'fleet_python=python3; '
        'for candidate in "$HOME/Applications/Fleet Workbench.app/Contents/Resources/python/bin/python3" '
        '"/Applications/Fleet Workbench.app/Contents/Resources/python/bin/python3"; do '
        'if [ -x "$candidate" ]; then fleet_python="$candidate"; break; fi; done; '
        f'exec "$fleet_python" -E -s -B {arguments}'
    )


def install(host, ssh, batch):
    data, digest = bundle()
    return subprocess.run([*ssh, "-T", "--", host, remote_python(shlex.join(["-c", INSTALL, digest]))],
                          input=data, capture_output=batch, timeout=12 if batch else None)


def remote_command(args):
    _, digest = bundle()
    script = f'"$HOME"/.cache/vhscode/fleet/{digest}/fleet.py'
    return remote_python(f"{script} {shlex.join(args)}")


def connect(args):
    host = destination(args.ssh)
    ssh = ssh_options(host, False)
    if args.shell:
        folder = shlex.quote(args.repo)
        for prefix in ("~", "$HOME", "${HOME}"):
            if args.repo == prefix or args.repo.startswith(prefix + "/"):
                folder = '"$HOME"' + shlex.quote(args.repo[len(prefix):])
                break
        os.execvp("ssh", [*ssh, "-t", "--", host, f'cd -- {folder}; exec "${{SHELL:-/bin/sh}}" -il'])
    result = install(host, ssh, False)
    if result.returncode:
        return result.returncode
    from fleet_hosts import remember
    remember(host, args.repo)
    remote = ["--agent", args.agent]
    if args.native:
        remote.append("--native")
        if args.session is not None:
            remote.append(f"--session={args.session}")
    if args.filter:
        remote.append("--filter")
    if args.persist:
        remote.append("--persist")
    if args.view:
        remote.extend(["--view", args.view])
    remote.extend(["--", args.repo])
    # Replace the local bootstrap interpreter with the terminal transport.
    os.execvp("ssh", [*ssh, "-t", "--", host, remote_command(remote)])


def authenticate(host):
    host = destination(host)
    helper = Path(__file__).resolve().parent.parent / "bin" / "fleet-askpass"
    if not helper.is_file():
        raise OSError("Open the desktop app to authenticate this SSH connection.")
    env = dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE="force")
    options = ssh_options(host, False)
    result = subprocess.run([*options, "-T", "-o", "ConnectTimeout=12", "--", host, "true"],
                            stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=120)
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace").strip()[-800:] or "SSH authentication failed")


def query(host, payload):
    host = destination(host)
    ssh = ssh_options(host, True)
    _, digest = bundle()
    # Check the digest before invoking RPC. A mutation is sent only to an installed bundle.
    check = f'test -f "$HOME"/.cache/vhscode/fleet/{digest}/fleet.py || exit 72'
    args = [*ssh, "-T", "--", host, check + "; " + remote_command(["--rpc"])]
    body = json.dumps(payload, ensure_ascii=False).encode()
    timeout = 20 if payload.get("op") in {"models", "list_sessions", "resume_session", "test_cli_command"} else 10
    for attempt in range(2):
        result = subprocess.run(args, input=body, capture_output=True, timeout=timeout)
        if result.returncode != 72 or attempt:
            break
        result = install(host, ssh, True)
        if result.returncode:
            break
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace").strip()[-500:] or "SSH request failed")
    reply = json.loads(result.stdout)
    if "error" in reply:
        raise ValueError(reply["error"])
    return reply
