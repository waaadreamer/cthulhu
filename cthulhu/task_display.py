"""Task-owned labels and public progress; never infer identity from executor names."""
import re


def short_name(description: str, requested: str = "") -> str:
    if requested:
        value = " ".join(requested.split())
        if not 2 <= len(value) <= 32 or any(c in value for c in "<>\n"):
            raise ValueError("short_name must be a 2–32 character task label")
        return value
    # Migration/older clients: extract a subject phrase once, never a system-prompt prefix.
    patterns = (r"\d+节点[^，。；\n]{0,8}?(?:性能|训练)", r"部署界面(?:修复)?",
                r"Officia\s+Claude(?:连通性)?")
    for pattern in patterns:
        if match := re.search(pattern, description, re.I):
            return match.group(0)
    parts = re.split(r"[\n，。；：:！]", description)
    parts = [re.sub(r"^(?:主人要求|请|帮助|实现|检查|修复|任务目标|目标|为主人)+", "", p.strip()) for p in parts]
    candidates = [p for p in parts if 2 <= len(p) <= 32 and not p.startswith(("<", "#", "["))]
    if candidates:
        return min(candidates, key=len)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,20}", description)
    return " / ".join(dict.fromkeys(words[:2]))[:28] + "任务" if words else "专项任务"


def concise(text: str, limit: int = 180) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def progress_line(report: dict) -> str:
    parts = [report["facts"], report.get("decision") or "待阿撒托斯核实与安排"]
    actions = report.get("actions", [])
    if actions:
        parts.append("；".join(a["summary"] for a in actions))
    else:
        parts.append("尚无已记录的管理操作")
    return "；".join(parts)
