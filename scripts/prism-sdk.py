#!/usr/bin/env python3
"""Run a committed checkout in the locked SDK without persistent build caches.

Infrastructure only: this runner does not attest to model or product correctness.
It never pulls images, mounts host caches/sockets, or prunes other work.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import uuid


GIB = 1024**3
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")


def locked_image(root):
    lock = json.loads((root / "prismpm.lock").read_text())
    image = lock.get("sdk_image", "")
    if lock.get("schema") != "prismpm/sdk-lock/1" or not IMAGE.fullmatch(image):
        raise ValueError("expected an immutable SDK image in prismpm/sdk-lock/1")
    matches = [x for x in lock.get("inventory", []) if x.get("id") == "sdk-manifest"]
    if len(matches) != 1 or matches[0].get("digest") != image.split("@", 1)[1]:
        raise ValueError("SDK image and inventory digest disagree")
    if matches[0].get("version") != lock.get("sdk_version"):
        raise ValueError("SDK image and inventory version disagree")
    return image


def require_capacity(free_bytes, memory_bytes, scratch_gib):
    if free_bytes < 10 * GIB:
        raise ValueError("less than 10 GiB host disk reserve; no container started")
    # Scratch plus 1 GiB of process memory, retaining 2 GiB for other host work.
    if memory_bytes < (scratch_gib + 3) * GIB:
        raise ValueError("insufficient available host RAM for the bounded run")


def available_memory():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise ValueError("cannot establish available host memory")


def docker_command(image, name, scratch_gib, command):
    if not command:
        raise ValueError("an explicit SDK command is required")
    return [
        "docker", "run", "--name", name, "--rm", "--pull=never", "-i",
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--log-driver=none",
        "--cpus=2", "--pids-limit=256", f"--memory={scratch_gib + 1}g",
        f"--memory-swap={scratch_gib + 1}g", "--user=1000:1000",
        "--tmpfs", f"/work:rw,exec,nosuid,nodev,uid=1000,gid=1000,size={scratch_gib}g",
        "--workdir=/work", "--env=TMPDIR=/work/.tmp",
        "--env=CARGO_TARGET_DIR=/work/target", "--env=CARGO_BUILD_JOBS=2",
        "--env=CARGO_INCREMENTAL=0", "--env=CARGO_NET_OFFLINE=true",
        "--env=PYTHONDONTWRITEBYTECODE=1", "--entrypoint=/bin/sh", image,
        "-eu", "-c", 'tar -xf -; mkdir .tmp; exec "$@"',
        "prism-sdk", *command,
    ]


def checked_output(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=30).strip()


def bounded_timeout(value):
    value = int(value)
    if not 1 <= value <= 3600:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 3600 seconds")
    return value


def execute(root, image, scratch_gib, timeout, command):
    if checked_output("git", "status", "--porcelain", "--untracked-files=all", cwd=root):
        raise ValueError("commit the issue worktree before verification; dirty inputs refused")
    revision = checked_output("git", "rev-parse", "HEAD", cwd=root)
    inspected = json.loads(checked_output("docker", "image", "inspect", image))
    if len(inspected) != 1 or image not in (inspected[0].get("RepoDigests") or []):
        raise ValueError("the exact locked image is not installed; no automatic pull")
    require_capacity(shutil.disk_usage(root).free, available_memory(), scratch_gib)
    name = "hologram-prism-sdk-" + uuid.uuid4().hex
    archive = None
    container = None
    print(json.dumps({"source": revision, "sdk": image, "scratch_gib": scratch_gib,
                      "command": command, "acceptance": "not-established"}),
          file=sys.stderr, flush=True)
    try:
        archive = subprocess.Popen(["git", "archive", "--format=tar", revision],
                                   cwd=root, stdout=subprocess.PIPE)
        container = subprocess.Popen(docker_command(image, name, scratch_gib, command),
                                     stdin=archive.stdout)
        archive.stdout.close()
        status = container.wait(timeout=timeout)
        archive_status = archive.wait(timeout=10)
        if status == 0 and archive_status != 0:
            raise ValueError("source archive failed; the command result is not valid")
        if checked_output("git", "rev-parse", "HEAD", cwd=root) != revision:
            raise ValueError("source revision changed during execution")
        if checked_output("git", "status", "--porcelain", "--untracked-files=all", cwd=root):
            raise ValueError("source worktree changed during execution")
        return status
    finally:
        # Only this invocation's random container is eligible for removal.
        try:
            cleanup = subprocess.run(["docker", "container", "rm", "--force", name],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=20, check=False)
            if cleanup.returncode and container is not None and container.poll() is None:
                raise ValueError(f"cannot confirm cleanup of {name}; inspect before another run")
        finally:
            for process in (container, archive):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-gib", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--timeout", type=bounded_timeout, default=600)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("supply -- followed by an SDK command")
    root = Path(__file__).resolve().parent.parent

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return execute(root, locked_image(root), args.scratch_gib, args.timeout, command)
    except subprocess.TimeoutExpired:
        print("error: bounded SDK run timed out; no acceptance established", file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
