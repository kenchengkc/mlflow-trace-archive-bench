"""Run a benchmark in its own disposable PostgreSQL cluster."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Protocol


class Sql(Protocol):
    def __call__(self, statement: str, *, variables: dict[str, int] | None = None) -> str: ...


@contextmanager
def local_postgres() -> Iterator[Sql]:
    """Start a socket-only server and yield a function that runs psql scripts."""
    binaries = {}
    for name in ("initdb", "pg_ctl", "psql"):
        binary = shutil.which(name)
        if binary is None:
            raise RuntimeError(f"Install PostgreSQL and put {name} on PATH.")
        binaries[name] = binary
    if os.geteuid() == 0:
        raise RuntimeError("PostgreSQL initdb requires a non-root OS user.")

    # A short, private directory keeps Unix sockets within the OS path limit.
    with TemporaryDirectory(prefix="trace-pg-", dir="/tmp") as tmp:
        data_dir = Path(tmp) / "data"
        log_file = Path(tmp) / "server.log"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env.update(LC_ALL="C", TZ="UTC", PGSYSCONFDIR=tmp)

        def run(name: str, args: list[str], *, script: str | None = None) -> str:
            result = subprocess.run(
                [binaries[name], *args], input=script, capture_output=True,
                encoding="utf-8", errors="replace", env=env,
            )
            if result.returncode:
                details = (result.stderr or result.stdout).strip()
                if name == "pg_ctl" and log_file.is_file():
                    details += "\n" + log_file.read_text(errors="replace")[-4000:]
                raise RuntimeError(f"{name} exited with status {result.returncode}: {details}")
            return result.stdout

        run("initdb", [
            "-D", str(data_dir), "--locale=C", "--encoding=UTF8",
            "--username=benchmark", "--auth-local=trust", "--auth-host=reject",
        ])
        control = ["-D", str(data_dir), "-w", "-t", "60"]
        client = [
            "-X", "-qAt", "-w", "-h", tmp, "-p", "5432",
            "-U", "benchmark", "-d", "postgres",
        ]

        def sql(statement: str, *, variables: dict[str, int] | None = None) -> str:
            assignments = []
            for key, value in (variables or {}).items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or type(value) is not int:
                    raise ValueError("psql variables must have identifier names and integer values")
                assignments.append(f"--set={key}={value}")
            # psql expands :variables in scripts read from stdin.
            output = run("psql", [*client, *assignments, "--set=ON_ERROR_STOP=1"], script=statement)
            return output.removesuffix("\n")

        try:
            run("pg_ctl", [*control, "-l", str(log_file), "-o",
                           f"-c listen_addresses= -k {tmp} -p 5432", "start"])
            yield sql
        finally:
            if (data_dir / "postmaster.pid").exists():
                try:
                    run("pg_ctl", [*control, "-m", "fast", "stop"])
                except RuntimeError:
                    if (data_dir / "postmaster.pid").exists():
                        run("pg_ctl", [*control, "-m", "immediate", "stop"])
