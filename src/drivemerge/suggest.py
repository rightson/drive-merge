"""Folder suggestions for the visual workspace: related folders and duplicate folders.

Both finders are pure reads of the live catalog. They return ready-to-stage
move actions (the HTTP bodies of `POST /v1/drafts/{id}/actions`); nothing here
touches a cloud or a draft.

* related_folders   — folders scattered over different places whose names,
  inner file names and shared (hash-identical) files say they belong together.
* duplicate_folders — folders whose hash-identified content is (almost) the
  same; merging one into the other lets the workspace turn the merge into a
  witnessed deduplication.

Every suggestion carries its `title` and `reason` in English (for the CLI and MCP)
and the same sentences as a `code` plus `params`, which the web page words in its
own language.

Neither finder compares all folder pairs. Candidate pairs come from inverted
indexes (shared name words, shared content groups), so
the cost grows with the amount of real overlap, not with folders squared.
Words that occur in too many folders to tell anything apart are not used to
generate candidates (the limit tightens on dense catalogs so the work stays
bounded), which is the one deliberate approximation. Aggregates are
recomputed from `entries` instead of read from `dir_stats`, which may be empty
or stale.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations

from . import classify, db

# Edge weights of the related-folder score (they sum to 1).
W_NAME, W_CONTENT, W_SHARED = 0.6, 0.25, 0.15
RELATED_THRESHOLD = 0.25      # an edge needs this much to join two folders
MAX_CLUSTER = 8               # folders per suggestion
YEAR_WEIGHT = 0.3             # a 4-digit year is weak evidence next to a real word
CONTENT_TERMS = 12            # top file-name words kept per folder
NAME_DF_CAP = 60              # a name word in more folders than this does not generate candidates
PAIR_BUDGET = 250_000         # candidate pairs the name words may generate; the cap shrinks until it fits
MAX_GROUP_COPIES = 12         # a content group with more copies is boilerplate, not evidence
GROUP_LEVELS = 4              # ancestors of a copy considered when pairing folders by content

# --------------------------------------------------------------------------- tokens

_CJK = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[^\W\d_]+|\d+")

# Words that say nothing about what a folder (or a file in it) is about.
_STOP_WORDS = frozenset("""
new folder untitled document doc file data desktop download misc miscellaneous other etc stuff
temp tmp backup archive old copy photo image picture pic pics video audio my the and of for to in on
img dsc dscn dscf pxl mvimg vid screenshot screen shot scan scanned camera upload
""".split())
# Chinese equivalents; they are removed from a CJK run before it is cut into bigrams.
_STOP_CJK = tuple(sorted({
    "新增資料夾", "新增文件夾", "新建文件夹", "新建文件夾", "未命名資料夾", "未命名文件夹", "未命名",
    "資料夾", "資料夹", "文件夾", "文件夹", "文件", "資料", "资料", "檔案", "档案", "其他", "其它",
    "備份", "备份", "下載", "下载", "桌面", "照片", "相片", "圖片", "图片", "影片", "视频", "音樂", "音乐",
    "截圖", "截图", "雜物", "杂物", "副本", "新增", "新建", "我的",
}, key=lambda s: (-len(s), s)))
_STOP_CJK_SET = frozenset(_STOP_CJK)


def _singular(w: str) -> str:
    """Cheap plural folding so 'papers' meets 'paper' (it only has to be consistent)."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _fold(w: str) -> str:
    """Drop accents so 'résumé' and 'resume' agree."""
    return "".join(c for c in unicodedata.normalize("NFKD", w) if not unicodedata.combining(c))


def _latin_tokens(seg: str, out: dict[str, float], years: bool) -> None:
    for w in _WORD.findall(_CAMEL.sub(" ", seg)):
        if w[0].isdigit():
            if years and len(w) == 4 and w.isascii() and "1900" <= w <= "2099":
                out[w] = YEAR_WEIGHT
            continue
        w = w.lower()
        if not w.isascii():
            w = _fold(w)
        if len(w) < 2 or w in _STOP_WORDS:
            continue
        w = _singular(w)
        if len(w) >= 2 and w not in _STOP_WORDS:
            out[w] = 1.0


