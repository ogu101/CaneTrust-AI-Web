"""
API-free tests for chain building (docs/DESIGN.md, "Chains"; milestone 4).

    .venv/bin/python test_chains.py

Table-driven: each case is a list of invented documents and what the chains must be. The last
case builds chains from fixtures/gay/expected_map.json's documents and compares them with the
map's own "chains".
"""

import json
from datetime import date
from pathlib import Path

import testkit  # noqa: F401  (blocks real model calls; disables .env)

from chains import ChainDoc, build_chains, from_triaged, load_families

ROOT = Path(__file__).parent
MAP = json.loads((ROOT / "fixtures" / "gay" / "expected_map.json").read_text(encoding="utf-8"))
MARKER = "Zephyrquill"
WHO = f"Ambrose Q. {MARKER}"
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


_order = [0]


def doc(id, doc_type, when, *, principal=WHO, treatment="annotate", refers=(), revokes=(), **kw):
    _order[0] += 1
    return ChainDoc(id=id, doc_type=doc_type, principal=principal,
                    date=date.fromisoformat(when) if when else None, treatment=treatment,
                    order=kw.pop("order", _order[0]),
                    refers_to=tuple({"what": w, "date": d} for w, d in refers),
                    revokes=tuple({"what": w, "date": d} for w, d in revokes), **kw)


def shape(result):
    """Per chain: (family, base, active amendments, superseded ids, revoked ids)."""
    out = []
    for c in result.chains:
        out.append((c.family, c.base, c.amendments,
                    [m.id for m in c.members if m.status == "superseded"],
                    [m.id for m in c.members if m.status == "revoked"]))
    return out


def codes(result, document="any"):
    return sorted(f.code for f in result.flags if document == "any" or f.document == document)


ALL_FLAG_TEXT = []

