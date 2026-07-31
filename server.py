import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parent
COURSES_DIR = BASE_DIR / "courses"
APP_NAME = "study-sprint-api"
APP_VERSION = "2026-07-31-step-budget-v4"
WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_PHASES = 12
MAX_SESSIONS = 42
MAX_STEPS_PER_SESSION = 8
SYSTEM_PROMPT = (
    "You are an exam-cram planning assistant. Return only one JSON object with no explanation. "
    "Use three levels: phases group goals, sessions are schedulable Todos, and steps are minute-level instructions. "
    "Every step has a fixed role enum: setup, execute, or review. Setup is capped at ten minutes; execution gets the majority. "
    "Session and phase counts must follow the available days and workload; never force a fixed count. "
    "Follow the runtime content-language instruction exactly; it is independent of the website interface language. "
    "If it is absent, match the language used in the course name and exam scope. Keep every sentence short and action-oriented."
)
RATE_BUCKET = {}
RATE_LOCK = threading.Lock()
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


class PayloadValidationError(ValueError):
    pass


def load_json_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return data


def get_workspace_dir(workspace_id: str) -> Path:
    clean_id = str(workspace_id or "").strip()
    if not WORKSPACE_ID_RE.fullmatch(clean_id):
        raise PayloadValidationError("invalid workspace id")
    courses_root = COURSES_DIR.resolve()
    workspace_dir = (COURSES_DIR / clean_id).resolve()
    try:
        workspace_dir.relative_to(courses_root)
    except ValueError as exc:
        raise PayloadValidationError("workspace escaped courses directory") from exc
    return workspace_dir


def load_course_workspace(workspace_id: str) -> dict:
    workspace_dir = get_workspace_dir(workspace_id)
    course_path = workspace_dir / "course.json"
    plan_path = workspace_dir / "plan.json"
    materials_path = workspace_dir / "materials.json"
    if not workspace_dir.is_dir() or not course_path.is_file() or not plan_path.is_file() or not materials_path.is_file():
        raise FileNotFoundError(workspace_id)

    course = load_json_file(course_path)
    plan = load_json_file(plan_path)
    material_manifest = load_json_file(materials_path)
    if str(course.get("id", "")).strip() != workspace_id:
        raise ValueError("course.json id must match its workspace directory")

    days_left = max(1, min(365, int(course.get("days_left", 10))))
    hours_per_day = max(1, min(20, float(course.get("hours_per_day", 4))))
    target_score = max(60, min(100, int(course.get("target_score", 80))))
    materials = material_manifest.get("materials", [])
    legacy_artifacts = material_manifest.get("legacy_artifacts", [])
    if not isinstance(materials, list):
        materials = []
    if not isinstance(legacy_artifacts, list):
        legacy_artifacts = []

    return {
        "id": workspace_id,
        "name": str(course.get("name", workspace_id)).strip() or workspace_id,
        "language": normalize_language(course.get("language", "")) or "zh",
        "days_left": days_left,
        "hours_per_day": hours_per_day,
        "target_score": target_score,
        "exam_date": (date.today() + timedelta(days=days_left)).isoformat(),
        "keywords": str(course.get("keywords", "")).strip(),
        "plan": plan,
        "materials": materials,
        "legacy_artifacts": legacy_artifacts,
        "artifacts": [
            {
                "label": "Phase 0 生存大纲",
                "href": f"/courses/{workspace_id}/progress/00_生存大纲.md",
                "kind": "survival_outline",
            }
        ],
    }


def list_course_workspaces() -> list[dict]:
    if not COURSES_DIR.is_dir():
        return []
    workspaces = []
    for course_path in sorted(COURSES_DIR.glob("*/course.json")):
        workspace_id = course_path.parent.name
        try:
            workspace = load_course_workspace(workspace_id)
        except (FileNotFoundError, PayloadValidationError, ValueError, json.JSONDecodeError):
            continue
        workspaces.append(
            {
                "id": workspace["id"],
                "name": workspace["name"],
                "language": workspace["language"],
                "days_left": workspace["days_left"],
                "hours_per_day": workspace["hours_per_day"],
                "target_score": workspace["target_score"],
                "material_count": len(workspace["materials"]),
            }
        )
    return workspaces


def load_env_file():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].strip()
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and ((value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'"))):
            value = value[1:-1]
        os.environ[key] = value


def load_system_prompt() -> str:
    prompt_text = os.environ.get("SYSTEM_PROMPT", "").strip()
    if prompt_text:
        return prompt_text
    prompt_file = os.environ.get("SYSTEM_PROMPT_FILE", "").strip()
    if prompt_file:
        fp = Path(prompt_file)
        if not fp.is_absolute():
            fp = BASE_DIR / fp
        if fp.exists():
            return fp.read_text(encoding="utf-8").strip() or SYSTEM_PROMPT
    default_file = BASE_DIR / "prompt.md"
    if default_file.exists():
        content = default_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    return SYSTEM_PROMPT


def check_rate_limit(client_ip: str):
    window_sec = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", "60"))
    max_requests = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "20"))
    now = time.time()
    with RATE_LOCK:
        bucket = RATE_BUCKET.get(client_ip, [])
        bucket = [ts for ts in bucket if now - ts <= window_sec]
        if len(bucket) >= max_requests:
            retry_after = max(1, int(window_sec - (now - bucket[0])))
            RATE_BUCKET[client_ip] = bucket
            return False, retry_after
        bucket.append(now)
        RATE_BUCKET[client_ip] = bucket
    return True, 0


