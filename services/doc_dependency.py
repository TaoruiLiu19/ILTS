"""单证依赖链（《多式联运.md》§10.3）。

一期实现范围：
  · 依赖表可配置（`doc_dependencies` 表，由 `services/docdict.seed()` 幂等播种，
    无环校验在 `db.set_doc_dependencies()`）。
  · 未满足上游 → 下游单证标灰并显示「待上游：《MBL》」。
  · 依赖视图：列表 + 简单连线图（`dependency_graph()` 供 UI 画箭头）。

依赖边语义（up_doc_key → down_doc_key）：
  · 两端都在 `DOC_TYPES` 里 = 单证 → 单证（如 MBL → HBL）。
  · 下游是 `VIRTUAL_TARGETS`（LOADING/PICKUP）= 单证 → 动作/节点
    （报关单 → 装船、D/O → 提货）。
  · 上游也可能落到节点：当上游 key 同时是一个 `node_key` 时，
    以「该节点是否完成」判定，而不是找同名单证。
"""

import db
from services import docdict

# 依赖图渲染用：虚拟目标（装船/提货）不在单证字典里，给个展示名与分类
VIRTUAL_META = {
    "LOADING": {"label": "装船", "category": "动作", "virtual": True},
    "PICKUP": {"label": "提货", "category": "动作", "virtual": True},
}

_UNSET = object()


# ── 基础查询 ──

def upstream_keys(doc_key):
    """返回该单证/动作的全部直接上游 key（顺序 = 依赖表 id 顺序）。"""
    if not doc_key:
        return []
    return list(db.upstream_docs(doc_key))


def downstream_keys(doc_key):
    """返回直接下游 key（反向查询，供依赖视图展开）。"""
    if not doc_key:
        return []
    return [r["down_doc_key"] for r in db.list_doc_dependencies()
            if r["up_doc_key"] == doc_key]


def all_upstream_keys(doc_key, _seen=None):
    """传递闭包的全部上游（去重，保持发现顺序）。用于「一键查看依赖图」。"""
    _seen = _seen if _seen is not None else set()
    out = []
    for k in upstream_keys(doc_key):
        if k in _seen:
            continue
        _seen.add(k)
        out.append(k)
        out.extend(all_upstream_keys(k, _seen))
    return out


def dependencies():
    """依赖边列表：[{up_doc_key, down_doc_key, up_name, down_name, down_virtual}]"""
    out = []
    for row in db.list_doc_dependencies():
        up, down = row["up_doc_key"], row["down_doc_key"]
        out.append({
            "up_doc_key": up,
            "down_doc_key": down,
            "up_name": display_name(up),
            "down_name": display_name(down),
            "down_virtual": down in VIRTUAL_META,
            "down_category": category_of(down),
        })
    return out


def display_name(key):
    if not key:
        return ""
    if key in VIRTUAL_META:
        return VIRTUAL_META[key]["label"]
    return docdict.doc_name_of(key)


def category_of(key):
    if not key:
        return ""
    if key in VIRTUAL_META:
        return VIRTUAL_META[key]["category"]
    for k, _name, cat, *_ in docdict.DOC_TYPES:
        if k == key:
            return cat
    return ""


def key_of_doc_name(doc_name):
    """单证名称 → 字典 key（转发 docdict.match_doc_type，便于 UI 单点调用）。"""
    return docdict.match_doc_type(doc_name)


# ── 批次上下文 ──

def build_context(batch_id):
    """收集一次判定所需的批次数据：单证状态表 + 节点完成表。

    返回 {"docs": {doc_key: {submitted, doc_name, file_id, file}}, "nodes": {key: 完成?}}
    """
    docs = {}
    for f in db.get_files_by_batch(batch_id):
        key = key_of_doc_name(f.get("doc_name"))
        if not key:
            continue
        submitted = f.get("status") == "submitted"
        slot = docs.get(key)
        # 同一类型可能有多份（如多张提单）：全部提交才算满足
        if slot is None:
            docs[key] = {"submitted": submitted, "doc_name": f.get("doc_name"),
                         "file_id": f.get("file_id"), "file": f}
        else:
            slot["submitted"] = slot["submitted"] and submitted
            if submitted and not slot.get("file_id"):
                slot["file_id"] = f.get("file_id")
                slot["file"] = f
    nodes = {}
    for n in db.get_nodes_by_batch(batch_id):
        nodes[n["node_key"]] = {"done": n.get("status") == "Done", "node": n}
    return {"docs": docs, "nodes": nodes}


