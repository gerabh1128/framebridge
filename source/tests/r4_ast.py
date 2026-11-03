"""Portable semantic AST fingerprints for the public R4 source guards.

CPython 3.13 omits optional empty fields from ``ast.dump`` by default. This
serializer matches the maintained viewer tests: fields are sorted, optional
None/empty-list fields are omitted, and placeholders in nonempty lists stay.
"""
import ast
import hashlib
import json


def semantic_ast(node):
    if isinstance(node, ast.AST):
        fields = []
        for name in sorted(node._fields):
            value = getattr(node, name, None)
            if value is None or isinstance(value, list) and not value:
                continue
            fields.append([name, semantic_ast(value)])
        return [type(node).__name__, fields]
    if isinstance(node, list):
        return [semantic_ast(value) for value in node]
    if isinstance(node, bytes):
        return ["bytes", node.hex()]
    if isinstance(node, complex):
        return ["complex", repr(node)]
    return node


def semantic_ast_digest(node):
    data = json.dumps(semantic_ast(node), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()
