#!/usr/bin/env python3
"""ghostty-cc-bilingual —— Claude Code 双语对照副屏（Ghostty 面板版）。

用法（交互）:
  python3 cc_bilingual.py --cwd <项目路径>            # 与面板1的 claude 同 cwd
  python3 cc_bilingual.py --cwd <项目路径> --terminal <面板UUID>
                                                      # 同 cwd 有多个面板时显式指定注入目标

面板1 正常跑 `claude`；本工具在旁边的分屏里跑：
  · 输入中文 → 翻译成英文 → 显示中英对照 → 注入英文到面板1
  · 监听 session 日志 → 面板1的英文回复 → 逐段显示英文原文 + 中文译文

其他模式:
  --once       处理一行输入（或 --once-text）→ 发送 → 等到回复翻译完 → 退出（脚本/测试用）
  --watch-only 只看不做（纯输出方向副屏）
  --no-inject  显示对照但不注入（练习模式）
  --dry        注入前 dry-run 演练，不真发

命令（交互输入框）:
  /en 文本      不翻译、直接发英文
  /quit         退出
  /help         帮助
"""
import argparse
import glob
import json
import os
import queue
import re
import select
import subprocess
import sys
import termios
import threading
import time
import unicodedata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import translate as T  # noqa: E402

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
INJECT_SH = os.path.join(BASE_DIR, "inject.sh")
DATA_DIR = T.DATA_DIR
TARGETS_PATH = os.path.join(DATA_DIR, "targets.json")
CRASH_LOG = os.path.join(DATA_DIR, "crash.log")


def _log_crash(label, tb):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CRASH_LOG, "a") as fh:
            fh.write("=== %s %s ===\n%s\n" % (label, time.strftime("%F %T"), tb))
    except Exception:
        pass

DIM, GRN, BLU, CYN, MAG, YEL, RED, RST, BOLD = (
    "\x1b[2m", "\x1b[32m", "\x1b[34m", "\x1b[36m",
    "\x1b[35m", "\x1b[33m", "\x1b[31m", "\x1b[0m", "\x1b[1m")

# 「我」说的话用反显色块表示，不再打「你说 / Claude 说」这类字眼：
#   正显（默认色） = Claude 的回复      反显（黑底色块） = 你说的话
REV, REV_OFF = "\x1b[7m", "\x1b[27m"


# ---------------- session 解析 ----------------

def resolve_session(cwd, now=None):
    """返回该 cwd 下最新的 session jsonl 路径；找不到返回 None；
    两个候选更新时间 <120s 视为歧义，返回 "ambiguous" 字符串。"""
    hits = []
    for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        first_cwd = ""
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    c = d.get("cwd")
                    if c:
                        first_cwd = c
                        break
        except OSError:
            continue
        if first_cwd == cwd:
            hits.append((os.path.getmtime(f), f))
    if not hits:
        return None
    hits.sort(reverse=True)
    if len(hits) > 1 and (hits[0][0] - hits[1][0]) < 120:
        return "ambiguous"
    return hits[0][1]


def today_start():
    """今天 0 点的本地时间戳（用于只列今天的会话）。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def session_candidates(cwd):
    """返回该 cwd 下的候选会话，按 mtime 降序：[(mtime, path), ...]。"""
    hits = []
    for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        first_cwd = ""
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    c = d.get("cwd")
                    if c:
                        first_cwd = c
                        break
        except OSError:
            continue
        if first_cwd == cwd:
            hits.append((os.path.getmtime(f), f))
    hits.sort(reverse=True)
    return hits


def session_info(path):
    """会话摘要（供选择器展示）：条数 + 首条/最近用户消息。"""
    n = 0
    first_user = last_user = ""
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                n += 1
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message") or {}
                if d.get("type") == "user" and isinstance(m.get("content"), str):
                    t = " ".join(m["content"].split())
                    if t:
                        if not first_user:
                            first_user = t
                        last_user = t
    except OSError:
        pass
    return {"n": n, "first_user": first_user, "last_user": last_user}


def human_age(mtime):
    dt = max(0, time.time() - mtime)
    if dt < 90:
        return "%d 秒前" % int(dt)
    if dt < 3600:
        return "%d 分钟前" % int(dt / 60)
    if dt < 86400:
        return "%d 小时前" % int(dt / 3600)
    return "%d 天前" % int(dt / 86400)


def choose_session(cands):
    """交互式选择要跟随的会话；返回 path，取消返回 None。"""
    print(YEL + "⚠ 该目录今天有 %d 个更新时间接近的活跃会话，请选择要跟随哪个：" % len(cands) + RST)
    for i, (mt, path) in enumerate(cands, 1):
        info = session_info(path)
        print("  %s[%d]%s %s · %d 条 · %s" % (
            BOLD, i, RST, human_age(mt), info["n"], os.path.basename(path)[:8]))
        print("      " + DIM + "首条：" + (info["first_user"][:72] or "(无用户消息)") + RST)
        if info["last_user"] and info["last_user"] != info["first_user"]:
            print("      " + DIM + "最近：" + info["last_user"][:72] + RST)
    while True:
        try:
            ans = input(GRN + "输入编号选择（回车=最新那个，q=退出）: " + RST).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans.lower() in ("q", "quit"):
            return None
        if not ans:
            return cands[0][1]
        if ans.isdigit() and 1 <= int(ans) <= len(cands):
            return cands[int(ans) - 1][1]
        print(RED + "请输入 1-%d 之间的编号（或 q 退出）。" % len(cands) + RST)


def wait_session(cwd, timeout=300):
    """轮询等待会话文件出现/明确。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = resolve_session(cwd)
        if r == "ambiguous":
            print(YEL + "⚠ 该目录有两个活跃会话，无法自动判断。" + RST)
            print("  请用 --session <路径> 明确指定（会话文件在 ~/.claude/projects/*/ 下）。")
            return None
        if r:
            return r
        time.sleep(4)
    return None


# ---------------- jsonl 解析 ----------------

def _text_of(block):
    return block.get("text", "") if isinstance(block, dict) else ""


# Claude Code 会往会话里注入系统消息（后台任务通知、系统提醒、本地命令输出等），
# 它们 role 也是 user，但不是用户说的话 —— 副屏不显示、也不翻译。
_SYSTEM_USER_TAGS = (
    "<task-notification>", "<system-reminder>", "<local-command-stdout>",
    "<local-command-caveat>", "<command-name>", "<command-message>",
    "<command-args>", "<user-prompt-submit-hook>", "<bash-input>",
    "<bash-stdout>", "<bash-interrupt>",
)


