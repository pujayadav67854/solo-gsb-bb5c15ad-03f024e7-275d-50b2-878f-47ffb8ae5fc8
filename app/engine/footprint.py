"""规则输入足迹（input footprint）静态分析。

仅做静态分析，**不执行任何文档求值、不写库**：

1. 对目标规则及其 ``depends_on`` 上游依赖闭包中的每条规则，静态扫描其
   ``condition`` 与 ``action`` 表达式中读取的文档访问链；
2. 只接受以 ``doc`` 为根、且每级下标均为**字符串或非负整数常量**的访问链，
   路径按 RFC 6901 转义为 JSON Pointer（``~`` → ``~0``、``/`` → ``~1``）；
   字典点访问 ``doc.foo`` 与 ``doc["foo"]`` 等价，同样视为常量字符串下标；
3. 发现动态下标（变量、运算结果、负整数、切片、布尔/浮点等非常量键）时，
   整体抛出 :class:`FootprintAnalysisError`
   （``UNSUPPORTED_DOCUMENT_ACCESS``），**不返回任何不完整结果**；
4. 每个目标返回去重路径及读取该路径的规则编码 ``used_by``：路径按字典序
   排列，规则编码按该版本的 ``evaluation_order`` 排列。
"""

from __future__ import annotations

import ast

from .ast_eval import parse
from .compiler import CompiledRule, CompiledRuleset