CASES = [
    dict(
        name="out-of-order dates: order by date, never by file order",
        docs=[doc("a2", "amendment", "2010-03-01"), doc("t", "trust", "2000-01-01"),
              doc("a1", "amendment", "2005-06-01")],
        chains=[("trust", "t", ["a1", "a2"], [], [])], flags=[],
    ),
    dict(
        name="restatement with unsupplied earlier instruments: superseded, not gaps",
        docs=[doc("r", "restated_trust", "2022-11-28",
                  refers=[("revocable trust", "2021-10-05"), ("amendment", "2022-06-09")])],
        chains=[("trust", "r", [], [], [])], flags=[],
        extra=lambda res: (res.chains[0].gaps == []
                           and [s["date"] for s in res.chains[0].superseded_not_provided]
                           == ["2021-10-05", "2022-06-09"]),
    ),
    dict(
        name="two restatements: the latest is the base; everything before it is superseded",
        docs=[doc("t", "trust", "2000-01-01"), doc("r1", "restated_trust", "2010-01-01"),
              doc("a1", "amendment", "2012-01-01"), doc("r2", "restated_trust", "2015-01-01"),
              doc("a2", "amendment", "2016-01-01")],
        chains=[("trust", "r2", ["a2"], ["t", "r1", "a1"], [])], flags=[],
        extra=lambda res: next(m for m in res.chains[0].members if m.id == "r1").reason == "superseded by r2",
    ),
    dict(
        name="a will with a codicil beside a trust chain: two chains, codicil amends the will",
        docs=[doc("r", "restated_trust", "2022-11-28"), doc("acct", "accounting", "2025-06-27", treatment="log_only"),
              doc("w", "will", "2021-10-05"), doc("cod", "codicil", "2022-11-28"),
              doc("notice", "notice_of_administration", None, treatment="facts_only")],
        chains=[("trust", "r", [], [], []), ("will", "w", ["cod"], [], [])], flags=[],
    ),
    dict(
        name="gap after a restatement is reported; a reference before it is superseded",
        docs=[doc("r", "restated_trust", "2015-01-01", refers=[("first amendment", "2012-01-01")]),
              doc("a3", "amendment", "2018-01-01",
                  refers=[("second amendment", "2017-05-05"), ("restated trust", "2015-01-01")])],
        chains=[("trust", "r", ["a3"], [], [])], flags=[("chain_gap", "r")],
        extra=lambda res: ([g["date"] for g in res.chains[0].gaps] == ["2017-05-05"]
                           and [s["date"] for s in res.chains[0].superseded_not_provided] == ["2012-01-01"]),
    ),
    dict(
        name="no restatement: any dated, unprovided amendment reference is a gap",
        docs=[doc("t", "trust", "2000-01-01"),
              doc("a2", "amendment", "2010-01-01", refers=[("First Amendment", "2005-01-01"),
                                                           ("the trust", "2000-01-01")])],
        chains=[("trust", "t", ["a2"], [], [])], flags=[("chain_gap", "t")],
        extra=lambda res: [g["date"] for g in res.chains[0].gaps] == ["2005-01-01"],
    ),
    dict(
        name="a reference whose date matches a provided document is not a gap",
        docs=[doc("t", "trust", "2000-01-01"), doc("a1", "amendment", "2005-01-01"),
              doc("a2", "amendment", "2010-01-01", refers=[("first amendment", "2005-01-01")])],
        chains=[("trust", "t", ["a1", "a2"], [], [])], flags=[],
    ),
    dict(
        name="a reference without a usable date is flagged, never a gap",
        docs=[doc("t", "trust", "2000-01-01"),
              doc("a2", "amendment", "2010-01-01", refers=[("first amendment", None)])],
        chains=[("trust", "t", ["a2"], [], [])], flags=[("reference_undated", None)],
        extra=lambda res: res.chains[0].gaps == [],
    ),
    dict(
        name="same-date amendments keep file order and are flagged",
        docs=[doc("t", "trust", "2000-01-01"), doc("a1", "amendment", "2005-01-01"),
              doc("a2", "amendment", "2005-01-01")],
        chains=[("trust", "t", ["a1", "a2"], [], [])], flags=[("same_date", "a2")],
    ),
    dict(
        name="a missing date is flagged and the document left unchained",
        docs=[doc("t", "trust", "2000-01-01"), doc("a?", "amendment", None)],
        chains=[("trust", "t", [], [], [])], flags=[("date_missing", "a?")],
        extra=lambda res: res.standalone == ["a?"],
    ),
    dict(
        name="no base: the chain is built and flagged",
        docs=[doc("a1", "amendment", "2005-01-01"), doc("a2", "amendment", "2006-01-01")],
        chains=[("trust", None, ["a1", "a2"], [], [])], flags=[("no_base", None)],
    ),
    dict(
        name="a codicil with no will: a will chain with no base",
        docs=[doc("r", "restated_trust", "2022-11-28"), doc("cod", "codicil", "2022-11-28")],
        chains=[("trust", "r", [], [], []), ("will", None, ["cod"], [], [])], flags=[("no_base", None)],
    ),
    dict(
        name="two principals: two chains; case, punctuation, order and Jr. do not split one",
        docs=[doc("t1", "trust", "2000-01-01", principal="Lawrence Gay Jr."),
              doc("a1", "amendment", "2005-01-01", principal="GAY, LAWRENCE"),
              doc("t2", "trust", "2001-01-01", principal="Another Person")],
        chains=[("trust", "t1", ["a1"], [], []), ("trust", "t2", [], [], [])], flags=[],
    ),
    dict(
        name="an unknown principal is chained on its own and flagged",
        docs=[doc("t", "trust", "2000-01-01", principal="unknown")],
        chains=[("trust", "t", [], [], [])], flags=[("principal_unknown", None)],
    ),
    dict(
        name="excluded and non-annotated documents are not chained; other_dispositive stands alone",
        docs=[doc("t", "trust", "2000-01-01"), doc("a1", "amendment", "2005-01-01", excluded=True),
              doc("a2", "amendment", "2006-01-01", treatment="log_only"),
              doc("gift", "other_dispositive", "2007-01-01")],
        chains=[("trust", "t", [], [], [])], flags=[("not_chainable", "gift")],
        extra=lambda res: res.standalone == ["gift"],
    ),
    dict(
        name="a user override 'amends' wins over type and principal",
        docs=[doc("t", "trust", "2000-01-01"),
              doc("x", "trust", "2003-01-01", principal="Someone Else", amends="t"),
              doc("y", "amendment", None, amends="t")],
        chains=[("trust", "t", ["x", "y"], [], [])], flags=[("order_assumed", "y")],
    ),
    dict(
        name="an override to a document that is not chained is flagged and ignored",
        docs=[doc("t", "trust", "2000-01-01"), doc("a", "amendment", "2005-01-01", amends="nope")],
        chains=[("trust", "t", ["a"], [], [])], flags=[("override_target_unchained", "a")],
    ),
    dict(
        name="a document marked superseded on the confirm screen",
        docs=[doc("t", "trust", "2000-01-01"), doc("a1", "amendment", "2005-01-01", superseded=True),
              doc("a2", "amendment", "2006-01-01")],
        chains=[("trust", "t", ["a2"], ["a1"], [])], flags=[],
    ),
    dict(
        name="a revoked amendment stays in the chain as revoked",
        docs=[doc("t", "trust", "2000-01-01"), doc("a1", "amendment", "2005-01-01"),
              doc("a2", "amendment", "2006-01-01", revokes=[("first amendment", "2005-01-01")])],
        chains=[("trust", "t", ["a2"], [], ["a1"])], flags=[],
        extra=lambda res: (res.chains[0].instruments == ["t", "a1", "a2"]
                           and next(m for m in res.chains[0].members if m.id == "a1").reason == "revoked by a2"),
    ),
    dict(
        name="a revoked restatement revives what it superseded, flagged as an assumption",
        docs=[doc("t", "trust", "2000-01-01"), doc("a1", "amendment", "2005-01-01"),
              doc("r", "restated_trust", "2010-01-01"),
              doc("a2", "amendment", "2012-01-01", revokes=[("restatement", "2010-01-01")])],
        chains=[("trust", "t", ["a1", "a2"], [], ["r"])],
        flags=[("revival_assumed", "a1"), ("revival_assumed", "t")],
        extra=lambda res: [m.id for m in res.chains[0].members if m.revival_assumed] == ["t", "a1"],
    ),
    dict(
        name="a revocation that matches nothing is flagged",
        docs=[doc("t", "trust", "2000-01-01"),
              doc("a2", "amendment", "2012-01-01", revokes=[("first amendment", "2004-01-01")]),
              doc("a3", "amendment", "2013-01-01", revokes=[("something", None)])],
        chains=[("trust", "t", ["a2", "a3"], [], [])],
        flags=[("revokes_unmatched", "a2"), ("revokes_unmatched", "a3")],
    ),
    dict(
        name="two originals: the latest is the base, flagged",
        docs=[doc("w1", "will", "2000-01-01"), doc("w2", "will", "2010-01-01"), doc("c", "codicil", "2011-01-01")],
        chains=[("will", "w2", ["c"], ["w1"], [])], flags=[("multiple_bases", "w2")],
    ),
]