def _satisfied(key, ctx):
    """上游 key 是否已满足（单证已提交 / 节点已完成）。"""
    if key in ctx["nodes"]:
        return ctx["nodes"][key]["done"]
    if key in ctx["docs"]:
        return ctx["docs"][key]["submitted"]
    return False


def unmet_upstream_keys(doc_key, ctx, transitive=False):
    """返回该单证未满足的上游 key 列表。

    transitive=False（默认）：只看直接上游（§10.3 展示口径）。
    transitive=True：包含传递闭包（依赖视图展开用）。
    """
    keys = all_upstream_keys(doc_key) if transitive else upstream_keys(doc_key)
    return [k for k in keys if not _satisfied(k, ctx)]


def waiting_docs(doc_key, ctx):
    """依赖视图/标灰所需：未满足上游的展示名列表。"""
    keys = unmet_upstream_keys(doc_key, ctx)
    return [display_name(k) for k in keys]


def waiting_text(doc_key, ctx, keys=None):
    """§10.3 文案：「待上游：《MBL》」。无未满足上游返回 ''。"""
    keys = unmet_upstream_keys(doc_key, ctx) if keys is None else keys
    if not keys:
        return ""
    return "待上游：" + "".join(f"《{display_name(k)}》" for k in keys)


# ── 行级判定（供 FileRow 使用） ──

def status_for_doc(doc_name, ctx, doc_key=None):
    """单证行依赖态。返回 dict：
        {blocked, text, waiting_keys, waiting_names}
    blocked=True 时 UI 标灰并显示 text。
    """
    key = doc_key or key_of_doc_name(doc_name)
    if not key or not ctx:
        return {"blocked": False, "text": "", "waiting_keys": [], "waiting_names": []}
    keys = unmet_upstream_keys(key, ctx)
    if not keys:
        return {"blocked": False, "text": "", "waiting_keys": [], "waiting_names": []}
    return {"blocked": True, "text": waiting_text(key, ctx, keys),
            "waiting_keys": keys,
            "waiting_names": [display_name(k) for k in keys]}


def apply_context(batch_id, files=None):
    """一次算好整批单证的依赖态：{file_id: {...}} + {doc_key: {...}}。

    files 可传入已取好的单证列表（避免重复查库）。
    返回 (by_file_id, by_doc_key, ctx)
    """
    ctx = build_context(batch_id)
    files = files if files is not None else db.get_files_by_batch(batch_id)
    by_file, by_key = {}, {}
    for f in files:
        key = key_of_doc_name(f.get("doc_name"))
        if not key:
            continue
        st = status_for_doc(f.get("doc_name"), ctx, key)
        # 已提交的单证不再标「待上游」（提交即视为依赖已放行）
        if f.get("status") == "submitted":
            st = {"blocked": False, "text": "", "waiting_keys": [], "waiting_names": []}
        by_key[key] = st
        if f.get("file_id") is not None:
            by_file[f["file_id"]] = st
    return by_file, by_key, ctx


# ── 依赖视图数据 ──

