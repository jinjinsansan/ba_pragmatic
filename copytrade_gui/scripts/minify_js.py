#!/usr/bin/env python3
"""
Build-time JS minifier for BACOPY Copytrade GUI.
Strips comments and reduces whitespace to make source code less readable.
Run before electron-builder to obfuscate bundled JS.
"""
import re
import os
import sys
import shutil

TARGETS = [
    ("src/main.js",         "src/main.min.js"),
    ("src/renderer/app.js", "src/renderer/app.min.js"),
]

def _strip_comments(code: str) -> str:
    # State machine: skip // and /* */ but not inside strings or regex literals
    result = []
    i = 0
    in_single_quote = False
    in_double_quote = False
    in_template = False
    n = len(code)
    while i < n:
        c = code[i]
        if in_single_quote:
            result.append(c)
            if c == '\\' and i + 1 < n:
                result.append(code[i + 1])
                i += 2
                continue
            if c == "'":
                in_single_quote = False
        elif in_double_quote:
            result.append(c)
            if c == '\\' and i + 1 < n:
                result.append(code[i + 1])
                i += 2
                continue
            if c == '"':
                in_double_quote = False
        elif in_template:
            result.append(c)
            if c == '\\' and i + 1 < n:
                result.append(code[i + 1])
                i += 2
                continue
            if c == '`':
                in_template = False
        elif c == "'" and not (in_double_quote or in_template):
            in_single_quote = True
            result.append(c)
        elif c == '"' and not (in_single_quote or in_template):
            in_double_quote = True
            result.append(c)
        elif c == '`' and not (in_single_quote or in_double_quote):
            in_template = True
            result.append(c)
        elif c == '/' and i + 1 < n:
            nc = code[i + 1]
            if nc == '/':
                # Single-line comment: skip until newline
                i += 2
                while i < n and code[i] != '\n':
                    i += 1
                result.append('\n')
                continue
            elif nc == '*':
                # Multi-line comment: skip until */
                i += 2
                while i < n - 1:
                    if code[i] == '*' and code[i + 1] == '/':
                        i += 2
                        break
                    if code[i] == '\n':
                        result.append('\n')
                    i += 1
                continue
            else:
                result.append(c)
        else:
            result.append(c)
        i += 1
    return ''.join(result)


def _reduce_whitespace(code: str) -> str:
    # Collapse multiple blank lines to one
    code = re.sub(r'\n{3,}', '\n\n', code)
    # Remove trailing spaces
    lines = [ln.rstrip() for ln in code.split('\n')]
    # Remove leading whitespace on lines that are pure whitespace
    lines = ['' if not ln.strip() else ln for ln in lines]
    return '\n'.join(lines)


def minify_file(src: str, dst: str) -> None:
    code = open(src, encoding='utf-8').read()
    code = _strip_comments(code)
    code = _reduce_whitespace(code)
    os.makedirs(os.path.dirname(dst) if os.path.dirname(dst) else '.', exist_ok=True)
    open(dst, 'w', encoding='utf-8').write(code)
    orig_size = os.path.getsize(src)
    mini_size = os.path.getsize(dst)
    pct = 100 * (1 - mini_size / orig_size)
    print(f"  {os.path.basename(src)}: {orig_size:,} → {mini_size:,} bytes ({pct:.1f}% smaller)")


def main():
    # --inplace フラグ: 元ファイルを直接上書き (electron-builder prebuild 用)
    inplace = '--inplace' in sys.argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"[minify] root={root} inplace={inplace}")
    for src_rel, dst_rel in TARGETS:
        src = os.path.join(root, src_rel)
        dst = os.path.join(root, dst_rel)
        if not os.path.exists(src):
            print(f"  SKIP (not found): {src_rel}")
            continue
        if inplace:
            # ビルド時: 元ファイルを直接上書き
            minify_file(src, src + '.tmp')
            shutil.move(src + '.tmp', src)
            print(f"  → replaced {src_rel} in-place")
        else:
            # 確認用: .min.js に出力
            minify_file(src, dst)
            print(f"  → wrote {dst_rel}")
    print("[minify] done")


if __name__ == '__main__':
    main()