def is_system_user_text(txt):
    t = txt.lstrip()
    if t.startswith(_SYSTEM_USER_TAGS):
        return True
    if "<task-notification>" in t:
        return True
    if t.startswith("Caveat: The messages below were generated by the user while running local commands"):
        return True
    return False


def parse_line(line, session_id):
    """解析一条 jsonl → 事件 dict 或 None。只认主 session 的记录。"""
    try:
        d = json.loads(line)
    except Exception:
        return None
    if d.get("sessionId", session_id) != session_id:
        return None
    t = d.get("type")
    m = d.get("message") or {}
    if t == "user":
        content = m.get("content")
        if isinstance(content, str):
            txt = content.strip()
            if not txt:
                return None
            if is_system_user_text(txt):
                return None      # 系统注入消息（任务通知等）不显示
            return [{"kind": "user_text", "text": txt}]
        if isinstance(content, list):
            has_result = any(b.get("type") == "tool_result" for b in content if isinstance(b, dict))
            if has_result:
                size = sum(len(json.dumps(b, ensure_ascii=False))
                           for b in content if isinstance(b, dict) and b.get("type") == "tool_result")
                return [{"kind": "tool_result", "size": size}]
            txt = " ".join(_text_of(b) for b in content if isinstance(b, dict)).strip()
            if not txt or is_system_user_text(txt):
                return None
            return [{"kind": "user_text", "text": txt}]
        return None
    if t == "assistant":
        events = []
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text", "").strip()
                if txt:
                    events.append({"kind": "assistant_text", "text": txt})
            elif bt == "tool_use":
                desc = json.dumps(b.get("input", {}), ensure_ascii=False)
                events.append({"kind": "tool_use", "name": b.get("name", "?"), "desc": desc})
        return events or None
    if t == "summary":
        return [{"kind": "summary"}]
    return None


# ---------------- watcher ----------------

def watcher_loop(cwd, session_path, q, stop, state):
    """跟随 session 文件增量，把事件（含翻译结果）投进队列。
    session_path 为 None 时轮询等待会话文件出现（全新对话）。
    一旦确定就锁定该会话，不再自动切换到别的会话。"""
    try:
        _watcher_inner(cwd, session_path, q, stop, state)
    except Exception:
        import traceback
        _log_crash("watcher 线程", traceback.format_exc())
        q.put({"kind": "note", "text": RED + "⚠ 跟随线程异常退出（已记入 %s），副屏将不再更新译文。" % CRASH_LOG + RST})


def _watcher_inner(cwd, session_path, q, stop, state):
    f = None
    pos = 0
    while not stop.is_set():
        if session_path is None:
            r = resolve_session(cwd)
            if isinstance(r, str) and r != "ambiguous" and os.path.exists(r):
                session_path = r
                state.session_path = r
                q.put({"kind": "note", "text": "🆕 会话已建立，开始跟随 %s" % os.path.basename(r)})
            else:
                time.sleep(2)
                continue
        session_id = os.path.basename(session_path).removesuffix(".jsonl")
        # 会话在启动时就已匹配好（歧义时还让你手动选过），此后一律锁定。
        # 故意**不再**做"发现更新的会话就自动切换"：同 cwd 下只要有另一个 claude
        # 会话活跃，副屏就会莫名其妙跳到别人的对话上去。
        # 想换对话 → 新开一个副屏，它会在启动时匹配到新的那个。
        if f is None:
            try:
                f = open(session_path, errors="ignore")
                f.seek(0, 2)
                pos = f.tell()
            except FileNotFoundError:
                time.sleep(1)
                continue
        try:
            sz = os.path.getsize(session_path)
        except OSError:
            sz = pos
        if sz < pos:  # 文件被截断（/compact 等）→ 从头跟
            f.seek(0)
            pos = 0
        f.seek(pos)
        buf = ""
        for line in f:
            buf += line
        pos = f.tell()
        for line in buf.splitlines():
            for ev in (parse_line(line, session_id) or []):
                if ev["kind"] == "user_text" and ev["text"].strip() == (state.last_injected or "").strip():
                    continue  # 自己注入的消息：已在发送时显示过对照
                if ev["kind"] == "assistant_text":
                    if state.show_source:
                        q.put(ev)  # 需要时先推英文原文
                    if len(ev["text"]) >= 200:
                        q.put({"kind": "pending_zh", "chars": len(ev["text"])})
                    try:
                        zh = T.translate_with_mask(ev["text"], "en2zh")
                        q.put({"kind": "assistant_zh", "text": zh})
                    except T.ProviderError as e:
                        q.put({"kind": "note", "text": RED + "翻译失败（en2zh）: %s" % e + RST})
                elif ev["kind"] == "user_text" and not T.has_cjk(ev["text"]):
                    if state.show_source:
                        q.put(ev)
                    try:
                        zh = T.translate_with_mask(ev["text"], "en2zh")
                        q.put({"kind": "user_zh", "text": zh})
                    except T.ProviderError:
                        pass
                else:
                    q.put(ev)
        time.sleep(0.8)


# ---------------- markdown 渲染（中文译文） ----------------

