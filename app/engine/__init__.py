"""规则引擎：安全表达式、编译校验、定点求值与冲突处理。"""

from .ast_eval import (
    CompileError,
    EvaluationError,
    RuleRef,
    safe_eval,
)
from .compiler import (
    CompiledRule,
    CompiledRuleset,
    RulesetCompileError,
    compile_ruleset,
    detect_cycle,
    topo_order,
)
from .evaluator import EvaluationFailed, evaluate
from .footprint import (
    FootprintAnalysisError,
    extract_doc_paths,
    input_footprint,
    to_json_pointer,
)
from .impact import ImpactAnalysisError, impact_analysis, reverse_dependencies

__all__ = [
    "CompileError",
    "EvaluationError",
    "RuleRef",
    "safe_eval",
    "CompiledRule",
    "CompiledRuleset",
    "RulesetCompileError",
    "compile_ruleset",
    "detect_cycle",
    "topo_order",
    "EvaluationFailed",
    "evaluate",
    "ImpactAnalysisError",
    "impact_analysis",
    "reverse_dependencies",
    "FootprintAnalysisError",
    "input_footprint",
    "extract_doc_paths",
    "to_json_pointer",
]
