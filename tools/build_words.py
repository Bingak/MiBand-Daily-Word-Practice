# -*- coding: utf-8 -*-
"""
词库分片构建工具
================
把单个巨大的 `words.js`（ES module 对象数组）转换为「精简 + 分片 + 紧凑字符串」格式，
用于解决小米手环 10 快应用安装/运行时因一次性构造海量 JS 对象导致设备重启的问题。

用法：
    python tools/build_words.py                     # 默认：全量词条，每片 200 条
    python tools/build_words.py --limit 2000        # 只取前 2000 条
    python tools/build_words.py --per-part 150      # 每片 150 条
    python tools/build_words.py --keep-detail       # 保留完整释义（不精简）

输出：
    src/common/words/part_00.js, part_01.js, ...    每片一个紧凑字符串
    src/common/words/index.js                       元数据 + 分片聚合
"""

import argparse
import json
import os
import re
import sys

# 字段分隔符：使用不可能出现在单词/音标/释义中的控制字符
FIELD_SEP = "\x01"
ENTRY_SEP = "\n"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 源词库放在 data/ 而不是 src/，避免被打包进 rpk
SRC_WORDS = os.path.join(ROOT, "data", "words.source.js")
OUT_DIR = os.path.join(ROOT, "src", "common", "words")

# 精简释义时，单条 chinese 允许保留的最大字符数
MAX_MEANING_LEN = 46
# 精简释义时，最多保留的义项段数（以 | 分隔）
MAX_MEANING_SEGS = 2


