from __future__ import annotations

import getpass
import shutil
from datetime import datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def main() -> None:
    backup = ROOT / ".runtime" / "config_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for filename in (".env", "config.yaml"):
        if (ROOT / filename).exists():
            shutil.copy2(ROOT / filename, backup / filename)
    current = ROOT / "config.yaml"
    example = yaml.safe_load((current if current.exists() else ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    print("\n=== 王总微信数字分身首次配置 ===")
    owner_name = ask("本人姓名", example.get("owner_name", "用户"))
    owner_alias = ask("微信回复中常用称呼", example.get("owner_alias", "我"))
    model = ask("日常回复模型", "gpt-6-luna")
    high_model = ask("高风险草稿模型", "gpt-6.1-sol")
    contacts_raw = ask("先监听哪些联系人（英文逗号分隔，首次建议只填文件传输助手）", "文件传输助手")
    contacts = [item.strip() for item in contacts_raw.replace("，", ",").split(",") if item.strip()]

    api_key = getpass.getpass("OpenAI API Key（输入时不显示；也可回车后手工填 .env）: ").strip()
    env_path = ROOT / ".env"
    if api_key:
        env_path.write_text(f"OPENAI_API_KEY={api_key}\nOPENAI_BASE_URL=\n", encoding="utf-8")
    elif not env_path.exists():
        shutil.copyfile(ROOT / ".env.example", env_path)

    example["owner_name"] = owner_name
    example["owner_alias"] = owner_alias
    example["openai"]["primary_model"] = model
    example["openai"]["high_risk_model"] = high_model
    example["mode"] = "shadow"
    example["contacts"] = [
        {
            "name": name,
            "relationship": "首次测试联系人" if name == "文件传输助手" else "业务联系人",
            "domain": "general",
            "tone": "自然直接",
            "mode": "shadow",
            "notes": "首次运行先只生成草稿，确认正常后再改为 inherit。",
        }
        for name in contacts
    ]
    config_path = ROOT / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(f"\n配置已保存：{config_path}")
    print("下一步运行“DIAGNOSE.bat”，再运行“START_SHADOW_MODE.bat”。")


if __name__ == "__main__":
    main()
