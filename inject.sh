#!/bin/sh
# inject.sh —— 向 Ghostty 里运行的交互式 Claude Code TUI 注入一条文本并回车，
# 然后回读 session 日志验证消息真的落库。
#
# 基于 rockcli/agent-ops-skills（tui-continue-scheduler）的注入与回读机制，改动：
#   1. 文本经临时文件传给 AppleScript（read POSIX file），支持多行/引号/任意 Unicode
#   2. 支持【全新会话】：session 文件在第一条消息发出前不存在 → 先发送，
#      发送后再等文件出现并回读验证（先发后验）
#   3. Ghostty 面板 wd 与进程 cwd 比对时做 pwd -P 归一化（macOS /tmp → /private/tmp）
#
# 用法:
#   inject.sh --cwd <项目路径> [--dry-run] [--text "..."|--file <路径>]
#              [--session <路径>] [--terminal <UUID>] [--expect-user-regex <RE>]
#
# 退出码: 0=成功落库  1=参数/环境错误  2=定位歧义或找不到目标
#         3=osascript 发送失败  4=发送了但未确认落库（需人工检查）
#
# 硬约束（都踩过，别改）:
#   - input text 与 send key "enter" 分两次 osascript 调用，间隔 >=2s
#   - 会话身份取 session 文件里【第一条】cwd，不是最后一条（sidechain 会污染）
#   - 纯 POSIX sh：本机非交互 shell 是 zsh，bash 专属语法会炸
#   - 不硬编码 terminal UUID，运行时枚举

set -u

# ---------- 默认配置 ----------
TARGET_CWD=""
SESSION_FILE=""
TERMINAL_ID=""
EXCLUDE_TID=""
TEXT=""
TEXT_FILE=""
DRY_RUN=0
EXPECT_RE=""
ENTER_DELAY="${TUI_ENTER_DELAY:-3}"      # 必须 >=2
VERIFY_TIMEOUT="${TUI_VERIFY_TIMEOUT:-60}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
# 注意：只打印 $1（消息本体），不能用 $*——那会把退出码也拼进消息里
die() { log "ERROR: $1"; exit "${2:-1}"; }

# ---------- 参数解析 ----------
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)           DRY_RUN=1 ;;
    --text)              shift; [ $# -gt 0 ] || die "--text 缺少值" 1; TEXT="$1" ;;
    --file)              shift; [ $# -gt 0 ] || die "--file 缺少值" 1; TEXT_FILE="$1" ;;
    --cwd)               shift; [ $# -gt 0 ] || die "--cwd 缺少值" 1; TARGET_CWD="$1" ;;
    --session)           shift; [ $# -gt 0 ] || die "--session 缺少值" 1; SESSION_FILE="$1" ;;
    --terminal)          shift; [ $# -gt 0 ] || die "--terminal 缺少值" 1; TERMINAL_ID="$1" ;;
    --exclude-terminal)  shift; [ $# -gt 0 ] || die "--exclude-terminal 缺少值" 1; EXCLUDE_TID="$1" ;;
    --expect-user-regex) shift; [ $# -gt 0 ] || die "--expect-user-regex 缺少值" 1; EXPECT_RE="$1" ;;
    -h|--help)           sed -n '2,20p' "$0"; exit 0 ;;
    *)                   die "未知参数: $1" 1 ;;
  esac
  shift
done

[ -n "$TEXT_FILE" ] && [ -r "$TEXT_FILE" ] && TEXT=$(cat "$TEXT_FILE")
[ -n "$TARGET_CWD" ] || die "必须指定 --cwd <项目路径>" 1
[ -n "$TEXT" ] || die "必须指定 --text 或 --file" 1
command -v osascript >/dev/null 2>&1 || die "找不到 osascript" 1
[ -d "/Applications/Ghostty.app" ] || die "Ghostty 未安装" 1

# ---------- 1. 定位目标 agent 进程（校验在 Ghostty 内且匹配 cwd） ----------
# 定位方式：按 argv 匹配（cc 会把自己的进程名改成版本号，pgrep -x claude 找不到它）
in_ghostty() {
  _P=$1
  while [ "$_P" -gt 1 ]; do
    _PP=$(ps -o ppid= -p "$_P" 2>/dev/null | tr -d ' ')
    [ -z "$_PP" ] && return 1
    # 用 command=（完整路径）而不是 comm=（会被截断成 /Applications/Gh）
    _PNAME=$(ps -o command= -p "$_PP" 2>/dev/null)
    case "$_PNAME" in
      */Ghostty.app/Contents/MacOS/ghostty*) return 0 ;;
    esac
    _P=$_PP
  done
  return 1
}

