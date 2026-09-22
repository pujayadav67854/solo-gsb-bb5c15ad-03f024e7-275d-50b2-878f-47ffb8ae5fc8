"""静态输入足迹分析：文档访问链提取、上游闭包、动态下标整体失败。"""

import pytest

from app.engine import compile_ruleset, input_footprint
from app.engine.footprint import (
    FootprintAnalysisError,
    extract_doc_paths,
    to_json_pointer,
)


# ---------- 访问链提取 ----------


def test_extract_string_subscript_chain():
    paths, violations = extract_doc_paths('doc["customer"]["vip"] == true')
    assert paths == {"/customer/vip"}
    assert violations == []


def test_extract_integer_index_chain():
    paths, violations = extract_doc_paths('doc["items"][0] == "x"')
    assert paths == {"/items/0"}
    assert violations == []


def test_extract_dot_attribute_chain():
    # 字典点访问与下标访问等价（求值器运行时同样支持 doc.foo）
    paths, _ = extract_doc_paths("doc.a.b >= 1")
    assert paths == {"/a/b"}


def test_extract_bare_doc_means_whole_document():
    paths, _ = extract_doc_paths("len(doc) > 0")
    assert paths == {""}
    paths, _ = extract_doc_paths("doc")
    assert paths == {""}


def test_extract_multiple_independent_chains():
    paths, _ = extract_doc_paths('doc["x"] >= 1 and doc["y"] <= 9')
    assert paths == {"/x", "/y"}


def test_extract_prefix_also_reported_when_independently_used():
    # doc["x"] 既单独使用又被更长链使用，是两次独立读取
    paths, _ = extract_doc_paths('doc["x"] + doc["x"]["y"]')
    assert paths == {"/x", "/x/y"}


def test_extract_rfc6901_escaping():
    assert to_json_pointer(("a~b", "c/d")) == "/a~0b/c~1d"
    paths, _ = extract_doc_paths('doc["a/b~c"] == 1')
    assert paths == {"/a~1b~0c"}


def test_extract_string_method_does_not_add_method_name_segment():
    paths, violations = extract_doc_paths('doc["name"].lower() == "a"')
    assert paths == {"/name"}
    assert violations == []
    paths, violations = extract_doc_paths('doc["names"][0].startswith("a")')
    assert paths == {"/names/0"}
    assert violations == []


def test_extract_rule_refs_ignored():
    paths, violations = extract_doc_paths('r1.hit and doc["x"] == 1')
    assert paths == {"/x"}
    assert violations == []


@pytest.mark.parametrize(
    "expr",
    [
        'doc["items"][i]',          # 变量下标
        'doc["items"][0:3]',        # 切片
        'doc["x"][-1]',             # 负整数
        'doc["x"][1 + 2]',          # 运算结果
        'doc["x"][True]',           # 布尔
        'doc["x"][1.5]',            # 浮点
        'doc["x"][(0,)]',           # 元组
    ],
)
def test_extract_dynamic_subscript_collects_violation(expr):
    paths, violations = extract_doc_paths(expr)
    assert violations
    assert len(violations) == 1


# ---------- 规则集级足迹 ----------


def _compile():
    return compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            {
                "code": "base",
                "condition": 'doc["customer"]["vip"] == true',
                "action": '{"vip": true}',
            },
            {
                "code": "mid",
                "condition": 'base.hit and doc["amount"] >= 500',
                "action": '{"discount": doc["rates"]["vip"]}',
                "depends_on": ["base"],
            },
            {
                "code": "top",
                "condition": 'mid.hit == false and doc["amount"] >= 3000',
                "action": '{"review": true}',
                "depends_on": ["mid"],
            },
            {
                "code": "side",
                "condition": 'doc["region"] == "north"',
                "action": '{"ship": true}',
            },
        ],
    )