def _cjk_tokens(run: str, out: dict[str, float]) -> None:
    if len(run) == 1:
        if run not in _STOP_CJK_SET:
            out[run] = 1.0
        return
    segs = [run]
    for phrase in _STOP_CJK:
        if any(phrase in s for s in segs):
            segs = [piece for s in segs for piece in s.split(phrase)]
    for s in segs:
        for i in range(len(s) - 1):
            out[s[i:i + 2]] = 1.0


def _tokens(text: str, *, years: bool = True) -> dict[str, float]:
    """{token: weight} of a folder or file name, in a deterministic order.

    Lowercase words split on non-alphanumerics, camelCase and letter/digit
    boundaries; numbers are dropped except 4-digit years (low weight); CJK runs
    become overlapping character bigrams, a single character only when the run
    has length 1; generic words are dropped.
    """
    text = unicodedata.normalize("NFC", text)
    out: dict[str, float] = {}
    pos = 0
    for m in _CJK.finditer(text):
        _latin_tokens(text[pos:m.start()], out, years)
        _cjk_tokens(m.group(), out)
        pos = m.end()
    _latin_tokens(text[pos:], out, years)
    return out


def _file_tokens(name: str) -> tuple[str, ...]:
    """Content words of a file name (extension and years left out); hidden files say nothing."""
    if name.startswith("."):
        return ()
    stem, dot, ext = name.rpartition(".")
    if not (dot and stem and 0 < len(ext) <= 5):
        stem = name
    return tuple(_tokens(stem, years=False))


def _name_tokens(name: str) -> dict[str, float]:
    """Folder-name tokens; a name made only of years carries no evidence."""
    toks = _tokens(name)
    return toks if any(w != YEAR_WEIGHT for w in toks.values()) else {}


# --------------------------------------------------------------------------- the directory tree