CANDIDATES=""
for p in $(ps -axo pid=,tty=,command= 2>/dev/null | awk '$2 ~ /^(tty|s0)/ && $3 ~ /(^|\/)claude($| )/ {print $1}'); do
  # 必须有控制终端（awk 已过滤空 tty）
  tty=$(ps -o tty= -p "$p" 2>/dev/null | tr -d ' ')
  # 排除 headless（--print / -p）
  cmd=$(ps -o command= -p "$p" 2>/dev/null)
  if printf '%s' "$cmd" | grep -qE '(^| )(--print|-p)( |$)'; then
    continue
  fi
  CANDIDATES="$CANDIDATES$p
"
done
CANDIDATES=$(printf '%s' "$CANDIDATES" | grep . || true)

TARGET_CWD=$(cd "$TARGET_CWD" 2>/dev/null && pwd -P) || die "--cwd 路径不存在: $TARGET_CWD" 1

[ -n "$CANDIDATES" ] || die "没有正在运行的交互式 Claude Code 进程" 2
MATCHED=""
GHOSTTY_MATCHED=""
for p in $CANDIDATES; do
  cwd=$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
  [ -n "$cwd" ] && cwd=$(cd "$cwd" 2>/dev/null && pwd -P)
  if [ "$cwd" = "$TARGET_CWD" ]; then
    MATCHED="$MATCHED$p
"
    if in_ghostty "$p"; then
      GHOSTTY_MATCHED="$GHOSTTY_MATCHED$p
"
    fi
  fi
done
MATCHED=$(printf '%s' "$MATCHED" | grep . || true)
GHOSTTY_MATCHED=$(printf '%s' "$GHOSTTY_MATCHED" | grep . || true)

if [ -n "$MATCHED" ] && [ -z "$GHOSTTY_MATCHED" ]; then
  log "安全护栏：claude 进程不在 Ghostty 里（可能跑在 Terminal.app/iTerm/ssh 等其他终端）。"
  log "本工具只注入 Ghostty 面板，拒绝发送，防止消息被误打进别的窗口。"
  exit 2
fi

# 目标进程/面板选择：
#   --terminal 指定 → 仍须确认当前 cwd 有跑在 Ghostty 里的交互式 claude，再校验该面板 cwd
#   未指定 → 仅认唯一匹配的 Ghostty 内 claude 实例
if [ -n "$TERMINAL_ID" ]; then
  [ -n "$GHOSTTY_MATCHED" ] || die "cwd=$TARGET_CWD 下没有运行在 Ghostty 中的交互式 Claude Code 进程，拒绝向锁定面板注入" 2
  TWD=$(osascript - "$TERMINAL_ID" <<'AS' 2>/dev/null
on run argv
  tell application "Ghostty" to get working directory of (terminal id (item 1 of argv))
end run
AS
)
  [ -n "$TWD" ] || die "无法获取面板 $TERMINAL_ID 的 working directory，拒绝发送" 2
  TWD=$(cd "$TWD" 2>/dev/null && pwd -P) || die "无法解析面板 cwd: $TWD" 2
  if [ "$TWD" != "$TARGET_CWD" ]; then
    die "指定面板的 cwd ($TWD) 与 --cwd ($TARGET_CWD) 不一致，拒绝发送" 2
  fi
  CLAUDE_CWD="$TARGET_CWD"
  CLAUDE_PID=""
  log "目标面板已指定: $TERMINAL_ID (cwd 与 Ghostty claude 进程校验通过)"