def test_footprint_includes_upstream_closure():
    compiled = _compile()
    result = input_footprint(compiled, ["top"])
    assert result["rule_codes"] == ["top"]
    item = result["footprints"][0]
    assert item["rule_code"] == "top"
    # top -> mid -> base；side 不在闭包内
    paths = {p["path"]: p["used_by"] for p in item["paths"]}
    assert set(paths) == {
        "/customer/vip",
        "/amount",
        "/rates/vip",
    }
    assert paths["/customer/vip"] == ["base"]
    assert paths["/amount"] == ["mid", "top"]
    assert paths["/rates/vip"] == ["mid"]
    # 路径字典序
    assert [p["path"] for p in item["paths"]] == sorted(paths)


def test_footprint_paths_sorted_and_used_by_evaluation_order():
    compiled = _compile()
    result = input_footprint(compiled, ["side"])
    item = result["footprints"][0]
    assert [p["path"] for p in item["paths"]] == ["/region"]
    assert item["paths"][0]["used_by"] == ["side"]


def test_footprint_multiple_targets_preserve_input_order():
    compiled = _compile()
    result = input_footprint(compiled, ["side", "top", "side"])
    assert result["rule_codes"] == ["side", "top"]
    assert [f["rule_code"] for f in result["footprints"]] == ["side", "top"]


def test_footprint_rule_without_doc_reads_has_empty_paths():
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            {"code": "a"},
            {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
        ],
    )
    result = input_footprint(compiled, ["b"])
    item = result["footprints"][0]
    assert item["paths"] == []


def test_footprint_condition_and_action_both_scanned():
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            {
                "code": "r",
                "condition": 'doc["cond"] == 1',
                "action": '{"v": doc["act"]}',
            }
        ],
    )
    result = input_footprint(compiled, ["r"])
    paths = {p["path"] for p in result["footprints"][0]["paths"]}
    assert paths == {"/cond", "/act"}


def test_footprint_dynamic_subscript_fails_whole_request():
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            {"code": "base", "condition": 'doc["items"][0:3] == []'},
            {
                "code": "top",
                "condition": "base.hit",
                "depends_on": ["base"],
            },
        ],
    )
    with pytest.raises(FootprintAnalysisError) as exc:
        input_footprint(compiled, ["top"])
    assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert exc.value.details["rule_code"] == "base"
    assert exc.value.details["expression"] == "condition"
    assert exc.value.details["access"] == "0:3"


def test_footprint_negative_index_fails_whole_request():
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            {"code": "base", "condition": 'doc["items"][-1] == 1'},
            {
                "code": "top",
                "condition": "base.hit",
                "depends_on": ["base"],
            },
        ],
    )
    with pytest.raises(FootprintAnalysisError) as exc:
        input_footprint(compiled, ["top"])
    assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert exc.value.details["access"] == "-1"


def test_footprint_dynamic_subscript_in_action_fails_whole_request():
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            {"code": "r", "condition": "true", "action": '{"v": doc[doc["k"]]}'},
        ],
    )
    with pytest.raises(FootprintAnalysisError) as exc:
        input_footprint(compiled, ["r"])
    assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert exc.value.details["expression"] == "action"


def test_footprint_unknown_rule_raises_rule_not_found():
    compiled = _compile()
    with pytest.raises(FootprintAnalysisError) as exc:
        input_footprint(compiled, ["top", "nope"])
    assert exc.value.code == "RULE_NOT_FOUND"
    assert exc.value.details == {"rule_codes": ["nope"]}


def test_footprint_demo_ruleset_targets():
    from app.seed import DEMO_PAYLOAD

    compiled = compile_ruleset(
        DEMO_PAYLOAD["name"],
        DEMO_PAYLOAD["description"],
        DEMO_PAYLOAD["default_conflict_policy"],
        DEMO_PAYLOAD["rules"],
    )
    result = input_footprint(compiled, ["manual_review"])
    paths = {p["path"]: p["used_by"] for p in result["footprints"][0]["paths"]}
    assert paths == {
        "/amount": ["vip_discount", "manual_review"],
        "/customer/vip": ["is_vip"],
    }