class _Tree:
    """Live directories with recursive aggregates, built in one pass over `entries`.

    Directory ids are indexes into the parallel lists. Only candidate folders
    (>= min_files files, not inside an atomic unit) keep their shared-content
    sets and file-name word counts; the rest are folded into their parents and
    freed.
    """

    def __init__(self, conn: sqlite3.Connection, *, min_files: int, want_terms: bool):
        self.rnames = {int(r[0]): r[1] for r in conn.execute("SELECT id, name FROM remotes")}
        self.keys: list[tuple[int, str]] = []
        self.parent: list[int] = []
        self.depth: list[int] = []
        self.files: list[int] = []
        self.bytes: list[int] = []
        self.shared_files: list[int] = []      # files whose content has another live copy
        self.sets: list[set[int] | None] = []   # content groups with >= 2 live copies
        self.terms: list[dict[str, int] | None] = []
        self.copies: dict[int, list[int]] = {}  # shared group -> directories holding a copy
        self.group_size: dict[int, int] = {}
        self._load(conn, want_terms)
        self._roll_up_counts()
        self._mark_candidates(conn, min_files)
        self._roll_up_sets()

    def _load(self, conn: sqlite3.Connection, want_terms: bool) -> None:
        # Files parked in quarantine are deleted files: they are neither folders to tidy nor copies.
        live = f"present = 1 AND is_dir = 0 AND {db.outside_quarantine(conn)}"
        # Empty files all share one hash: `__init__.py` in two projects says nothing about the folders.
        shared = {
            r[0] for r in conn.execute(
                f"SELECT content_group FROM entries WHERE {live} AND size > 0 "
                "AND content_group IS NOT NULL GROUP BY content_group HAVING COUNT(*) >= 2"
            )
        }
        index: dict[tuple[int, str], int] = {}
        keys, parent, depth = self.keys, self.parent, self.depth
        files, nbytes, shared_files, sets, terms = self.files, self.bytes, self.shared_files, self.sets, self.terms

        def dir_of(rid: int, path: str) -> int:
            i = index.get((rid, path))
            if i is None:
                up = path.rpartition("/")[0]
                pi = dir_of(rid, up) if up else -1
                i = index[(rid, path)] = len(keys)
                keys.append((rid, path))
                parent.append(pi)
                depth.append(path.count("/"))
                files.append(0)
                nbytes.append(0)
                shared_files.append(0)
                sets.append(None)
                terms.append(None)
            return i

        cache: dict[str, tuple[str, ...]] = {}
        for rid, par, name, size, grp in conn.execute(
            f"SELECT remote_id, parent, name, size, content_group FROM entries WHERE {live} "
            "ORDER BY remote_id, path"
        ):
            if not par:
                continue
            d = index.get((rid, par))
            if d is None:
                d = dir_of(rid, par)
            files[d] += 1
            nbytes[d] += size or 0
            if grp in shared:
                shared_files[d] += 1
                s = sets[d]
                if s is None:
                    s = sets[d] = set()
                s.add(grp)
                self.copies.setdefault(grp, []).append(d)
                self.group_size.setdefault(grp, size or 0)
            if want_terms:
                toks = cache.get(name)
                if toks is None:
                    toks = cache[name] = _file_tokens(name)
                if toks:
                    t = terms[d]
                    if t is None:
                        t = terms[d] = Counter()
                    t.update(toks)

    def _roll_up_counts(self) -> None:
        for i in sorted(range(len(self.keys)), key=self.depth.__getitem__, reverse=True):
            p = self.parent[i]
            if p >= 0:
                self.files[p] += self.files[i]
                self.bytes[p] += self.bytes[i]
                self.shared_files[p] += self.shared_files[i]

    def _mark_candidates(self, conn: sqlite3.Connection, min_files: int) -> None:
        """Candidates: enough files, and not strictly inside an atomic unit (the unit root itself may move)."""
        units = {(u.remote_id, u.path) for u in classify.detect_units(conn, classify.default_rules())}
        n = len(self.keys)
        blocked = [False] * n
        for i in sorted(range(n), key=self.depth.__getitem__):
            p = self.parent[i]
            if p >= 0 and (blocked[p] or self.keys[p] in units):
                blocked[i] = True
        self.is_cand = [self.files[i] >= min_files and not blocked[i] for i in range(n)]
        self.cands = [i for i in range(n) if self.is_cand[i]]
        # Ancestor ids of every directory, nearest first: containment is then a tuple lookup.
        anc: list[tuple[int, ...]] = [()] * n
        for i in sorted(range(n), key=self.depth.__getitem__):
            p = self.parent[i]
            if p >= 0:
                anc[i] = (p, *anc[p])
        self.anc = anc

    def _roll_up_sets(self) -> None:
        sets, terms = self.sets, self.terms
        for i in sorted(range(len(self.keys)), key=self.depth.__getitem__, reverse=True):
            p = self.parent[i]
            si, ti = sets[i], terms[i]
            if p >= 0:
                if si:
                    if sets[p] is None:
                        sets[p] = set(si)
                    else:
                        sets[p] |= si
                if ti:
                    tp = terms[p]
                    if tp is None:
                        terms[p] = dict(ti)
                    else:
                        get = tp.get
                        for t, c in ti.items():
                            tp[t] = get(t, 0) + c
            if not self.is_cand[i]:
                sets[i] = None
                terms[i] = None

    # ------------------------------------------------------------------ helpers

    def elements(self, i: int) -> int:
        """Distinct contents under a folder: each shared group once, every other file on its own.

        A file that is not hash-identified, or whose content has no second copy,
        can never match anything, so it counts as an element of its own; that
        keeps a folder full of unidentified files from looking like a copy.
        """
        s = self.sets[i]
        return (len(s) if s else 0) + self.files[i] - self.shared_files[i]

    def jaccard(self, a: int, b: int) -> tuple[float, int]:
        """(Jaccard of the content sets, size of the intersection)."""
        sa, sb = self.sets[a], self.sets[b]
        if not sa or not sb:
            return 0.0, 0
        inter = len(sa & sb)
        if not inter:
            return 0.0, 0
        return inter / (self.elements(a) + self.elements(b) - inter), inter

    def nested(self, a: int, b: int) -> bool:
        return self.keys[a][0] == self.keys[b][0] and (a in self.anc[b] or b in self.anc[a])

    def folder(self, i: int) -> dict:
        rid, path = self.keys[i]
        return {"remote": self.rnames[rid], "path": path, "files": self.files[i], "bytes": self.bytes[i]}

    def order(self, i: int) -> tuple[str, int]:
        """Deterministic tie-break between folders: path order, then remote."""
        rid, path = self.keys[i]
        return path, rid

    def group_pairs(self, windows: int = GROUP_LEVELS) -> set[int]:
        """Pairs of candidate folders that both hold a copy of some shared content group.

        Folders are the nearest `windows` ancestors of each copy; groups with
        too many copies are skipped. Pair keys are `a * len(keys) + b`, a < b.
        """
        m = len(self.keys)
        out: set[int] = set()
        seen: set[tuple[int, ...]] = set()
        for g in sorted(self.copies):
            dirs = self.copies[g]
            if len(dirs) > MAX_GROUP_COPIES:
                continue
            near: set[int] = set()
            for d in dirs:
                for _ in range(windows):
                    if d < 0:
                        break
                    if self.is_cand[d]:
                        near.add(d)
                    d = self.parent[d]
            if len(near) < 2:
                continue
            key = tuple(sorted(near))
            if key in seen:
                continue
            seen.add(key)
            out.update(a * m + b for a, b in combinations(key, 2))
        return out


