"""Package tracked source only; never include credentials or development images."""
from pathlib import Path
import hashlib
import json
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1"


def package():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    names = sorted(name for name in names if name)
    for name in names:
        parts = Path(name).parts
        if Path(name).name == "secrets.yaml" or any(part in (".venv", ".esphome", ".validation", "data") for part in parts) or name.endswith(".bin"):
            raise ValueError("Refusing to package a secret or build artifact: " + name)
    directory = ROOT / "dist"
    directory.mkdir(exist_ok=True)
    output = directory / f"ha-jarvis-{VERSION}.zip"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(ROOT / name, "ha-jarvis/" + name)
        archive.writestr("ha-jarvis/BUILD.json", json.dumps({"version": VERSION, "source_commit": commit, "stage": "local_audio_bench", "cloud_enabled": False}, indent=2))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(digest + "  " + output.name + "\n")
    print(json.dumps({"archive": str(output), "bytes": output.stat().st_size, "sha256": digest, "files": len(names), "commit": commit}))


if __name__ == "__main__": package()
