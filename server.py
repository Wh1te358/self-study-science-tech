import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "study-sprint-api"
APP_VERSION = "2026-07-21-p1-1"
SYSTEM_PROMPT = (
    "You are an exam-cram planning assistant. Return only one JSON object with no explanation. "
    "Schema: headline:string, summary:string, must:string[5], drop:string[3], "
    "schedule:string[6], hits:string[10]. Follow the runtime output-language instruction exactly; "
    "if it is absent, match the language used in the course name and keywords. Keep every sentence short and action-oriented."
)
RATE_BUCKET = {}
RATE_LOCK = threading.Lock()
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


class PayloadValidationError(ValueError):
    pass


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


def detect_text_language(text: str) -> str:
    content = str(text or "")
    han_count = len(re.findall(r"[\u3400-\u9fff]", content))
    latin_count = len(re.findall(r"[A-Za-z]", content))
    if han_count == 0 and latin_count == 0:
        return ""
    return "zh" if han_count > max(1, latin_count * 0.12) else "en"


def resolve_response_language(payload: dict) -> str:
    frontend_language = normalize_language(payload.get("language", ""))
    if frontend_language:
        return frontend_language
    declared_input_language = normalize_language(payload.get("input_language", ""))
    if declared_input_language:
        return declared_input_language
    text = f"{payload.get('course', '')}\n{payload.get('keywords', '')}"
    return detect_text_language(text) or "zh"


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


def sanitize_plan(plan: dict, payload: dict) -> dict:
    keywords = extract_keywords(payload)
    language = resolve_response_language(payload)
    default_course = "Current course" if language == "en" else "当前课程"
    course = str(payload.get("course", default_course)).strip() or default_course
    mode = detect_subject_mode(course, keywords)
    kw_fallback = keywords if keywords else [course]
    calc_words = ["计算题", "公式", "推导", "积分", "数值", "建模", "证明", "calculation", "formula", "derivation", "integral", "numeric", "modeling", "proof"]
    memory_words = ["名词解释", "论述", "选择题", "填空题", "背诵", "记忆", "definition", "essay", "multiple choice", "fill in the blank", "memorize", "memory"]

    must = [normalize_sentence(x) for x in normalize_list(plan.get("must", []), 5)]
    drop = [normalize_sentence(x) for x in normalize_list(plan.get("drop", []), 3)]
    schedule = [normalize_sentence(x) for x in normalize_list(plan.get("schedule", []), 6)]
    hits = [normalize_sentence(x) for x in normalize_list(plan.get("hits", []), 10)]

    for i, item in enumerate(hits):
        if not item:
            suffix = "high-frequency exam target" if language == "en" else "高频考点"
            hits[i] = f"{kw_fallback[i % len(kw_fallback)]} × {suffix}"
            continue
        if mode == "memory" and any(w in item for w in calc_words):
            suffix = "high-frequency recall target" if language == "en" else "高频记忆点"
            hits[i] = f"{kw_fallback[i % len(kw_fallback)]} × {suffix}"
        if mode == "calc" and any(w in item for w in memory_words):
            suffix = "high-frequency calculation target" if language == "en" else "高频计算点"
            hits[i] = f"{kw_fallback[i % len(kw_fallback)]} × {suffix}"

    generic_drop = [
        "low-frequency obscure chapters", "ultra-long derivation problems", "high-time low-return edge cases"
    ] if language == "en" else ["低频冷门章节深挖", "超长推导压轴题", "高耗时低收益边角点"]
    uniq_keywords = []
    for kw in keywords:
        if kw and kw not in uniq_keywords:
            uniq_keywords.append(kw)
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
        if not item:
            drop[i] = dynamic_drop[i]
            continue
        if item in generic_drop:
            drop[i] = dynamic_drop[i]
            continue
        # 防止“可放弃清单”机械地把原关键词原样搬运
        if any(kw == item or (kw in item and len(kw) >= 3) for kw in keywords):
            drop[i] = dynamic_drop[i]

    for i, item in enumerate(must):
        if not item:
            suffix = "high-yield core marks" if language == "en" else "高频核心拿分"
            must[i] = f"{kw_fallback[i % len(kw_fallback)]}: {suffix}"

    for i, item in enumerate(schedule):
        if not item:
            if language == "en":
                schedule[i] = f"Hours {i * 4 + 1}-{(i + 1) * 4}: pressure-test {kw_fallback[i % len(kw_fallback)]}"
            else:
                schedule[i] = f"第{i * 4 + 1}-{(i + 1) * 4}小时：围绕{kw_fallback[i % len(kw_fallback)]}高压训练"

    plan["must"] = must
    plan["drop"] = drop
    plan["schedule"] = schedule
    plan["hits"] = hits
    if not str(plan.get("headline", "")).strip():
        if language == "en":
            plan["headline"] = f"{course}: {'calculation' if mode == 'calc' else 'recall'} survival sprint"
        else:
            plan["headline"] = f"{course}：{ '理解计算型' if mode == 'calc' else '背诵记忆型' }冲刺"
    if not str(plan.get("summary", "")).strip():
        plan["summary"] = "Bank certain marks first, then chase upside. Zero low-return work." if language == "en" else "先拿确定分，再处理增益分，严禁无效投入。"
    return plan


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
            "input_language": input_language,
        }
    )
    return clean