_FENCE_SPLIT = re.compile(r"(```[^\n]*\n.*?(?:```|$))", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BQUOTE = re.compile(r"^>\s?(.*)$")
_HR = re.compile(r"^\s*(---+|\*\*\*+|___+)\s*$")


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
_NUM_ITEM = re.compile(r"(?<![.\d\u00b7])\s+(\d{1,2})\.\s+")


def _disp_width(s):
    """终端显示宽度：CJK 等宽字符算 2。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad_cell(cell, width):
    return cell + " " * (width - _disp_width(cell))


def _term_width(default=100):
    """副屏面板的可用列宽（非 tty 时给默认值）。"""
    try:
        import shutil
        cols = shutil.get_terminal_size((default, 24)).columns
        return max(40, min(160, cols))
    except Exception:
        return default


def _hard_split(text, width):
    """按显示宽度硬切超宽 token，返回 (piece, rest)。"""
    w = 0
    for i, ch in enumerate(text):
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width:
            return text[:i], text[i:]
        w += cw
    return text, ""


def _wrap_display(text, width):
    """按显示宽度折行：优先空格断词，CJK/超长 token 硬切。"""
    if width <= 2:
        return [text]
    lines = []
    for para in text.split("\n"):
        cur, cur_w = "", 0
        for token in (re.findall(r"\S+\s*", para) or [para]):
            tw = _disp_width(token)
            if cur_w + tw <= width:
                cur += token
                cur_w += tw
                continue
            if cur.strip():
                lines.append(cur.rstrip())
            cur, cur_w = "", 0
            while _disp_width(token) > width:
                piece, token = _hard_split(token, width)
                lines.append(piece)
            cur, cur_w = token, _disp_width(token)
        lines.append(cur.rstrip())
    return lines or [""]


def _render_table(rows):
    """markdown 表格 → 终端对齐表格。

    关键：第三列常是超长文本，不能按最长列撑开（会超出面板宽度被终端硬折行，
    管道线就错位）。这里按面板宽度分配列宽，并在单元格内折行，
    多行单元格用分隔线隔开（与 Claude Code 自己的表格观感一致）。
    """
    def cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header = cells(rows[0])
    body = [cells(r) for r in rows[1:] if not _TABLE_SEP.match(r)]
    ncols = max([len(header)] + [len(b) for b in body] or [len(header)])
    header += [""] * (ncols - len(header))
    body = [b + [""] * (ncols - len(b)) for b in body]

    natural = []
    for i in range(ncols):
        w = _disp_width(header[i])
        for b in body:
            w = max(w, _disp_width(b[i]))
        natural.append(max(w, 3))

    avail = _term_width() - (3 * (ncols - 1) + 4)
    widths = list(natural)
    total = sum(widths)
    min_w = 4
    while total > avail:
        idx = max(range(ncols), key=lambda i: widths[i])
        if widths[idx] <= min_w:
            break
        widths[idx] -= 1
        total -= 1

    total_w = sum(widths) + 3 * (ncols - 1) + 2

    def fmt_line(cell_texts, bold=False):
        inner = " | ".join(_pad_cell(c, w) for c, w in zip(cell_texts, widths))
        if bold:
            inner = BOLD + inner + RST
        return DIM + "|" + RST + " " + inner + " " + DIM + "|" + RST

    sep = DIM + "|" + "-" * total_w + "|" + RST
    out = [fmt_line(header, bold=True), sep]
    for b in body:
        wrapped = [_wrap_display(c, w) for c, w in zip(b, widths)]
        nlines = max(len(x) for x in wrapped)
        for k in range(nlines):
            out.append(fmt_line([x[k] if k < len(x) else "" for x in wrapped]))
        if nlines > 1:
            out.append(sep)
    return "\n".join(out)


def _fix_lists(text):
    """翻译模型偶尔把编号列表合并成一行：在编号标记前拆行。"""
    orig = text
    text = _NUM_ITEM.sub(lambda m: "\n" + m.group(1) + ". ", text)
    if text.startswith("\n") and not orig.startswith("\n"):
        text = text[1:]
    return text


def _inline_md(text):
    """行内标记：`code` 先占位保护，再处理 **粗体** *斜体* ~~删除~~ [链接](url)。"""
    codes = []

    def stash(m):
        codes.append(m.group(1))
        return "\x01c%d\x01" % (len(codes) - 1)

    text = _INLINE_CODE.sub(stash, text)
    # 先解 Markdown 链接，避免后续插入的 \x1b[1m 等 ANSI 转义里的 '[' 被 _LINK 误匹配
    text = _LINK.sub(lambda m: m.group(1), text)
    # 中文排版：CJK 与加粗标记之间的多余空格去掉（"进行 **两次** 修改" → "进行**两次**修改"）
    cjk = "\\u3000-\\u303f\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff\\uff00-\\uffef"
    text = re.sub(r"(?<=[" + cjk + r"])\s+(\*\*)", r"\1", text)
    text = re.sub(r"(\*\*)\s+(?=[" + cjk + r"])", r"\1", text)
    text = _BOLD.sub(lambda m: BOLD + (m.group(1) or m.group(2)) + RST, text)
    text = _ITALIC.sub(lambda m: "\x1b[3m" + m.group(1) + RST, text)
    text = _STRIKE.sub(lambda m: "\x1b[9m" + m.group(1) + RST, text)
    for i, c in enumerate(codes):
        text = text.replace("\x01c%d\x01" % i, CYN + c + RST)
    return text


def _fence_body(seg):
    """取出代码围栏内部内容与语言标签。"""
    lines = seg.split("\n")
    lang = lines[0][3:].strip()
    body = lines[1:]
    if body and body[-1].strip() == "```":
        body = body[:-1]
    return lang, "\n".join(body)


def markdown_to_ansi(text):
    """极简 markdown→ANSI：标题/粗体/斜体/删除线/行内代码/代码块/链接/引用。"""
    out = []
    for seg in _FENCE_SPLIT.split(text):
        if seg.startswith("```"):
            lang, body = _fence_body(seg)
            head = DIM + ("── code: %s ──" % lang if lang else "── code ──") + RST
            out.append(head + "\n" + CYN + body + RST)
            continue
        lines = seg.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if _TABLE_ROW.match(line):
                j = i
                while j < len(lines) and _TABLE_ROW.match(lines[j]):
                    j += 1
                out.append(_render_table(lines[i:j]))
                i = j
                continue
            m = _HEADING.match(line)
            if m:
                out.append(BOLD + "\x1b[4m" + _inline_md(m.group(1)) + RST)
            elif _HR.match(line):
                out.append(DIM + "─" * 40 + RST)
            elif _BQUOTE.match(line):
                out.append(DIM + "▎" + _inline_md(_BQUOTE.match(line).group(1)) + RST)
            else:
                out.append(_inline_md(line))
            i += 1
    return "\n".join(out)


def strip_markdown(text):
    """过滤 md 语法标记：去掉 ** * ` ~~ # > 和代码围栏，保留内容。"""
    out = []
    for seg in _FENCE_SPLIT.split(text):
        if seg.startswith("```"):
            _, body = _fence_body(seg)
            out.append(body)
            continue
        seg = _INLINE_CODE.sub(r"\1", seg)
        seg = _BOLD.sub(lambda m: m.group(1) or m.group(2), seg)
        seg = _ITALIC.sub(r"\1", seg)
        seg = _STRIKE.sub(r"\1", seg)
        seg = _LINK.sub(r"\1", seg)
        for line in seg.split("\n"):
            line = _HEADING.sub(r"\1", line)
            line = _BQUOTE.sub(r"\1", line)
            if not _HR.match(line):
                out.append(line)
    return "\n".join(out)


def clip(s, n):
    s = s.rstrip("\n")
    return s if len(s) <= n else s[:n] + "\n" + DIM + "…[显示截断，省略 %d 字符]" % (len(s) - n) + RST


def fmt_zh(text, state):
    """中文译文的显示处理：列表拆行修复 + 截断 + markdown 渲染或语法过滤。"""
    text = _fix_lists(text)
    text = clip(text, state.max_chars)
    return markdown_to_ansi(text) if state.md_render else strip_markdown(text)


def _rev_block(text, width=0):
    """把一段文字渲染成反显色块：逐行补满到 width（width<=2 时不补空）。"""
    out = []
    for para in text.split("\n"):
        lines = _wrap_display(para, width) if width > 2 else [para]
        for line in lines:
            body = " " + line
            if width > 2:
                body += " " * max(0, width - _disp_width(body))
            out.append(REV + body + REV_OFF)
    return "\n".join(out)


def user_block(text, state):
    """「我」说的话：反显色块（Claude Code 风格），不带任何说话人字眼。

    自己的话按纯文本显示（不渲染 markdown），宽度铺满副屏，
    读起来就是"我这一轮说的那块"——正显是 Claude，反显是我。
    """
    body = clip(_fix_lists(text), state.max_chars)
    try:
        width = max(0, _term_width() - 1)   # 留最后一列，避免触发终端自动换行
    except Exception:
        width = 0
    return _rev_block(body, width)


def render(ev, state):
    kind = ev["kind"]
    # 翻译中提示：占当前行不换行，下一个事件会先清掉它（避免看起来卡住）
    if kind == "pending_zh":
        sys.stdout.write("\r\x1b[2K" + DIM + "⏳ 翻译中…（本节 %d 字，正在翻译）" % ev.get("chars", 0) + RST)
        sys.stdout.flush()
        state.pending_line = True
        return
    if getattr(state, "pending_line", False):
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
        state.pending_line = False
    # 工具调用/结果不显示（面板1 里本来就能看到），只保留正文译文
    if kind in ("tool_use", "tool_result"):
        return
    if kind == "assistant_text":
        if state.show_source:
            print()   # 空行分隔：色块=你说的话，普通文字=Claude
            print(DIM + clip(ev["text"], state.max_chars) + RST)
    elif kind == "assistant_zh":
        if state.show_source:
            print(CYN + "中文：" + RST)
        else:
            print()   # 不显示原文时，这里才是这一段的开头
        print(fmt_zh(ev["text"], state))
    elif kind == "user_text":
        if state.show_source:
            print(user_block(ev["text"], state))
    elif kind == "user_zh":
        print(user_block(ev["text"], state))
    elif kind == "summary":
        print(YEL + "📝 上下文被压缩：较早的消息已替换为摘要" + RST)
    elif kind == "note":
        print(YEL + ev["text"] + RST)


# ---------------- 发送 ----------------

def _run_capture(cmd, timeout=None):
    """跑子进程并稳健解码输出。

    曾有 bug：inject.sh 的输出里混入非法 UTF-8 字节时，subprocess(text=True)
    抛 UnicodeDecodeError 直接把整个副屏打穿。这里一律按字节收，再 errors="replace" 解码。
    """
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception as e:
        return -1, "", "子进程执行失败: %r" % (e,)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out, err


def do_inject(cwd, text, state):
    """调用 inject.sh，返回 (rc, 输出)。"""
    cmd = [INJECT_SH, "--cwd", cwd, "--text", text]
    sp = getattr(state, "session_path", None)
    if sp and os.path.exists(sp):
        cmd += ["--session", sp]
    tt = getattr(state, "target_terminal", None)
    if tt:
        cmd += ["--terminal", tt]          # 启动时锁定的目标面板，避免发送时再仲裁
    elif getattr(state, "mirror_terminal", None):
        cmd += ["--exclude-terminal", state.mirror_terminal]
    if state.dry:
        cmd.append("--dry-run")
    rc, so, se = _run_capture(cmd)
    out = so + se
    if rc == -1:
        print(RED + "❌ " + se + RST)
        return -1, [se]
    lines = [l for l in out.splitlines() if l.strip()]
    # 关键行只在出错或 dry-run 时显示；成功时保持安静
    keep = [l for l in lines if any(k in l for k in
            ("目标进程", "目标 session", "目标面板", "DRY-RUN", "已发送", "VERIFIED", "SUCCESS",
             "NOT-VERIFIED", "VERIFY-ERROR", "ERROR", "拒绝", "歧义", "无法", "没有正在运行",
             "安全护栏", "安全检查"))]
    if rc != 0:
        # 失败时一律不过滤：关键字过滤会吞掉真实原因（曾把 shell 的
        # "unbound variable" 整条丢掉，只剩一句无从下手的"退出码 1"）
        for l in lines[-8:]:
            print(DIM + l + RST)
    elif state.dry:
        for l in keep[-8:]:
            print(DIM + l + RST)
    return rc, keep


def send_message(cwd, text, state, is_english=False):
    """显示对照并注入。返回是否成功送达。"""
    if getattr(state, "watch_only", False):
        print(RED + "只看模式：禁止注入。" + RST)
        return False
    if is_english:
        en = text
        print(user_block(en, state))          # 我说的英文：反显块
    else:
        print(user_block(text, state))        # 我说的中文：立刻反显回显（翻译前先让你看到）
        try:
            en = T.translate_with_mask(text, "zh2en")
        except T.ProviderError as e:
            print(RED + "翻译失败: %s —— 未发送。" % e + RST)
            return False
        # 将要注入的英文同属"我说的话"，同样反显；两块连着读就是这一轮我说的内容
        print(user_block(en, state))
        # 兜底：译文明显短于原文 → 模型可能误执行了文本里的指令（如"只回复OK"）
        if len(en.strip()) < len(text.strip()) * 0.35 and len(text.strip()) >= 15:
            print(YEL + "⚠ 译文明显短于原文——翻译模型可能把文本里的指令当成给自己的命令执行了。" + RST)
            if not sys.stdin.isatty():
                print(RED + "非交互模式无法确认短译文，已安全拦截发送。" + RST)
                return False
            try:
                ans = input("  仍要发送这段译文？[y/N] ")
            except (KeyboardInterrupt, EOFError):
                ans = ""
            if ans.strip().lower() != "y":
                print(RED + "已取消发送。可换一种说法重新输入。" + RST)
                return False
    if state.no_inject:
        print(YEL + "练习模式：跳过注入。" + RST)
        state.last_injected = en
        return True
    state.last_injected = en  # 先标记，防 watcher 在注入期间抓到这条消息重复显示
    rc, _ = do_inject(cwd, en, state)
    if rc == 0:
        print(GRN + "✅ 已送达面板1（session 已记录）" + RST)
        return True
    if rc == 2:
        print(RED + "❌ 未发送：找不到/无法唯一定位面板1的 claude（退出码2）。" + RST)
        print("  面板1要在跑着 claude，且 cwd 一致。按回车重试，输入 /quit 退出。")
        return False
    if rc == 3:
        print(RED + "❌ 注入失败（osascript 错误），未发送。" + RST)
        return False
    if rc == 4:
        print(YEL + "⚠ 已发送但 60s 内未确认落库——请去面板1人工确认，不要重复发送。" + RST)
        state.last_injected = en  # 防 watcher 重复显示
        return False
    print(RED + "❌ inject.sh 退出码 %d（参数/环境错误），未发送；原因见上方 inject.sh 输出。" % rc + RST)
    return False


# ---------------- 输入循环 ----------------

PROMPT = GRN + "🀄 > " + RST

# 5 行方块字（用于启动页 logo）
_GLYPHS = {
    "A": [" ██████ ", "██    ██", "████████", "██    ██", "██    ██"],
    "B": ["██████ ", "██   ██", "██████ ", "██   ██", "██████ "],
    "C": [" ██████", "██     ", "██     ", "██     ", " ██████"],
    "G": [" ██████ ", "██      ", "██  ███ ", "██    ██", " ██████ "],
    "I": ["██", "██", "██", "██", "██"],
    "L": ["██    ", "██    ", "██    ", "██    ", "██████"],
    "M": ["███    ███", "████  ████", "██ ████ ██", "██  ██  ██", "██      ██"],
    "N": ["███   ██", "████  ██", "██ ██ ██", "██  ████", "██   ███"],
    "O": [" ██████ ", "██    ██", "██    ██", "██    ██", " ██████ "],
    "R": ["██████ ", "██   ██", "██████ ", "██   ██", "██   ██"],
    "U": ["██    ██", "██    ██", "██    ██", "██    ██", " ██████ "],
    " ": ["  "] * 5,
}


def _block_word(word):
    """把词渲染成 5 行方块字。"""
    rows = [""] * 5
    for i, ch in enumerate(word):
        glyph = _GLYPHS.get(ch)
        if not glyph:
            continue
        for r in range(5):
            rows[r] += ("" if i == 0 else " ") + glyph[r]
    return rows


def _clip_disp(s, limit):
    """按显示宽度截断（超长加省略号）。"""
    if _disp_width(s) <= limit:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > limit - 1:
            break
        out += ch
        w += cw
    return out + "…"


def _pad_to(s, width):
    """按显示宽度右侧补空格（中文双宽）。"""
    return s + " " * max(0, width - _disp_width(s))


def _ghostty_panels():
    """枚举 Ghostty 面板：[(id, name, working_directory), ...]（失败返回空）。

    name 是终端标题，用来在多个同 cwd 面板里辨认哪个是面板1
    （Ghostty 的 AppleScript 不暴露 tty，claude 进程没法直接映射到面板）。
    """
    script = ('tell application "Ghostty"\nset out to ""\n'
              "repeat with w from 1 to (count of windows)\n"
              "repeat with t from 1 to (count of tabs of window w)\n"
              "repeat with tm in terminals of tab t of window w\n"
              'set out to out & (id of tm) & "|||" & (name of tm) & "|||" & (working directory of tm) & linefeed\n'
              "end repeat\nend repeat\nend repeat\nreturn out\nend tell")
    try:
        _, out, _ = _run_capture(["osascript", "-e", script], timeout=8)
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        if line.count("|||") >= 2:
            tid, name, wd = line.split("|||", 2)
            res.append((tid.strip(), name.strip(), wd.strip()))
    return res


def _ghostty_terminals():
    """枚举 Ghostty 面板：[(id, working_directory), ...]（失败返回空）。"""
    return [(tid, wd) for tid, _name, wd in _ghostty_panels()]


def _load_targets():
    try:
        with open(TARGETS_PATH, errors="ignore") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_target(cwd, uuid):
    """记住这个 cwd 选定的注入面板，下次启动直接沿用。"""
    try:
        d = _load_targets()
        d[cwd] = uuid
        os.makedirs(os.path.dirname(TARGETS_PATH), exist_ok=True)
        with open(TARGETS_PATH, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def choose_panel(cands, remembered=None):
    """同 cwd 有多个面板时，让你指出哪个是面板1（注入目标）。"""
    print(YEL + "⚠ 该目录下有 %d 个 Ghostty 面板，需要你指出哪个是「面板1」：" % len(cands) + RST)
    first_looks_shell = "/" in (cands[0][1] or "") or "~" in (cands[0][1] or "")
    for i, (tid, name, _wd) in enumerate(cands, 1):
        tip = DIM + "（上次选的是这个）" + RST if tid == remembered else ""
        if "/" in name or "~" in name:
            # 标题是路径 → 多半是普通 shell；选错会把英文当命令敲进去并回车，必须警示
            tip = YEL + "（像普通 shell，别选）" + RST + " " + tip
        print("  [%d] %s %s %s" % (i, _clip_disp(name or "(无标题)", 40), DIM + tid[:8] + RST, tip))
    print(DIM + "      跑 claude 的那个才是目标；副屏自己所在面板已自动排除。" + RST)
    prompt_str = (GRN + "输入编号选择（q=退出）: " + RST) if first_looks_shell else (GRN + "输入编号选择（回车=1，q=退出）: " + RST)
    while True:
        try:
            ans = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            return ""
        if ans.lower() == "q":
            return ""
        if not ans:
            if first_looks_shell:
                print(RED + "第 1 项看起来是普通 shell，请显式输入编号、UUID 前缀或 q 退出。" + RST)
                continue
            return cands[0][0]
        if ans.isdigit() and 1 <= int(ans) <= len(cands):
            return cands[int(ans) - 1][0]
        for tid, _n, _w in cands:          # 也允许直接粘 UUID（前缀即可）
            if tid.lower().startswith(ans.lower()):
                return tid
        print(RED + "请输入 1-%d 的编号、UUID 前缀，或 q 退出。" % len(cands) + RST)


def resolve_target_panel(cwd, mirror_terminal, explicit=""):
    """确定注入目标面板：只在启动时定一次，之后全程锁定。

    同时检查该 cwd 是否确有交互式 claude 进程在运行，避免在没有 claude 时
    把普通 shell 面板自动锁定为注入目标。
    """
    tgt = find_target(cwd, mirror_terminal)
    has_claude = bool(tgt.get("pid"))
    panels = _ghostty_panels()
    if explicit:
        if not has_claude:
            print(YEL + "⚠ 当前 cwd 没有运行中的交互式 claude 进程，暂不锁定面板 %s。" % explicit + RST)
            return ""
        for tid, _n, _w in panels:
            if tid == explicit or tid.startswith(explicit):
                return tid
        print(YEL + "⚠ 指定的注入面板 %s 不在当前 Ghostty 面板里（已关闭？）" % explicit + RST)
        return ""
    if not panels or not has_claude:
        return ""
    cands = [p for p in panels
             if p[0] != mirror_terminal and p[2] and os.path.realpath(p[2]) == cwd]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0][0]
    remembered = _load_targets().get(cwd)
    if remembered:
        for tid, _n, _w in cands:
            if tid == remembered:
                print(DIM + "沿用上次选择的注入面板 %s（要重选：传 --terminal，或删 %s）"
                      % (remembered[:8], TARGETS_PATH) + RST)
                return tid
    if not sys.stdin.isatty():
        print(YEL + "⚠ 该目录有 %d 个 Ghostty 面板，非交互环境无法选择。" % len(cands) + RST)
        for i, (tid, name, _wd) in enumerate(cands, 1):
            print("  [%d] %s %s" % (i, name or "(无标题)", tid))
        print("  请用 --terminal <UUID> 指定注入目标。")
        return ""
    picked = choose_panel(cands, remembered)
    if picked:
        _save_target(cwd, picked)
    return picked


def find_target(cwd, exclude_terminal=None):
    """尽力定位注入目标：返回 {"pid","tty","panel"}（找不到的字段为 None）。"""
    info = {"pid": None, "tty": None, "panel": None}
    try:
        _, out, _ = _run_capture(["ps", "-axo", "pid=,tty=,command="], timeout=5)
    except Exception:
        return info
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, cmd = parts
        if not tty.startswith(("tty", "s0")):
            continue
        argv0 = (cmd.split() or [""])[0]
        if os.path.basename(argv0) != "claude":
            continue
        if re.search(r"(^| )(--print|-p)( |$)", cmd):
            continue
        try:
            _, lout, _ = _run_capture(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], timeout=5)
        except Exception:
            continue
        pcwd = ""
        for l in lout.splitlines():
            if l.startswith("n"):
                pcwd = l[1:]
                break
        if pcwd and os.path.realpath(pcwd) == cwd:
            info["pid"], info["tty"] = pid, tty
            break
    for tid, wd in _ghostty_terminals():
        if exclude_terminal and tid == exclude_terminal:
            continue
        if wd and os.path.realpath(wd) == cwd:
            info["panel"] = tid[:8]
            break
    return info