# --------------------------------------------------------------------------- clustering

def _components(adj: dict[int, dict[int, float]], nodes: set[int]) -> list[set[int]]:
    """Connected components of the subgraph induced by `nodes`, in a deterministic order."""
    seen: set[int] = set()
    out: list[set[int]] = []
    for start in sorted(nodes):
        if start in seen:
            continue
        comp = {start}
        stack = [start]
        while stack:
            u = stack.pop()
            for v in adj.get(u, ()):
                if v in nodes and v not in comp:
                    comp.add(v)
                    stack.append(v)
        seen |= comp
        out.append(comp)
    return out


def _resolve_nested(tree: _Tree, comp: set[int], adj: dict[int, dict[int, float]], tie: dict) -> set[int]:
    """Drop folders that contain, or sit inside, a better-supported member of the component.

    A folder that merely wraps another (same files, only a generic parent name)
    scores like the folder inside it, so the strongest ties win and, on equal
    strength, the deeper, more specific folder. Ancestors are found by walking
    parent links, so this is linear in the component's size.
    """
    strength = {i: sum(w for n, w in adj[i].items() if n in comp) for i in comp}
    kept: set[int] = set()
    covered: set[int] = set()
    for m in sorted(comp, key=lambda i: (-strength[i], -tree.depth[i], tie[i])):
        if m in covered:
            continue
        p = tree.parent[m]
        while p >= 0 and p not in kept:
            p = tree.parent[p]
        if p >= 0:
            continue
        kept.add(m)
        p = tree.parent[m]
        while p >= 0:
            covered.add(p)
            p = tree.parent[p]
    return kept


def _peel(comp: set[int], adj: dict[int, dict[int, float]], tie: dict) -> list[list[int]]:
    """Cut an oversized component into clusters of at most MAX_CLUSTER folders.

    Edges are taken strongest first; each free pair seeds a cluster that keeps
    adding the free folder most strongly tied to the folders chosen so far (so
    it stays connected) until it is full. Linear in the component's edges.
    """
    ranked = sorted((-s, a, b) for a in comp for b, s in adj[a].items() if a < b and b in comp)
    taken: set[int] = set()
    out: list[list[int]] = []
    for _, a, b in ranked:
        if a in taken or b in taken:
            continue
        chosen = [a, b]
        taken.update(chosen)
        gain: dict[int, float] = defaultdict(float)
        for m in chosen:
            for n, w in adj[m].items():
                if n in comp and n not in taken:
                    gain[n] += w
        while len(chosen) < MAX_CLUSTER and gain:
            n = min(gain, key=lambda x: (-gain[x], tie[x], x))
            del gain[n]
            chosen.append(n)
            taken.add(n)
            for k, w in adj[n].items():
                if k in comp and k not in taken:
                    gain[k] += w
        out.append(sorted(chosen))
    return out


def _clusters(tree: _Tree, edges: dict[tuple[int, int], float], tie) -> list[list[int]]:
    """Clusters of folders that do not contain one another, at most MAX_CLUSTER each.

    Connected components of the edge graph, with nested folders resolved first;
    an oversized component is peeled into several clusters.
    """
    adj: dict[int, dict[int, float]] = defaultdict(dict)
    for (a, b), w in edges.items():
        adj[a][b] = w
        adj[b][a] = w
    ties = {i: tie(i) for i in adj}
    pending = _components(adj, set(adj))
    out: list[list[int]] = []
    while pending:
        comp = pending.pop()
        if len(comp) < 2:
            continue
        kept = _resolve_nested(tree, comp, adj, ties)
        if kept != comp:
            pending.extend(_components(adj, kept))
        elif len(comp) <= MAX_CLUSTER:
            out.append(sorted(comp))
        else:
            out.extend(_peel(comp, adj, ties))
    return out


