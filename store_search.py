"""DecisionStore mixin: BM25 + multi-signal retrieval, fusion, reflect."""

from __future__ import annotations

import math
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Iterable

try:
    from .store_common import (
        B, BOOST_DIVISOR, DAY, EXPANSION_WEIGHT, FIELD_GEN, FIELD_LEARNED, FIELD_WEIGHT, K1,
        KIND_WEIGHT, MAX_DF_FRACTION, MAX_MULTI_QUERIES, PENDING_WEIGHT, RECENCY_HALF_LIFE_DAYS, RE_CJK_CHAR,
        SOURCE_ARMS, STALE_PENALTY, FB_MAX_EFFECT, open_rate_factor, TRUST_DEFAULT, TRUST_HIDE_BELOW, _clean_scope, _clean_tags, _clip, day_bounds, valid_at,
        _now, _parse_when, _set_sim, hotness, numbers, query_time_window, sim_features, token_weight,
        tokenize, query_token_weights, query_term_factor
    )
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import (
        B, BOOST_DIVISOR, DAY, EXPANSION_WEIGHT, FIELD_GEN, FIELD_LEARNED, FIELD_WEIGHT, K1,
        KIND_WEIGHT, MAX_DF_FRACTION, MAX_MULTI_QUERIES, PENDING_WEIGHT, RECENCY_HALF_LIFE_DAYS, RE_CJK_CHAR,
        SOURCE_ARMS, STALE_PENALTY, FB_MAX_EFFECT, open_rate_factor, TRUST_DEFAULT, TRUST_HIDE_BELOW, _clean_scope, _clean_tags, _clip, day_bounds, valid_at,
        _now, _parse_when, _set_sim, hotness, numbers, query_time_window, sim_features, token_weight,
        tokenize, query_token_weights, query_term_factor
    )