else
  N_MATCHED=$(printf '%s\n' "$GHOSTTY_MATCHED" | grep -c . || true)
  if [ "$N_MATCHED" = "1" ]; then
    CLAUDE_PID=$(printf '%s\n' "$GHOSTTY_MATCHED" | head -1)
    log "目标进程: pid=$CLAUDE_PID tty=$(ps -o tty= -p "$CLAUDE_PID" | tr -d ' ') etime=$(ps -o etime= -p "$CLAUDE_PID" | tr -d ' ')"
    CLAUDE_CWD=$(lsof -a -p "$CLAUDE_PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    [ -n "$CLAUDE_CWD" ] || CLAUDE_CWD="$TARGET_CWD"
    CLAUDE_CWD=$(cd "$CLAUDE_CWD" 2>/dev/null && pwd -P) || die "无法解析进程 cwd" 2
    log "进程 cwd: $CLAUDE_CWD"
    if [ "$CLAUDE_CWD" != "$TARGET_CWD" ]; then
      die "进程 cwd ($CLAUDE_CWD) 与 --cwd ($TARGET_CWD) 不一致，拒绝发送" 2
    fi
    log "安全检查通过：claude 进程确实跑在 Ghostty 里"
  else
    log "共 $(printf '%s\n' "$CANDIDATES" | grep -c .) 个交互式 claude 进程，其中 Ghostty 内 cwd=$TARGET_CWD 的有 $N_MATCHED 个，无法安全判断目标："
    for p in $CANDIDATES; do
      log "  pid=$p tty=$(ps -o tty= -p "$p" 2>/dev/null | tr -d ' ') cwd=$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    done
    die "请用 --terminal <UUID> 明确指定目标" 2
  fi
fi

# ---------- 2. 定位 session 日志（会话身份 = 第一条 cwd） ----------
# 全新会话在第一条消息发出前没有 session 文件 → 允许为空，发送后再等它出现并验证。
RESOLVER='
import sys, os, glob, json
target = sys.argv[1]
root = os.path.expanduser("~/.claude/projects")
hits = []
for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
    first_cwd = ""
    try:
        with open(f, errors="ignore") as fh:
            for line in fh:
                try: d = json.loads(line)
                except Exception: continue
                c = d.get("cwd")
                if c:
                    first_cwd = c
                    break
    except OSError:
        continue
    if first_cwd != target:
        continue
    hits.append((os.path.getmtime(f), f))
if not hits:
    print(json.dumps({"status": "none"})); sys.exit(0)
hits.sort(reverse=True)
top_m, top_f = hits[0]
if len(hits) > 1 and (top_m - hits[1][0]) < 120:
    print(json.dumps({"status": "ambiguous", "a": top_f, "b": hits[1][1]})); sys.exit(0)
print(json.dumps({"status": "ok", "path": top_f, "count": len(hits)}))
'
resolve_session() {
  python3 -c "$RESOLVER" "$CLAUDE_CWD" 2>/dev/null
}

if [ -z "$SESSION_FILE" ]; then
  RESOLVED=$(resolve_session)
  [ -n "$RESOLVED" ] || die "session 解析脚本执行失败" 2
  R_STATUS=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
  case "$R_STATUS" in
    ok)
      SESSION_FILE=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin)["path"])')
      N_CAND=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count",1))')
      [ "$N_CAND" -gt 1 ] && log "该 cwd 下有 $N_CAND 个历史会话，取最新的一个"
      ;;
    ambiguous)
      log "两个候选会话更新时间过于接近（<120s），无法安全判断哪个是活动会话："
      printf '%s' "$RESOLVED" | python3 -c 'import sys,json,os
d=json.load(sys.stdin)
print("  " + os.path.basename(d["a"]))
print("  " + os.path.basename(d["b"]))'
      die "拒绝猜测。请显式传 --session <路径>" 2
      ;;
    *)
      log "session 文件尚未建立（全新会话，第一条消息发出前正常现象），发送后再验证"
      SESSION_FILE=""
      ;;
  esac
fi
if [ -n "$SESSION_FILE" ]; then
  [ -f "$SESSION_FILE" ] || die "session 文件不存在: $SESSION_FILE" 2
  log "目标 session: $(basename "$SESSION_FILE")"
fi