def call_model(payload: dict) -> dict:
    api_key = os.environ.get("AI_API_KEY", "").strip()
    if not api_key or api_key.startswith("<<"):
        raise RuntimeError("请在 .env 中填写真实 AI_API_KEY")
    api_base = os.environ.get("AI_API_BASE", "https://api.siliconflow.cn/v1").strip().rstrip("/")
    model = os.environ.get("AI_API_MODEL", "Qwen/Qwen2.5-7B-Instruct").strip()
    target_url = f"{api_base}/chat/completions"
    language = resolve_response_language(payload)
    default_course = "Current course" if language == "en" else "当前课程"
    course = str(payload.get("course", default_course)).strip() or default_course
    days_left = int(payload.get("days_left", 3) or 3)
    hours_per_day = float(payload.get("hours_per_day", 8) or 8)
    goal_score = int(payload.get("goal_score", 80) or 80)
    keywords = str(payload.get("keywords", "")).strip()
    keywords_max_chars = int(os.environ.get("KEYWORDS_MAX_CHARS", "1000"))
    if len(keywords) > keywords_max_chars:
        keywords = keywords[:keywords_max_chars]
    if language == "en":
        user_prompt = (
            f"Course: {course}\nDays remaining: {days_left}\nStudy hours per day: {hours_per_day:g}\n"
            f"Target score: {goal_score}\nMaterial keywords: {keywords}\nGenerate the plan in English."
        )
        runtime_language_contract = (
            "Runtime output language: English. Every string value in the JSON must be English. "
            "Keep the JSON property names unchanged. Do not mix in Chinese except inside an untranslatable proper noun supplied by the user."
        )
    else:
        user_prompt = f"课程：{course}\n剩余天数：{days_left}\n每日学习时长：{hours_per_day:g}\n目标分：{goal_score}\n资料关键词：{keywords}\n请用简体中文生成计划。"
        runtime_language_contract = (
            "运行时输出语言：简体中文。JSON 中的所有字符串值必须使用简体中文，JSON 属性名保持不变。"
        )
    system_prompt = f"{load_system_prompt()}\n\n# Runtime Language Contract\n{runtime_language_contract}"
    req_body = json.dumps(
        {
            "model": model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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
    raw_plan = parse_json_from_text(content)
    plan = {
        "headline": str(raw_plan.get("headline", "已生成计划")),
        "summary": str(raw_plan.get("summary", "")),
        "must": normalize_list(raw_plan.get("must", []), 5),
        "drop": normalize_list(raw_plan.get("drop", []), 3),
        "schedule": normalize_list(raw_plan.get("schedule", []), 6),
        "hits": normalize_list(raw_plan.get("hits", []), 10),
    }
    return sanitize_plan(plan, payload)


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
        if self.path == "/api/health":
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