def load_source(path):
    """解析 `export const words = [ {...}, {...}, ]` 形式的源文件。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("未能在 %s 中定位数组字面量" % path)

    body = text[start:end + 1]
    # 去掉尾随逗号，["a", "b", ] -> ["a", "b"]
    body = re.sub(r",\s*\]", "]", body)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError("解析词库 JSON 失败：%s" % e)

    return data


def simplify_meaning(text):
    """
    精简中文释义：
      "v. 抛弃，舍弃，放弃 | 遗弃；放纵 | abandon oneself to 沉溺于 | abandon hope 放弃希望"
      -> "v. 抛弃，舍弃，放弃 | 遗弃；放纵"
    去掉的是例句/搭配，学习时并非必需，但体积占比很高。
    """
    if not text:
        return ""
    segs = [s.strip() for s in text.split("|") if s.strip()]
    if not segs:
        return ""
    kept = segs[:MAX_MEANING_SEGS]
    out = " | ".join(kept)
    if len(out) > MAX_MEANING_LEN:
        out = out[:MAX_MEANING_LEN].rstrip("，,、 ") + "…"
    return out


def sanitize(s):
    """去掉会破坏紧凑格式的控制字符。"""
    if s is None:
        return ""
    s = str(s).replace(FIELD_SEP, " ").replace("\n", " ").replace("\r", " ")
    return s.strip()


# 源文件里存在键名拼写/大小写不一致的问题（如 Chinese / photetic / chromatic），
# 这里做容错归一化，否则这些词条的释义或音标会变成空字符串。
KEY_ALIASES = {
    "english": "english",
    "phonetic": "phonetic",
    "photetic": "phonetic",
    "photnetic": "phonetic",
    "chinese": "chinese",
    "chromatic": "chinese",
}


def normalize_entry(raw):
    """把一条原始记录归一化成 {english, phonetic, chinese}。"""
    out = {"english": "", "phonetic": "", "chinese": ""}
    for k, v in raw.items():
        target = KEY_ALIASES.get(str(k).strip().lower())
        if target and not out[target]:
            out[target] = v
    return out


def normalize_all(entries):
    """批量归一化，顺带统计被键名容错挽回的字段数。"""
    out = []
    fixed_meaning = 0
    fixed_phonetic = 0
    for raw in entries:
        e = normalize_entry(raw)
        if not raw.get("chinese") and e.get("chinese"):
            fixed_meaning += 1
        if not raw.get("phonetic") and e.get("phonetic"):
            fixed_phonetic += 1
        out.append(e)
    return out, fixed_meaning, fixed_phonetic


def js_escape(s):
    """把字符串转成可安全嵌入 JS 双引号字面量的形式。"""
    out = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace(FIELD_SEP, "\\u0001")
        .replace("\n", "\\n")
        .replace("\r", "")
    )
    return out


def dedupe(entries):
    """按 english 去重，保留释义最完整的那条。返回 (结果, 去重掉的条数)。"""
    best = {}
    order = []
    for e in entries:
        eng = str(e.get("english", "")).strip()
        if not eng:
            continue
        score = len(str(e.get("chinese", "") or "")) + len(str(e.get("phonetic", "") or ""))
        if eng not in best:
            best[eng] = (e, score)
            order.append(eng)
        elif score > best[eng][1]:
            best[eng] = (e, score)
    removed = len(entries) - len(order)
    return [best[k][0] for k in order], removed


def build(entries, per_part, keep_detail):
    """生成分片数据。返回 (parts, stats)，parts 为字符串列表。"""
    total = len(entries)
    buf = []
    raw_bytes = 0
    out_bytes = 0
    fixed_meaning = 0
    fixed_phonetic = 0
    empty_meaning = 0

    for e in entries:
        eng = sanitize(e.get("english", ""))
        pho = sanitize(e.get("phonetic", ""))
        chi = e.get("chinese", "")
        chi = sanitize(chi if keep_detail else simplify_meaning(chi))
        if not eng:
            continue
        if not chi:
            empty_meaning += 1
        raw_bytes += len((e.get("english", "") + e.get("phonetic", "") +
                          e.get("chinese", "")).encode("utf-8"))
        line = FIELD_SEP.join([eng, pho, chi])
        out_bytes += len(line.encode("utf-8"))
        buf.append(line)

    lines = buf
    parts = []
    for i in range(0, len(lines), per_part):
        chunk = lines[i:i + per_part]
        parts.append(ENTRY_SEP.join(chunk))

    stats = {
        "total": len(lines),
        "parts": len(parts),
        "raw_bytes": raw_bytes,
        "out_bytes": out_bytes,
        "empty_meaning": empty_meaning,
    }
    return parts, stats


def write_parts(parts):
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    # 清理旧分片
    for name in os.listdir(OUT_DIR):
        if re.match(r"^part_\d+\.js$", name) or name == "index.js":
            os.remove(os.path.join(OUT_DIR, name))

    for idx, content in enumerate(parts):
        path = os.path.join(OUT_DIR, "part_%02d.js" % idx)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("// AUTO-GENERATED by tools/build_words.py -- DO NOT EDIT\n")
            f.write("// 分片 %d，格式: english\\u0001phonetic\\u0001chinese，词条间以 \\n 分隔\n" % idx)
            f.write('export default "%s";\n' % js_escape(content))


def write_index(parts, stats, per_part):
    """生成元数据 + 静态聚合模块。"""
    lines = []
    lines.append("// AUTO-GENERATED by tools/build_words.py -- DO NOT EDIT")
    lines.append("// 词库元数据：修改词库后请重新运行 python tools/build_words.py")
    lines.append("")
    for i in range(len(parts)):
        lines.append('import part%02d from "./part_%02d.js";' % (i, i))
    lines.append("")
    lines.append("// 每个分片的原始字符串（惰性解析，不要在启动时 split 全部）")
    lines.append("const RAW_PARTS = [")
    for i in range(len(parts)):
        lines.append("  part%02d%s" % (i, "," if i < len(parts) - 1 else ""))
    lines.append("];")
    lines.append("")
    lines.append("// 每个分片的词条数量（最后一片可能不足）")
    lines.append("const PART_SIZES = [")
    sizes = [len(p.split(ENTRY_SEP)) for p in parts]
    for i, s in enumerate(sizes):
        lines.append("  %d%s" % (s, "," if i < len(sizes) - 1 else ""))
    lines.append("];")
    lines.append("")
    lines.append("export const META = {")
    lines.append("  total: %d," % stats["total"])
    lines.append("  partCount: %d," % len(parts))
    lines.append("  perPart: %d," % per_part)
    lines.append("  partSizes: PART_SIZES")
    lines.append("};")
    lines.append("")
    lines.append("export default RAW_PARTS;")
    lines.append("")

    path = os.path.join(OUT_DIR, "index.js")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def human(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.2f MB" % (n / 1024.0 / 1024.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC_WORDS, help="源 words.js 路径")
    ap.add_argument("--limit", type=int, default=0, help="只保留前 N 条（0 = 全部）")
    ap.add_argument("--per-part", type=int, default=200, help="每片词条数")
    ap.add_argument("--keep-detail", action="store_true", help="保留完整释义，不精简")
    args = ap.parse_args()

    if not os.path.isfile(args.src):
        print("[x] 找不到源词库: %s" % args.src)
        return 1

    entries = load_source(args.src)
    print("[i] 源词库条目数: %d" % len(entries))
    if args.limit and args.limit < len(entries):
        entries = entries[:args.limit]
        print("[i] 按 --limit 截取为: %d" % len(entries))

    entries, fixed_meaning, fixed_phonetic = normalize_all(entries)
    entries, removed = dedupe(entries)
    print("[i] 去重后唯一单词: %d（移除重复 %d 条）" % (len(entries), removed))

    parts, stats = build(entries, args.per_part, args.keep_detail)
    write_parts(parts)
    write_index(parts, stats, args.per_part)

    print("[√] 输出目录: %s" % OUT_DIR)
    print("    词条总数 : %d" % stats["total"])
    print("    分片数量 : %d（每片 %d 条）" % (stats["parts"], args.per_part))
    print("    释义精简 : %s" % ("关闭（保留完整）" if args.keep_detail else "开启"))
    print("    原始文本 : %s" % human(stats["raw_bytes"]))
    print("    优化文本 : %s（减少 %.1f%%）" % (
        human(stats["out_bytes"]),
        (1 - stats["out_bytes"] / float(stats["raw_bytes"])) * 100 if stats["raw_bytes"] else 0,
    ))
    if fixed_meaning or fixed_phonetic:
        print("    键名修复 : 挽回释义 %d 条、音标 %d 条" % (fixed_meaning, fixed_phonetic))
    if stats["empty_meaning"]:
        print("    [!] 仍缺失释义: %d 条（源数据本身为空）" % stats["empty_meaning"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
