"""Model aliases and the CN-exclusive model gate (issues #617 / #618).

Claude Desktop's custom endpoint only exposes ``claude-*`` names, while the
upstream only knows real model ids. These tests pin the alias rewrite at the
dispatch layer, the legacy settings shape it must keep reading, and the
neighbouring CN-only rule that must not swallow every ``minimax-*`` model.

Run with: python tests/_test_model_alias_and_realm.py
No upstream credentials or network access are used.
"""
import os
import sys
import tempfile

_startup_dir = tempfile.TemporaryDirectory(prefix="model-alias-realm-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P
import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] the legacy model_aliases object is read")

directory = tempfile.mkdtemp(prefix="alias-settings-")
S.save(directory, {
    "model_aliases": {
        "claude-opus-5": "hy4-preview-f",
        "Claude-Sonnet-5": "deepseek-v4.1-flash",
        "bad empty": "",
        "same": "same",
    }
})
aliases = S.model_aliases(directory)
check("object aliases are normalized to lowercase keys",
      aliases.get("claude-opus-5") == "hy4-preview-f", aliases)
check("mixed-case aliases match case-insensitively",
      aliases.get("claude-sonnet-5") == "deepseek-v4.1-flash", aliases)
check("empty and identity mappings are dropped",
      "bad empty" not in aliases and "same" not in aliases, aliases)

print()
print("[2] the list spelling is accepted without breaking a bad file")

S.save(directory, {"model_aliases": [
    {"from": "claude-opus-5", "to": "hy4-preview-f"},
    {"alias": "claude-sonnet-5", "model": "deepseek-v4.1-flash"},
    "not-an-object",
    {"from": "", "to": "x"},
]})
aliases = S.model_aliases(directory)
check("list aliases are read", aliases.get("claude-opus-5") == "hy4-preview-f",
      aliases)
check("alternate list keys are read",
      aliases.get("claude-sonnet-5") == "deepseek-v4.1-flash", aliases)

S.save(directory, {"model_aliases": "not-a-table"})
check("malformed table reads as empty", S.model_aliases(directory) == {})

print()
print("[3] the dispatch rewrite touches only configured aliases")

body = {"model": "CLAUDE-OPUS-5", "messages": []}
P._apply_model_aliases(body, {"claude-opus-5": "hy4-preview-f"})
check("alias is resolved case-insensitively", body["model"] == "hy4-preview-f",
      body)

untouched = {"model": "deepseek-v4.1-flash", "messages": []}
P._apply_model_aliases(untouched, {"claude-opus-5": "hy4-preview-f"})
check("unknown models pass through", untouched["model"] == "deepseek-v4.1-flash",
      untouched)

missing = {"messages": []}
P._apply_model_aliases(missing, {"claude-opus-5": "hy4-preview-f"})
check("a body without a model is untouched", missing == {"messages": []}, missing)

P._apply_model_aliases(None, {"claude-opus-5": "hy4-preview-f"})
P._apply_model_aliases({"model": 42}, {"claude-opus-5": "hy4-preview-f"})

print()
print("[4] minimax is not a wildcard CN-only prefix (issue #618)")

check("minimax-m3.1-flash is not CN-exclusive",
      P.exclusive_realm("minimax-m3.1-flash") == "",
      P.exclusive_realm("minimax-m3.1-flash"))
check("explicit minimax-m3 stays CN-exclusive",
      P.exclusive_realm("minimax-m3") == "cn",
      P.exclusive_realm("minimax-m3"))
check("deepseek-v4-pro stays CN-exclusive",
      P.exclusive_realm("deepseek-v4-pro") == "cn",
      P.exclusive_realm("deepseek-v4-pro"))
check("deepseek-v4-pro-preview also stays CN-exclusive",
      P.exclusive_realm("deepseek-v4-pro-preview") == "cn",
      P.exclusive_realm("deepseek-v4-pro-preview"))

print()
print("[5] the background rewrite remains independent")

body = {"model": "gpt-5.6-terra"}
P._apply_background_model_alias(body)
check("explicit gpt-5.6-terra is not background-rewritten",
      body["model"] == "gpt-5.6-terra", body)

print()
print("[6] legacy realm-prefixed model ids split at the entry (2026-10 upstream)")

# 上游收紧后 /v2/chat/completions 只认裸名："cn:x" 会回 11102 service info
# not found。前缀只用于本地选区，进门就剥掉；不在词汇表里的冒号命名空间
# （qwen:7b 这类第三方模型名）与空值必须原样保留。
cases = [
    ("cn:deepseek-v4.1-flash", ("deepseek-v4.1-flash", "cn")),
    ("CN:hy3", ("hy3", "cn")),
    ("global:deepseek-v4.1-flash", ("deepseek-v4.1-flash", "intl")),
    ("intl:hy4-preview", ("hy4-preview", "intl")),
    ("deepseek-v4.1-flash", ("deepseek-v4.1-flash", None)),
    ("qwen:7b", ("qwen:7b", None)),
    ("a/b:caret", ("a/b:caret", None)),
    ("", ("", None)),
]
for raw, want in cases:
    got = P.split_realm_prefix(raw)
    check("split_realm_prefix(%r) == %r" % (raw, want), got == want, got)
check("split_realm_prefix(None) is safe", P.split_realm_prefix(None) == ("", None),
      P.split_realm_prefix(None))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