for case in CASES:
    result = build_chains(case["docs"])
    got = shape(result)
    check(got == case["chains"], f"{case['name']}: chains {got}")
    got_flags = sorted((f.code, f.document) for f in result.flags)
    check(got_flags == sorted(case["flags"]), f"{case['name']}: flags {got_flags}")
    if "extra" in case:
        check(case["extra"](result), f"{case['name']}: details")
    check([c.id for c in result.chains] == [f"c{i}" for i in range(1, len(result.chains) + 1)],
          f"{case['name']}: chain ids")
    ALL_FLAG_TEXT.extend(f"{f.code} {f.document} {f.detail}" for f in result.flags)
    json.dumps(result.to_dict())

# ---------------------------------------------------------------------------
# Families, conversion, and no document text in flags
# ---------------------------------------------------------------------------
families = load_families()
check(set(families) == {"trust", "will"} and families["will"]["roles"]["codicil"] == "amendment",
      "chain_families.json: trust and will families, codicil amends")
check(all("other_dispositive" not in f["roles"] for f in families.values()), "other_dispositive is never chained")


class Triaged:
    def __init__(self, **kw):
        self.__dict__.update(kw)


t = Triaged(id="d1", doc_type="trust", principal=WHO, date=date(2000, 1, 1), treatment="annotate",
            refers_to=[{"what": "x", "date": None}], revokes=[])
cd = from_triaged(t, order=0, excluded=True)
check(cd.id == "d1" and cd.excluded and cd.refers_to == ({"what": "x", "date": None},), "from_triaged copies fields")
check(len(ALL_FLAG_TEXT) > 10 and not any(MARKER in s for s in ALL_FLAG_TEXT), "flags carry no document text")

# ---------------------------------------------------------------------------
# expected_map.json: its documents reproduce its chains
# ---------------------------------------------------------------------------
map_docs = [ChainDoc(id=d["id"], doc_type=d["type"], principal="Map Principal",
                     date=date.fromisoformat(d["date"]) if d["date"] else None, treatment=d["treatment"],
                     order=i, refers_to=tuple({"what": r["what"], "date": r["date"]} for r in d.get("refers_to", [])),
                     amends=d.get("amends"))
            for i, d in enumerate(MAP["documents"])]
result = build_chains(map_docs)
got = [{"base": c.base, "amendments": c.amendments} for c in result.chains]
want = [{"base": c["base"], "amendments": c["amendments"]} for c in MAP["chains"]]
check(got == want, f"expected_map documents reproduce expected_map chains: {got}")
check(len(result.chains) == MAP["expected_counts"]["chains"], "chain count matches expected_counts")
trust_chain = result.chains[0]
check([s["date"] for s in trust_chain.superseded_not_provided]
      == [x.split()[0] for x in MAP["chains"][0]["superseded_not_provided"]] and trust_chain.gaps == [],
      "the trust chain's earlier trust and amendment are superseded, not gaps")
check(not result.flags and not result.standalone, f"no flags on the map's packet: {result.flags}")

print(f"\nALL {passed} CHECKS PASSED")
