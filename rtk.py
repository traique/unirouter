"""RTK Token Saver (port rút gọn từ 9router/open-sse/rtk).

Nén nội dung tool_result (git diff, grep, log lặp, output rất dài...) trong request
trước khi gửi lên provider. Không bao giờ làm output dài hơn hay rỗng; bỏ qua lỗi.
"""
from __future__ import annotations

import re
from typing import Any

MIN_SIZE = 500
RAW_CAP = 10 * 1024 * 1024
DETECT_WINDOW = 1024
HUNK_MAX = 100
DIFF_MAX_LINES = 500
GREP_PER_FILE = 10
DEDUP_MAX = 2000
TRUNC_HEAD, TRUNC_TAIL, TRUNC_MIN = 120, 60, 250

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RE_DIFF = re.compile(r"^(diff --git |@@ )", re.M)
RE_BUILD = re.compile(r"^(npm (warn|error|ERR!)|yarn (warn|error)|\s*Compiling\s+\S+|\s*Downloading\s+\S+|"
                      r"added \d+ package|Collecting \S+|\s*Using cached |Requirement already satisfied)", re.M | re.I)
RE_PROGRESS = re.compile(r"^\s*(Compiling|Downloading|Collecting|Using cached|Requirement already satisfied|"
                         r"Installing|Fetching|Resolving|Checking|Building)\b", re.I)


def git_diff(text: str) -> str:
    out: list[str] = []
    current, added, removed = "", 0, 0
    in_hunk, shown, skipped = False, 0, 0

    def flush_skip() -> None:
        nonlocal skipped
        if skipped:
            out.append(f"  ... ({skipped} lines truncated)")
            skipped = 0

    for line in text.split("\n"):
        if line.startswith("diff --git"):
            flush_skip()
            if current and (added or removed):
                out.append(f"  +{added} -{removed}")
            parts = line.split(" b/", 1)
            current = parts[1] if len(parts) > 1 else "unknown"
            out.append(f"\n{current}")
            added = removed = 0
            in_hunk, shown = False, 0
        elif line.startswith("@@"):
            flush_skip()
            in_hunk, shown = True, 0
            out.append(f"  {line}")
        elif in_hunk:
            is_add = line.startswith("+") and not line.startswith("+++")
            is_del = line.startswith("-") and not line.startswith("---")
            if is_add or is_del:
                added += is_add
                removed += is_del
                if shown < HUNK_MAX:
                    out.append(f"  {line}")
                    shown += 1
                else:
                    skipped += 1
            elif shown and shown < HUNK_MAX and not line.startswith("\\"):
                out.append(f"  {line}")
                shown += 1
        if len(out) >= DIFF_MAX_LINES:
            out.append("\n... (more changes truncated)")
            break
    flush_skip()
    if current and (added or removed):
        out.append(f"  +{added} -{removed}")
    return "\n".join(out)


def _grep_line(line: str) -> tuple[str, str, str] | None:
    parts = line.split(":", 2)
    if len(parts) == 3 and parts[1].isdigit():
        return parts[0], parts[1], parts[2]
    return None


def grep(text: str) -> str:
    by_file: dict[str, list[tuple[str, str]]] = {}
    total = 0
    for line in text.split("\n"):
        g = _grep_line(line)
        if g:
            total += 1
            by_file.setdefault(g[0], []).append((g[1], g[2]))
    if not total:
        return text
    out = [f"{total} matches in {len(by_file)}F:\n"]
    for f in sorted(by_file):
        matches = by_file[f]
        out.append(f"[file] {f} ({len(matches)}):")
        for num, content in matches[:GREP_PER_FILE]:
            out.append(f"  {num.rjust(4)}: {content.strip()}")
        if len(matches) > GREP_PER_FILE:
            out.append(f"  +{len(matches) - GREP_PER_FILE}")
        out.append("")
    return "\n".join(out)


def build_output(text: str) -> str:
    """Gom các dòng tiến trình (Compiling/Downloading/...) giữ lại lỗi/cảnh báo."""
    out: list[str] = []
    progress = 0
    for line in text.split("\n"):
        if RE_PROGRESS.match(line):
            progress += 1
            continue
        if progress:
            out.append(f"  ... ({progress} progress lines)")
            progress = 0
        out.append(line)
    if progress:
        out.append(f"  ... ({progress} progress lines)")
    return dedup_log("\n".join(out))


def dedup_log(text: str) -> str:
    out: list[str] = []
    prev, run, blanks = None, 0, 0

    def flush() -> None:
        if prev is not None and run > 1:
            out.append(f"  ... ({run - 1} duplicate lines)")

    for line in text.split("\n"):
        if not line.strip():
            if blanks < 1:
                out.append(line)
            blanks += 1
            flush()
            prev, run = None, 0
            continue
        blanks = 0
        if line == prev:
            run += 1
            continue
        flush()
        out.append(line)
        prev, run = line, 1
        if len(out) >= DEDUP_MAX:
            out.append(f"... (truncated at {DEDUP_MAX} lines)")
            return "\n".join(out)
    flush()
    return "\n".join(out)


def smart_truncate(text: str) -> str:
    lines = text.split("\n")
    if len(lines) < TRUNC_MIN:
        return text
    cut = len(lines) - TRUNC_HEAD - TRUNC_TAIL
    return "\n".join(lines[:TRUNC_HEAD] + [f"... +{cut} lines truncated"] + lines[-TRUNC_TAIL:])


def detect(text: str):
    head = text[:DETECT_WINDOW]
    if RE_DIFF.search(head):
        return git_diff
    if RE_BUILD.search(head):
        return build_output
    non_empty = [l for l in head.split("\n") if l.strip()]
    if any(_grep_line(l) for l in non_empty[:5]):
        return grep
    if len(non_empty) >= 5:
        return lambda t: smart_truncate(dedup_log(t))
    if text.count("\n") >= TRUNC_MIN:
        return smart_truncate
    return None


def compress_text(text: str) -> str:
    if len(text) < MIN_SIZE or len(text) > RAW_CAP:
        return text
    cleaned = ANSI.sub("", text)
    fn = detect(cleaned)
    try:
        out = fn(cleaned) if fn else cleaned
    except Exception:
        return text
    if not out or len(out) >= len(text):
        return text
    return out


def compress_chat(body: dict[str, Any]) -> int:
    """Nén tại chỗ các message role=tool. Trả về số ký tự tiết kiệm được."""
    saved = 0
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if content.startswith("[ERROR]"):
                continue
            new = compress_text(content)
            saved += len(content) - len(new)
            msg["content"] = new
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    new = compress_text(part["text"])
                    saved += len(part["text"]) - len(new)
                    part["text"] = new
    return saved


def compress_responses(body: dict[str, Any]) -> int:
    saved = 0
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            out = item.get("output")
            if isinstance(out, str):
                new = compress_text(out)
                saved += len(out) - len(new)
                item["output"] = new
            elif isinstance(out, list):
                for part in out:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        new = compress_text(part["text"])
                        saved += len(part["text"]) - len(new)
                        part["text"] = new
    return saved
