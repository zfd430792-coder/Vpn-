import shlex
from typing import Awaitable, Callable


async def provision_agent(
    host: str,
    port: int,
    user: str,
    password: str,
    install_url: str,
    agent_port: int,
    token: str,
    on_log: Callable[[str], Awaitable[None]],
) -> bool:
    """Зайти по SSH и поставить агента. Стримит вывод через on_log. True — успех."""
    try:
        import asyncssh  # noqa: WPS433
    except ImportError:
        await on_log("‼ на главном сервере нет asyncssh. установи:")
        await on_log("/opt/vpn-traffic-bot/.venv/bin/pip install asyncssh && systemctl restart vpn-traffic-bot")
        return False

    inner = f"curl -fsSL {install_url} | ROLE=agent AGENT_PORT={agent_port} AGENT_TOKEN={token} bash 2>&1"
    if user != "root":
        cmd = "sudo -S -p '' bash -lc " + shlex.quote(inner)
    else:
        cmd = inner

    try:
        async with asyncssh.connect(host, port=port, username=user, password=password,
                                    known_hosts=None, connect_timeout=20) as conn:
            await on_log(f"🔌 подключился к {host}, ставлю агента…")
            proc = await conn.create_process(cmd, stdin=asyncssh.PIPE)
            if user != "root":
                try:
                    proc.stdin.write(password + "\n")
                except Exception:
                    pass
            async for line in proc.stdout:
                line = line.rstrip()
                if line:
                    await on_log(line)
            await proc.wait()
            return proc.returncode == 0
    except Exception as e:  # noqa: BLE001
        await on_log(f"⛔ SSH: {e}")
        return False
