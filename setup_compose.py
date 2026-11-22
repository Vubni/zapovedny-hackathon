"""Create the local Compose credentials once. An existing file is preserved."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
from tempfile import NamedTemporaryFile


def create_settings(target):
    with NamedTemporaryFile(mode="w", dir=target.parent, encoding="utf-8") as handle:
        for name in ("METEO_PG_ADMIN_PASSWORD", "METEO_APP_DB_PASSWORD", "METEO_WEB_OPERATOR_PASSWORD"):
            handle.write(f"{name}={secrets.token_hex(32)}\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            os.link(handle.name, target)
        except FileExistsError:
            print(f"Используется существующий файл {target}.")
            return
    print(f"Настройки сохранены в {target} с правами 600.")


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=root / ".env")
    parser.add_argument("--up", action="store_true", help="собрать и запустить сервисы, дождаться healthcheck")
    args = parser.parse_args()
    target = args.file.absolute()
    create_settings(target)
    if args.up:
        # macOS can present the same project basename as NFC and NFD. Bake accepts
        # either spelling when the build context is a child of its working directory.
        subprocess.run(["docker", "compose", "-f", str(root / "compose.yaml"), "--env-file", str(target),
                        "up", "--build", "-d", "--wait"], cwd=root.parent, check=True)


if __name__ == "__main__":
    main()
