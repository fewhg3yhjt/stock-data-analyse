"""LLM API 客户端 — 对接 DeepSeek Chat API"""

import json
import logging
from typing import Optional

import httpx

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用异常"""


class DeepSeekClient:
    """DeepSeek Chat API 客户端（兼容 OpenAI 格式）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        self.api_key = api_key or Config.DEEPSEEK_API_KEY
        self.base_url = (base_url or Config.DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or Config.DEEPSEEK_MODEL
        self.timeout = timeout

        if not self.api_key:
            logger.warning("DEEPSEEK_API_KEY 未设置，LLM 调用将失败")
            logger.warning("请通过 .env 文件或环境变量设置: DEEPSEEK_API_KEY=sk-xxx")

    def _chat_completion(self, messages: list[dict], **kwargs) -> dict:
        """调用 Chat Completion API"""
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            **kwargs,
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)

        if resp.status_code != 200:
            raise LLMError(
                f"API 请求失败 (HTTP {resp.status_code}): {resp.text}"
            )

        return resp.json()

    def analyze(self, prompt: str, system_prompt: Optional[str] = None,
                temperature: float = 0.3, max_tokens: int = 4096) -> str:
        """发送分析 Prompt 并获取回复

        Parameters
        ----------
        prompt : str
            填充好的分析 Prompt
        system_prompt : str, optional
            系统提示词（角色设定）
        temperature : float
            生成温度，分析场景建议 0.1~0.3
        max_tokens : int
            最大输出长度

        Returns
        -------
        str
            LLM 返回的分析文本
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        else:
            messages.append({
                "role": "system",
                "content": "你是一位A股资深分析师，基于数据做严谨判断。回答简洁、专业、可执行。"
            })
        messages.append({"role": "user", "content": prompt})

        logger.info("发送 LLM 分析请求 (model=%s, max_tokens=%d)", self.model, max_tokens)

        try:
            data = self._chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            raise LLMError(f"LLM 调用失败: {e}") from e

        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")

        usage = data.get("usage", {})
        if usage:
            logger.info(
                "Token 用量: %d 输入 / %d 输出 (总计 %d)",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )

        return content

    def analyze_stream(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """流式调用（适合长输出场景）"""
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = [
            {"role": "system", "content": system_prompt or "你是一位A股资深分析师。"},
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 4096,
            "stream": True,
        }

        collected = []
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    raise LLMError(f"API 错误 {resp.status_code}")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            collected.append(content)
                        except json.JSONDecodeError:
                            continue

        return "".join(collected)