def print_startup(cwd, session_path, state, hint=None):
    """启动页：logo + 做什么 + 关键信息。"""
    width = _term_width()
    logo = _block_word("CC BILINGUAL")
    logo_w = max(_disp_width(l) for l in logo)

    if width >= logo_w + 8:
        inner = logo_w + 2
        print(CYN + "╭" + "─" * inner + "╮" + RST)
        for l in logo:
            print(CYN + "│" + RST + " " + BOLD + CYN + _pad_to(l, logo_w) + RST + " " + CYN + "│" + RST)
        print(CYN + "╰" + "─" * inner + "╯" + RST)
        print(DIM + "Claude Code 双语对照副屏 · 你打中文，面板1 收到英文；Claude 的英文回复，这里读中文" + RST)
    else:
        print(BOLD + CYN + "◆ ghostty-cc-bilingual" + RST + DIM + " · Claude Code 双语对照副屏" + RST)
        print(DIM + "🀄 中文 → 🇬🇧 注入面板1   ｜   🤖 回复 → 🇨🇳 译文" + RST)

    # 会话信息
    sess, sess_extra = "(尚未建立，发出第一条消息后自动跟随)", ""
    if session_path:
        sess = os.path.basename(session_path)[:8]
        try:
            si = session_info(session_path)
            sess_extra = " · %s · %d 条" % (human_age(os.path.getmtime(session_path)), si["n"])
        except Exception:
            pass

    # 翻译链路（标注不可用的 provider）
    try:
        cfg = T.load_config()
        parts = []
        for name in cfg.get("chain", []):
            pcfg = cfg.get(name, {})
            model = pcfg.get("model", "?")
            key_env = pcfg.get("api_key_env") or ""
            avail = (not key_env) or bool(os.environ.get(key_env))
            item = "%s:%s" % (name, model)
            parts.append(item if avail else DIM + item + "（无 key）" + RST)
        chain = " → ".join(parts)
    except Exception:
        chain = "?"

    show = "仅中文译文" if not state.show_source else "中英对照"
    md = "markdown 渲染" if state.md_render else "纯文本"
    if getattr(state, "watch_only", False):
        target = "仅跟随日志（只看模式，不注入）"
    else:
        tgt = find_target(cwd, state.mirror_terminal)
        if getattr(state, "target_terminal", None):
            tgt["panel"] = state.target_terminal[:8]      # 以启动时锁定的目标为准
        if tgt["pid"]:
            target = "claude pid %s (%s)" % (tgt["pid"], tgt["tty"])
            if tgt["panel"]:
                target += " · 面板 %s（已锁定）" % tgt["panel"]
        elif tgt["panel"]:
            target = "面板 %s（已锁定）" % tgt["panel"]
        else:
            target = "未锁定 · 发送时仍由 inject.sh 定位"

    mode = []
    if getattr(state, "watch_only", False):
        mode.append("只看（不接受输入）")
    if getattr(state, "dry", False):
        mode.append("演练 dry-run（只对照不发送）")
    if getattr(state, "no_inject", False):
        mode.append("练习（不注入）")
    rows = [
        ("项目", cwd),
        ("会话", sess + sess_extra),
        ("翻译", chain),
        ("显示", "%s · %s" % (show, md)),
        ("注入", target),
    ]
    if mode:
        rows.append(("模式", "、".join(mode)))
    rows.append(("命令", hint or "/en 直接发英文 · /dry 演练 · /help 帮助 · /quit 退出"))

    label_w = max(_disp_width(k) for k, _ in rows)
    val_w = max(20, width - label_w - 5)
    print()
    for k, v in rows:
        print("  " + DIM + _pad_to(k, label_w) + RST + "  " + _clip_disp(v, val_w))
    print()