# ---------- 3. 定位 Ghostty 面板 ----------
if [ -z "$TERMINAL_ID" ]; then
  # 注意：AppleScript 的 `tab` 是字面量字符串 "tab"，不是制表符，所以用 ||| 分隔
  TM_LIST=$(osascript -e 'tell application "Ghostty"
set out to ""
repeat with w from 1 to (count of windows)
repeat with t from 1 to (count of tabs of window w)
repeat with tm in terminals of tab t of window w
set out to out & (id of tm) & "|||" & (working directory of tm) & linefeed
end repeat
end repeat
end repeat
return out
end tell' 2>/dev/null)
  [ -n "$TM_LIST" ] || die "无法枚举 Ghostty 面板" 2
  # 先精确匹配，再按 pwd -P 归一化匹配（/tmp 是 /private/tmp 的符号链接）
  HIT=$(printf '%s\n' "$TM_LIST" | awk -F'\\|\\|\\|' -v cwd="$CLAUDE_CWD" '$2==cwd {print $1}')
  if [ -z "$HIT" ]; then
    HIT=""
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      tid=${line%%|||*}
      twd=${line#*|||}
      [ -n "$twd" ] || continue
      rtwd=$(cd "$twd" 2>/dev/null && pwd -P)
      [ "$rtwd" = "$CLAUDE_CWD" ] && HIT="$HIT$tid
"
    done <<TM_EOF
$TM_LIST
TM_EOF
    HIT=$(printf '%s' "$HIT" | grep . || true)
  fi
  [ -n "$HIT" ] || die "Ghostty 里没有 cwd=$CLAUDE_CWD 的面板（有人在面板里 cd 过？）" 2
  # 排除副屏自身面板（--exclude-terminal），防止消息打进副屏自己
  if [ -n "$EXCLUDE_TID" ]; then
    HIT=$(printf '%s\n' "$HIT" | grep -Fvx "$EXCLUDE_TID" || true)
  fi
  [ -n "$HIT" ] || die "排除副屏自身后，Ghostty 里没有 cwd=$CLAUDE_CWD 的其他面板" 2
  N_HIT=$(printf '%s\n' "$HIT" | grep -c .)
  if [ "$N_HIT" -gt 1 ]; then
    log "该 cwd 下有 $N_HIT 个候选面板（已排除副屏自身），无法安全判断："
    printf '%s\n' "$HIT" | sed 's/^/  /'
    die "请用 --terminal <UUID> 明确指定目标" 2
  fi
  TERMINAL_ID=$(printf '%s\n' "$HIT" | head -1)
fi
log "目标面板: $TERMINAL_ID"

# ---------- 4. 发送 ----------
SIZE_BEFORE=0
if [ -f "$SESSION_FILE" ]; then
  SIZE_BEFORE=$(stat -f %z "$SESSION_FILE")
  LINES_BEFORE=$(wc -l < "$SESSION_FILE" | tr -d ' ')
  log "发送前: size=$SIZE_BEFORE lines=$LINES_BEFORE mtime=$(stat -f %Sm "$SESSION_FILE")"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY-RUN：跳过注入。将执行："
  log "  input text <$(printf '%s' "$TEXT" | wc -l) 行, $(printf '%s' "$TEXT" | wc -c) 字节> -> terminal id $TERMINAL_ID"
  log "  sleep $ENTER_DELAY; send key enter -> terminal id $TERMINAL_ID"
  exit 0
fi

# 文本写临时文件，用 do shell script "cat" 读入：
# read POSIX file 会按旧编码（MacRoman）解码，中文会变 mojibake；
# do shell script 的结果按 UTF-8 解码（实测逐字节一致）。
TMPF=$(mktemp /tmp/ghostty-cc-bilingual-inject.XXXXXX) || die "mktemp 失败" 3
printf '%s' "$TEXT" > "$TMPF"
trap 'rm -f "$TMPF"' EXIT

osascript - "$TMPF" "$TERMINAL_ID" <<'AS' || die "input text 失败" 3
on run argv
  set tmpf to item 1 of argv
  set tid to item 2 of argv
  set txt to (do shell script "cat " & quoted form of tmpf)
  tell application "Ghostty" to input text txt to (terminal id tid)
end run
AS
sleep "$ENTER_DELAY"
osascript - "$TERMINAL_ID" <<'AS' || die "send key enter 失败" 3
on run argv
  set tid to item 1 of argv
  tell application "Ghostty" to send key "enter" to (terminal id tid)
end run
AS
log "已发送: $(printf '%s' "$TEXT" | wc -c) 字节 + enter（间隔 ${ENTER_DELAY}s）"

# ---------- 5. 回读验证 ----------
# 全新会话：发送后才会有 session 文件，先等它出现
if [ -z "$SESSION_FILE" ]; then
  DEADLINE=$(( $(date +%s) + VERIFY_TIMEOUT ))
  while [ -z "$SESSION_FILE" ] && [ "$(date +%s)" -lt "$DEADLINE" ]; do
    sleep 2
    RESOLVED=$(resolve_session)
    [ -n "$RESOLVED" ] || continue
    R_STATUS=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
    if [ "$R_STATUS" = "ok" ]; then
      SESSION_FILE=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin)["path"])')
      log "会话文件已出现: $(basename "$SESSION_FILE")（发送后验证）"
    elif [ "$R_STATUS" = "ambiguous" ]; then
      break
    fi
  done