def _inner(members: list[int], scored: dict) -> list:
    """Values of `scored` (keyed by sorted folder pairs) between the given folders."""
    return [scored[k] for k in combinations(sorted(members), 2) if k in scored]


# --------------------------------------------------------------------------- presentation

def _sid(tree: _Tree, kind: str, members: list[int]) -> str:
    """Stable id: hash of the kind and the sorted `remote:path` locations (remote names, not ids)."""
    locs = sorted(f"{tree.rnames[tree.keys[i][0]]}:{tree.keys[i][1]}" for i in members)
    digest = hashlib.sha256("\0".join([kind, *locs]).encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:12]}"


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _move(f: dict, dst: dict, note: str, new_name: str | None = None) -> dict:
    action = {
        "kind": "move",
        "items": [{"remote": f["remote"], "path": f["path"]}],
        "dst": {"remote": dst["remote"], "path": dst["path"]},
    }
    if new_name:
        action["new_name"] = new_name
    action["note"] = note
    return action


def _involves(tree: _Tree, members: list[int], rid: int | None) -> bool:
    return rid is None or any(tree.keys[m][0] == rid for m in members)


# --------------------------------------------------------------------------- related folders

def _normalise(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(w * w for w in vec.values()))
    return {t: w / norm for t, w in vec.items()} if norm else {}


def _idf(n: int, df: int) -> float:
    return 1.0 + math.log((1 + n) / (1 + df))


def _name_vectors(tree: _Tree) -> dict[int, dict[str, float]]:
    """TF-IDF vectors of folder-name tokens (idf over all candidate folders)."""
    raw = {i: _name_tokens(tree.keys[i][1].rpartition("/")[2]) for i in tree.cands}
    df = Counter(t for toks in raw.values() for t in toks)
    n = len(tree.cands)
    return {i: _normalise({t: toks[t] * _idf(n, df[t]) for t in sorted(toks)})
            for i, toks in raw.items() if toks}


def _content_vectors(tree: _Tree) -> dict[int, dict[str, float]]:
    """TF-IDF vectors of the top file-name words inside each candidate folder."""
    n = len(tree.cands)
    df: Counter = Counter()
    for i in tree.cands:
        t = tree.terms[i]
        if t:
            df.update(t.keys())
    out: dict[int, dict[str, float]] = {}
    for i in tree.cands:
        t = tree.terms[i]
        if not t:
            continue
        weighted = [((1.0 + math.log(c)) * _idf(n, df[w]), w) for w, c in t.items()]
        top = sorted(weighted, key=lambda x: (-x[0], x[1]))[:CONTENT_TERMS]
        out[i] = _normalise({w: s for s, w in sorted(top, key=lambda x: x[1])})
    return out


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    total = 0.0
    get = b.get
    for t, w in a.items():
        v = get(t)
        if v is not None:
            total += w * v
    return total