class State:
    def __init__(self, dry, no_inject):
        self.dry = dry
        self.no_inject = no_inject
        self.last_injected = None
        self.mirror_terminal = None   # 副屏自己所在面板的 id（注入时排除，防打回自己）
        self.target_terminal = None   # 启动时锁定、之后全程沿用的注入目标面板（完整 UUID）
        self.tool_count = 0           # 连续工具调用折叠计数
        self.session_path = None      # 当前跟随的会话文件（歧义时由用户选择）
        self.pending_line = False     # 是否正在显示"翻译中"占位行
        d = T.load_config().get("display", {})
        self.max_chars = d.get("max_chars", 2000)
        self.show_source = d.get("show_source", False)   # 副屏是否显示英文原文（原文在面板1）
        self.md_render = d.get("md_render", True)        # 中文译文是否渲染 markdown


def handle_line(line, cwd, state, q):
    line = line.strip()
    if not line:
        return True
    if line == "/quit" or line == "/q":
        return False
    if line == "/help" or line == "/h":
        print("  /en 文本   不翻译，直接发英文给面板1")
        print("  /quit      退出（不影响面板1的对话）")
        print("  /dry       切换 dry-run 模式（演练注入，不真发）")
        print("  其他输入   视为中文 → 翻译成英文 → 显示对照 → 注入面板1")
        print("  样式       反显色块 = 你说的话；正显（普通文字）= Claude 的回复")
        return True
    if line == "/dry":
        state.dry = not state.dry
        print("dry-run 模式：%s" % ("开（只演练，不真发）" if state.dry else "关"))
        return True
    if line.startswith("/en "):
        send_message(cwd, line[4:].strip(), state, is_english=True)
        return True
    # 英文输入 → 不翻译直接发（对照价值低，避免浪费翻译调用）
    if not T.has_cjk(line):
        print(YEL + "检测到英文输入，直接发送（不翻译）。中文请直接输入。" + RST)
        send_message(cwd, line, state, is_english=True)
        return True
    send_message(cwd, line, state)
    return True


