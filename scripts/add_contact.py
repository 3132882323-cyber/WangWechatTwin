from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description="添加或更新授权联系人")
    parser.add_argument("name")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--relationship", default="业务联系人")
    parser.add_argument("--domain", default="general")
    parser.add_argument("--mode", default="inherit", choices=["inherit", "shadow", "low_risk_auto", "full_auto", "off"])
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    path = Path(args.config)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    contacts = data.setdefault("contacts", [])
    new_item = {
        "name": args.name,
        "relationship": args.relationship,
        "domain": args.domain,
        "tone": "自然直接",
        "mode": args.mode,
        "notes": args.notes,
    }
    for idx, item in enumerate(contacts):
        if item.get("name") == args.name:
            contacts[idx] = new_item
            break
    else:
        contacts.append(new_item)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"已写入联系人：{args.name}")


if __name__ == "__main__":
    main()