class FootprintAnalysisError(ValueError):
    """输入足迹分析无法完成（规则不存在或存在不支持的文档访问）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _escape_token(token: str) -> str:
    """按 RFC 6901 转义单个引用 token：先 ~ 后 /。"""
    return token.replace("~", "~0").replace("/", "~1")


def to_json_pointer(tokens: tuple[str, ...]) -> str:
    """把访问链各段转为 JSON Pointer；空链（裸 ``doc``）得到空串。"""
    return "".join("/" + _escape_token(token) for token in tokens)


def _constant_key(slice_node: ast.AST) -> str | None:
    """仅接受字符串或非负整数常量下标，返回其 token 文本；其余返回 None。

    布尔是 int 的子类，需显式排除；负整数在 AST 中是 UnaryOp(USub)，
    不会落到这里。
    """
    # 兼容 Python 3.8 的 ast.Index 包装
    if isinstance(slice_node, ast.Index):  # pragma: no cover
        slice_node = slice_node.value
    if isinstance(slice_node, ast.Constant):
        value = slice_node.value
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, int) and value >= 0:
            return str(value)
    return None


def extract_doc_paths(expr: str) -> tuple[set[str], list[tuple[int, int, str]]]:
    """静态提取单个表达式中读取的全部 ``doc`` 访问链。

    只报告**最长链**：``doc["customer"]["vip"]`` 是一条访问链，只产生
    ``/customer/vip``，其中间节点不单独计读；``doc["x"]`` 同时又被
    ``doc["x"]["y"]`` 覆盖时，若 ``doc["x"]`` 在表达式中另有独立使用
    （如直接参与运算），仍会作为独立链出现。裸 ``doc``（如 ``len(doc)``
    或动作整体返回 doc）对应空串，表示读取整个文档。

    返回 ``(json_pointer 集合, 违例列表)``。违例元组为
    ``(lineno, col_offset, 访问片段源码文本)``，按源码位置排序后取首个，
    调用方据此整体失败。
    """
    tree = parse(expr)

    # 字符串方法调用（doc["x"].lower(...)）的属性节点集合：
    # 该 Attribute 是方法名而非文档字段，访问链到此终止。
    method_attrs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method_attrs.add(id(node.func))

    # 节点 id -> 以 doc 为根的常量访问链 token；仅记录仍然“连着”的节点
    chains: dict[int, tuple[str, ...]] = {}
    # 被更长的常量链直接延伸的中间节点（不作为独立读取上报）
    prefix_nodes: set[int] = set()
    violations: list[tuple[int, int, str]] = []

    # ast.walk 父先于子；反向遍历保证处理下标/属性时其值已归类
    for node in reversed(list(ast.walk(tree))):
        if isinstance(node, ast.Name) and node.id == "doc":
            chains[id(node)] = ()
            continue

        if isinstance(node, ast.Attribute):
            tokens = chains.get(id(node.value))
            if tokens is None:
                continue
            if id(node) in method_attrs:
                # doc["x"].lower(...)：lower 是方法名，链终止于此，
                # 接收者 doc["x"] 仍是独立读取
                continue
            # 字典点访问与 ["..."] 等价（求值器同样支持 doc.foo）
            chains[id(node)] = (*tokens, node.attr)
            prefix_nodes.add(id(node.value))
            continue

        if isinstance(node, ast.Subscript):
            tokens = chains.get(id(node.value))
            if tokens is None:
                continue
            if isinstance(node.slice, ast.Slice):
                violations.append(
                    (node.lineno, node.col_offset, ast.unparse(node.slice))
                )
                continue
            key = _constant_key(node.slice)
            if key is None:
                violations.append(
                    (node.lineno, node.col_offset, ast.unparse(node.slice))
                )
                continue
            chains[id(node)] = (*tokens, key)
            prefix_nodes.add(id(node.value))

    paths = {
        to_json_pointer(tokens)
        for node_id, tokens in chains.items()
        if node_id not in prefix_nodes
    }
    violations.sort()
    return paths, violations


def _upstream_closure(compiled: CompiledRuleset, code: str) -> set[str]:
    """目标规则自身加全部（传递）上游依赖；保存期已保证依赖存在且无环。"""
    seen = {code}
    stack = [code]
    while stack:
        current = stack.pop()
        for dep in compiled.by_code[current].depends_on:
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _rule_doc_paths(rule: CompiledRule) -> set[str]:
    """汇总单条规则 condition 与 action 读取的文档路径。

    发现动态下标立即抛出，避免向调用方泄漏任何部分结果。
    """
    paths: set[str] = set()
    for kind, expression, label in (
        ("condition", rule.condition, "条件"),
        ("action", rule.action, "动作"),
    ):
        if not expression:
            continue
        expr_paths, violations = extract_doc_paths(expression)
        if violations:
            _lineno, _col, access = violations[0]
            raise FootprintAnalysisError(
                "UNSUPPORTED_DOCUMENT_ACCESS",
                (
                    f"规则 {rule.code} 的{label}表达式中存在不支持的文档访问："
                    f"下标 {access!r} 不是字符串或非负整数常量"
                ),
                {
                    "rule_code": rule.code,
                    "expression": kind,
                    "access": access,
                },
            )
        paths |= expr_paths
    return paths


def input_footprint(
    compiled: CompiledRuleset, rule_codes: list[str]
) -> dict[str, object]:
    """计算给定规则在该编译版本上的静态输入足迹。

    返回结构：

    - ``rule_codes``：去重保序后的输入规则回显；
    - ``footprints``：与输入同序，每项含 ``rule_code`` 与 ``paths``；
      每个路径带 ``used_by``（目标自身及上游闭包中实际读取该路径的规则，
      按 evaluation_order 排列），路径本身按字典序排列。
    """
    inputs = list(dict.fromkeys(rule_codes))
    missing = [code for code in inputs if code not in compiled.by_code]
    if missing:
        raise FootprintAnalysisError(
            "RULE_NOT_FOUND",
            f"规则编码在该版本中不存在：{missing}",
            {"rule_codes": missing},
        )

    order = compiled.evaluation_order
    closures = {code: _upstream_closure(compiled, code) for code in inputs}

    # 先整体扫描全部相关规则（按 evaluation_order，condition 后 action）：
    # 任一规则存在不支持的访问即整体失败，绝不产出部分结果。
    involved: set[str] = set()
    for members in closures.values():
        involved |= members
    paths_by_rule: dict[str, set[str]] = {}
    for code in order:
        if code in involved:
            paths_by_rule[code] = _rule_doc_paths(compiled.by_code[code])

    footprints: list[dict[str, object]] = []
    for target in inputs:
        members = closures[target]
        readers: dict[str, list[str]] = {}
        for code in order:
            if code not in members:
                continue
            for path in paths_by_rule[code]:
                readers.setdefault(path, []).append(code)
        footprints.append(
            {
                "rule_code": target,
                "paths": [
                    {"path": path, "used_by": readers[path]}
                    for path in sorted(readers)
                ],
            }
        )

    return {"rule_codes": inputs, "footprints": footprints}