class RawLineEditor:
    """UTF-8 感知的极简行编辑器（raw 模式）。

    背景：macOS 终端规范模式的行编辑在内核里【按字节】删字符，
    中文一个字 3 字节，退格一次只删 1 字节，剩下半个字变成删不掉的乱码。
    这里改成 raw 模式，退格按【字符】删除，中文/emoji 都能删干净。

    多行粘贴：开启括号粘贴协议（CSI?2004），终端把粘贴内容包在
    ESC[200~ / ESC[201~ 之间——粘贴里的换行按文本保留进缓冲区，
    只有（粘贴之外的）真正回车才提交整段，多行 SQL 等一股脑发送。
    """

    def __init__(self, prompt):
        self.prompt = prompt
        self.fd = sys.stdin.fileno()
        self.is_tty = os.isatty(self.fd)
        self.old = None
        self.raw = b""              # 当前缓冲区的原始字节（可含 \n）
        self._esc_state = 0         # 转义序列状态机：0 正常 / 1 见 ESC / 2 收集参数
        self._esc_buf = ""          # CSI 参数字节（识别 200~/201~）
        self._paste = False         # 是否处于括号粘贴内容中
        self._old_lines = 1         # 输入区上次占用的物理行数
        if self.is_tty:
            self.old = termios.tcgetattr(self.fd)
            new = termios.tcgetattr(self.fd)
            new[3] &= ~(termios.ECHO | termios.ICANON)   # 关回显、关规范模式
            new[3] |= termios.ISIG                        # 保留 Ctrl-C 信号
            new[0] &= ~termios.ICRNL                      # \r 不转 \n
            termios.tcsetattr(self.fd, termios.TCSANOW, new)
            try:
                os.write(self.fd, b"\x1b[?2004h")         # 开启括号粘贴
            except Exception:
                pass

    def restore(self):
        if self.is_tty:
            try:
                os.write(self.fd, b"\x1b[?2004l")         # 关闭括号粘贴
            except Exception:
                pass
        if self.old is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.old)
            except Exception:
                pass

    def _erase_area(self, n):
        """清掉输入区占用的 n 个物理行（光标当前在该区域最后一行末尾）。"""
        sys.stdout.write("\r\x1b[2K")
        for _ in range(n - 1):
            sys.stdout.write("\x1b[1A\x1b[2K")
        sys.stdout.flush()

    def draw(self):
        txt = self.raw.decode("utf-8", "replace")
        new_lines = txt.count("\n") + 1
        self._erase_area(self._old_lines)
        sys.stdout.write(self.prompt + txt)
        sys.stdout.flush()
        self._old_lines = new_lines

    def clear(self):
        self._erase_area(self._old_lines)
        self._old_lines = 1

    def handle(self, data):
        """处理一段输入字节。返回 (line, done)；回车时 line 为完整文本。"""
        for b in data:
            if self._esc_state:
                # ESC 后若跟 '['/'O' 则是 CSI 序列：收集参数到终字节 (0x40-0x7e)
                if self._esc_state == 1:
                    if b in (0x5B, 0x4F):
                        self._esc_state = 2
                        self._esc_buf = ""
                    else:
                        self._esc_state = 0
                else:
                    if 0x30 <= b <= 0x3F:        # 参数字节：0-9 ; : < = > ?
                        self._esc_buf += chr(b)
                        continue
                    if 0x40 <= b <= 0x7E:        # 终字节
                        if b == 0x7E and self._esc_buf == "200":
                            self._paste = True
                        elif b == 0x7E and self._esc_buf == "201":
                            self._paste = False
                        self._esc_state = 0
                        self._esc_buf = ""
                    # 非法序列：放弃该转义
                    else:
                        self._esc_state = 0
                        self._esc_buf = ""
                continue
            if b == 0x1B:
                self._esc_state = 1
                continue
            if b in (0x7F, 0x08):            # 退格：删最后一个【字符】（可跨行）
                if self.raw:
                    txt = self.raw.decode("utf-8", "replace")
                    self.raw = txt[:-1].encode("utf-8")
                    self.draw()
                continue
            if b == 0x15:                    # Ctrl-U：清空整行
                self.raw = b""
                self.draw()
                continue
            if b in (0x0D, 0x0A):           # 回车/换行
                if self._paste:
                    # 粘贴内容里的换行：作为文本保留（CRLF 去重），不提交
                    if not self.raw.endswith(b"\n"):
                        self.raw += b"\n"
                        os.write(self.fd, b"\r\n")
                        self._old_lines += 1
                else:
                    line = self.raw.decode("utf-8", "replace")
                    self.raw = b""
                    self._old_lines = 1
                    return line, True
                continue
            if b == 0x03:
                raise KeyboardInterrupt
            if b == 0x04 and not self.raw:   # Ctrl-D 且空行：EOF
                raise EOFError
            if b >= 0x20:                    # 可打印字节：追加并原样回显
                self.raw += bytes([b])
                os.write(sys.stdout.fileno(), bytes([b]))   # 直接写字节，避免文本流二次编码
        return None, False