def dependency_view(batch_id=None, doc_keys=None):
    """依赖视图模型（§10.3 列表 + 连线图）。

    返回 {
      "batch_id": ..., "nodes": [...], "edges": [...], "roots": [...],
      "waiting": [...], "tree": [...]
    }
      · nodes：[{key, name, category, virtual, state, submitted, in_batch}]
        state ∈ submitted / waiting_upstream / ready / absent
      · edges：[{up, down}]（仅两端都在 nodes 中）
      · roots：无上游的起点（连线图左列）
      · tree：缩进树（根 → 子），供列表视图
    """
    ctx = build_context(batch_id) if batch_id else {"docs": {}, "nodes": {}}
    edges = [(r["up_doc_key"], r["down_doc_key"]) for r in db.list_doc_dependencies()]

    keep = set(doc_keys) if doc_keys else None
    if keep is not None:
        expanded = set(keep)
        for up, down in edges:
            if down in keep:
                expanded.add(up)
            if up in keep:
                expanded.add(down)
        keep = expanded

    def in_scope(k):
        return keep is None or k in keep

    all_keys = []
    for up, down in edges:
        for k in (up, down):
            if k not in all_keys and in_scope(k):
                all_keys.append(k)

    nodes = []
    for k in all_keys:
        docs = ctx["docs"].get(k)
        nds = ctx["nodes"].get(k)
        submitted = bool(docs and docs["submitted"]) or bool(nds and nds["done"])
        upstreams = [u for u in upstream_keys(k) if in_scope(u)]
        waiting = [u for u in upstreams if not _satisfied(u, ctx)]
        if submitted:
            state = "submitted"
        elif waiting:
            state = "waiting_upstream"
        elif docs or nds:
            state = "ready"
        else:
            state = "absent"
        nodes.append({
            "key": k,
            "name": display_name(k),
            "category": category_of(k),
            "virtual": k in VIRTUAL_META,
            "state": state,
            "submitted": submitted,
            "in_batch": bool(docs or nds),
            "waiting_keys": waiting,
            "waiting_text": waiting_text(k, ctx, waiting),
        })

    node_keys = {n["key"] for n in nodes}
    vis_edges = [{"up": u, "down": d} for u, d in edges
                 if u in node_keys and d in node_keys]
    has_up = {e["up"] for e in vis_edges}
    roots = [n["key"] for n in nodes if n["key"] not in has_up]

    # ── 层级（连线图的列 / 列表的缩进）──
    # 取「从任意根出发的**最长**路径长度」为层级，等价于 DAG 的拓扑层：
    # 由于无环，任何边 up→down 都满足 level(up) < level(down)，
    # 因此下游永远画在全部上游的右侧，被多个上游共享的节点也只画一次。
    parents = {}
    for e in vis_edges:
        parents.setdefault(e["down"], []).append(e["up"])

    level = {}
    visiting = set()

    def _level(k):
        if k in level:
            return level[k]
        if k in visiting:                       # 依赖表已做无环校验，此处仅兜底
            return 0
        visiting.add(k)
        ups = parents.get(k) or []
        lv = 0 if not ups else max(_level(u) for u in ups) + 1
        visiting.discard(k)
        level[k] = lv
        return lv

    for k in node_keys:
        _level(k)

    # 行序：层级 → 根序 → 发现序，保证父节点排在其子节点之前
    order_index = {k: i for i, k in enumerate(all_keys)}
    root_index = {k: i for i, k in enumerate(roots)}
    ordered = sorted(node_keys,
                     key=lambda k: (level.get(k, 0),
                                    min([root_index.get(a, 999)
                                         for a in _ancestors(k, parents)] or [999]),
                                    order_index.get(k, 999)))

    by_key = {n["key"]: n for n in nodes}
    tree = [{**by_key[k], "depth": level.get(k, 0)}
            for k in ordered if k in by_key]

    return {
        "batch_id": batch_id,
        "nodes": nodes,
        "edges": vis_edges,
        "roots": roots,
        "waiting": [n for n in nodes if n["state"] == "waiting_upstream"],
        "tree": tree,
    }


def _ancestors(key, parents, _seen=None):
    """key 的全部祖先（含自身），用于稳定行序。"""
    _seen = _seen if _seen is not None else set()
    if key in _seen:
        return [key]
    _seen.add(key)
    out = [key]
    for p in parents.get(key, []):
        out.extend(_ancestors(p, parents, _seen))
    return out
