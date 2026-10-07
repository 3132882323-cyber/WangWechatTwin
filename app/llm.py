from __future__ import annotations


from app.config import AppConfig, openai_credentials
from app.models import ReplyDecision, RiskLevel


class LLMError(RuntimeError):
    pass


class ReplyLLM:
    def __init__(self, config: AppConfig):
        self.config = config
        self.account = None
        self.hybrid=None
        if config.openai.provider=='hybrid_web':
            from app.web_llm import WebReplyLLM
            self.hybrid={'chatgpt':WebReplyLLM(config.model_copy(update={'openai':config.openai.model_copy(update={'provider':'web'})})),
                         'deepseek':WebReplyLLM(config.model_copy(update={'openai':config.openai.model_copy(update={'provider':'deepseek_web'})}))}
            return
        if config.openai.provider in {"web","deepseek_web"}:
            from app.web_llm import WebReplyLLM
            self.account = WebReplyLLM(config)
            return
        api_key, base_url = openai_credentials()
        self.account = None
        if config.openai.provider == "account" or (config.openai.provider == "auto" and not api_key):
            from app.codex_llm import AccountReplyLLM
            self.account = AccountReplyLLM(config)
            return
        if not api_key:
            raise LLMError("未设置 OPENAI_API_KEY")
        kwargs = {
            "api_key": api_key,
            "timeout": config.openai.timeout_seconds,
            "max_retries": 1,
        }
        if base_url:
            kwargs["base_url"] = base_url
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError(
                "缺少 openai 依赖，请先运行安装脚本或执行 pip install -r requirements.txt"
            ) from exc
        self.client = OpenAI(**kwargs)

    def decide(self, system_prompt: str, user_payload: str, risk: RiskLevel) -> ReplyDecision:
        if self.hybrid:
            import json,re
            parsed=json.loads(user_payload);text=parsed.get('incoming',{}).get('content','')
            visual_task=parsed.get('incoming',{}).get('message_type') in {'image','sticker'} or bool(parsed.get('__media_paths'))
            complex_task=risk!=RiskLevel.low or visual_task or len(text)>160 or bool(re.search('算一下|分析|方案|对比|合同|解释清楚',text))
            return self.hybrid['chatgpt' if complex_task else 'deepseek'].decide(system_prompt,user_payload,risk)
        if self.account is not None:
            return self.account.decide(system_prompt, user_payload, risk)
        model = (
            self.config.openai.high_risk_model
            if risk in {RiskLevel.high, RiskLevel.critical}
            else self.config.openai.primary_model
        )
        try:
            response = self.client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                text_format=ReplyDecision,
                max_output_tokens=self.config.openai.max_output_tokens,
            )
        except Exception as exc:  # SDK/network exceptions differ by version.
            raise LLMError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise LLMError("模型没有返回可解析的结构化结果")
        return parsed