def _input_loop_plain(cwd, q, stop, state):
    """非 tty（管道等）退化的逐行读取循环。"""
    prompt_shown = False
    while True:
        try:
            while True:
                ev = q.get_nowait()
                if prompt_shown:
                    sys.stdout.write("\r\x1b[2K")
                    prompt_shown = False
                render(ev, state)
        except queue.Empty:
            pass
        if not prompt_shown:
            sys.stdout.write(PROMPT)
            sys.stdout.flush()
            prompt_shown = True
        try:
            readable, _, _ = select.select([sys.stdin], [], [], 0.25)
        except (KeyboardInterrupt, ValueError):
            print()
            break
        if not readable:
            continue
        line = sys.stdin.readline()
        if not line:
            break
        if prompt_shown:
            sys.stdout.write("\r\x1b[2K")
            prompt_shown = False
        try:
            if not handle_line(line, cwd, state, q):
                break
        except KeyboardInterrupt:
            print()
            break
        print()


def input_loop(cwd, q, stop, state):
    print_startup(cwd, state.session_path, state)
    ed = RawLineEditor(PROMPT)
    try:
        if not ed.is_tty:
            _input_loop_plain(cwd, q, stop, state)
            return
        ed.draw()
        while True:
            # 1. 渲染 watcher 事件（擦输入行 → 渲染 → 重画输入行）
            try:
                while True:
                    ev = q.get_nowait()
                    ed.clear()
                    render(ev, state)
                    ed.draw()
            except queue.Empty:
                pass
            # 2. 等输入
            try:
                readable, _, _ = select.select([sys.stdin], [], [], 0.25)
            except (KeyboardInterrupt, ValueError):
                print()
                break
            if not readable:
                continue
            try:
                data = os.read(sys.stdin.fileno(), 4096)
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if not data:
                break
            try:
                line, done = ed.handle(data)
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if done:
                ed.clear()
                try:
                    if not handle_line(line, cwd, state, q):
                        break
                except KeyboardInterrupt:
                    print()
                    break
                ed.draw()
    finally:
        ed.restore()
    stop.set()
    print(CYN + "👋 已退出 ghostty-cc-bilingual（面板1的对话不受影响）" + RST)


