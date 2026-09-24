#!/usr/bin/env python3
"""用 Jev（TypeSafe System One）做结构化判断，并把这次判断本身的 token 花费记进账本。

用法：
    python tools/jev_judgment.py --request decisions/next-step.json \
        --save decisions/next-step.verdict.json \
        --book-ledger <账本目录> --agent jev-judgment --model jev-latest

设计要点：
* 只用标准库（urllib），维持项目"零运行时依赖"的承诺。
* 代码负责流程与阈值，Jev 只负责给出 typed judgment（Choice / Noul / Score）。
* API 响应里的 usage 会作为一张凭证入账 —— **判断也要有凭证**。
* 置信度低于阈值时返回退出码 2，可挂 CI 要求人工复核。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
ENV_KEY = "TYPESAFE_API_KEY"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_REVIEW = 2


class JevError(RuntimeError):
    """调用或解析 TypeSafe API 失败。"""


def load_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get(ENV_KEY)
    if not key:
        raise JevError("未找到 API key：请设置环境变量 {0}（密钥只从环境读取，不写进文件）".format(ENV_KEY))
    return key


def call_jev(
    state,
    questions: dict,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = API_URL,
    timeout: float = 180.0,
) -> dict:
    body = json.dumps({"state": state, "model": model, "questions": questions},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise JevError("TypeSafe API 返回 HTTP {0}：{1}".format(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise JevError("无法访问 TypeSafe API：{0}".format(exc.reason)) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevError("API 返回的不是合法 JSON：{0}".format(exc)) from exc


def format_answers(response: dict, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD):
    """把答案渲染成可读文本，并返回 (文本行, 需要复核的问题 id)。"""
    lines = []
    review = []
    answers = response.get("answers") or {}
    if not answers:
        lines.append("（响应里没有 answers）")
    for question_id, answer in answers.items():
        kind = answer.get("type")
        if kind == "choice":
            confidence = float(answer.get("confidence") or 0.0)
            probabilities = answer.get("probabilities") or {}
            ranked = sorted(probabilities.items(), key=lambda item: -float(item[1]))
            detail = "，".join("{0}={1:.3f}".format(name, float(value)) for name, value in ranked)
            lines.append("[Choice] {0} -> {1}（置信度 {2:.3f}）".format(question_id, answer.get("choice"), confidence))
            if detail:
                lines.append("         分布：{0}".format(detail))
            if confidence < threshold:
                review.append(question_id)
        elif kind == "noul":
            lines.append("[Noul ] {0} -> {1:.3f}（1=是，0=否）".format(question_id, float(answer.get("noul") or 0.0)))
        elif kind == "score":
            confidence = float(answer.get("confidence") or 0.0)
            score = answer.get("score")
            legend = answer.get("legend") or {}
            level = None
            if score is not None and legend:
                level = legend.get(str(int(round(float(score)))))
            lines.append(
                "[Score] {0} -> {1}{2}（置信度 {3:.3f}）".format(
                    question_id,
                    score,
                    "（落在：{0}）".format(level) if level else "",
                    confidence,
                )
            )
            if confidence < threshold:
                review.append(question_id)
        else:
            lines.append("[?    ] {0} -> {1}".format(question_id, json.dumps(answer, ensure_ascii=False)))
    return lines, review


def book_usage(ledger_root: str, response: dict, *, agent: str, model: str) -> int:
    """把这次调用的 token 花费作为凭证记入账本（复用 tledger record 的全部逻辑）。"""
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root / "src"))
    from tokenledger.cli import main as tledger_main
    from tokenledger.models import canonical_json, sha256_text

    usage = response.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if not input_tokens and not output_tokens:
        print("警告：响应里没有 usage，无法为这次判断入账", file=sys.stderr)
        return EXIT_ERROR

    # 每次 API 调用都是一次独立事件；重复登记同一个响应才会被幂等键挡住
    dedup_key = "jev:" + sha256_text(canonical_json(response))[:32]
    argv = [
        "--repo", str(ledger_root), "record",
        "--agent", agent,
        "--model", response.get("model") or model,
        "--provider", "typesafe",
        "--project", "tokenledger",
        "--input-tokens", str(input_tokens),
        "--output-tokens", str(output_tokens),
        "--dedup-key", dedup_key,
        "--recorded-by", "tools/jev_judgment.py",
    ]
    return tledger_main(argv)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 Jev 做结构化判断，并可选把花费入账")
    parser.add_argument("--request", help="请求 JSON：{\"state\":..., \"questions\":...}")
    parser.add_argument("--from-saved", help="已保存的响应文件：只补记账，不重新调用 API")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=API_URL)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--save", help="把原始响应保存到该文件（作为决策证据）")
    parser.add_argument("--book-ledger", help="账本目录；指定后把本次花费入账")
    parser.add_argument("--agent", default="jev-judgment", help="入账时的 agent 标识")
    parser.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    parser.add_argument("--questions", help="只发送请求里的部分问题，逗号分隔（跳过其它问题以省 token）")
    args = parser.parse_args(argv)

    if args.from_saved:
        # 证据已经存在，这里只补记账：不重新调用 API，也就不会重复花费
        try:
            response = json.loads(Path(args.from_saved).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print("错误：无法读取已保存的响应：{0}".format(exc), file=sys.stderr)
            return EXIT_ERROR
        print("来源：已保存的证据 {0}（未重新调用 API）".format(args.from_saved))
    else:
        if not args.request:
            print("错误：需要 --request 或 --from-saved 之一", file=sys.stderr)
            return EXIT_ERROR
        try:
            request = json.loads(Path(args.request).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print("错误：无法读取请求文件：{0}".format(exc), file=sys.stderr)
            return EXIT_ERROR

        questions = request.get("questions") or {}
        if args.questions:
            wanted = [item.strip() for item in args.questions.split(",") if item.strip()]
            missing = [item for item in wanted if item not in questions]
            if missing:
                print("错误：请求文件里没有这些问题：{0}".format(", ".join(missing)), file=sys.stderr)
                return EXIT_ERROR
            questions = {key: questions[key] for key in wanted}
        if not questions:
            print("错误：请求文件里没有 questions", file=sys.stderr)
            return EXIT_ERROR

        try:
            response = call_jev(
                request.get("state"),
                questions,
                api_key=load_api_key(),
                model=args.model,
                base_url=args.base_url,
                timeout=args.timeout,
            )
        except JevError as exc:
            print("错误：{0}".format(exc), file=sys.stderr)
            return EXIT_ERROR

    if args.save:
        Path(args.save).expanduser().write_text(
            json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("原始响应已保存：{0}".format(args.save))

    lines, review = format_answers(response, threshold=args.confidence_threshold)
    print("模型：{0}".format(response.get("model")))
    for line in lines:
        print(line)

    usage = response.get("usage") or {}
    print("本次用量：input={0} output={1}".format(usage.get("input_tokens"), usage.get("output_tokens")))

    if args.book_ledger:
        code = book_usage(args.book_ledger, response, agent=args.agent, model=args.model)
        if code != EXIT_OK:
            return code

    if review:
        print("")
        print("注意：以下问题置信度低于阈值 {0}，建议人工复核：{1}".format(
            args.confidence_threshold, "，".join(review)))
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())