fi

if [ -z "$SESSION_FILE" ]; then
  log "WARNING：发送后 ${VERIFY_TIMEOUT}s 内 session 文件仍未出现，无法确认落库（rc=4）"
  exit 4
fi

python3 -c '
import sys, json, time, os, re

def _int(s, d):
    try: return int(s)
    except (TypeError, ValueError): return d

def _float(s, d):
    try: return float(s)
    except (TypeError, ValueError): return d

a = sys.argv
path = a[1] if len(a) > 1 else ""
before = max(0, _int(a[2] if len(a) > 2 else "0", 0))
timeout = _float(a[3] if len(a) > 3 else "60", 60.0)
expect_re = a[4] if len(a) > 4 else ""
sent_text = a[5] if len(a) > 5 else ""
try:
    rx = re.compile(expect_re) if expect_re else None
except re.error:
    rx = None
deadline = time.time() + timeout
found = None
while time.time() < deadline and not found:
    try:
        if path and os.path.getsize(path) > before:
            with open(path, errors="ignore") as f:
                f.seek(before)
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: d = json.loads(line)
                    except Exception: continue
                    if not isinstance(d, dict): continue
                    # 面板1正忙时注入的消息会被 Claude Code 记成 queue-operation/enqueue，
                    # 而不是普通 user 记录——必须认，否则会假阴性（误报"未落库"）。
                    if d.get("type") == "queue-operation":
                        if d.get("operation") != "enqueue": continue
                        c = d.get("content")
                        txt = c if isinstance(c, str) else ""
                    else:
                        m = d.get("message") or {}
                        if not isinstance(m, dict) or m.get("role") != "user": continue
                        c = m.get("content")
                        if isinstance(c, str): txt = c
                        elif isinstance(c, list):
                            txt = " ".join(b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text")
                        else: txt = ""
                    if not txt.strip(): continue
                    if rx:
                        if not rx.search(txt): continue
                    elif sent_text.strip() and sent_text.strip() not in txt:
                        continue
                    found = (d.get("timestamp"), txt.strip()[:120]); break
    except FileNotFoundError:
        pass
    except Exception as e:
        # 任何意外异常都归为"未确认落库"(4)，绝不冒泡成 rc=1——那会被上层误报成"未发送"
        print("VERIFY-ERROR: %r" % (e,)); sys.exit(4)
    if not found: time.sleep(1.5)
if found:
    print("VERIFIED ts=%s text=%r" % found); sys.exit(0)
print("NOT-VERIFIED: %ss 内未在 jsonl 尾部找到匹配的 user/queue 记录" % timeout)
sys.exit(4)
' "$SESSION_FILE" "$SIZE_BEFORE" "$VERIFY_TIMEOUT" "$EXPECT_RE" "$TEXT"
RC=$?

LINES_AFTER=$(wc -l < "$SESSION_FILE" | tr -d ' ')
log "发送后: size=$(stat -f %z "$SESSION_FILE") lines=$LINES_AFTER"
if [ "$RC" = "0" ]; then
  log "SUCCESS：消息已确认落到 session，TUI 已收到"
elif [ "$RC" = "4" ]; then
  log "WARNING：消息未能确认落库（rc=${RC}），请人工检查该对话，不要重复发送"
else
  # 到这里说明验证步骤本身出问题了（不是"没发出去"）。注入已经执行过，
  # 一律降级成 4（已发送但未确认），绝不能返回 1 让上层误报"未发送"。
  log "WARNING：验证步骤异常退出（rc=${RC}）——消息多半已送达，请人工检查面板1，不要重复发送"
  RC=4
fi
exit "$RC"
