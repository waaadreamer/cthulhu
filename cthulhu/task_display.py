"""Task-owned labels and public progress, without guessing subjects from prompts."""


def short_name(description: str, requested: str = "") -> str:
    # Existing/older tasks are named by an explicit, one-time manager mapping.
    if not requested:
        return "任务"
    value = " ".join(requested.split())
    if not 2 <= len(value) <= 32 or any(c in value for c in "<>\n"):
        raise ValueError("short_name must be a 2–32 character task label")
    return value


def concise(text: str, limit: int = 240) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def progress_line(report: dict) -> str:
    # Verified management synthesis supersedes the unverified checkpoint snapshot.
    # The snapshot and real operations remain attached as expandable evidence.
    if report.get("decision"):
        return report["decision"]
    actions = report.get("actions", [])
    operations = "；".join(a["summary"] for a in actions) or "尚无已记录的管理操作"
    return "待核实：" + report["facts"] + "；管理摘要待回传；" + operations
