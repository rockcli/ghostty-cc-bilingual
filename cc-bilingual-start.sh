#!/bin/sh
# cc-bilingual-start.sh —— 在当前 Ghostty 面板右侧开一个分屏跑 ghostty-cc-bilingual。
#
# 用法（在要干活的目录里执行）:
#   cc-bilingual-start.sh                 # 只开副屏（面板1的 claude 照旧跑）
#   cc-bilingual-start.sh --launch        # 顺带在当前面板启动 claude
#   cc-bilingual-start.sh <项目目录>      # 指定 cwd
#
# 布局:
#   ┌──────────────┬──────────────┐
#   │ 面板1: claude │ 面板2: 副屏   │
#   └──────────────┴──────────────┘
# 之后你在面板2输入中文，会自动翻译成英文发到面板1；面板1的英文回复
# 会在面板2显示中文译文。切面板用 Cmd+[ / Cmd+]。

set -u
BASE=$(cd "$(dirname "$0")" && pwd)
DIR=""
LAUNCH=0
for a in "$@"; do
  case "$a" in
    --launch) LAUNCH=1 ;;
    -*) echo "未知参数: $a"; exit 1 ;;
    *) DIR="$a" ;;
  esac
done
DIR=$(cd "${DIR:-$PWD}" && pwd -P) || { echo "目录不存在"; exit 1; }

osascript - "$DIR" "$BASE" "$LAUNCH" <<'AS'
on run argv
  set dir to item 1 of argv
  set base to item 2 of argv
  set launch to item 3 of argv as integer
  tell application "Ghostty"
    set t1 to focused terminal of selected tab of front window
    -- 右侧分屏跑 cc_bilingual.py
    set cfgMirror to new surface configuration
    set initial working directory of cfgMirror to dir
    set t2 to split t1 direction right with configuration cfgMirror
    set cmd to "python3 " & quoted form of (base & "/cc_bilingual.py") & " --cwd " & quoted form of dir
    input text cmd to t2
    delay 3
    send key "enter" to t2
    -- 需要的话在当前面板启动 claude
    if launch is 1 then
      input text "claude" to t1
      delay 3
      send key "enter" to t1
    end if
    -- 焦点留给副屏（副屏启动时据此识别自己所在面板）
  end tell
end run
AS
RC=$?
[ "$RC" -eq 0 ] || exit "$RC"
echo "ghostty-cc-bilingual 分屏已开（右侧）。面板1跑 claude，面板2输入中文即可。"
