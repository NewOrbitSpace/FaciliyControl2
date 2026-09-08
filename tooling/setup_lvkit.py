#!/usr/bin/env python3
"""setup_lvkit.py - one-shot environment setup for reading LabVIEW VIs from Python.

Installs lvkit (which bundles pylabview) and verifies it can parse VI binaries.
No LabVIEW installation is required.

Usage
-----
    python setup_lvkit.py                 # install into the current interpreter
    python setup_lvkit.py --offline wheels # install from a local wheel folder (no network)
    python setup_lvkit.py --proxy http://user:pass@proxy.corp:8080
    python setup_lvkit.py --trusted-host   # bypass TLS-inspection cert errors
    python setup_lvkit.py --venv .venv    # create ./.venv and install into it
    python setup_lvkit.py --visualize     # also install the graph/render extras
    python setup_lvkit.py --check         # verify an existing install, change nothing

After a --venv install, activate it before running vi2json.py:
    source .venv/bin/activate        (Linux/macOS)
    .venv\\Scripts\\activate           (Windows)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 10)


def run(cmd: list[str], *, check: bool = True) -> int:
    print("  $", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check).returncode


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


PIP_EXTRA: list[str] = []       # populated from CLI flags (proxy / index / trusted host)
OFFLINE_DIR: Path | None = None  # populated by --offline


def pip_install(python: Path | str, packages: list[str]) -> None:
    cmd = [str(python), "-m", "pip", "install", "--upgrade", *PIP_EXTRA]
    if OFFLINE_DIR is not None:
        # Resolve everything from the local wheel folder, touch no network.
        cmd += ["--no-index", "--find-links", str(OFFLINE_DIR)]
    cmd += list(packages)
    try:
        run(cmd)
        return
    except subprocess.CalledProcessError:
        pass
    if OFFLINE_DIR is not None:
        # Nothing to retry: no network was used, so this is a wheel/platform
        # mismatch (wrong Python version, wrong OS/arch) or a missing dep.
        raise SystemExit(
            "\nOffline install failed. The wheels in "
            f"{OFFLINE_DIR} must match this interpreter:\n"
            f"  python  {sys.version.split()[0]}  ({'win' if os.name == 'nt' else os.name})\n"
            "Check the cp3XX / platform tags in the wheel filenames.",
        )
    # Debian/Ubuntu system interpreters refuse plain installs (PEP 668).
    print("  ! plain install failed, retrying with --break-system-packages")
    try:
        run([*cmd, "--break-system-packages"])
    except subprocess.CalledProcessError:
        raise SystemExit(
            "\nInstall failed. If the log above shows ConnectionResetError / 10054 /"
            " 'connection\nforcibly closed', your network is blocking PyPI. Options:\n"
            "  python setup_lvkit.py --offline WHEELDIR     (fully offline, no network)\n"
            "  python setup_lvkit.py --proxy http://proxy.corp:8080\n"
            "  python setup_lvkit.py --index-url https://artifactory.corp/api/pypi/pypi/simple\n"
            "  python setup_lvkit.py --trusted-host         (TLS-inspecting proxy)",
        ) from None


def check_install(python: Path | str) -> bool:
    """Import lvkit in the target interpreter and report what it can do."""
    probe = (
        "import lvkit, sys;"
        "from lvkit import parse_vi;"
        "print('lvkit', lvkit.__version__);"
        "print('python', sys.version.split()[0]);"
        "import importlib.util as u;"
        "print('pylabview', 'yes' if u.find_spec('pylabview') else 'bundled/absent')"
    )
    print("\nVerifying installation:")
    rc = subprocess.run([str(python), "-c", probe], check=False).returncode
    if rc != 0:
        print("  ! lvkit could not be imported", file=sys.stderr)
        return False
    # The CLI is a separate entry point; make sure it resolves too.
    subprocess.run([str(python), "-m", "lvkit", "--version"], check=False)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venv", metavar="DIR", help="create a virtualenv at DIR and install there")
    ap.add_argument("--visualize", action="store_true", help="also install lvkit[visualize] extras")
    ap.add_argument("--check", action="store_true", help="only verify an existing install")
    ap.add_argument("--no-setup", action="store_true", help="skip 'lvkit setup' (agent skills + .lvkit/ store)")
    ap.add_argument("--offline", metavar="DIR", help="install from a local folder of .whl files (no network)")
    ap.add_argument("--proxy", metavar="URL", help="pip proxy, e.g. http://user:pass@proxy.corp:8080")
    ap.add_argument("--index-url", metavar="URL", help="internal package index (Artifactory/Nexus/devpi)")
    ap.add_argument("--trusted-host", action="store_true",
                    help="trust pypi hosts (works around TLS-inspecting proxies)")
    args = ap.parse_args()

    global PIP_EXTRA, OFFLINE_DIR
    if args.proxy:
        PIP_EXTRA += ["--proxy", args.proxy]
    if args.index_url:
        PIP_EXTRA += ["--index-url", args.index_url]
    if args.trusted_host:
        for host in ("pypi.org", "files.pythonhosted.org", "pypi.python.org"):
            PIP_EXTRA += ["--trusted-host", host]
    if args.offline:
        OFFLINE_DIR = Path(args.offline).resolve()
        if not OFFLINE_DIR.is_dir():
            print(f"--offline: no such folder: {OFFLINE_DIR}", file=sys.stderr)
            return 1
        wheels = list(OFFLINE_DIR.glob("*.whl"))
        if not wheels:
            print(f"--offline: no .whl files in {OFFLINE_DIR}", file=sys.stderr)
            return 1
        print(f"Offline install from {OFFLINE_DIR} ({len(wheels)} wheels)")

    if sys.version_info < MIN_PY:
        print(f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required, have {sys.version.split()[0]}", file=sys.stderr)
        return 1

    if args.check:
        return 0 if check_install(sys.executable) else 1

    if args.venv:
        venv_dir = Path(args.venv).resolve()
        print(f"Creating virtualenv at {venv_dir}")
        run([sys.executable, "-m", "venv", str(venv_dir)])
        python: Path | str = venv_python(venv_dir)
        if OFFLINE_DIR is None:
            pip_install(python, ["pip"])
    else:
        python = sys.executable
        print(f"Installing into the current interpreter: {python}")

    print("\nInstalling lvkit:")
    if OFFLINE_DIR is None and not PIP_EXTRA:
        print("  (if this fails with ConnectionResetError/10054, your network is blocking"
              " PyPI -\n   re-run with --offline WHEELDIR, --proxy URL, --index-url URL,"
              " or --trusted-host)")
    pip_install(python, ["lvkit[visualize]" if args.visualize else "lvkit"])

    if not args.no_setup:
        # Creates a project-local .lvkit/ resolution store and installs the
        # bundled agent skills. Harmless to re-run; failure is not fatal.
        print("\nRunning 'lvkit setup' in", Path.cwd())
        subprocess.run([str(python), "-m", "lvkit", "setup"], check=False)
        # Reports a locally installed LabVIEW, if any, so vi.lib calls can be
        # resolved. Absence is fine - VI parsing does not need LabVIEW.
        subprocess.run([str(python), "-m", "lvkit", "detect"], check=False)

    ok = check_install(python)

    print("\n" + ("-" * 60))
    if not ok:
        print("Setup did not complete cleanly - see errors above.")
        return 1
    if args.venv:
        act = ".venv\\Scripts\\activate" if os.name == "nt" else f"source {args.venv}/bin/activate"
        print(f"Done. Activate with:  {act}")
    else:
        print("Done.")
    print("Then:  python vi2json.py path/to/MyVI.vi -o dump/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