def extract_keywords(payload: dict):
    raw = str(payload.get("keywords", "")).strip()
    return [x.strip() for x in re.split(r"[，,;；、\n]", raw) if x.strip()]


def normalize_language(value: str) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw == "zh" or raw.startswith("zh-") or raw in {"chinese", "中文", "简体中文"}:
        return "zh"
    if raw == "en" or raw.startswith("en-") or raw in {"english", "英文", "英语"}:
        return "en"
    return ""


def count_language_signals(text: str):
    content = str(text or "")
    return (
        len(re.findall(r"[\u3400-\u9fff]", content)),
        len(re.findall(r"[A-Za-z]", content)),
    )


def analyze_language_scores(han_count: float, latin_count: float):
    # One English word is roughly five Latin letters; compare language units
    # rather than raw characters so short Chinese titles do not dominate a long
    # English exam scope.
    zh_units = max(0.0, float(han_count))
    en_units = max(0.0, float(latin_count)) / 5.0
    total = zh_units + en_units
    if total <= 0:
        return "", 0.0
    language = "zh" if zh_units >= en_units else "en"
    confidence = max(zh_units, en_units) / total
    return language, round(confidence, 3)


def analyze_text_language(text: str):
    return analyze_language_scores(*count_language_signals(text))


def detect_text_language(text: str) -> str:
    return analyze_text_language(text)[0]


def analyze_input_language(course: str, keywords: str):
    course_han, course_latin = count_language_signals(course)
    scope_han, scope_latin = count_language_signals(keywords)
    return analyze_language_scores(
        course_han + scope_han * 3,
        course_latin + scope_latin * 3,
    )


def resolve_content_language(payload: dict):
    requested = normalize_language(payload.get("content_language", ""))
    source = str(payload.get("content_language_source", "") or "").strip().lower()
    if source == "manual" and requested:
        return requested, "manual", 1.0

    detected, confidence = analyze_input_language(
        payload.get("course", ""),
        payload.get("keywords", ""),
    )
    if detected:
        return detected, "auto", confidence

    declared_input = normalize_language(payload.get("input_language", ""))
    if declared_input:
        return declared_input, "auto", 0.5
    if requested:
        return requested, source or "auto", 0.5

    # Backward compatibility for older clients. UI language is deliberately the
    # final fallback and can no longer override an input-language signal.
    legacy_language = normalize_language(payload.get("language", ""))
    if legacy_language:
        return legacy_language, "legacy", 0.0
    return "zh", "fallback", 0.0


def resolve_response_language(payload: dict) -> str:
    return resolve_content_language(payload)[0]


def detect_subject_mode(course: str, keywords: list[str]) -> str:
    memory_signals = [
        "毛概", "思政", "政治", "历史", "法学", "背诵", "名词解释", "论述", "选择题", "填空题", "马克思",
        "politics", "history", "law", "memorization", "definition", "essay", "multiple choice", "fill in the blank",
    ]
    calc_signals = [
        "数学", "高数", "线代", "概率", "物理", "化学", "力学", "电路", "编程", "算法", "计算", "公式", "推导", "证明", "建模",
        "mathematics", "calculus", "linear algebra", "probability", "physics", "chemistry", "mechanics", "circuit", "programming", "algorithm", "calculation", "formula", "derivation", "proof", "modeling",
    ]
    text = f"{course} {' '.join(keywords)}".lower()
    mem_score = sum(1 for s in memory_signals if s in text)
    calc_score = sum(1 for s in calc_signals if s in text)
    return "calc" if calc_score > mem_score else "memory"


def normalize_sentence(item: str) -> str:
    return re.sub(r"\s+", " ", str(item).strip())


def normalize_minutes(value, default=0, minimum=5, maximum=1200, quantum=5):
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        minutes = int(default or 0)
    if minutes <= 0:
        return 0
    minutes = int(round(minutes / quantum) * quantum)
    return min(maximum, max(minimum, minutes))


def normalize_source_refs(value):
    refs = []
    for item in value if isinstance(value, list) else []:
        source = item if isinstance(item, dict) else {"label": item}
        label = normalize_sentence(source.get("label", source.get("name", "")))
        if not label:
            continue
        refs.append(
            {
                "label": label,
                "href": str(source.get("href", "")).strip(),
                "pages": normalize_sentence(source.get("pages", "")),
                "kind": normalize_sentence(source.get("kind", "")),
            }
        )
    return refs[:8]


def allocate_step_minutes(raw_values, total_minutes):
    total_units = max(1, int(round(total_minutes / 5)))
    count = min(len(raw_values), total_units)
    values = raw_values[:count]
    if not values:
        return []
    weights = [max(1, int(value or 0)) for value in values]
    allocated = [1] * count
    remaining = total_units - count
    if remaining > 0:
        weight_sum = sum(weights)
        exact = [remaining * weight / weight_sum for weight in weights]
        floors = [int(value) for value in exact]
        allocated = [base + extra for base, extra in zip(allocated, floors)]
        leftover = remaining - sum(floors)
        order = sorted(range(count), key=lambda index: exact[index] - floors[index], reverse=True)
        for index in order[:leftover]:
            allocated[index] += 1
    return [units * 5 for units in allocated]


