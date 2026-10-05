"""Connect one explicitly selected, user-owned logged-in WeChat account."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-dir", type=Path, required=True, help="Your account's db_storage directory")
    parser.add_argument("--i-own-this-account", action="store_true", required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Native Windows is required.")
    db_dir = args.db_dir.resolve()
    if db_dir.name != "db_storage" or not db_dir.is_dir():
        raise SystemExit("Select the account's db_storage directory.")
    import psutil
    import yaml
    import wcdb_readonly as reader
    runtime = ROOT / ".runtime" / "history_reader"
    runtime.mkdir(parents=True, exist_ok=True)
    username = psutil.Process(os.getpid()).username()
    subprocess.run(["icacls", str(runtime), "/inheritance:r", "/grant:r",
                    username + ":(OI)(CI)F", "SYSTEM:(OI)(CI)F"], check=True, capture_output=True)
    original_collect = reader.collect_db_files
    original_pids = reader._get_pids_windows
    reader._print = lambda *a, **kw: None

    def collect(directory):
        files, _ = original_collect(directory)
        chosen, salts = [], {}
        for rel, path, size, salt, page in files:
            rel = rel.replace("\\", "/")
            if not (re.fullmatch(r"message/message_\d+\.db", rel) or rel in
                    {"contact/contact.db", "session/session.db"}):
                continue
            rel = db_dir.parent.name + "/" + rel
            chosen.append((rel, path, size, salt, page))
            salts.setdefault(salt, []).append(rel)
        return chosen, salts

    def same_user_pids():
        result = []
        for pid, size in original_pids():
            try:
                if psutil.Process(pid).username().lower() == username.lower():
                    result.append((pid, size))
            except psutil.Error:
                pass
        return result

    def save(files, salts, key_map, directory, filename):
        data = {rel: {"enc_key": key_map[salt], "salt": salt, "source": path, "bytes": size}
                for rel, path, size, salt, _ in files if salt in key_map}
        Path(filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    reader.collect_db_files = collect
    reader._get_pids_windows = same_user_pids
    reader._save_results = save
    keys = runtime / "keys.json"
    if keys.exists():
        shutil.copy2(keys, runtime / "keys.previous.json")
    matched = reader._scan_memory_raw_key(str(db_dir), str(keys))
    if not matched:
        raise SystemExit("No authenticated database keys found; no connection enabled.")
    local = ROOT / "config.yaml"
    if not local.exists():
        shutil.copyfile(ROOT / "config.example.yaml", local)
    config = yaml.safe_load(local.read_text(encoding="utf-8"))
    config.update(adapter="history_verified_sender", mode="shadow")
    config["wechat"].update(sender_all_existing_chats=False, send_holding_on_review=False)
    config["paths"]["database"] = ".runtime/history.sqlite3"
    (ROOT / "config.history.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"Connected {len(matched)} authenticated database keys. Draft-only mode; nothing sent.")


if __name__ == "__main__":
    main()
