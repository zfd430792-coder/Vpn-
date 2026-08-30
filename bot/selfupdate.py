import shlex
import shutil
import subprocess


LOG = "/tmp/vpn-selfupdate.log"


def _update_cmd(install_dir: str, branch: str, service: str) -> str:
    pip = shlex.quote(f"{install_dir}/.venv/bin/pip")
    d = shlex.quote(install_dir)
    return (
        f"exec >{LOG} 2>&1; set -x; echo start; "
        f"cd {d} "
        f"&& git fetch --depth 1 origin {shlex.quote(branch)} && echo fetched "
        f"&& git reset --hard FETCH_HEAD && echo reset "
        f"&& (timeout 120 {pip} install -q -r requirements.txt || echo pip-skip) "
        f"&& echo restarting && systemctl restart {shlex.quote(service)}; echo done"
    )


def run_self_update(install_dir: str, branch: str, service: str) -> bool:
    """Запустить обновление в отдельном процессе (переживёт рестарт сервиса)."""
    cmd = _update_cmd(install_dir, branch, service)
    try:
        if shutil.which("systemd-run"):
            subprocess.Popen([
                "systemd-run", "--collect", "--quiet",
                "--unit", f"selfupdate-{service}",
                "/bin/bash", "-lc", cmd,
            ])
        else:
            subprocess.Popen(["/bin/bash", "-lc", cmd], start_new_session=True)
        return True
    except Exception:
        return False


def local_head(install_dir: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", install_dir, "rev-parse", "HEAD"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def remote_head(install_dir: str, branch: str) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", install_dir, "ls-remote", "origin", branch],
            text=True, timeout=20, stderr=subprocess.DEVNULL,
        )
        return out.split()[0] if out.strip() else ""
    except Exception:
        return ""