# ---------------- 一次性模式（测试/脚本） ----------------

def once_mode(cwd, text, q, stop, state):
    print_startup(cwd, state.session_path, state)
    handle_line(text if text else "", cwd, state, q)
    if text:
        deadline = time.time() + 300
        got_reply = False
        while time.time() < deadline:
            try:
                ev = q.get(timeout=0.5)
                render(ev, state)
                if ev["kind"] == "assistant_zh":
                    got_reply = True
                    break
            except queue.Empty:
                continue
        stop.set()
        return 0 if got_reply else 1
    return 1


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="Claude Code 双语对照副屏")
    ap.add_argument("--cwd", required=True, help="与面板1 claude 相同的项目目录")
    ap.add_argument("--session", help="显式指定 session jsonl 路径（自动解析歧义时用）")
    ap.add_argument("--terminal", help="显式指定注入目标面板 UUID（同 cwd 多个面板时用）")
    ap.add_argument("--once", action="store_true", help="一次性模式：处理一行后等到回复翻译完退出")
    ap.add_argument("--once-text", help="--once 模式直接指定要发送的文本（不读 stdin）")
    ap.add_argument("--watch-only", action="store_true", help="只跟随显示，不接受输入")
    ap.add_argument("--no-inject", action="store_true", help="显示对照但不注入面板1")
    ap.add_argument("--dry", action="store_true", help="注入用 dry-run 演练")
    ap.add_argument("--max-chars", type=int, help="每段显示上限（覆盖配置）")
    args = ap.parse_args()

    if args.watch_only and (args.once or args.once_text is not None):
        ap.error("--watch-only 不能与 --once / --once-text 同时使用")

    cwd = os.path.realpath(args.cwd)
    if not os.path.isdir(cwd):
        print(RED + "cwd 不存在: %s" % cwd + RST, file=sys.stderr)
        sys.exit(1)

    try:
        T.load_config()
    except T.ProviderError as e:
        print(RED + str(e) + RST, file=sys.stderr)
        sys.exit(2)

    state = State(args.dry, args.no_inject)
    state.watch_only = args.watch_only
    if args.max_chars:
        state.max_chars = args.max_chars
    if not args.watch_only:      # 只看模式不注入，不碰 Ghostty AppleScript
        try:
            _, out, _ = _run_capture(
                ["osascript", "-e",
                 'tell application "Ghostty" to get id of focused terminal of selected tab of front window'],
                timeout=10)
            out = out.strip()
            if out:
                state.mirror_terminal = out
        except Exception:
            pass
        # 注入目标面板：启动时定一次并锁定（同 cwd 多面板时问你一次，之后沿用）
        state.target_terminal = resolve_target_panel(cwd, state.mirror_terminal, args.terminal or "")
    if args.no_inject:
        state.no_inject = True
        print(YEL + "练习模式：只显示对照，不注入面板1。" + RST)

    session_path = args.session
    if not session_path:
        cands = session_candidates(cwd)
        if cands:
            today = [c for c in cands if c[0] >= today_start()]
            if today:
                cands = today          # 只列今天的会话
        if len(cands) > 1 and (cands[0][0] - cands[1][0]) < 120:
            if sys.stdin.isatty():
                session_path = choose_session(cands)
                if not session_path:
                    print(RED + "未选择会话，退出（不影响面板1的对话）。" + RST)
                    sys.exit(2)
            else:
                print(YEL + "⚠ 该目录有多个活跃会话（非交互环境无法选择）。" + RST)
                for i, (mt, path) in enumerate(cands, 1):
                    print("  [%d] %s %s" % (i, human_age(mt), path))
                print("  请用 --session <路径> 指定。")
                sys.exit(2)
        elif cands:
            session_path = cands[0][1]
    if session_path:
        state.session_path = session_path

    q = queue.Queue()
    stop = threading.Event()
    w = threading.Thread(target=watcher_loop, args=(cwd, session_path, q, stop, state), daemon=True)
    w.start()

    try:
        if args.once:
            text = args.once_text if args.once_text is not None else (
                sys.stdin.readline().strip() if not sys.stdin.isatty() else "")
            rc = once_mode(cwd, text, q, stop, state)
            sys.exit(rc)
        if args.watch_only:
            print_startup(cwd, session_path, state, hint="只看模式：Ctrl-C 退出（不接受输入）")
            try:
                while True:
                    try:
                        ev = q.get(timeout=1)
                        render(ev, state)
                    except queue.Empty:
                        continue
            except KeyboardInterrupt:
                print()
                return
        input_loop(cwd, q, stop, state)
    except SystemExit:
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        _log_crash("主线程", tb)
        print(RED + "\n❌ 副屏崩溃（异常详情已写入 %s）：" % CRASH_LOG + RST)
        print(tb)
        sys.exit(70)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