class SearchMixin:
    # -- search -------------------------------------------------------------------------------------------------

    def _query_terms(self, query: str) -> dict[str, tuple[float, str]]:
        """term -> (query weight, concept). The query's own tokens are their own concepts; a synonym-table term found
        in the query forms one concept with its expansions (weighted lower), so a note matching several phrasings
        of the same idea is not rewarded for each of them (max within a concept, sum across concepts)."""
        terms: dict[str, tuple[float, str]] = {}
        for tok, w in query_token_weights(query).items():
            if w > 0 and w > terms.get(tok, (0.0, ""))[0]:
                terms[tok] = (w, tok)
        topical = any(w > 0 for w in query_token_weights(query, fallback=False).values())
        for term, alts in self.synonyms.expand_terms(query):
            factor = query_term_factor(term, query) if topical else 1.0
            if factor <= 0:
                continue
            concept = "~" + term
            for tok in tokenize(term):
                if tok in terms and terms[tok][1] == tok:
                    terms[tok] = (terms[tok][0], concept)
            for alt in alts:
                for tok in tokenize(alt):
                    w = token_weight(tok) * EXPANSION_WEIGHT * factor
                    if w > 0 and tok not in terms:
                        terms[tok] = (w, concept)
        return terms

    def _scopes(self, scope: str | None) -> list[str] | None:
        """Scopes a read sees: the given (or the store's) scope plus 'shared'; None (no filter) for '*'."""
        s = scope if scope else self.scope
        if s == "*":
            return None
        s = _clean_scope(s)
        return [s] if s == "shared" else [s, "shared"]

    def _live_stats(self, now: float) -> tuple[int, float, float]:
        r = self.conn.execute(
            "SELECT COUNT(*), AVG(doc_len_content), AVG(doc_len_keywords) FROM notes "
            "WHERE expires_at IS NULL OR expires_at > ? OR pinned=1", (now,)).fetchone()
        return r[0] or 0, max(r[1] or 1.0, 1.0), max(r[2] or 1.0, 1.0)

    def _bm25_candidates(self, terms: dict[str, float], n_docs: int, avg_c: float, avg_k: float, now: float,
                         limit: int, window: tuple | None, scopes: list | None = None) -> tuple[dict, dict, dict]:
        """SQL-side BM25 over content+keywords: returns (bm25*coverage, keyword-field score, matched weight) for the
        top *limit* notes (active and hidden; hidden ones are folded into their canonical note later)."""
        return self._bm25_multi([terms], n_docs, avg_c, avg_k, now, limit, window, scopes)[0]

    def _bm25_multi(self, term_sets: list[dict], n_docs: int, avg_c: float, avg_k: float, now: float, limit: int,
                    window: tuple | None, scopes: list | None = None, df_cache: dict | None = None,
                    W: dict | None = None) -> list[tuple]:
        """_bm25_candidates for several queries in ONE statement: each distinct term's postings are scanned and
        BM25-scored once, then aggregated per query (concept max, coverage) and the top *limit* kept per query."""
        df_cache = {} if df_cache is None else df_cache
        W = {**self.weights, **(W or {})}
        fw_gen, fw_learned = float(W.get("gen_field", 0.5)), float(W.get("learned_field", 0.3))
        missing = list({t for terms in term_sets for t in terms if t not in df_cache})
        for i in range(0, len(missing), 400):
            chunk = missing[i:i + 400]
            q = ",".join("?" * len(chunk))
            got = dict(self.conn.execute(
                f"SELECT term, COUNT(DISTINCT note_id) FROM postings WHERE term IN ({q}) GROUP BY term", chunk))
            df_cache.update({t: got.get(t, 0) for t in chunk})
        maxidf = math.log(1 + (n_docs + 0.5) / 1.5)
        qrows, totals = [], []
        for qi, terms in enumerate(term_sets):
            concept_max: dict[str, float] = {}
            for t, (qw, concept) in terms.items():
                concept_max[concept] = max(concept_max.get(concept, 0.0), qw * maxidf)
                df = df_cache.get(t)
                # Very common terms (stop-like unigrams, near-universal words) cost the most posting rows and carry
                # almost no BM25 weight: they still count in the coverage denominator but are not scored.
                uni = len(t) == 1 and RE_CJK_CHAR.match(t) is not None
                if df and df > MAX_DF_FRACTION.get(uni, 0.5) * n_docs and n_docs > 200:
                    continue
                if df:
                    qrows.append((t, qw * math.log(1 + (n_docs - df + 0.5) / (df + 0.5)), concept, qi))
            totals.append(sum(concept_max.values()) or 1.0)
        out: list[tuple] = [({}, {}, {}) for _ in term_sets]
        if not qrows:
            return out
        values = ",".join("(?,?,?,?)" for _ in qrows)
        args: list = [x for r in qrows for x in r]
        tvalues = ",".join("(?,?)" for _ in totals)
        targs: list = [x for qi, tot in enumerate(totals) for x in (qi, tot)]
        where = "(n.expires_at IS NULL OR n.expires_at > ? OR n.pinned=1)"
        args_where: list = [now]
        if window:
            where += " AND COALESCE(n.event_at, n.created_at) >= ? AND COALESCE(n.event_at, n.created_at) < ?"
            args_where += [window[0], window[1]]
        if scopes is not None:
            where += f" AND n.scope IN ({','.join('?' * len(scopes))})"
            args_where += scopes
        field_filter = "".join(f" AND p.field != {f}" for f, w in ((FIELD_GEN, fw_gen), (FIELD_LEARNED, fw_learned))
                               if w <= 0)
        sql = f"""
        WITH q(term, w, c, qi) AS (VALUES {values}),
        tq(qi, total) AS (VALUES {tvalues}),
        per AS (
          SELECT p.note_id AS nid, q.qi AS qi, q.c AS c, q.w AS w,
                 SUM(CASE p.field WHEN 0 THEN {FIELD_WEIGHT[0]} WHEN 1 THEN {FIELD_WEIGHT[1]} WHEN 2 THEN {fw_gen}
                     ELSE {fw_learned} END * p.tf * {K1 + 1} /
                     (p.tf + {K1} * CASE p.field WHEN 0 THEN 1 - {B} + {B} * n.doc_len_content * 1.0 / ?
                                    WHEN 1 THEN 1 - {B} + {B} * n.doc_len_keywords * 1.0 / ? ELSE 1 END)) AS s,
                 MAX(p.field = 1) AS kw
          FROM q CROSS JOIN postings p ON p.term = q.term
               CROSS JOIN notes n INDEXED BY notes_bm25 ON n.id = p.note_id
          WHERE {where}{field_filter}
          GROUP BY p.note_id, q.qi, p.term),
        con AS (
          SELECT nid, qi, MAX(w * s) AS cs, MAX(w) AS cw, MAX(CASE kw WHEN 1 THEN w * s ELSE 0 END) AS ck
          FROM per GROUP BY nid, qi, c),
        tot AS (
          SELECT con.nid AS nid, con.qi AS qi, SUM(cs) AS bm, SUM(cw) AS matched, SUM(ck) AS kws,
                 ROW_NUMBER() OVER (PARTITION BY con.qi ORDER BY SUM(cs) * (0.5 + SUM(cw) / tq.total) DESC,
                                                      con.nid DESC) AS r
          FROM con JOIN tq ON tq.qi = con.qi GROUP BY con.nid, con.qi)
        SELECT nid, qi, bm, matched, kws FROM tot WHERE r <= ? ORDER BY qi, r"""
        rows = self.conn.execute(sql, args + targs + [avg_c, avg_k] + args_where + [int(limit)]).fetchall()
        for nid, qi, bmv, mv, kv in rows:
            bm, kws, matched = out[qi]
            bm[nid] = bmv * (0.5 + mv / totals[qi])
            if kv > 0:
                kws[nid] = kv
            matched[nid] = mv / totals[qi]
        return out

    def _graph_arm(self, seeds, q_ents, n_docs, W, idf) -> dict[int, float]:
        """One-hop graph signal: notes sharing the top hits' rarer entities, plus neighbours of the query's entities."""
        hop_w: dict[int, float] = defaultdict(float)
        max_df = max(3, int(0.08 * n_docs))
        for rank, (nid, ents) in enumerate(
                sorted(self.note_entities(seeds).items(), key=lambda x: seeds.index(x[0]))):
            for eid, _, df in ents:
                if 1 < df <= max_df and eid not in q_ents:
                    hop_w[eid] += idf(df) / (1 + rank)
        for eid in list(q_ents)[:6]:
            for r in self.conn.execute(
                    "SELECT CASE WHEN a=? THEN b ELSE a END, weight FROM entity_links WHERE a=? OR b=? "
                    "ORDER BY weight DESC, a, b LIMIT 3", (eid, eid, eid)):
                hop_w[r[0]] += 0.5 * min(1.0, r[1] / 5.0)
        graph_score: dict[int, float] = defaultdict(float)
        if hop_w:
            top = sorted(hop_w.items(), key=lambda x: -x[1])[:12]
            wmap = dict(top)
            fanout = int(W.get("graph_fanout") or 0)
            if fanout:  # cap notes per entity (newest first) so a hub entity cannot flood the arm
                pairs = [(nid, eid) for eid, _ in top for (nid,) in self.conn.execute(
                    "SELECT note_id FROM note_entities WHERE entity_id=? ORDER BY note_id DESC LIMIT ?",
                    (eid, fanout))]
            else:
                q = ",".join("?" * len(top))
                pairs = self.conn.execute(f"SELECT note_id, entity_id FROM note_entities WHERE entity_id IN ({q})",
                                          [e for e, _ in top]).fetchall()
            shared: dict[int, int] = defaultdict(int)
            for nid, eid in pairs:
                if nid not in seeds:
                    shared[nid] += 1
                    if W.get("graph_tanh"):
                        graph_score[nid] = max(graph_score[nid], wmap[eid])
                    else:
                        graph_score[nid] += wmap[eid]
            if W.get("graph_tanh"):  # link_expansion.py: tanh(0.5 * shared entities) + (normalised) link weight
                wmax = max(wmap.values()) or 1.0
                for nid in graph_score:
                    graph_score[nid] = math.tanh(0.5 * shared[nid]) + graph_score[nid] / wmax
        return graph_score

    def _temporal_arm(self, qwin) -> dict[int, float]:
        """Notes dated inside or near the query's time window, closer to its middle scoring higher."""
        temporal: dict[int, float] = {}
        if qwin:
            for r in self.conn.execute(
                    "SELECT id, COALESCE(event_at, created_at) FROM notes WHERE COALESCE(event_at, created_at) "
                    ">= ? AND COALESCE(event_at, created_at) < ? LIMIT 400", (qwin[0] - DAY, qwin[1] + DAY)):
                mid = (qwin[0] + qwin[1]) / 2
                temporal[r[0]] = 1.0 / (1.0 + abs(r[1] - mid) / DAY)
        return temporal

    def _hard_filter(self, cand, info, now, as_end, kind, window, include_low_trust, scopes, want_tags) -> list[int]:
        """Candidates that pass status, validity-date, expiry, kind, window, trust, scope and tag filters."""
        ok = []
        for i in cand:
            r = info.get(i)
            if r is None or r["status"] == "rejected":
                continue
            if as_end is not None and not valid_at(r, as_end):
                continue
            if r["expires_at"] is not None and r["expires_at"] <= now and not r["pinned"]:
                continue
            if kind and r["kind"] != kind:
                continue
            if window:
                t = r["event_at"] or r["created_at"]
                if not (window[0] <= t < window[1]):
                    continue
            if not include_low_trust and r["trust"] < TRUST_HIDE_BELOW:
                continue
            if scopes is not None and r["scope"] not in scopes:
                continue
            if want_tags and not set(want_tags) <= set(r["tags"].split()):
                continue
            ok.append(i)
        return ok

    def _rescore(self, fused, info, recency, now, W):
        """Multiplicative adjustments after fusion: trust, idle decay, pending review, open-rate, stale, recency."""
        # low-trust notes (negative feedback) are demoted multiplicatively; trust 0.5 (default) is neutral
        fused = [(i, v * (min(1.0, 0.6 + 0.8 * info[i]["trust"]))) for i, v in fused]
        fused = [(i, v * self._idle_factor(info[i], now)) for i, v in fused]
        pw = float(W.get("pending", PENDING_WEIGHT))
        if pw != 1.0:   # review queue: LLM-extracted notes nobody confirmed yet rank lower
            fused = [(i, v * (pw if info[i]["review"] == "pending" else 1.0)) for i, v in fused]
        fb_scale = max(0.0, min(1.0, float(W.get("openrate", 1.0))))
        if fb_scale > 0 and any(info[i]["shown_count"] for i, _ in fused):
            base = self.open_rate_base()    # notes people open once shown drift up, ignored ones down (+-10% at most)
            fused = [(i, v * open_rate_factor(info[i]["shown_count"], info[i]["opened_count"], info[i]["fb_at"],
                                              now, base, FB_MAX_EFFECT * fb_scale)) for i, v in fused]
        stale = self.stale_ids(i for i, _ in fused)
        if stale:
            fused = [(i, v * (STALE_PENALTY if i in stale else 1.0)) for i, v in fused]
        # mild multiplicative recency decay (floor 0.85, 120-day half-life) so near-ties go to the newer note
        fused = sorted(((i, v * (0.85 + 0.15 * 0.5 ** (max(0.0, now - recency[i]) / DAY / RECENCY_HALF_LIFE_DAYS)))
                        for i, v in fused), key=lambda x: -x[1])
        return fused, stale

    def search(self, query: str, limit: int = 5, kind: str | None = None, now: float | None = None, *,
               since=None, until=None, include_history: bool = False, weights: dict | None = None,
               explain: bool = False, fusion: str = "rrf", include_low_trust: bool = False,
               scope: str | None = None, tags: str | Iterable[str] = "", track: bool = True,
               as_of=None, _shared: dict | None = None) -> list[dict]:
        """Multi-signal retrieval fused with reciprocal rank fusion.

        Signals: bm25 (content+keywords, with query coverage), keywords (keyword/alias field only), entity (notes
        linked to entities named in the query, incl. aliases), graph (one hop: notes sharing the top hits' rarer
        entities, and neighbours of query entities), temporal (closeness to a date/relative time in the query),
        recency, prior (kind weight x proof_count x pinned), trust (feedback above the default). since/until
        hard-filter on the note's event date (or creation date). Merged/superseded notes are hidden and credit their
        canonical note, unless include_history=True; notes with trust < 0.2 are hidden unless include_low_trust.
        fusion="interleave" returns each candidate arm's #1, #2, ... in turn (used for dedup candidates).
        scope: the store's scope by default (plus 'shared'); "*" = all scopes. tags: notes carrying all of them.
        as_of ('YYYY-MM-DD'): the notes that were valid on that day - valid_from <= day and (valid_to is NULL or
        day < valid_to), valid_from falling back to the creation time - including superseded versions that were
        current then (merged duplicates still fold into their canonical note)."""
        now = _now() if now is None else now
        as_end = day_bounds(as_of)[1] if as_of not in (None, "") else None
        keep_status = ("active", "superseded") if as_end is not None else ("active",)
        W = {**self.weights, **(weights or {})}
        kconst = float(W["k"])
        window = None
        s_since, s_until = _parse_when(since), _parse_when(until)
        if s_since is not None or s_until is not None:
            window = (s_since if s_since is not None else 0.0,
                      (s_until + DAY) if s_until is not None else 1e18)
        terms = (_shared or {}).get("terms", {}).get(query) or self._query_terms(query)
        qwin = query_time_window(query, now)
        with self._lock:
            if _shared is not None:  # search_multi: corpus stats computed once for all its queries
                if "stats" not in _shared:
                    _shared["stats"] = self._live_stats(now)
                n_docs, avg_c, avg_k = _shared["stats"]
            else:
                n_docs, avg_c, avg_k = self._live_stats(now)
            if not n_docs:
                return []
            n_cand = int(W.get("candidates", 200))
            scopes = self._scopes(scope)
            want_tags = _clean_tags(tags).split()
            if _shared is not None and query in _shared.get("bm25", {}):
                bm, kws, matched = (dict(d) for d in _shared["bm25"][query])
            else:
                bm, kws, matched = (self._bm25_multi([terms], n_docs, avg_c, avg_k, now, n_cand, window, scopes,
                                                     W=W)[0] if terms else ({}, {}, {}))
            q_ents = self.detect_entities(query)
            ent_df = {}
            if q_ents:
                q = ",".join("?" * len(q_ents))
                ent_df = dict(self.conn.execute(f"SELECT id, df FROM entities WHERE id IN ({q})", list(q_ents)))
            idf = lambda df: math.log(1 + n_docs / (1 + df))  # noqa: E731
            # entity signal: notes linked to query entities
            ent_score: dict[int, float] = defaultdict(float)
            if q_ents:
                q = ",".join("?" * len(q_ents))
                for nid, eid in self.conn.execute(
                        f"SELECT note_id, entity_id FROM note_entities WHERE entity_id IN ({q})", list(q_ents)):
                    ent_score[nid] += q_ents[eid] * idf(ent_df.get(eid, 0))
            # preliminary fused list -> seeds for the one-hop expansion
            prelim = self._rrf({"bm25": bm, "keywords": kws, "entity": ent_score}, W, kconst, self.boost)
            seeds = [nid for nid, _ in prelim[:5]]
            graph_score = self._graph_arm(seeds, q_ents, n_docs, W, idf)
            # temporal signal: notes dated inside / near the query's time window
            temporal = self._temporal_arm(qwin)
            # restrict non-lexical lists to the strongest few hundred
            ent_score = dict(sorted(ent_score.items(), key=lambda x: (-x[1], -bm.get(x[0], 0.0)))[:n_cand])
            graph_score = dict(sorted(graph_score.items(), key=lambda x: (-x[1], -bm.get(x[0], 0.0)))[:n_cand // 2])
            if not (bm or ent_score or temporal):
                return []
            cand = set(bm) | set(ent_score) | set(graph_score) | set(temporal)
            info = self._fetch(cand, None if _shared is None else _shared.setdefault("rows", {}))
            # fold hidden notes into their canonical note
            fold = float(W.get("fold", 0.9))
            if not include_history:
                hidden = [i for i in cand if info.get(i) is not None and info[i]["status"] not in keep_status]
                canon_ids = {info[i]["canonical_id"] for i in hidden if info[i]["canonical_id"]} - set(info)
                info.update(self._fetch(canon_ids))
                for sig in (bm, kws, ent_score, graph_score, temporal):
                    for i in hidden:
                        if i in sig:
                            v = sig.pop(i) * fold
                            c = info[i]["canonical_id"]
                            if c and c in info and info[c]["status"] == "active" and v > sig.get(c, 0.0):
                                sig[c] = v
                cand = {i for i in set(bm) | set(ent_score) | set(graph_score) | set(temporal)
                        if i in info and info[i]["status"] in keep_status}
            # hard filters
            ok = self._hard_filter(cand, info, now, as_end, kind, window, include_low_trust, scopes, want_tags)
            cand = set(ok)
            # per-arm cap before fusion: one over-expanding arm cannot crowd the others out of the candidate pool
            cap = int(W.get("cap_per_source") or 0)
            if cap > 0:
                bm, kws, ent_score, graph_score, temporal = (
                    dict(sorted(((i, v) for i, v in d.items() if i in cand), key=lambda x: -x[1])[:cap])
                    for d in (bm, kws, ent_score, graph_score, temporal))
                cand = set(bm) | set(ent_score) | set(graph_score) | set(temporal)
        recency = {i: (info[i]["event_at"] or info[i]["created_at"]) for i in cand}
        prior = {i: KIND_WEIGHT.get(info[i]["kind"], 1.0) * (1 + 0.15 * math.log(max(1, info[i]["proof_count"] or 1)))
                 * (1.1 if info[i]["pinned"] else 1.0) * (0.9 if info[i]["candidate"] else 1.0) for i in cand}
        trust = {i: info[i]["trust"] for i in cand if info[i]["trust"] > TRUST_DEFAULT}
        hot = {i: hotness(info[i]["access_count"], (now - info[i]["last_access"]) / DAY)
               for i in cand if info[i]["access_count"]}
        lists = {"bm25": bm, "keywords": kws, "entity": ent_score, "graph": graph_score, "temporal": temporal,
                 "recency": recency, "prior": prior, "trust": trust, "hotness": hot}
        lists = {k: {i: v for i, v in d.items() if i in cand} for k, d in lists.items()}
        if fusion == "interleave":
            fused = self._interleave([lists[a] for a in SOURCE_ARMS])
            return [self._row(info[i]) | {"score": 0.0} for i, _ in fused[:max(1, int(limit))]]
        fused = self._rrf(lists, W, kconst, self.boost)
        fused, stale = self._rescore(fused, info, recency, now, W)
        ordered = self._diversify(fused, info, limit, float(W.get("diversity", 0.6)))
        out = []
        if track:
            self._note_access([nid for nid, _ in ordered[:max(1, int(limit))]], now)
        for nid, score in ordered[:max(1, int(limit))]:
            d = self._row(info[nid])
            d["score"] = round(score, 5)
            if nid in stale:
                d["outdated_by"] = stale[nid]
            if explain:
                d["signals"] = {k: round(v[nid], 4) for k, v in lists.items() if nid in v}
            out.append(d)
        return out

    def search_multi(self, queries: Iterable[str], limit: int = 5, now: float | None = None, *,
                     weights: dict | None = None, explain: bool = False, **filters) -> list[dict]:
        """Search several phrasings of one question (the first is the user's original wording; the others are
        rewrites by the calling agent, e.g. written Chinese / English / answer keywords) and fuse the per-query
        rankings with weighted RRF: the original counts multi_original (1.5), each rewrite 1.0. Results are deduped
        by id; filters (kind, since, until, include_history, scope, tags, ...) apply to every query."""
        qs = list(dict.fromkeys(" ".join(str(q).split()) for q in (queries or ()) if q and str(q).strip()))
        qs = qs[:MAX_MULTI_QUERIES]
        if not qs:
            return []
        now = _now() if now is None else now
        if len(qs) == 1:
            return self.search(qs[0], limit=limit, now=now, weights=weights, explain=explain, **filters)
        W = {**self.weights, **(weights or {})}
        kconst, pool = float(W["multi_k"]), max(int(limit), int(W["multi_pool"]))
        # per-query lists skip the MMR diversity pass (O(n^2)); it runs once on the fused list instead
        sub_w = {**(weights or {}), "diversity": 0}
        # shared work: corpus stats, query terms, document frequencies, note rows, and one BM25 statement whose
        # posting scan serves every query (instead of one scan per query)
        shared: dict = {"terms": {q: self._query_terms(q) for q in qs}, "dfs": {}}
        s_since, s_until = _parse_when(filters.get("since")), _parse_when(filters.get("until"))
        window = None
        if s_since is not None or s_until is not None:
            window = (s_since if s_since is not None else 0.0, (s_until + DAY) if s_until is not None else 1e18)
        with self._lock:
            shared["stats"] = self._live_stats(now)
            if shared["stats"][0]:
                with_terms = [q for q in qs if shared["terms"][q]]
                res = self._bm25_multi([shared["terms"][q] for q in with_terms], *shared["stats"], now,
                                       int(W.get("candidates", 200)), window, self._scopes(filters.get("scope")),
                                       shared["dfs"], W=W)
                shared["bm25"] = dict(zip(with_terms, res))
        fused: dict[int, float] = defaultdict(float)
        rows: dict[int, dict] = {}
        ranks: dict[int, dict] = defaultdict(dict)
        for qi, q in enumerate(qs):
            w = float(W["multi_original"]) if qi == 0 else 1.0
            for rank, h in enumerate(self.search(q, limit=pool, now=now, weights=sub_w, explain=explain,
                                                 track=False, _shared=shared, **filters)):
                fused[h["id"]] += w / (kconst + rank + 1)
                rows.setdefault(h["id"], h)
                ranks[h["id"]][qi] = rank + 1
        ranked = sorted(fused.items(), key=lambda x: (-x[1], min(ranks[x[0]].values())))
        ordered = [i for i, _ in self._diversify(ranked, rows, limit, float(W.get("diversity", 0.6)))]
        ordered = ordered[:max(1, int(limit))]
        self._note_access(ordered, now)
        out = []
        for nid in ordered:
            d = dict(rows[nid])
            d["score"] = round(fused[nid], 5)
            if explain:
                d["query_ranks"] = {qs[qi]: r for qi, r in ranks[nid].items()}
            out.append(d)
        return out

    @staticmethod
    def _rrf(lists: dict[str, dict], W: dict, kconst: float, boost: dict | None = None) -> list[tuple[int, float]]:
        """Weighted RRF; an arm with a boost level is promoted in rank space instead: 1/(k + rank/divisor)."""
        fused: dict[int, float] = defaultdict(float)
        boost = boost or {}
        for name, scores in lists.items():
            w = float(W.get(name, 0.0))
            if not w or not scores:
                continue
            div = BOOST_DIVISOR.get(str(boost.get(name, "none")).lower(), 1.0)
            rank_space = W.get("rank_space")  # every arm in rank space: weight acts as the rank divisor
            for rank, nid in enumerate(sorted(scores, key=lambda i: -scores[i])):
                if rank_space:
                    fused[nid] += 1.0 / (kconst + (rank + 1) / (w * div))
                elif div > 1.0:
                    fused[nid] += 1.0 / (kconst + (rank + 1) / div)
                else:
                    fused[nid] += w / (kconst + rank + 1)
        return sorted(fused.items(), key=lambda x: -x[1])

    @staticmethod
    def _interleave(lists: list[dict]) -> list[tuple[int, float]]:
        """Round-robin over the arms' rankings (#1 of each arm, then #2 of each, ...), without duplicates."""
        ranked = [sorted(d, key=lambda i: -d[i]) for d in lists if d]
        out, seen = [], set()
        for r in range(max((len(x) for x in ranked), default=0)):
            for x in ranked:
                if r < len(x) and x[r] not in seen:
                    seen.add(x[r])
                    out.append((x[r], 0.0))
        return out

    @staticmethod
    def _diversify(fused: list, info: dict, limit: int, threshold: float) -> list:
        """MMR-style: a result nearly identical to one already chosen is pushed behind the other results."""
        if threshold <= 0 or threshold >= 1:
            return fused
        chosen, deferred, feats = [], [], []
        window = fused[:max(limit * 3, 15)]
        for nid, s in window:
            f = sim_features(info[nid]["content"])
            if any(_set_sim(f, g)[0] >= threshold for g in feats):
                deferred.append((nid, s))
                continue
            chosen.append((nid, s))
            feats.append(f)
        return chosen + deferred + fused[len(window):]

    def _fetch(self, ids: Iterable[int], cache: dict | None = None) -> dict[int, sqlite3.Row]:
        ids = list(ids)
        out = {}
        if cache is not None:
            out = {i: cache[i] for i in ids if i in cache}
            ids = [i for i in ids if i not in cache]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.conn.execute(f"SELECT * FROM notes WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                out[r["id"]] = r
        if cache is not None:
            cache.update(out)
        return out

    # -- reflect ------------------------------------------------------------------------------------------------

    def reflect(self, query: str, limit: int = 12, now: float | None = None, **kw) -> dict:
        """Compact evidence pack for the calling agent to synthesise from (no LLM here): the top notes grouped by
        their most query-relevant entity, with dates, kind, proof_count, the superseded/merged history behind each
        note, and possible conflicts (same entity, overlapping topic, different quantities)."""
        hits = self.search(query, limit=limit, now=now, **kw)
        if not hits:
            return {"query": query, "groups": [], "conflicts": [], "count": 0}
        with self._lock:
            q_ents = self.detect_entities(query)
            ents = self.note_entities([h["id"] for h in hits])
        counts = Counter(eid for h in hits for eid, _, _ in ents.get(h["id"], []))
        names = {eid: name for h in hits for eid, name, _ in ents.get(h["id"], [])}
        dfs = {eid: df for h in hits for eid, _, df in ents.get(h["id"], [])}

        def group_key(h):
            mine = [eid for eid, _, _ in ents.get(h["id"], [])]
            if not mine:
                return None
            qm = [e for e in mine if e in q_ents]
            pool = qm or [e for e in mine if counts[e] >= 2] or mine
            return max(pool, key=lambda e: (counts[e], -dfs.get(e, 0)))

        groups: dict = {}
        for rank, h in enumerate(hits):
            g = group_key(h)
            item = {"id": h["id"], "rank": rank + 1, "kind": h["kind"],
                    "date": time.strftime("%Y-%m-%d", time.localtime(h.get("event_at") or h["created_at"])),
                    "proof_count": h.get("proof_count") or 1, "content": _clip(h["content"], 260)}
            hist = self.history(h["id"])
            if hist:
                item["history"] = [{"id": x["id"], "rel": x["rel"], "kind": x["kind"],
                                    "date": time.strftime("%Y-%m-%d", time.localtime(x.get("event_at") or
                                                                                     x["created_at"])),
                                    "content": _clip(x["content"], 160)} for x in hist[:4]]
            groups.setdefault(g, []).append(item)
        out_groups = [{"entity": names.get(g, "(other)") if g else "(other)", "notes": v}
                      for g, v in groups.items()]
        out_groups.sort(key=lambda g: min(n["rank"] for n in g["notes"]))
        conflicts = []
        for i, a in enumerate(hits):
            for b in hits[i + 1:]:
                ea = {e for e, _, _ in ents.get(a["id"], [])}
                eb = {e for e, _, _ in ents.get(b["id"], [])}
                if not (ea & eb):
                    continue
                na, nb = numbers(a["content"]), numbers(b["content"])
                j, _ = _set_sim(sim_features(a["content"]), sim_features(b["content"]))
                if na and nb and na != nb and j >= 0.25:
                    newer, older = (a, b) if (a.get("event_at") or a["created_at"]) >= \
                        (b.get("event_at") or b["created_at"]) else (b, a)
                    conflicts.append({"newer": newer["id"], "older": older["id"],
                                      "about": sorted({names.get(e, "") for e in ea & eb})[:3],
                                      "values": {"newer": sorted(numbers(newer["content"]))[:6],
                                                 "older": sorted(numbers(older["content"]))[:6]}})
        return {"query": query, "count": len(hits), "groups": out_groups, "conflicts": conflicts[:8],
                "hint": "Synthesise from these notes; prefer newer dated notes when values conflict."}
