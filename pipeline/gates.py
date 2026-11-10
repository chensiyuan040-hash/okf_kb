"""gates.py — 质量门(AD-7 阻断/告警两级;清单单一来源 gate.yaml)。

基座 = 既有 okf-tidy 工具组(gate.sh = iwe schema validate + vault-health --gate),
其余门项在其上扩展,设计与脚本不出现两套(修订②)。

八门对应(门 1 拆两条 code:lint 阻断 / lint-review 告警):
  lint / lint-review  wiki_lint(经 MCP):Missing、Orphans 阻断清零;矛盾/歧义/gaps 告警提示
  schema              okf-tidy gate.sh(结构合规 + 内容整洁)
  structure           iwe find:本批新页必须被某 synthesis 枢纽约束
  registry            磁盘 .md 不在 registry = 绕过工具写入的证据
  status              本批 source 页 status ∈ {draft, stable, deprecated}
  raw-immutable       raw 与批次前基线快照比对(只增不改;不用裸 git diff)
  wiki-status         blockingDiagnostics 空 + okf-0.2 声明在
  extraction          源包 extraction_status 非 success → 阻断候补、人工处置
  extraction-length   提取文本长度比异常(告警)
  knowledge-score     本批新增/变更页内容质量评分(告警;rubric 配置化在 gate.yaml
                      knowledge_score 段,标准未配置自动跳过——占位待确认)

每个门返回 GateResult;编排器据 gate.yaml 的 level 决定放行/告警。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .mdfront import parse_frontmatter, read_frontmatter
from .paths import SourcePaths, iwe_bin, node_bin
from .state import load_raw_baseline, save_raw_baseline

ALLOWED_SOURCE_STATUS = {"draft", "stable", "deprecated"}
GENERATED_MD = {"index.md", "log.md"}
PAGE_DIRS = ("concepts", "entities", "analyses", "requirements", "syntheses", "sources")


@dataclass
class GateResult:
    code: str
    level: str
    ok: bool
    counts: dict = field(default_factory=dict)
    detail: str = ""

    def event_fields(self) -> dict:
        return {"ok": self.ok, "counts": self.counts, "detail": self.detail[:400]}


@dataclass
class GateContext:
    repo: Path
    paths: SourcePaths
    cfg: Config
    batch_id: str
    mcp: object | None = None          # McpClient(写侧建库通道)
    new_pages: list[str] = field(default_factory=list)   # 本批新建/更新页 key(不含 sources/syntheses,structure 门口径)
    new_srcs: list[str] = field(default_factory=list)    # 本批捕获的 SRC id
    # 评分门口径:本批全部产出页(含 sources/),created/updated 分开记(evolution 项只对更新页要求)
    pages_created: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)


def run_gates(ctx: GateContext) -> list[GateResult]:
    """按 gate.yaml 顺序跑全部门(不短路:一次跑完,报告里给出全貌)。"""
    checks = {
        "lint": lambda: _gate_lint(ctx, review=False),
        "lint-review": lambda: _gate_lint(ctx, review=True),
        "schema": lambda: _gate_schema(ctx),
        "structure": lambda: _gate_structure(ctx),
        "registry": lambda: _gate_registry(ctx),
        "status": lambda: _gate_status(ctx),
        "raw-immutable": lambda: _gate_raw_immutable(ctx),
        "wiki-status": lambda: _gate_wiki_status(ctx),
        "extraction": lambda: _gate_extraction(ctx, warn_only=False),
        "extraction-length": lambda: _gate_extraction(ctx, warn_only=True),
        "knowledge-score": lambda: _gate_knowledge_score(ctx),
    }
    results: list[GateResult] = []
    for g in ctx.cfg.gates():
        code, level = g["code"], g["level"]
        fn = checks.get(code)
        if fn is None:
            results.append(GateResult(code, level, False, {}, f"gate.yaml 登记了未实现的门: {code}"))
            continue
        try:
            res = fn()
            res.level = level
        except Exception as e:  # noqa: BLE001 —— 门自身异常 = 该门不放行(宁停勿漏)
            res = GateResult(code, level, False, {}, f"门执行异常: {type(e).__name__}: {e}")
        results.append(res)
    return results


# ---------------------------------------------------------------- 门:lint

_LINT_COUNT = re.compile(r"^-\s*(Orphans|Missing|Contradictions|Gaps|Unfiled):\s*(\d+)\s*$", re.M)
_LINT_GAPS = re.compile(r"(\d+)\s+knowledge gap\(s\) tracked")   # gaps 无 "- Gaps:" 行,是尾注文案


def _gate_lint(ctx: GateContext, review: bool) -> GateResult:
    code = "lint-review" if review else "lint"
    if ctx.mcp is None:
        return GateResult(code, "block", False, {}, "无 MCP 连接,无法跑 wiki_lint")
    text = ctx.mcp.call("wiki_lint", {}, timeout=300.0)   # 对齐 manage lint:库增长后 120s 不够
    counts = {m.group(1).lower(): int(m.group(2)) for m in _LINT_COUNT.finditer(text)}
    if "gaps" not in counts and (gm := _LINT_GAPS.search(text)):
        counts["gaps"] = int(gm.group(1))   # bundles 尾注形态:"💡 N knowledge gap(s) tracked"
    # 工具正常输出**总含** "- Missing: N" / "- Orphans: N" 两行(零值也打印,实测)。
    # 解析不到 = 输出形态变了/调用返回异常文本——不放行(宁停勿漏,与 kb-patrol 的 -1 口径一致);
    # 若只看缺省 0 会让门静默通过。
    if "missing" not in counts or "orphans" not in counts:
        return GateResult(code, "block", False, counts,
                          f"wiki_lint 输出缺 Missing/Orphans 计数行(输出无法解析):{text[:200]}")
    if review:
        # 矛盾/歧义/gaps:分类处置、不得丢弃——告警级,提示随时归档(人审聚焦点)
        review_counts = {k: counts.get(k, 0) for k in ("contradictions", "gaps")}
        bad = sum(review_counts.values())
        return GateResult(code, "warn", bad == 0, review_counts, "存在矛盾/gap,需分类处置(不得丢弃)")
    missing = counts.get("missing", 0)
    orphans = counts.get("orphans", 0)
    detail = f"Missing={missing} Orphans={orphans}" + (f" Unfiled={counts.get('unfiled', 0)}" if counts.get("unfiled") else "")
    return GateResult(code, "block", missing == 0 and orphans == 0, counts, detail)


# ---------------------------------------------------------------- 门:schema(okf-tidy gate.sh)

def _gate_schema(ctx: GateContext) -> GateResult:
    gate_sh = ctx.repo / "tools" / "okf-tidy" / "gate.sh"
    if not gate_sh.exists():
        return GateResult("schema", "block", False, {}, f"找不到基座门禁脚本: {gate_sh}")
    iwe_shim = ctx.repo / "pipeline" / "deploy" / "iwe.sh"
    env = {
        "NODE": str(node_bin(ctx.repo)),
        "IWE": str(iwe_shim),
    }
    import os
    full_env = {**os.environ, **env}
    proc = subprocess.run(
        ["bash", str(gate_sh), str(ctx.paths.vault)],
        capture_output=True, text=True, env=full_env, timeout=600,
    )
    ok = proc.returncode == 0
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-12:]
    return GateResult("schema", "block", ok, {"rc": proc.returncode}, "\n".join(tail))


# ---------------------------------------------------------------- 门:structure(体系复核)

def _gate_structure(ctx: GateContext) -> GateResult:
    if not ctx.new_pages:
        return GateResult("structure", "block", True, {"new_pages": 0}, "本批无新页")
    wiki = ctx.paths.vault / ".llm-wiki" / "wiki"
    node = str(node_bin(ctx.repo))
    iwe = str(iwe_bin(ctx.repo))
    unbound = []
    for key in ctx.new_pages:
        proc = subprocess.run(
            [node, iwe, "find", "--key", key, "--format", "json"],
            capture_output=True, text=True, cwd=str(wiki), timeout=120,
        )
        if proc.returncode != 0:
            unbound.append(f"{key}(iwe 查询失败: {proc.stderr.strip()[:80]})")
            continue
        try:
            docs = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            unbound.append(f"{key}(iwe 输出非 JSON)")
            continue
        if not docs:
            unbound.append(f"{key}(iwe 中不存在)")
            continue
        included_by = docs[0].get("includedBy") or []
        if not any(str(x.get("key", "")).startswith("syntheses/") for x in included_by):
            unbound.append(key)
    ok = not unbound
    return GateResult("structure", "block", ok, {"unbound": len(unbound), "new_pages": len(ctx.new_pages)},
                      "未被枢纽约束: " + ", ".join(unbound[:10]) if unbound else "全部新页已被 synthesis 枢纽约束")


# ---------------------------------------------------------------- 门:registry 外页面

def _gate_registry(ctx: GateContext) -> GateResult:
    vault = ctx.paths.vault
    registry_file = vault / ".llm-wiki" / "meta" / "registry.json"
    if not registry_file.exists():
        return GateResult("registry", "block", False, {"outside": 0}, "registry.json 不存在(建库未完成?)")
    try:
        registered = set(json.loads(registry_file.read_text(encoding="utf-8")).get("pages", {}))
    except json.JSONDecodeError:
        return GateResult("registry", "block", False, {}, "registry.json 损坏")
    wiki = vault / ".llm-wiki" / "wiki"
    outside = []
    for d in PAGE_DIRS:
        for p in (wiki / d).glob("*.md"):
            if p.name in GENERATED_MD:
                continue
            key = f"{d}/{p.stem}"
            if key not in registered:
                outside.append(key)
    ok = not outside
    return GateResult("registry", "block", ok, {"outside": len(outside), "registered": len(registered)},
                      "registry 外页面: " + ", ".join(outside[:10]) if outside else "")


# ---------------------------------------------------------------- 门:status 归一

def _gate_status(ctx: GateContext) -> GateResult:
    bad = []
    for src in ctx.new_srcs:
        page = ctx.paths.vault / ".llm-wiki" / "wiki" / "sources" / f"{src}.md"
        if not page.exists():
            bad.append(f"{src}(页缺失)")
            continue
        status = str(read_frontmatter(page).get("status", ""))
        if status not in ALLOWED_SOURCE_STATUS:
            bad.append(f"{src}(status={status or '空'})")
    ok = not bad
    return GateResult("status", "block", ok, {"bad": len(bad), "checked": len(ctx.new_srcs)},
                      "status 未归一: " + ", ".join(bad[:10]) if bad else "")


# ---------------------------------------------------------------- 门:raw 不可变

def _raw_snapshot(raw_dir: Path, previous: dict | None, max_bytes: int | None = None) -> dict:
    """raw/** 的文件指纹;size+mtime 与上一基一致时沿用旧 hash(少读)。

    `raw_immutable_max_mb`(gate.yaml)语义 = 基线快照的体积预算:累计**新算** hash
    的字节数超预算后,后续文件只记 size/mtime、不读内容(大库不至于每批全量读盘);
    比较时任一侧无 sha256 即退化为只比 size。预算内行为与不设预算完全一致。
    """
    prev_files = (previous or {}).get("files", {})
    snapshot = {"files": {}}
    if not raw_dir.is_dir():
        return snapshot
    hashed_bytes = 0
    for p in sorted(raw_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(raw_dir))
        st = p.stat()
        old = prev_files.get(rel)
        digest = None
        if old and old.get("size") == st.st_size and old.get("mtime") == int(st.st_mtime):
            digest = old.get("sha256")
        if not digest and (max_bytes is None or hashed_bytes + st.st_size <= max_bytes):
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            hashed_bytes += st.st_size
        entry = {"size": st.st_size, "mtime": int(st.st_mtime)}
        if digest:
            entry["sha256"] = digest
        snapshot["files"][rel] = entry
    snapshot["hashed_bytes"] = hashed_bytes
    return snapshot


def _gate_raw_immutable(ctx: GateContext) -> GateResult:
    raw_dir = ctx.paths.vault / ".llm-wiki" / "raw"
    prev = load_raw_baseline(ctx.paths.state_dir, ctx.paths.source)
    max_bytes = int(ctx.cfg.alerts().get("raw_immutable_max_mb", 2048)) * 1024 * 1024
    current = _raw_snapshot(raw_dir, prev, max_bytes=max_bytes)
    violations = []
    if prev:
        for rel, meta in prev.get("files", {}).items():
            cur = current["files"].get(rel)
            if cur is None:
                violations.append(f"删除: {rel}")
            elif cur.get("sha256") and meta.get("sha256"):
                if cur["sha256"] != meta["sha256"]:
                    violations.append(f"改动: {rel}")
            elif cur.get("size") != meta.get("size"):
                violations.append(f"改动(无 hash,比 size): {rel}")
    if violations:
        # 违规**不更新基线**:否则违规被本批吞掉、下一批不再报;留待人工处置,持续报警
        return GateResult("raw-immutable", "block", False,
                          {"violations": len(violations), "files": len(current["files"])},
                          "raw 违反只增不改(基线未更新,处置前每批持续报警): "
                          + "; ".join(violations[:10]))
    current["batch"] = ctx.batch_id
    save_raw_baseline(ctx.paths.state_dir, ctx.paths.source, current)
    return GateResult("raw-immutable", "block", True,
                      {"violations": 0, "files": len(current["files"]),
                       "hashed_mb": round(current.get("hashed_bytes", 0) / (1 << 20), 1)},
                      f"基线已更新({len(current['files'])} 个文件)")


# ---------------------------------------------------------------- 门:wiki_status + okf-0.2

def _gate_wiki_status(ctx: GateContext) -> GateResult:
    if ctx.mcp is None:
        return GateResult("wiki-status", "block", False, {}, "无 MCP 连接")
    data = ctx.mcp.call_json("wiki_status", {})
    blocking = data.get("blockingDiagnostics") or []
    index = ctx.paths.vault / ".llm-wiki" / "wiki" / "index.md"
    okf_version = str(read_frontmatter(index).get("okf_version", ""))
    ok = not blocking and okf_version.startswith("0.2")
    counts = {"blocking": len(blocking), "okf": okf_version or "缺失"}
    detail = ""
    if blocking:
        detail = "阻塞诊断: " + "; ".join(str(b.get("code", b)) for b in blocking[:5])
    elif not okf_version.startswith("0.2"):
        detail = f"根 index.md 缺 okf_version 0.2 声明(现: {okf_version or '空'})"
    return GateResult("wiki-status", "block", ok, counts, detail)


# ---------------------------------------------------------------- 门:提取质量(门 8)

def _gate_extraction(ctx: GateContext, warn_only: bool) -> GateResult:
    code = "extraction-length" if warn_only else "extraction"
    config = ctx.cfg.alerts()
    ratio_threshold = float(config.get("extraction_length_ratio", 0.2))
    bad, short, checked = [], [], 0
    sources_dir = ctx.paths.vault / ".llm-wiki" / "raw" / "sources"
    for src in ctx.new_srcs:
        manifest_file = sources_dir / src / "manifest.json"
        if not manifest_file.exists():
            bad.append(f"{src}(manifest 缺失)")
            continue
        checked += 1
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bad.append(f"{src}(manifest 损坏)")
            continue
        status = manifest.get("extraction_status", "success")
        if status != "success":
            bad.append(f"{src}(extraction_status={status})")
        extracted = sources_dir / src / "extracted.md"
        original_dir = sources_dir / src / "original"
        if extracted.exists() and original_dir.is_dir():
            orig_bytes = sum(f.stat().st_size for f in original_dir.rglob("*") if f.is_file())
            if orig_bytes > 0 and extracted.stat().st_size / orig_bytes < ratio_threshold:
                short.append(f"{src}({extracted.stat().st_size}/{orig_bytes})")
    if warn_only:
        return GateResult(code, "warn", not short, {"short": len(short), "checked": checked},
                          "提取文本过短: " + ", ".join(short[:10]) if short else "")
    ok = not bad
    return GateResult(code, "block", ok, {"bad": len(bad), "checked": checked},
                      "提取异常: " + ", ".join(bad[:10]) if bad else "")


# ---------------------------------------------------------------- 门:增量知识评分(warn,配置驱动)

# rubric 不写死在代码里:评分项与分值落 gate.yaml `knowledge_score` 段,
# 两段留空 = 门跳过(占位待标准确认,确认后填分值即启用,零代码改动)。
# 每个 checker 返回 0..1 的得分比例;None = N/A(该项记满分,不惩罚——
# 如非 entity 页的 aliases、新建页的 evolution)。

_BODY_MIN_CHARS = 300      # body_solid 达标线(校准点,标准确认时一并定)
_PROV_FIELDS = ("origin_url", "author", "source_system", "source_updated_at")


def _key_terms_count(body: str) -> int:
    """`## Key Terms` 段的列表项数;无该段 = 0。"""
    in_section = False
    n = 0
    for line in body.splitlines():
        if line.startswith("## "):
            if line[3:].strip().lower() == "key terms":
                in_section = True
                continue
            if in_section:
                break
        elif in_section and line.lstrip().startswith(("-", "*")):
            n += 1
    return n


def _first_para(body: str) -> str:
    """H1 之后第一个非空、非标题段落。"""
    lines, started = body.splitlines(), False
    para = []
    for line in lines:
        if not started:
            if line.startswith("# "):
                started = True
            continue
        s = line.strip()
        if not s:
            if para:
                break
            continue
        if s.startswith("#"):
            break
        para.append(s)
    return " ".join(para)


def _ck_provenance_fields(front: dict, body: str, extra: dict):
    have = sum(1 for f in _PROV_FIELDS if str(front.get(f) or "").strip())
    return have / len(_PROV_FIELDS)


def _ck_key_terms(front: dict, body: str, extra: dict):
    n = _key_terms_count(body)
    return 1.0 if n >= 5 else n / 5


def _ck_lint_clean(front: dict, body: str, extra: dict):
    # 边界匹配而非裸子串:concepts/c1 不能被明细里的 concepts/c10 误伤
    return 0.0 if re.search(re.escape(extra["key"]) + r"(?![\w-])", extra["lint_text"]) else 1.0


def _ck_body_solid(front: dict, body: str, extra: dict):
    if "[LLM:" in body:
        return 0.0
    t = body.strip()
    return 1.0 if len(t) >= _BODY_MIN_CHARS else len(t) / _BODY_MIN_CHARS


def _ck_raw_exists(front: dict, body: str, extra: dict):
    rp = str(front.get("raw_path") or "").strip()
    return 1.0 if rp and (extra["vault"] / ".llm-wiki" / rp).exists() else 0.0


def _ck_links_out(front: dict, body: str, extra: dict):
    return 1.0 if "](/" in body else 0.0


def _ck_links_min2(front: dict, body: str, extra: dict):
    n = body.count("](/")
    return 1.0 if n >= 2 else (0.5 if n == 1 else 0.0)


def _ck_summary(front: dict, body: str, extra: dict):
    return 1.0 if str(front.get("summary") or front.get("description") or "").strip() else 0.0


def _ck_definition(front: dict, body: str, extra: dict):
    p = _first_para(body)
    return 1.0 if p and "[LLM:" not in p else 0.0


def _ck_evolution(front: dict, body: str, extra: dict):
    if not extra["updated"]:          # 新建页无演进要求
        return None
    return 1.0 if ("## Evolution" in body or "## 演进" in body) else 0.0


def _ck_aliases(front: dict, body: str, extra: dict):
    if not extra["key"].startswith("entities/"):
        return None                   # Aliases 是 entity 页纪律,其他页 N/A
    if str(front.get("aliases") or "").strip():
        return 1.0
    return 1.0 if any(l.lstrip().startswith("Aliases") for l in body.splitlines()[:6]) else 0.0


_SOURCE_CHECKERS = {
    "provenance_fields": _ck_provenance_fields,
    "key_terms": _ck_key_terms,
    "lint_clean": _ck_lint_clean,
    "body_solid": _ck_body_solid,
    "raw_exists": _ck_raw_exists,
    "links_out": _ck_links_out,
    "summary": _ck_summary,
}
_OTHER_CHECKERS = {
    "lint_clean": _ck_lint_clean,
    "definition": _ck_definition,
    "links_min2": _ck_links_min2,
    "body_solid": _ck_body_solid,
    "summary": _ck_summary,
    "evolution": _ck_evolution,
    "aliases": _ck_aliases,
}


def score_pages(vault: Path, page_keys: list[str], rubric: dict,
                mcp=None, batch_id: str = "", replace_all: bool = False) -> dict:
    """逐页确定性评分(页集无关:评分门传本批页,将来的全量跑传 registry 全部页)。

    rubric = gate.yaml knowledge_score 段:{source_page: {项: 分值}, other_page: {...},
    low_score, min_avg}。结果落 vault/.llm-wiki/meta/knowledge-scores.json(批次 update,
    replace_all=True 时整库覆盖并清掉已删页的旧分)。
    """
    source_items: dict = rubric.get("source_page") or {}
    other_items: dict = rubric.get("other_page") or {}
    low_line = float(rubric.get("low_score", 60))
    updated_keys = rubric.get("_updated_keys") or set()
    per_page, scores, low, missing = {}, [], [], []
    lint_text = ""
    if "lint_clean" in source_items or "lint_clean" in other_items:
        lint_text = mcp.call("wiki_lint", {}, timeout=300.0) if mcp is not None else ""
    for key in page_keys:
        page_file = vault / ".llm-wiki" / "wiki" / f"{key}.md"
        if not page_file.exists():
            missing.append(key)
            continue
        front, body = parse_frontmatter(page_file.read_text(encoding="utf-8"))
        checkers = _SOURCE_CHECKERS if key.startswith("sources/") else _OTHER_CHECKERS
        items_cfg = source_items if key.startswith("sources/") else other_items
        items, got, full = {}, 0, 0
        extra = {"key": key, "updated": key in updated_keys, "vault": vault, "lint_text": lint_text}
        for name, weight in items_cfg.items():
            fn = checkers.get(name)
            if fn is None:
                continue
            w = float(weight)
            ratio = fn(front, body, extra)
            full += w
            if ratio is None:
                items[name] = "n/a"
                got += w
            else:
                earned = round(ratio * w, 1)
                items[name] = f"{earned:g}/{w:g}"
                got += earned
        score = round(100 * got / full) if full > 0 else None
        if score is None:
            continue
        per_page[key] = {"score": score, "items": items, "batch_id": batch_id,
                         "ts": datetime.now().isoformat(timespec="seconds")}
        scores.append(score)
        if score < low_line:
            low.append(f"{key}({score})")
    # 落盘:批次 update(旧批页保留,同名覆盖);replace_all 整库覆盖
    scores_file = vault / ".llm-wiki" / "meta" / "knowledge-scores.json"
    scores_file.parent.mkdir(parents=True, exist_ok=True)
    if replace_all or not scores_file.exists():
        merged = {}
    else:
        try:
            merged = json.loads(scores_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            merged = {}
    merged.update(per_page)
    tmp = scores_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(scores_file)
    return {
        "per_page": per_page, "missing": missing,
        "avg": round(sum(scores) / len(scores), 1) if scores else None,
        "min": min(scores) if scores else None, "low": low,
    }


def _gate_knowledge_score(ctx: GateContext) -> GateResult:
    rubric = ctx.cfg.gate.get("knowledge_score") or {}
    source_items = rubric.get("source_page") or {}
    other_items = rubric.get("other_page") or {}
    if not source_items and not other_items:
        # 占位状态:标准与分值待确认,确认后填 gate.yaml 即启用,不需要改代码
        return GateResult("knowledge-score", "warn", True, {},
                          "评分标准未配置(knowledge_score 两段空),占位跳过")
    unknown = sorted((set(source_items) | set(other_items))
                     - set(_SOURCE_CHECKERS) - set(_OTHER_CHECKERS))
    if unknown:
        return GateResult("knowledge-score", "warn", False, {},
                          f"未知评分项: {', '.join(unknown)}(可用项见 gates.py _SOURCE/_OTHER_CHECKERS)")
    if ("lint_clean" in source_items or "lint_clean" in other_items) and ctx.mcp is None:
        return GateResult("knowledge-score", "warn", False, {}, "启用 lint_clean 但无 MCP 连接,无法跑 wiki_lint")
    pages = sorted(set(ctx.pages_created) | set(ctx.pages_updated))
    if not pages:
        return GateResult("knowledge-score", "warn", True, {}, "本批无新增/变更页,跳过评分")
    rubric = dict(rubric)
    rubric["_updated_keys"] = set(ctx.pages_updated)
    result = score_pages(ctx.paths.vault, pages, rubric, mcp=ctx.mcp, batch_id=ctx.batch_id)
    low, avg = result["low"], result["avg"]
    ok = not low and avg is not None and avg >= float(rubric.get("min_avg", 70))
    detail = f"均分 {avg}·最低 {result['min']}·低分(<{rubric.get('low_score', 60)}): {', '.join(low[:10]) or '无'}"
    if result["missing"]:
        detail += f"·文件缺失: {', '.join(result['missing'][:5])}"
    return GateResult("knowledge-score", "warn", ok,
                      {"scored": len(result["per_page"]), "avg": avg,
                       "min": result["min"], "low": len(low)}, detail)
