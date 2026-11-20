#!/usr/bin/env python3
"""ghostty-cc-bilingual 翻译指标报告。

读取 translate.py 写入的 JSONL 统计（默认 ~/.ghostty-cc-bilingual/stats.jsonl），输出：
  · 调用次数与模式分布（fast 快路径 / repair 段落返修 / rebuild 确定性重建）
  · 端到端耗时 P50 / P95（以及快路径耗时）
  · 生成速度 tok/s（原生 ollama 接口的 eval 明细）
  · 格式保真率（加粗保留、占位符丢失率）
  · 按 provider / model 分组

用法:
  python3 stats_report.py                # 全部
  python3 stats_report.py --today        # 只看今天
  python3 stats_report.py --last 50      # 只看最近 50 次
"""
import argparse
import json
import os
import time
from collections import Counter, defaultdict

STATS_PATH = os.environ.get(
    "CC_BILINGUAL_STATS",
    os.path.join(
        os.path.expanduser(os.environ.get("CC_BILINGUAL_HOME", "~/.ghostty-cc-bilingual")),
        "stats.jsonl",
    ),
)


def percentile(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = int(round((p / 100.0) * (len(vals) - 1)))
    return vals[max(0, min(len(vals) - 1, k))]


def fnum(v, unit="", fmt="%.2f"):
    return "—" if v is None else (fmt % v) + unit


def tok_per_s(rec):
    """从一次调用的明细算 tok/s（原生 ollama 用 eval 时长，其余用墙钟）。"""
    for key in ("last", "fast"):
        d = rec.get(key) or {}
        n, ms = d.get("eval_count"), d.get("eval_ms")
        if n and ms and ms > 0:
            return n / (ms / 1000.0)
        n, w = d.get("eval_count"), d.get("wall_s")
        if n and w and w > 0:
            return n / float(w)
    return None


def load(path, today_only=False, last_n=0):
    rows = []
    if not os.path.exists(path):
        return rows
    today = time.strftime("%F")
    with open(path, errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if today_only and not str(r.get("ts", "")).startswith(today):
                continue
            rows.append(r)
    return rows[-last_n:] if last_n else rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=STATS_PATH)
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--last", type=int, default=0)
    args = ap.parse_args()

    rows = load(args.file, args.today, args.last)
    print("统计文件: %s" % args.file)
    if not rows:
        print("（暂无数据；翻译几次后再看）")
        return
    print("样本数: %d%s\n" % (len(rows), "（今天）" if args.today else ""))

    modes = Counter(r.get("mode", "?") for r in rows)
    walls = [r["wall_s"] for r in rows if isinstance(r.get("wall_s"), (int, float))]
    fasts = [r["fast_s"] for r in rows if isinstance(r.get("fast_s"), (int, float))]
    tps = [t for t in (tok_per_s(r) for r in rows) if t]
    keep = [r for r in rows if (r.get("bold_src") or 0) >= 2]
    kept = [r for r in keep if (r.get("bold_out") or 0) >= (r.get("bold_src") or 0)]
    token_loss = [r for r in rows if (r.get("tokens") or 0) and (r.get("tokens_missing") or 0) > 0]
    repaired = [r.get("repaired_blocks") or 0 for r in rows if r.get("mode") in ("repair", "rebuild")]

    print("== 模式分布 ==")
    for m, c in modes.most_common():
        print("  %-8s %4d 次  %5.1f%%" % (m, c, 100.0 * c / len(rows)))

    print("\n== 耗时 ==")
    print("  端到端  P50 %s   P95 %s   max %s" % (
        fnum(percentile(walls, 50), "s"), fnum(percentile(walls, 95), "s"),
        fnum(max(walls) if walls else None, "s")))
    print("  快路径  P50 %s   P95 %s" % (
        fnum(percentile(fasts, 50), "s"), fnum(percentile(fasts, 95), "s")))
    if tps:
        print("  生成速度 P50 %s   P95 %s" % (
            fnum(percentile(tps, 50), " tok/s", "%.1f"), fnum(percentile(tps, 95), " tok/s", "%.1f")))
    else:
        print("  生成速度 无 token 明细（用 http 接口或未返回 usage）")

    print("\n== 格式保真 ==")
    print("  含加粗的调用 %d 次，加粗完整保留 %d 次（%s）" % (
        len(keep), len(kept),
        ("%.1f%%" % (100.0 * len(kept) / len(keep))) if keep else "—"))
    print("  占位符(代码/URL)丢失的调用 %d 次（%s）" % (
        len(token_loss), ("%.1f%%" % (100.0 * len(token_loss) / len(rows))) if rows else "—"))
    if repaired:
        print("  返修时平均重翻 %.1f 个段落" % (sum(repaired) / len(repaired)))

    by = defaultdict(list)
    for r in rows:
        prov = (r.get("last") or {}).get("provider") or (r.get("fast") or {}).get("provider") or "?"
        model = (r.get("last") or {}).get("model") or "?"
        by["%s / %s" % (prov, model)].append(r)
    print("\n== 按 provider / model ==")
    for k, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        w = [x["wall_s"] for x in rs if isinstance(x.get("wall_s"), (int, float))]
        print("  %-34s %4d 次   P50 %s" % (k, len(rs), fnum(percentile(w, 50), "s")))


if __name__ == "__main__":
    main()