def _pairs_within_budget(postings: dict[str, list[int]], cap: int, budget: int, m: int) -> set[int]:
    """Pair keys of the folders sharing a word, ignoring words that are too common.

    A word counts only while it occurs in at most `cap` folders; on dense data
    the cap shrinks (deterministically) until the pairs fit the budget, which
    bounds the work however the words are distributed.
    """
    sizes = [len(lst) for lst in postings.values()]
    while cap > 2 and sum(n * (n - 1) // 2 for n in sizes if 2 <= n <= cap) > budget:
        cap = max(2, cap * 3 // 4)
    out: set[int] = set()
    for lst in postings.values():
        if 2 <= len(lst) <= cap:
            out.update(a * m + b for a, b in combinations(lst, 2))
    return out


def _related_pairs(tree: _Tree, nv: dict) -> set[int]:
    """Candidate pairs: folders sharing a name word or a content group.

    File-name vocabulary alone never makes two folders related (see
    _related_edges), so it does not generate candidates.
    """
    postings: dict[str, list[int]] = defaultdict(list)
    for i in sorted(nv):
        for t in nv[i]:
            postings[t].append(i)
    out = _pairs_within_budget(postings, NAME_DF_CAP, PAIR_BUDGET, len(tree.keys))
    out |= tree.group_pairs()
    return out


def _related_edges(tree: _Tree, nv: dict, cv: dict) -> dict[tuple[int, int], float]:
    """Score every candidate pair; keep those at or above the threshold.

    A pair needs evidence beyond a similar vocabulary of file names: a shared
    word in the folder names or at least one shared file. The file-name cosine
    only strengthens such a pair (two unrelated 'Downloads' folders full of
    PDFs sound alike but are not related).
    """
    m = len(tree.keys)
    floor = RELATED_THRESHOLD - 1e-9
    keys, parent, anc, sets = tree.keys, tree.parent, tree.anc, tree.sets
    edges: dict[tuple[int, int], float] = {}
    for key in sorted(_related_pairs(tree, nv)):
        a, b = divmod(key, m)
        if keys[a][0] == keys[b][0] and (parent[a] == parent[b] or a in anc[b] or b in anc[a]):
            continue                      # same parent, or one contains the other
        va, vb = nv.get(a), nv.get(b)
        name = _dot(va, vb) if va and vb else 0.0
        jac = tree.jaccard(a, b)[0] if sets[a] and sets[b] else 0.0
        if name == 0.0 and jac == 0.0:
            continue
        score = W_NAME * name + W_SHARED * jac
        if score + W_CONTENT < floor:
            continue
        va, vb = cv.get(a), cv.get(b)
        if va and vb:
            score += W_CONTENT * _dot(va, vb)
        if score >= floor:
            edges[(a, b)] = score
    return edges


def _shared_terms(members: list[int], vectors: dict, limit: int) -> list[str]:
    weight: Counter = Counter()
    holders: Counter = Counter()
    for i in members:
        for t, w in vectors.get(i, {}).items():
            weight[t] += w
            holders[t] += 1
    return [t for t, _ in sorted(((t, w) for t, w in weight.items() if holders[t] >= 2),
                                 key=lambda x: (-x[1], x[0]))[:limit]]


def _words(terms: list[str]) -> list[str]:
    """Show overlapping CJK bigrams as the word they came from: 研究, 究計, 計畫 -> 研究計畫."""
    chains: list[str] = []
    for t in terms:
        if len(t) == 2 and _CJK.fullmatch(t):
            for i, c in enumerate(chains):
                if c[-1] == t[0] and _CJK.fullmatch(c):
                    chains[i] = c + t[1]
                    break
            else:
                chains.append(t)
        else:
            chains.append(t)
    return chains


def _quote(terms: list[str]) -> str:
    return ", ".join(f'"{t}"' for t in terms)


def _folder_names(folders: list[dict]) -> list[str]:
    """Distinct folder names, in listing order: what a title names (a client shows the first few)."""
    return list(dict.fromkeys(f["path"].rpartition("/")[2] for f in folders))


def _shown(names: list[str]) -> str:
    return ", ".join(names[:3]) + ("…" if len(names) > 3 else "")


def _related_text(p: dict) -> tuple[str, str]:
    """English title and reason of a RELATED suggestion; the page words them from `code` and `params` itself."""
    reasons = []
    if p["name_terms"]:
        reasons.append(f"their folder names share {_quote(p['name_terms'])}")
    if p["content_terms"]:
        reasons.append(f"file names inside often contain {_quote(p['content_terms'])}")
    if p["same"]:
        reasons.append(f"{p['same']} file{' inside has' if p['same'] == 1 else 's inside have'} exactly the same content")
    why = "; ".join(reasons) or "similar names and contents"
    return (f"These {p['count']} folders look related: {_shown(p['names'])}",
            f'{why}. Suggested: tidy them under "{p["anchor"]}".')


def _duplicates_text(p: dict) -> tuple[str, str]:
    """English title and reason of a DUPLICATES suggestion."""
    return (f"These {p['count']} folders hold almost the same content: {_shown(p['names'])}",
            f"Content similarity {p['similarity']:.0%} (only content proven identical by hash counts; "
            f"{p['shared']} shared). Merging the others into \"{p['kept']}\" turns identical files into witnessed "
            f"dedupes and frees about {_human(p['saved_bytes'])}.")


def _related_suggestion(tree: _Tree, members: list[int], score: float, sid: str, nv: dict, cv: dict) -> dict:
    folders = [tree.folder(i) for i in members]
    anchor, rest = folders[0], sorted(folders[1:], key=lambda f: (f["path"], f["remote"]))
    name_terms = _shared_terms(members, nv, 6)
    content_terms = [t for t in _shared_terms(members, cv, 6) if t not in name_terms]
    name_terms, content_terms = _words(name_terms)[:4], _words(content_terms)[:4]
    groups: Counter = Counter()
    for i in members:
        groups.update(tree.sets[i] or ())
    same = sum(1 for c in groups.values() if c >= 2)
    params = {"count": len(folders), "names": _folder_names(folders), "anchor": f"{anchor['remote']}:{anchor['path']}",
              "name_terms": name_terms, "content_terms": content_terms, "same": same}
    title, reason = _related_text(params)
    return {
        "id": sid,
        "kind": "related",
        "score": round(score, 3),
        "title": title,
        "reason": reason,
        # The same sentences as a code and the facts behind them, so that a client can word them in its own language.
        "code": "RELATED",
        "params": params,
        "folders": [anchor, *rest],
        "terms": [*name_terms, *content_terms],
        "actions": [_move(f, anchor, sid) for f in rest],
    }


def related_folders(conn: sqlite3.Connection, *, remote: str | None = None, min_files: int = 3,
                    limit: int = 20) -> list[dict]:
    """Groups of scattered folders that look like they belong together.

    A suggestion bundles up to eight folders (in no ancestor/descendant
    relation) joined by pairwise scores >= RELATED_THRESHOLD:
        0.6 * cosine(folder-name TF-IDF) + 0.25 * cosine(file-name TF-IDF)
        + 0.15 * Jaccard(content groups).
    The folder with the most files is the anchor; every other folder is moved
    into it, keeping its name (a same-named child there makes it a merge).
    `remote` keeps suggestions that involve at least one folder of that remote;
    cross-remote candidates are still found. Suggestions are ranked by
    score * log(total files).
    """
    rid = db.remote_id(conn, remote) if remote is not None else None
    if limit <= 0:
        return []
    tree = _Tree(conn, min_files=max(1, min_files), want_terms=True)
    if len(tree.cands) < 2:
        return []
    nv, cv = _name_vectors(tree), _content_vectors(tree)
    edges = _related_edges(tree, nv, cv)

    def prefer(i: int):
        return (-tree.files[i], *tree.order(i))

    ranked: list[tuple[float, str, float, list[int]]] = []
    for comp in _clusters(tree, edges, prefer):
        members = sorted(comp, key=prefer)       # anchor first: most files, then path order
        inner = _inner(members, edges)
        if not inner or not _involves(tree, members, rid):
            continue
        score = sum(inner) / len(inner)
        total = sum(tree.files[i] for i in members)
        ranked.append((score * math.log(max(total, 2)), _sid(tree, "related", members), score, members))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    return [_related_suggestion(tree, members, score, sid, nv, cv) for _, sid, score, members in ranked[:limit]]


# --------------------------------------------------------------------------- duplicate folders

def _duplicate_pairs(tree: _Tree, threshold: float) -> dict[tuple[int, int], tuple[float, int]]:
    """{(a, b): (jaccard, shared contents)} for folder pairs above the threshold, a < b."""
    m = len(tree.keys)
    memo: dict[int, tuple[float, int] | None] = {}

    def check(a: int, b: int) -> tuple[float, int] | None:
        key = a * m + b
        if key in memo:
            return memo[key]
        res = None
        ea, eb = tree.elements(a), tree.elements(b)
        # Jaccard <= min/max of the sizes, so a lopsided pair cannot reach the threshold.
        if min(ea, eb) >= threshold * max(ea, eb):
            jac, inter = tree.jaccard(a, b)
            if jac >= threshold:
                res = (jac, inter)
        memo[key] = res
        return res

    found: dict[tuple[int, int], tuple[float, int]] = {}
    for key in sorted(tree.group_pairs()):
        a, b = divmod(key, m)
        if tree.nested(a, b):
            continue
        res = check(a, b)
        if res:
            found[(a, b)] = res
    # Climb: a matching pair may have a matching pair of parents above the 4-level window.
    for a, b in sorted(found):
        pa, pb = tree.parent[a], tree.parent[b]
        while pa >= 0 and pb >= 0 and pa != pb and tree.is_cand[pa] and tree.is_cand[pb] \
                and not tree.nested(pa, pb):
            x, y = min(pa, pb), max(pa, pb)
            res = check(x, y)
            if not res:
                break
            found[(x, y)] = res
            pa, pb = tree.parent[pa], tree.parent[pb]
    # Report only the highest level: merging the parents of a pair already merges the pair itself
    # when the two folders have the same name, so such a pair is left to its parents.
    def name(i: int) -> str:
        return unicodedata.normalize("NFC", tree.keys[i][1].rpartition("/")[2]).casefold()

    def covered(a: int, b: int) -> bool:
        pa, pb = tree.parent[a], tree.parent[b]
        return pa >= 0 and pb >= 0 and (min(pa, pb), max(pa, pb)) in found and name(a) == name(b)

    return {pair: v for pair, v in found.items() if not covered(*pair)}


def _duplicate_suggestion(tree: _Tree, members: list[int], score: float, shared: int, saved: int, sid: str) -> dict:
    folders = [tree.folder(i) for i in members]
    kept, rest = folders[0], sorted(folders[1:], key=lambda f: (f["path"], f["remote"]))
    kept_name = kept["path"].rpartition("/")[2]
    dst = {"remote": kept["remote"], "path": kept["path"].rpartition("/")[0]}
    params = {"count": len(folders), "names": _folder_names(folders), "kept": f"{kept['remote']}:{kept['path']}",
              "similarity": round(score, 3), "shared": shared, "saved_bytes": saved}
    title, reason = _duplicates_text(params)
    return {
        "id": sid,
        "kind": "duplicates",
        "score": round(score, 3),
        "title": title,
        "reason": reason,
        "code": "DUPLICATES",
        "params": params,
        "folders": [kept, *rest],
        "terms": list(_name_tokens(kept_name))[:5],
        "actions": [_move(f, dst, sid, kept_name if f["path"].rpartition("/")[2] != kept_name else None)
                    for f in rest],
    }


def duplicate_folders(conn: sqlite3.Connection, *, remote: str | None = None, min_files: int = 3,
                      threshold: float = 0.9, limit: int = 20) -> list[dict]:
    """Folders holding (almost) the same hash-identified content.

    Candidate pairs come from content groups with >= 2 copies (the 4 nearest
    ancestors of every copy), never from comparing all folders. A pair needs
    Jaccard >= threshold over its sets of contents, where a file that is not
    proven identical to another counts as content of its own, and >= min_files
    files on each side. Ancestor/descendant pairs are dropped, and so is a pair
    of same-named folders whose parents also form a pair (merging the parents
    merges them), so a copied tree is reported once at its top. Folders that
    duplicate each other are grouped
    (usually just two); the one with the most files stays, every other folder
    is moved into the parent of the kept one under the kept one's name, which
    the workspace turns into a merge with witnessed deduplication. With equal
    sizes the folder on the first-listed remote is kept. Ranked by the bytes
    the merge makes redundant. `remote` keeps suggestions that involve it.
    """
    rid = db.remote_id(conn, remote) if remote is not None else None
    if limit <= 0:
        return []
    tree = _Tree(conn, min_files=max(1, min_files), want_terms=False)
    pairs = _duplicate_pairs(tree, threshold)
    edges = {k: v[0] for k, v in pairs.items()}

    def keep(i: int):
        return (-tree.files[i], -tree.bytes[i], tree.keys[i][0], tree.keys[i][1])

    ranked: list[tuple[int, float, str, int, list[int]]] = []
    for comp in _clusters(tree, edges, keep):
        members = sorted(comp, key=keep)         # kept folder first
        inner = _inner(members, pairs)
        if not inner or not _involves(tree, members, rid):
            continue
        kept_set = tree.sets[members[0]] or set()
        saved = sum(tree.group_size.get(g, 0) for i in members[1:] for g in kept_set & (tree.sets[i] or set()))
        ranked.append((saved, sum(j for j, _ in inner) / len(inner), _sid(tree, "duplicates", members),
                       max(n for _, n in inner), members))
    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
    return [_duplicate_suggestion(tree, members, score, shared, saved, sid)
            for saved, score, sid, shared, members in ranked[:limit]]


__all__ = ["related_folders", "duplicate_folders"]