def round_to_five(value):
    return max(5, int((float(value) + 2.5) // 5) * 5)


def session_step_budgets(total_minutes):
    total = max(15, int(round(float(total_minutes) / 5) * 5))
    setup = min(10, max(5, round_to_five(total * 0.05)), max(5, total - 10))
    review_ceiling = max(5, total - setup - 5)
    review_minimum = 5 if total < 60 else 15
    review_minimum = min(review_ceiling, max(review_minimum, round_to_five(total * 0.15)))
    review_target = min(review_ceiling, 40, max(review_minimum, round_to_five(total * 0.20)))
    review_maximum = min(review_ceiling, 40, max(review_target, round_to_five(total * 0.25)))
    return {
        "setup": setup,
        "review_min": review_minimum,
        "review_target": review_target,
        "review_max": review_maximum,
    }


def normalize_step_role(value, action=""):
    raw = str(value or "").strip().lower()
    aliases = {
        "setup": "setup",
        "prepare": "setup",
        "preparation": "setup",
        "准备": "setup",
        "execute": "execute",
        "practice": "execute",
        "study": "execute",
        "执行": "execute",
        "review": "review",
        "check": "review",
        "复盘": "review",
        "检查": "review",
    }
    text = str(action or "").strip().lower()
    setup_signals = [
        "定位本次目标", "所需材料", "准备材料", "确认本次题型", "输入检查清单",
        "locate the", "required material", "prepare material", "gather material", "input checklist",
    ]
    review_signals = [
        "核对", "检查", "复盘", "批改", "错题", "纠错", "定位错误",
        "check the", "review", "verify", "correct", "isolate errors", "error log",
    ]
    if any(signal in text for signal in setup_signals):
        return "setup"
    if text.startswith("执行：") or text.startswith("执行:") or text.startswith("execute:"):
        return "execute"
    if any(signal in text for signal in review_signals):
        return "review"
    if raw in aliases:
        return aliases[raw]
    return "execute"


def rebalance_step_minutes(steps, total_minutes):
    if not steps:
        return steps

    allocated = allocate_step_minutes([step.get("minutes", 0) for step in steps], total_minutes)
    steps[:] = steps[: len(allocated)]
    for step, minutes in zip(steps, allocated):
        step["minutes"] = minutes
        step["role"] = normalize_step_role(step.get("role"), step.get("action"))

    setup_indices = [index for index, step in enumerate(steps) if step["role"] == "setup"]
    for index in setup_indices[1:]:
        steps[index]["role"] = "execute"
    setup_indices = setup_indices[:1]
    review_indices = [index for index, step in enumerate(steps) if step["role"] == "review"]
    execute_indices = [index for index, step in enumerate(steps) if step["role"] == "execute"]
    if not execute_indices:
        return steps

    budgets = session_step_budgets(total_minutes)

    def add_to_execute(minutes):
        if minutes <= 0:
            return
        additions = allocate_step_minutes([steps[index]["minutes"] for index in execute_indices], minutes)
        for index, addition in zip(execute_indices, additions):
            steps[index]["minutes"] += addition

    if setup_indices:
        setup_index = setup_indices[0]
        excess = max(0, steps[setup_index]["minutes"] - budgets["setup"])
        if excess:
            steps[setup_index]["minutes"] -= excess
            add_to_execute(excess)

    if review_indices:
        review_total = sum(steps[index]["minutes"] for index in review_indices)
        review_cap = max(budgets["review_max"], len(review_indices) * 5)
        if review_total > review_cap:
            reduced = allocate_step_minutes([steps[index]["minutes"] for index in review_indices], review_cap)
            excess = review_total - sum(reduced)
            for index, minutes in zip(review_indices, reduced):
                steps[index]["minutes"] = minutes
            add_to_execute(excess)

        review_total = sum(steps[index]["minutes"] for index in review_indices)
        needed = max(0, budgets["review_min"] - review_total)
        while needed >= 5:
            donors = [index for index in execute_indices if steps[index]["minutes"] > 5]
            if not donors:
                break
            donor = max(donors, key=lambda index: steps[index]["minutes"])
            recipient = min(review_indices, key=lambda index: steps[index]["minutes"])
            steps[donor]["minutes"] -= 5
            steps[recipient]["minutes"] += 5
            needed -= 5
    return steps


def default_session_steps(title, duration_minutes, success_criteria, language, source_refs):
    source = source_refs[0]["label"] if source_refs else ("supplied exam scope" if language == "en" else "用户提供的考试范围")
    if language == "en":
        templates = [
            ("setup", "Locate the exact target and required material", "Write a short input checklist"),
            ("execute", f"Execute: {title}", success_criteria or "Produce a complete first attempt"),
            ("review", "Check the output and isolate errors", "Record the next correction or review trigger"),
        ]
    else:
        templates = [
            ("setup", "定位本次目标与所需材料", "写出一份输入检查清单"),
            ("execute", f"执行：{title}", success_criteria or "留下完整的首次作答或训练结果"),
            ("review", "核对产出并定位错误", "记录下一次复做或复习入口"),
        ]
    budgets = session_step_budgets(duration_minutes)
    minutes = [
        budgets["setup"],
        duration_minutes - budgets["setup"] - budgets["review_target"],
        budgets["review_target"],
    ]
    return [
        {"role": role, "minutes": minutes[index], "action": action, "output": output, "source": source}
        for index, (role, action, output) in enumerate(templates[: len(minutes)])
    ]


def normalize_session(item, index, payload, language, default_title):
    source = item if isinstance(item, dict) else {"title": item}
    daily_capacity = normalize_minutes(float(payload.get("hours_per_day", 8)) * 60, 60, 15, 1200)
    title = normalize_sentence(source.get("title", "")) or default_title
    success_criteria = normalize_sentence(source.get("success_criteria", source.get("successCriteria", "")))
    source_refs = normalize_source_refs(source.get("source_refs", source.get("sourceRefs", [])))
    raw_steps = source.get("steps", source.get("guide", []))
    parsed_steps = []
    for raw_step in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(raw_step, dict):
            continue
        action = normalize_sentence(raw_step.get("action", raw_step.get("title", "")))
        if not action:
            continue
        parsed_steps.append(
            {
                "role": normalize_step_role(raw_step.get("role"), action),
                "minutes": normalize_minutes(raw_step.get("minutes", raw_step.get("duration_minutes", 0)), 0, 5, 240),
                "action": action,
                "output": normalize_sentence(raw_step.get("output", raw_step.get("success_criteria", ""))),
                "source": normalize_sentence(raw_step.get("source", "")),
            }
        )
        if len(parsed_steps) >= MAX_STEPS_PER_SESSION:
            break

    raw_duration = source.get("duration_minutes", source.get("durationMinutes", 0))
    step_total = sum(step["minutes"] for step in parsed_steps)
    duration = normalize_minutes(raw_duration, step_total or daily_capacity, 15, daily_capacity)
    if not duration:
        duration = daily_capacity

    if parsed_steps:
        if len(parsed_steps) == 3:
            first_action = parsed_steps[0]["action"].lower()
            middle_action = parsed_steps[1]["action"].lower()
            last_action = parsed_steps[2]["action"].lower()
            known_three_step_guide = (
                ("所需材料" in first_action and ("执行" in middle_action or "execute:" in middle_action))
                or ("required material" in first_action and "execute:" in middle_action)
            ) and ("错误" in last_action or "error" in last_action)
            if known_three_step_guide:
                for step, role in zip(parsed_steps, ("setup", "execute", "review")):
                    step["role"] = role
        parsed_steps = parsed_steps[: max(1, duration // 5)]
        rebalance_step_minutes(parsed_steps, duration)
        default_source = source_refs[0]["label"] if source_refs else ("supplied exam scope" if language == "en" else "用户提供的考试范围")
        for step in parsed_steps:
            if not step["output"]:
                step["output"] = success_criteria or ("A checkable result" if language == "en" else "一份可检查的产出")
            if not step["source"]:
                step["source"] = default_source
    else:
        parsed_steps = default_session_steps(title, duration, success_criteria, language, source_refs)

    try:
        day_index = int(source.get("day_index", source.get("dayIndex", 0)) or 0)
    except (TypeError, ValueError):
        day_index = 0
    days_left = int(payload.get("days_left", 1))
    if not 1 <= day_index <= days_left:
        day_index = 0

    return {
        "id": normalize_sentence(source.get("id", "")) or f"session-{index + 1:02d}",
        "title": title,
        "day_index": day_index,
        "duration_minutes": duration,
        "success_criteria": success_criteria,
        "source_refs": source_refs,
        "steps": parsed_steps,
    }


def normalize_phases(plan, payload, language, keywords, course):
    raw_phases = plan.get("phases", []) if isinstance(plan, dict) else []
    phases = []
    session_count = 0
    session_limit = min(MAX_SESSIONS, max(1, int(payload.get("days_left", 1)) * 2))
    for raw_phase in raw_phases if isinstance(raw_phases, list) else []:
        if session_count >= session_limit or len(phases) >= MAX_PHASES:
            break
        source = raw_phase if isinstance(raw_phase, dict) else {"title": raw_phase}
        raw_sessions = source.get("sessions", [])
        sessions = []
        for raw_session in raw_sessions if isinstance(raw_sessions, list) else []:
            if session_count >= session_limit:
                break
            fallback_topic = keywords[session_count % len(keywords)] if keywords else course
            default_title = f"Pressure-test {fallback_topic}" if language == "en" else f"围绕{fallback_topic}完成训练"
            sessions.append(normalize_session(raw_session, session_count, payload, language, default_title))
            session_count += 1
        if not sessions:
            continue
        phase_index = len(phases) + 1
        phases.append(
            {
                "id": normalize_sentence(source.get("id", "")) or f"phase-{phase_index:02d}",
                "title": normalize_sentence(source.get("title", "")) or (f"Phase {phase_index}" if language == "en" else f"阶段 {phase_index}"),
                "goal": normalize_sentence(source.get("goal", source.get("summary", ""))),
                "sessions": sessions,
            }
        )

    if not phases:
        raw_tasks = plan.get("tasks", plan.get("schedule", [])) if isinstance(plan, dict) else []
        legacy_sessions = []
        for raw_task in raw_tasks if isinstance(raw_tasks, list) else []:
            if len(legacy_sessions) >= session_limit:
                break
            fallback_topic = keywords[len(legacy_sessions) % len(keywords)] if keywords else course
            default_title = f"Pressure-test {fallback_topic}" if language == "en" else f"围绕{fallback_topic}完成训练"
            legacy_sessions.append(normalize_session(raw_task, len(legacy_sessions), payload, language, default_title))
        if legacy_sessions:
            phases = [
                {
                    "id": "phase-01",
                    "title": "Execution plan" if language == "en" else "执行计划",
                    "goal": "Complete the schedulable study blocks in order." if language == "en" else "按顺序完成可独立排期的学习块。",
                    "sessions": legacy_sessions,
                }
            ]

    if not phases:
        fallback_count = max(1, min(int(payload.get("days_left", 1)), 3))
        sessions = []
        for index in range(fallback_count):
            topic = keywords[index % len(keywords)] if keywords else course
            title = f"Pressure-test {topic}" if language == "en" else f"围绕{topic}完成训练"
            sessions.append(normalize_session({"title": title}, index, payload, language, title))
        phases = [
            {
                "id": "phase-01",
                "title": "Survival sprint" if language == "en" else "保命冲刺",
                "goal": "Convert the supplied scope into checkable outputs." if language == "en" else "把给定范围压成可检查的产出。",
                "sessions": sessions,
            }
        ]

    seen_phase_ids = set()
    for phase_index, phase in enumerate(phases):
        if phase["id"] in seen_phase_ids:
            phase["id"] = f"phase-{phase_index + 1:02d}"
        seen_phase_ids.add(phase["id"])

    sessions = [session for phase in phases for session in phase["sessions"]]
    seen_session_ids = set()
    for session_index, session in enumerate(sessions):
        if session["id"] in seen_session_ids:
            session["id"] = f"session-{session_index + 1:02d}"
        seen_session_ids.add(session["id"])
    days_left = int(payload.get("days_left", 1))
    for index, session in enumerate(sessions):
        if session["day_index"]:
            continue
        if len(sessions) == 1:
            session["day_index"] = 1
        else:
            session["day_index"] = 1 + round(index * (days_left - 1) / (len(sessions) - 1))

    capacity_minutes = max(60, normalize_minutes(float(payload.get("hours_per_day", 8)) * days_left * 60, 60, 60, 365 * 1200))
    total_minutes = sum(session["duration_minutes"] for session in sessions)
    if total_minutes > capacity_minutes:
        scaled = allocate_step_minutes([session["duration_minutes"] for session in sessions], capacity_minutes)
        for session, duration in zip(sessions, scaled):
            session["duration_minutes"] = duration
            rebalance_step_minutes(session["steps"], duration)
    return phases


def flatten_phase_sessions(phases):
    tasks = []
    for phase in phases:
        for session in phase["sessions"]:
            task = dict(session)
            task["phase_id"] = phase["id"]
            task["phase_title"] = phase["title"]
            task["phase_goal"] = phase["goal"]
            tasks.append(task)
    return tasks


def sanitize_plan(plan: dict, payload: dict) -> dict:
    keywords = extract_keywords(payload)
    language, language_source, language_confidence = resolve_content_language(payload)
    default_course = "Current course" if language == "en" else "当前课程"
    course = str(payload.get("course", default_course)).strip() or default_course
    mode = detect_subject_mode(course, keywords)
    kw_fallback = keywords if keywords else [course]
    calc_words = ["计算题", "公式", "推导", "积分", "数值", "建模", "证明", "calculation", "formula", "derivation", "integral", "numeric", "modeling", "proof"]
    memory_words = ["名词解释", "论述", "选择题", "填空题", "背诵", "记忆", "definition", "essay", "multiple choice", "fill in the blank", "memorize", "memory"]

    result = dict(plan) if isinstance(plan, dict) else {}
    must = [normalize_sentence(x) for x in normalize_list(result.get("must", []), 5)]
    drop = [normalize_sentence(x) for x in normalize_list(result.get("drop", []), 3)]
    hits = [normalize_sentence(x) for x in normalize_list(result.get("hits", []), 10)]

    for i, item in enumerate(hits):
        if not item:
            suffix = "high-frequency exam target" if language == "en" else "高频考点"
            hits[i] = f"{kw_fallback[i % len(kw_fallback)]} × {suffix}"
            continue
        if mode == "memory" and any(word in item for word in calc_words):
            suffix = "high-frequency recall target" if language == "en" else "高频记忆点"
            hits[i] = f"{kw_fallback[i % len(kw_fallback)]} × {suffix}"
        if mode == "calc" and any(word in item for word in memory_words):
            suffix = "high-frequency calculation target" if language == "en" else "高频计算点"
            hits[i] = f"{kw_fallback[i % len(kw_fallback)]} × {suffix}"

    generic_drop = [
        "low-frequency obscure chapters", "ultra-long derivation problems", "high-time low-return edge cases"
    ] if language == "en" else ["低频冷门章节深挖", "超长推导压轴题", "高耗时低收益边角点"]
    uniq_keywords = []
    for keyword in keywords:
        if keyword and keyword not in uniq_keywords:
            uniq_keywords.append(keyword)
    source_keywords = (uniq_keywords[-3:] if len(uniq_keywords) >= 3 else uniq_keywords) or [course]
    if mode == "memory":
        dynamic_suffix = ["low-frequency extension", "obscure source-question variant", "high time cost, low return"] if language == "en" else ["低频延展", "材料题冷门变体", "耗时长收益低"]
    else:
        dynamic_suffix = ["complex transformed problem", "overlong derivation chain", "low-frequency boundary condition"] if language == "en" else ["复杂变形题", "超长推导链", "低频边界条件"]
    if language == "en":
        dynamic_drop = [f"Defer: {source_keywords[i % len(source_keywords)]} ({dynamic_suffix[i]})" for i in range(3)]
    else:
        dynamic_drop = [f"暂缓：{source_keywords[i % len(source_keywords)]}（{dynamic_suffix[i]}）" for i in range(3)]
    for i, item in enumerate(drop):
        if not item or item in generic_drop or any(keyword == item or (keyword in item and len(keyword) >= 3) for keyword in keywords):
            drop[i] = dynamic_drop[i]

    for i, item in enumerate(must):
        if not item:
            suffix = "high-yield core marks" if language == "en" else "高频核心拿分"
            must[i] = f"{kw_fallback[i % len(kw_fallback)]}: {suffix}"

    phases = normalize_phases(result, payload, language, keywords, course)
    result["schema_version"] = 4
    result["content_language"] = language
    result["language_source"] = language_source
    result["language_confidence"] = language_confidence
    result["must"] = must
    result["drop"] = drop
    result["phases"] = phases
    result["tasks"] = flatten_phase_sessions(phases)
    result["hits"] = hits
    if not str(result.get("headline", "")).strip():
        if language == "en":
            result["headline"] = f"{course}: {'calculation' if mode == 'calc' else 'recall'} survival sprint"
        else:
            result["headline"] = f"{course}：{ '理解计算型' if mode == 'calc' else '背诵记忆型' }冲刺"
    if not str(result.get("summary", "")).strip():
        result["summary"] = "Bank certain marks first, then chase upside. Zero low-return work." if language == "en" else "先拿确定分，再处理增益分，严禁无效投入。"
    return result


def parse_json_from_text(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    fence = re.search(r"```json\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence and fence.group(1):
        return json.loads(fence.group(1).strip())
    block = re.search(r"\{[\s\S]*\}", raw)
    if block:
        return json.loads(block.group(0))
    raise ValueError("模型未返回可解析JSON")


def normalize_list(v, count):
    if isinstance(v, list):
        items = [str(x) for x in v if str(x).strip()]
    elif isinstance(v, str) and v.strip():
        parts = [p.strip() for p in re.split(r"[，,;；\n、]", v) if p.strip()]
        items = parts
    else:
        items = []
    if len(items) >= count:
        return items[:count]
    return items + [""] * (count - len(items))


def validate_plan_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PayloadValidationError("Request body must be a JSON object")

    course = str(payload.get("course", "")).strip()
    if not 2 <= len(course) <= 80:
        raise PayloadValidationError("course must contain 2-80 characters")

    try:
        days_left = int(payload.get("days_left"))
    except (TypeError, ValueError):
        raise PayloadValidationError("days_left must be an integer") from None
    if not 1 <= days_left <= 365:
        raise PayloadValidationError("days_left must be between 1 and 365")

    try:
        hours_per_day = float(payload.get("hours_per_day"))
    except (TypeError, ValueError):
        raise PayloadValidationError("hours_per_day must be a number") from None
    if not 1 <= hours_per_day <= 20:
        raise PayloadValidationError("hours_per_day must be between 1 and 20")

    try:
        goal_score = int(payload.get("goal_score"))
    except (TypeError, ValueError):
        raise PayloadValidationError("goal_score must be an integer") from None
    if float(payload.get("goal_score")) != goal_score or not 60 <= goal_score <= 100:
        raise PayloadValidationError("goal_score must be a whole number between 60 and 100")

    keywords = str(payload.get("keywords", "")).strip()
    keywords_max_chars = int(os.environ.get("KEYWORDS_MAX_CHARS", "1000"))
    if not 3 <= len(keywords) <= keywords_max_chars:
        raise PayloadValidationError(f"keywords must contain 3-{keywords_max_chars} characters")

    language = normalize_language(payload.get("language", ""))
    if payload.get("language") and not language:
        raise PayloadValidationError("language must be zh or en")
    ui_language = normalize_language(payload.get("ui_language", ""))
    if payload.get("ui_language") and not ui_language:
        raise PayloadValidationError("ui_language must be zh or en")
    content_language = normalize_language(payload.get("content_language", ""))
    if payload.get("content_language") and not content_language:
        raise PayloadValidationError("content_language must be zh or en")
    content_language_source = str(payload.get("content_language_source", "") or "").strip().lower()
    if content_language_source not in {"", "auto", "manual"}:
        raise PayloadValidationError("content_language_source must be auto or manual")
    if content_language_source == "manual" and not content_language:
        raise PayloadValidationError("manual content language requires content_language")
    input_language = normalize_language(payload.get("input_language", ""))
    if payload.get("input_language") and not input_language:
        raise PayloadValidationError("input_language must be zh or en")

    clean = dict(payload)
    clean.update(
        {
            "course": course,
            "days_left": days_left,
            "hours_per_day": hours_per_day,
            "goal_score": goal_score,
            "keywords": keywords,
            "language": language,
            "ui_language": ui_language,
            "content_language": content_language,
            "content_language_source": content_language_source,
            "input_language": input_language,
        }
    )
    return clean


def collect_plan_language_text(plan: dict) -> str:
    values = []

    def add(value):
        text = str(value or "").strip()
        if text:
            values.append(text)

    add(plan.get("headline"))
    add(plan.get("summary"))
    for key in ("must", "drop", "hits"):
        for item in plan.get(key, []) if isinstance(plan.get(key), list) else []:
            add(item)
    for phase in plan.get("phases", []) if isinstance(plan.get("phases"), list) else []:
        if not isinstance(phase, dict):
            continue
        add(phase.get("title"))
        add(phase.get("goal"))
        for session in phase.get("sessions", []) if isinstance(phase.get("sessions"), list) else []:
            if not isinstance(session, dict):
                continue
            add(session.get("title"))
            add(session.get("success_criteria", session.get("successCriteria")))
            for step in session.get("steps", []) if isinstance(session.get("steps"), list) else []:
                if not isinstance(step, dict):
                    continue
                add(step.get("action", step.get("title")))
                add(step.get("output", step.get("success_criteria")))
    return "\n".join(values)


def plan_matches_content_language(plan: dict, payload: dict, target_language: str) -> bool:
    audit_text = collect_plan_language_text(plan)
    protected_values = [str(payload.get("course", "")).strip(), *extract_keywords(payload)]
    for value in sorted((item for item in protected_values if len(item) >= 2), key=len, reverse=True):
        audit_text = audit_text.replace(value, " ")
    detected, confidence = analyze_text_language(audit_text)
    return not detected or confidence < 0.64 or detected == target_language


def request_chat_completion(api_key: str, api_base: str, model: str, messages: list, temperature: float) -> str:
    target_url = f"{api_base}/chat/completions"
    req_body = json.dumps(
        {
            "model": model,
            "temperature": temperature,
            "max_tokens": int(os.environ.get("AI_MAX_TOKENS", "8000")),
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        target_url,
        data=req_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    timeout_sec = int(os.environ.get("AI_TIMEOUT_SEC", "120"))
    retry_count = int(os.environ.get("AI_RETRY_COUNT", "1"))
    body = ""
    for attempt in range(retry_count + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            retriable = e.code in {429, 500, 502, 503, 504}
            if retriable and attempt < retry_count:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise RuntimeError(f"模型接口失败：{e.code} {err[:220]}")
        except Exception as e:
            msg = str(e)
            timed_out = "timed out" in msg.lower()
            if timed_out and attempt < retry_count:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise RuntimeError(f"模型接口不可用：{msg}")
    data = json.loads(body)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError("模型返回为空")
    return content


def call_model(payload: dict) -> dict:
    api_key = os.environ.get("AI_API_KEY", "").strip()
    if not api_key or api_key.startswith("<<"):
        raise RuntimeError("请在 .env 中填写真实 AI_API_KEY")
    api_base = os.environ.get("AI_API_BASE", "https://api.siliconflow.cn/v1").strip().rstrip("/")
    model = os.environ.get("AI_API_MODEL", "Qwen/Qwen2.5-7B-Instruct").strip()
    language = resolve_response_language(payload)
    default_course = "Current course" if language == "en" else "当前课程"
    course = str(payload.get("course", default_course)).strip() or default_course
    days_left = int(payload.get("days_left", 3) or 3)
    hours_per_day = float(payload.get("hours_per_day", 8) or 8)
    goal_score = int(payload.get("goal_score", 80) or 80)
    suggested_sessions = min(days_left, 28)
    keywords = str(payload.get("keywords", "")).strip()
    keywords_max_chars = int(os.environ.get("KEYWORDS_MAX_CHARS", "1000"))
    if len(keywords) > keywords_max_chars:
        keywords = keywords[:keywords_max_chars]
    if language == "en":
        user_prompt = (
            f"Course: {course}\nDays remaining: {days_left}\nStudy hours per day: {hours_per_day:g}\n"
            f"Target score: {goal_score}\nMaterial keywords: {keywords}\n"
            f"Suggested schedulable Sessions: about {suggested_sessions}. Use fewer only for deliberate buffer days; do not force a fixed count.\n"
            "Generate the plan in English."
        )
        runtime_language_contract = (
            "Runtime output language: English. Every string value in the JSON must be English. "
            "Keep the JSON property names unchanged and keep each step role as the fixed enum setup, execute, or review. "
            "Do not mix in Chinese except inside an untranslatable proper noun supplied by the user."
        )
    else:
        user_prompt = (
            f"课程：{course}\n剩余天数：{days_left}\n每日学习时长：{hours_per_day:g}\n目标分：{goal_score}\n资料关键词：{keywords}\n"
            f"建议生成约 {suggested_sessions} 个可独立排期的 Session；只有明确设置缓冲日时才减少，禁止固定数量。\n"
            "请用简体中文生成计划。"
        )
        runtime_language_contract = (
            "运行时输出语言：简体中文。除固定枚举 role（setup、execute、review）外，JSON 中的所有字符串值必须使用简体中文，JSON 属性名保持不变。"
        )
    base_system_prompt = f"{load_system_prompt()}\n\n# Runtime Content Language Contract\n{runtime_language_contract}"
    for language_attempt in range(2):
        correction = ""
        if language_attempt:
            correction = (
                "\n\nThe previous response was rejected because explanatory content mixed languages. "
                "Regenerate from scratch and use English for every generated content string; keep role as setup, execute, or review."
                if language == "en"
                else "\n\n上一份结果因内容语言混杂被拒绝。请从头重新生成；除 role 固定使用 setup、execute、review 外，所有生成的内容字符串必须使用简体中文。"
            )
        content = request_chat_completion(
            api_key,
            api_base,
            model,
            [
                {"role": "system", "content": f"{base_system_prompt}{correction}"},
                {"role": "user", "content": user_prompt},
            ],
            0.4 if language_attempt == 0 else 0.2,
        )
        raw_plan = parse_json_from_text(content)
        plan = sanitize_plan(raw_plan, payload)
        if plan_matches_content_language(plan, payload, language):
            return plan
    raise RuntimeError("模型连续两次未遵循内容语言要求，请检查输入语言或重试")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-App-Name", APP_NAME)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        flag = os.environ.get("REQUIRE_APP_TOKEN", "false").strip().lower()
        require_token = flag in {"1", "true", "yes", "on"}
        if not require_token:
            return True
        cookie = self.headers.get("Cookie", "")
        sid_match = re.search(r"(?:^|;\s*)sid=([^;]+)", cookie)
        if not sid_match:
            return False
        sid = sid_match.group(1).strip()
        with SESSIONS_LOCK:
            expires_at = SESSIONS.get(sid, 0)
        return expires_at > time.time()

    def _create_session(self):
        sid = uuid.uuid4().hex
        ttl_sec = int(os.environ.get("SESSION_TTL_SEC", "43200"))
        with SESSIONS_LOCK:
            SESSIONS[sid] = time.time() + ttl_sec
        return sid, ttl_sec

    def _clear_session(self):
        cookie = self.headers.get("Cookie", "")
        sid_match = re.search(r"(?:^|;\s*)sid=([^;]+)", cookie)
        if sid_match:
            sid = sid_match.group(1).strip()
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)

    def do_GET(self):
        request_path = unquote(urlsplit(self.path).path)
        if request_path == "/api/health":
            flag = os.environ.get("REQUIRE_APP_TOKEN", "false").strip().lower()
            require_token = flag in {"1", "true", "yes", "on"}
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": APP_NAME,
                    "version": APP_VERSION,
                    "require_app_token": require_token,
                    "port": int(os.environ.get("PORT", "8010")),
                    "pid": os.getpid(),
                },
            )
            return
        if request_path == "/api/workspaces":
            workspaces = list_course_workspaces()
            self._send_json(200, {"ok": True, "workspaces": workspaces})
            return
        if request_path.startswith("/api/workspaces/"):
            workspace_id = request_path.removeprefix("/api/workspaces/").strip("/")
            try:
                workspace = load_course_workspace(workspace_id)
                self._send_json(200, {"ok": True, "workspace": workspace})
            except PayloadValidationError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except FileNotFoundError:
                self._send_json(404, {"ok": False, "error": "Workspace not found"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(500, {"ok": False, "error": f"Invalid workspace: {exc}"})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/auth":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(raw or "{}")
                expected = os.environ.get("APP_AUTH_TOKEN", "").strip()
                token = str(payload.get("token", "")).strip()
                if not expected or token != expected:
                    self._send_json(401, {"ok": False, "error": "Unauthorized"})
                    return
                sid, ttl_sec = self._create_session()
                body = json.dumps({"ok": True, "message": "authenticated"}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", f"sid={sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age={ttl_sec}")
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
                return
        if self.path == "/api/logout":
            self._clear_session()
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "sid=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/api/plan":
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return
        try:
            if not self._auth_ok():
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            client_ip = (self.client_address[0] or "").strip() or "unknown"
            allowed, retry_after = check_rate_limit(client_ip)
            if not allowed:
                self.send_response(429)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Retry-After", str(retry_after))
                body = json.dumps({"ok": False, "error": "Too Many Requests"}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            length = int(self.headers.get("Content-Length", "0"))
            max_body = int(os.environ.get("MAX_BODY_BYTES", "20000"))
            if length > max_body:
                self._send_json(413, {"ok": False, "error": "Payload Too Large"})
                return
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            payload = json.loads(raw or "{}")
            payload = validate_plan_payload(payload)
            plan = call_model(payload)
            self._send_json(200, {"ok": True, "plan": plan})
        except (json.JSONDecodeError, PayloadValidationError) as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


def main():
    load_env_file()
    host = os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("PORT", "8010"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
