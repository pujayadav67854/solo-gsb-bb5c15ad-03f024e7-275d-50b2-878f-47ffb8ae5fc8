"""POST /api/rulesets/{id}/input-footprint 接口测试。"""


def test_footprint_demo_manual_review_includes_upstreams(client, demo_id):
    # manual_review -> vip_discount -> is_vip
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["manual_review"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleset_id"] == demo_id
    assert body["version"] == 1
    assert body["rule_codes"] == ["manual_review"]

    item = body["footprints"][0]
    assert item["rule_code"] == "manual_review"
    paths = {p["path"]: p["used_by"] for p in item["paths"]}
    assert paths == {
        "/amount": ["vip_discount", "manual_review"],
        "/customer/vip": ["is_vip"],
    }
    # 路径按字典序
    assert [p["path"] for p in item["paths"]] == [
        "/amount",
        "/customer/vip",
    ]


def test_footprint_demo_targets_with_shared_path(client, demo_id):
    # free_shipping 自身读 /amount 与 /region，并依赖 large_order（也读 /amount）
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["free_shipping"]},
    )
    assert resp.status_code == 200, resp.text
    paths = {p["path"]: p["used_by"] for p in resp.json()["footprints"][0]["paths"]}
    assert paths == {
        "/amount": ["large_order", "free_shipping"],
        "/region": ["free_shipping"],
    }


def test_footprint_multiple_targets_input_order_and_dedup(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["free_shipping", "free_shipping", "is_vip"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule_codes"] == ["free_shipping", "is_vip"]
    assert [f["rule_code"] for f in body["footprints"]] == [
        "free_shipping",
        "is_vip",
    ]


def test_footprint_integer_index_json_pointer(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "idx",
            "rules": [
                {
                    "code": "r",
                    "condition": 'doc["items"][0]["code"] == "X"',
                    "action": '{"v": doc["n"][3]}',
                }
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r"]},
    ).json()
    paths = {p["path"]: p["used_by"] for p in body["footprints"][0]["paths"]}
    assert paths == {"/items/0/code": ["r"], "/n/3": ["r"]}


def test_footprint_rfc6901_escaping(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "esc",
            "rules": [{"code": "r", "condition": 'doc["a/b"]["x~y"] == 1'}],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r"]},
    ).json()
    assert [p["path"] for p in body["footprints"][0]["paths"]] == [
        "/a~1b/x~0y"
    ]


def test_footprint_rule_without_doc_reads_has_empty_paths(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "nodoc",
            "rules": [
                {"code": "a"},
                {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["b"]},
    ).json()
    assert body["footprints"] == [{"rule_code": "b", "paths": []}]


def test_footprint_dynamic_subscript_returns_422_with_no_partial(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "dyn",
            "rules": [
                {"code": "r", "condition": 'doc["items"][-1] == 1'},
            ],
        },
    ).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert error["details"] == {
        "rule_code": "r",
        "expression": "condition",
        "access": "-1",
    }


def test_footprint_slice_returns_422(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "slc",
            "rules": [
                {"code": "base", "condition": 'doc["items"][0:2] == []'},
                {"code": "top", "condition": "base.hit", "depends_on": ["base"]},
            ],
        },
    ).json()["ruleset_id"]
    # 动态下标出现在上游依赖规则中：整个请求 422，不返回 top 的部分结果
    resp = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["top"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert error["details"]["rule_code"] == "base"
    assert error["details"]["access"] == "0:2"
    assert "footprints" not in resp.json()


def test_footprint_dynamic_subscript_in_action_returns_422(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "dyna",
            "rules": [
                {"code": "r", "condition": "true", "action": '{"v": doc["x"][0-1]}'},
            ],
        },
    ).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert error["details"]["expression"] == "action"


def test_footprint_does_not_write_evaluations(client, demo_id):
    before = client.get(f"/api/rulesets/{demo_id}/evaluations").json()
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 200
    after = client.get(f"/api/rulesets/{demo_id}/evaluations").json()
    assert before == after == []


def test_footprint_uses_specific_version_snapshot(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["z"] >= 1000'},
                {"code": "r3"},
            ],
        },
    )
    v1 = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r1"]},
    ).json()
    assert [p["path"] for p in v1["footprints"][0]["paths"]] == ["/x"]
    v2 = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 2, "rule_codes": ["r1"]},
    ).json()
    assert [p["path"] for p in v2["footprints"][0]["paths"]] == ["/z"]


def test_footprint_ruleset_not_found(client):
    resp = client.post(
        "/api/rulesets/nope/input-footprint",
        json={"version": 1, "rule_codes": ["a"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_footprint_version_not_found(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 99, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_footprint_unknown_rule_code_422(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["is_vip", "nope"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "RULE_NOT_FOUND"
    assert error["details"] == {"rule_codes": ["nope"]}


def test_footprint_validates_request_body(client, demo_id):
    # version 必须 >= 1
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 0, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 422

    # rule_codes 不能为空
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": []},
    )
    assert resp.status_code == 422

    # 缺少必填字段
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 422
