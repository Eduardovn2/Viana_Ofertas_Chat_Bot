"""Read-only Docker diagnostics; never print daemon output or configuration secrets."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9._-]+)?\b")
CHECKS = (("--version",), ("compose", "version"), ("info",))


def candidate_paths() -> list[Path]:
    """Inspect common installation locations without scanning disks or altering PATH."""
    paths = []
    for variable, suffix in (
        ("ProgramFiles", "Docker/Docker/resources/bin/docker.exe"),
        ("ProgramFiles(x86)", "Docker/Docker/resources/bin/docker.exe"),
        ("LOCALAPPDATA", "Programs/Docker/Docker/resources/bin/docker.exe"),
        ("LOCALAPPDATA", "Docker/resources/bin/docker.exe"),
        ("ProgramData", "chocolatey/bin/docker.exe"),
        ("USERPROFILE", "scoop/apps/docker/current/docker.exe"),
        ("USERPROFILE", "scoop/apps/docker-desktop/current/resources/bin/docker.exe"),
    ):
        root = os.environ.get(variable)
        if root:
            paths.append(Path(root) / suffix)
    return paths


def diagnose() -> dict:
    paths = candidate_paths()
    executable = shutil.which("docker")
    if executable is None:
        executable = next((str(path) for path in paths if path.is_file()), None)
    result = {
        "docker_found": executable is not None,
        "executable": executable,
        "env_file_present": (PROJECT_ROOT / ".env").is_file(),
        "checks": [],
        "ready": False,
        "blocking_reason": None,
    }
    if executable is None:
        result["blocking_reason"] = "docker_cli_not_found"
        result["searched_paths"] = [str(path) for path in paths]
        result["checks"] = [
            {"command": "docker " + " ".join(arguments), "status": "command_not_found"}
            for arguments in CHECKS
        ]
        return result

    for arguments in CHECKS:
        check = {"command": "docker " + " ".join(arguments), "status": "unavailable"}
        try:
            process = subprocess.run(
                [executable, *arguments],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            check["status"] = "timeout"
        except OSError:
            check["status"] = "execution_failed"
        else:
            check["return_code"] = process.returncode
            check["status"] = "ok" if process.returncode == 0 else "failed"
            if process.returncode == 0 and arguments != ("info",):
                match = VERSION_PATTERN.search(process.stdout)
                if match:
                    check["version"] = match.group(0)
        # stdout/stderr from docker info can contain proxy credentials and are discarded.
        result["checks"].append(check)
    result["ready"] = all(check["status"] == "ok" for check in result["checks"])
    if not result["ready"]:
        result["blocking_reason"] = "docker_command_or_daemon_unavailable"
    return result


def main() -> int:
    result = diagnose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
