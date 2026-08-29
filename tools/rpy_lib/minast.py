"""A CPython-`ast`-compatible facade over the rast PEG parser.

py2c.py is written against the standard library `ast` module: it calls
`ast.parse`, walks the tree with `ast.walk` / `ast.iter_child_nodes`, tests node
kinds with `isinstance(n, ast.Name)` &c., and reads standard fields (`.id`,
`.func`, `.body`, ...).  minipy has no `ast` module, so to run py2c on minipy we
reconstruct that surface here: rast parses the source into its own Node tree,
and `_conv` rewrites that tree into node objects whose classes and `_fields`
mirror CPython's.  The classes are real (so `isinstance` works by class id), and
`_fields` is stored per-instance because minipy has no class-level attributes.

Design constraints (all verified against the minipy backend):
  * no class-level attributes  -> every __init__ sets self._fields;
  * no setattr()               -> nodes are built by direct attribute writes,
                                   so NodeTransformer write-back is deferred;
  * getattr()/getattr(,default)/hasattr() work -> iter_fields/walk are generic.

This module runs under CPython too, which is how it is differentially tested
against the real `ast` module (see minast_test.py).
"""

# rast is the parser + Node type.  Under CPython these import directly; under
# minipy the same names are provided by the compile-time module linker.
from rast import parse_python, is_node


# ---------------------------------------------------------------------------
# Base class + node classes.  Each __init__ records self._fields (minipy has no
# class attributes) and the location quad, then its own fields.
# ---------------------------------------------------------------------------

class AST:
    def _init_loc(self):
        self.lineno = 0
        self.col_offset = 0
        self.end_lineno = 0
        self.end_col_offset = 0


class Module(AST):
    def __init__(self, body=None, type_ignores=None):
        self._fields = ("body", "type_ignores")
        self._typename = "Module"
        self.body = body
        self.type_ignores = type_ignores if type_ignores is not None else []


class FunctionDef(AST):
    def __init__(self, name=None, args=None, body=None, decorator_list=None, returns=None):
        self._fields = ("name", "args", "body", "decorator_list", "returns",
                        "type_comment", "type_params")
        self._typename = "FunctionDef"
        self._init_loc()
        self.name = name
        self.args = args
        self.body = body
        self.decorator_list = decorator_list
        self.returns = returns
        self.type_comment = None
        self.type_params = []


class ClassDef(AST):
    def __init__(self, name, bases, keywords, body, decorator_list):
        self._fields = ("name", "bases", "keywords", "body", "decorator_list",
                        "type_params")
        self._typename = "ClassDef"
        self._init_loc()
        self.name = name
        self.bases = bases
        self.keywords = keywords
        self.body = body
        self.decorator_list = decorator_list
        self.type_params = []


class Return(AST):
    def __init__(self, value=None):
        self._fields = ("value",)
        self._typename = "Return"
        self._init_loc()
        self.value = value


class Assign(AST):
    def __init__(self, targets=None, value=None, type_comment=None):
        self._fields = ("targets", "value", "type_comment")
        self._typename = "Assign"
        self._init_loc()
        self.targets = targets
        self.value = value
        self.type_comment = type_comment


class AugAssign(AST):
    def __init__(self, target, op, value):
        self._fields = ("target", "op", "value")
        self._typename = "AugAssign"
        self._init_loc()
        self.target = target
        self.op = op
        self.value = value


class AnnAssign(AST):
    def __init__(self, target=None, annotation=None, value=None, simple=None):
        self._fields = ("target", "annotation", "value", "simple")
        self._typename = "AnnAssign"
        self._init_loc()
        self.target = target
        self.annotation = annotation
        self.value = value
        self.simple = simple


class For(AST):
    def __init__(self, target, iter, body, orelse):
        self._fields = ("target", "iter", "body", "orelse", "type_comment")
        self._typename = "For"
        self._init_loc()
        self.target = target
        self.iter = iter
        self.body = body
        self.orelse = orelse
        self.type_comment = None


class While(AST):
    def __init__(self, test, body, orelse):
        self._fields = ("test", "body", "orelse")
        self._typename = "While"
        self._init_loc()
        self.test = test
        self.body = body
        self.orelse = orelse


class If(AST):
    def __init__(self, test, body, orelse):
        self._fields = ("test", "body", "orelse")
        self._typename = "If"
        self._init_loc()
        self.test = test
        self.body = body
        self.orelse = orelse


class Expr(AST):
    def __init__(self, value=None):
        self._fields = ("value",)
        self._typename = "Expr"
        self._init_loc()
        self.value = value


class Pass(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Pass"
        self._init_loc()


class Break(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Break"
        self._init_loc()


class Continue(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Continue"
        self._init_loc()


class Global(AST):
    def __init__(self, names):
        self._fields = ("names",)
        self._typename = "Global"
        self._init_loc()
        self.names = names


class Nonlocal(AST):
    def __init__(self, names):
        self._fields = ("names",)
        self._typename = "Nonlocal"
        self._init_loc()
        self.names = names


class Import(AST):
    def __init__(self, names):
        self._fields = ("names",)
        self._typename = "Import"
        self._init_loc()
        self.names = names


class ImportFrom(AST):
    def __init__(self, module, names, level):
        self._fields = ("module", "names", "level")
        self._typename = "ImportFrom"
        self._init_loc()
        self.module = module
        self.names = names
        self.level = level


class alias(AST):
    def __init__(self, name, asname):
        self._fields = ("name", "asname")
        self._typename = "alias"
        self.name = name
        self.asname = asname


# ---- expressions -----------------------------------------------------------

class BoolOp(AST):
    def __init__(self, op, values):
        self._fields = ("op", "values")
        self._typename = "BoolOp"
        self._init_loc()
        self.op = op
        self.values = values


class BinOp(AST):
    def __init__(self, left, op, right):
        self._fields = ("left", "op", "right")
        self._typename = "BinOp"
        self._init_loc()
        self.left = left
        self.op = op
        self.right = right


class UnaryOp(AST):
    def __init__(self, op, operand):
        self._fields = ("op", "operand")
        self._typename = "UnaryOp"
        self._init_loc()
        self.op = op
        self.operand = operand


class Compare(AST):
    def __init__(self, left, ops, comparators):
        self._fields = ("left", "ops", "comparators")
        self._typename = "Compare"
        self._init_loc()
        self.left = left
        self.ops = ops
        self.comparators = comparators


class Call(AST):
    def __init__(self, func=None, args=None, keywords=None):
        self._fields = ("func", "args", "keywords")
        self._typename = "Call"
        self._init_loc()
        self.func = func
        self.args = args
        self.keywords = keywords


class keyword(AST):
    def __init__(self, arg, value):
        self._fields = ("arg", "value")
        self._typename = "keyword"
        self.arg = arg
        self.value = value


class IfExp(AST):
    def __init__(self, test, body, orelse):
        self._fields = ("test", "body", "orelse")
        self._typename = "IfExp"
        self._init_loc()
        self.test = test
        self.body = body
        self.orelse = orelse


class Constant(AST):
    def __init__(self, value=None, kind=None):
        self._fields = ("value", "kind")
        self._typename = "Constant"
        self._init_loc()
        self.value = value
        self.kind = kind


class Name(AST):
    def __init__(self, id=None, ctx=None):
        self._fields = ("id", "ctx")
        self._typename = "Name"
        self._init_loc()
        self.id = id
        self.ctx = ctx


class Attribute(AST):
    def __init__(self, value=None, attr=None, ctx=None):
        self._fields = ("value", "attr", "ctx")
        self._typename = "Attribute"
        self._init_loc()
        self.value = value
        self.attr = attr
        self.ctx = ctx


class Subscript(AST):
    def __init__(self, value, slice, ctx):
        self._fields = ("value", "slice", "ctx")
        self._typename = "Subscript"
        self._init_loc()
        self.value = value
        self.slice = slice
        self.ctx = ctx


class Starred(AST):
    def __init__(self, value, ctx):
        self._fields = ("value", "ctx")
        self._typename = "Starred"
        self._init_loc()
        self.value = value
        self.ctx = ctx


# --- three nodes the facade owes CPython's `ast`, needed to run compiler.py ---
#
# The rast parser never produces Index or Str: Index is the pre-3.9 subscript
# wrapper and Str the pre-3.8 string constant, both folded into Slice/Constant
# by modern grammars. They exist here because code written against `ast` still
# tests for them on the back-compat path (compiler.py does, twice), and an
# `isinstance` against a name minast does not define resolves to None and
# crashes rather than returning False. Defining them makes those tests answer
# False the way they do under CPython 3.12.
#
# Expression is the `mode="eval"` module root. rast has no eval mode, but the
# node is constructed directly by callers that only want somewhere to hang a
# subtree for fix_missing_locations, which is exactly compiler.py's use.
class Index(AST):
    def __init__(self, value=None):
        self._fields = ("value",)
        self._typename = "Index"
        self._init_loc()
        self.value = value


class Str(AST):
    def __init__(self, s=None):
        self._fields = ("s",)
        self._typename = "Str"
        self._init_loc()
        self.s = s


class Expression(AST):
    def __init__(self, body=None):
        self._fields = ("body",)
        self._typename = "Expression"
        self.body = body


class Slice(AST):
    def __init__(self, lower, upper, step):
        self._fields = ("lower", "upper", "step")
        self._typename = "Slice"
        self._init_loc()
        self.lower = lower
        self.upper = upper
        self.step = step


class List(AST):
    def __init__(self, elts, ctx):
        self._fields = ("elts", "ctx")
        self._typename = "List"
        self._init_loc()
        self.elts = elts
        self.ctx = ctx


class Tuple(AST):
    def __init__(self, elts, ctx):
        self._fields = ("elts", "ctx")
        self._typename = "Tuple"
        self._init_loc()
        self.elts = elts
        self.ctx = ctx


class Set(AST):
    def __init__(self, elts):
        self._fields = ("elts",)
        self._typename = "Set"
        self._init_loc()
        self.elts = elts


class Dict(AST):
    def __init__(self, keys, values):
        self._fields = ("keys", "values")
        self._typename = "Dict"
        self._init_loc()
        self.keys = keys
        self.values = values


class comprehension(AST):
    def __init__(self, target, iter, ifs, is_async):
        self._fields = ("target", "iter", "ifs", "is_async")
        self._typename = "comprehension"
        self.target = target
        self.iter = iter
        self.ifs = ifs
        self.is_async = is_async


class ListComp(AST):
    def __init__(self, elt, generators):
        self._fields = ("elt", "generators")
        self._typename = "ListComp"
        self._init_loc()
        self.elt = elt
        self.generators = generators


class SetComp(AST):
    def __init__(self, elt, generators):
        self._fields = ("elt", "generators")
        self._typename = "SetComp"
        self._init_loc()
        self.elt = elt
        self.generators = generators


class GeneratorExp(AST):
    def __init__(self, elt, generators):
        self._fields = ("elt", "generators")
        self._typename = "GeneratorExp"
        self._init_loc()
        self.elt = elt
        self.generators = generators


class DictComp(AST):
    def __init__(self, key, value, generators):
        self._fields = ("key", "value", "generators")
        self._typename = "DictComp"
        self._init_loc()
        self.key = key
        self.value = value
        self.generators = generators


class arg(AST):
    def __init__(self, arg, annotation):
        self._fields = ("arg", "annotation", "type_comment")
        self._typename = "arg"
        self._init_loc()
        self.arg = arg
        self.annotation = annotation
        self.type_comment = None


class arguments(AST):
    def __init__(self, posonlyargs, args, vararg, kwonlyargs, kw_defaults,
                 kwarg, defaults):
        self._fields = ("posonlyargs", "args", "vararg", "kwonlyargs",
                        "kw_defaults", "kwarg", "defaults")
        self._typename = "arguments"
        self.posonlyargs = posonlyargs
        self.args = args
        self.vararg = vararg
        self.kwonlyargs = kwonlyargs
        self.kw_defaults = kw_defaults
        self.kwarg = kwarg
        self.defaults = defaults


# ---- operator / context singleton classes (no fields; used via isinstance) --

class Load(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Load"

class Store(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Store"

class Del(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Del"

class And(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "And"

class Or(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Or"

class Add(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Add"

class Sub(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Sub"

class Mult(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Mult"

class Div(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Div"

class FloorDiv(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "FloorDiv"

class Mod(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Mod"

class Pow(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Pow"

class LShift(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "LShift"

class RShift(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "RShift"

class BitOr(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "BitOr"

class BitXor(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "BitXor"

class BitAnd(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "BitAnd"

class USub(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "USub"

class UAdd(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "UAdd"

class Invert(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Invert"

class Not(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Not"

class Eq(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Eq"

class NotEq(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "NotEq"

class Lt(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Lt"

class LtE(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "LtE"

class Gt(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Gt"

class GtE(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "GtE"

class In(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "In"

class NotIn(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "NotIn"

class Is(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "Is"

class IsNot(AST):
    def __init__(self):
        self._fields = ()
        self._typename = "IsNot"


# ---------------------------------------------------------------------------
# Generic tree helpers (mirror ast.*), built on getattr + per-instance _fields.
# ---------------------------------------------------------------------------

def iter_fields(node):
    out = []
    for name in node._fields:
        if hasattr(node, name):
            out.append((name, getattr(node, name)))
    return out


def iter_child_nodes(node):
    out = []
    for pair in iter_fields(node):
        val = pair[1]
        if isinstance(val, AST):
            out.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, AST):
                    out.append(item)
    return out


def walk(node):
    # BFS, matching CPython's ast.walk (deque + popleft): a node then its
    # children in source order. A list + read index avoids the O(n^2) of
    # pop(0); the earlier todo.pop() was a LIFO stack, which reversed sibling
    # order and made field/discovery passes diverge from CPython.
    todo = [node]
    i = 0
    while i < len(todo):
        cur = todo[i]
        i = i + 1
        for k in iter_child_nodes(cur):
            todo.append(k)
    return todo


def copy_location(new_node, old_node):
    new_node.lineno = getattr(old_node, "lineno", 0)
    new_node.col_offset = getattr(old_node, "col_offset", 0)
    new_node.end_lineno = getattr(old_node, "end_lineno", 0)
    new_node.end_col_offset = getattr(old_node, "end_col_offset", 0)
    return new_node


def fix_missing_locations(node):
    todo = [node]
    while todo:
        cur = todo.pop()
        if not hasattr(cur, "lineno"):
            cur.lineno = 0
            cur.col_offset = 0
            cur.end_lineno = 0
            cur.end_col_offset = 0
        for k in iter_child_nodes(cur):
            todo.append(k)
    return node


# ---------------------------------------------------------------------------
# NodeVisitor / NodeTransformer.  Dispatch is by the node's _typename (minipy
# has no type(x).__name__), looked up with getattr(self, "visit_"+name) -- which
# returns a callable bound method on both minipy backends.  NodeTransformer's
# generic_visit rewrites in place using setattr for scalar fields and a rebuilt
# list for sequence fields (both minipy-supported).
# ---------------------------------------------------------------------------

class NodeVisitor:
    def visit(self, node):
        m = getattr(self, "visit_" + node._typename, None)
        if m is None:
            return self.generic_visit(node)
        return m(node)

    def generic_visit(self, node):
        for name in node._fields:
            if not hasattr(node, name):
                continue
            value = getattr(node, name)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, AST):
                        self.visit(item)
            elif isinstance(value, AST):
                self.visit(value)
        return None


class NodeTransformer(NodeVisitor):
    def generic_visit(self, node):
        for name in node._fields:
            if not hasattr(node, name):
                continue
            old = getattr(node, name)
            if isinstance(old, list):
                new = []
                for item in old:
                    if isinstance(item, AST):
                        r = self.visit(item)
                        if r is None:
                            continue
                        if isinstance(r, list):
                            for x in r:
                                new.append(x)
                        else:
                            new.append(r)
                    else:
                        new.append(item)
                setattr(node, name, new)
            elif isinstance(old, AST):
                r = self.visit(old)
                setattr(node, name, r)
        return node


# ---------------------------------------------------------------------------
# Operator lookups: rast stores operators as leaf strings; CPython wants a
# singleton instance per operator on the node's .op / .ops.
# ---------------------------------------------------------------------------

_CMP_OPS = ("<", ">", "<=", ">=", "==", "!=", "<>", "in", "not in", "is",
            "is not")


def _binop_cls(op):
    if op == "+": return Add()
    if op == "-": return Sub()
    if op == "*": return Mult()
    if op == "/": return Div()
    if op == "//": return FloorDiv()
    if op == "%": return Mod()
    if op == "**": return Pow()
    if op == "|": return BitOr()
    if op == "^": return BitXor()
    if op == "&": return BitAnd()
    if op == "<<": return LShift()
    if op == ">>": return RShift()
    raise ValueError("unknown binop: " + op)


def _cmpop_cls(op):
    if op == "<": return Lt()
    if op == ">": return Gt()
    if op == "<=": return LtE()
    if op == ">=": return GtE()
    if op == "==": return Eq()
    if op == "!=": return NotEq()
    if op == "<>": return NotEq()
    if op == "in": return In()
    if op == "not in": return NotIn()
    if op == "is": return Is()
    if op == "is not": return IsNot()
    raise ValueError("unknown cmpop: " + op)


def _augop_cls(op):
    # op is like "+=" ; drop trailing "=" and reuse _binop_cls
    return _binop_cls(op[:-1])


def _is(node, nm):
    return is_node(node) and node.name == nm


def _leaf(node):
    # a rast NAME/NUMBER/STRING wraps a single leaf child; an empty string
    # literal ("") yields a childless STRING, so fall back to "".
    if node.children:
        return node.children[0]
    return ""


# ---------------------------------------------------------------------------
# Expression conversion.
# ---------------------------------------------------------------------------

def _conv(node):
    nm = node.name
    if nm == "NAME":
        v = _leaf(node)
        if v == "None":
            return Constant(None)
        if v == "True":
            return Constant(True)
        if v == "False":
            return Constant(False)
        return Name(v, Load())
    if nm == "NUMBER":
        return Constant(_leaf(node))
    if nm == "STRING":
        return Constant(_leaf(node))
    if nm == "__getattr__":
        return Attribute(_conv(node.children[0]), _leaf(node.children[1]),
                         Load())
    if nm == "__call__":
        return _conv_call(node)
    if nm == "__binary__":
        return _conv_binary(node)
    if nm == "power":                          # a ** b  (right child may nest / be unary)
        return BinOp(_conv(node.children[0]), Pow(), _conv(node.children[2]))
    if nm == "__getitem__":
        return _conv_subscript(node)
    if nm == "or_test":
        return BoolOp(Or(), _conv_list(node.children))
    if nm == "and_test":
        return BoolOp(And(), _conv_list(node.children))
    if nm == "not_test":
        return UnaryOp(Not(), _conv(node.children[0]))
    if nm == "factor":
        return UnaryOp(_unaryop_cls(node.children[0]),
                       _conv(node.children[1]))
    if nm == "test":
        return _conv_ternary(node)
    if nm == "listmaker":
        return _conv_listmaker(node)
    if nm == "tuple":
        return Tuple(_conv_list(node.children), Load())
    if nm == "exprlist":
        return Tuple(_conv_list(node.children), Load())
    if nm == "testlist":
        return Tuple(_conv_list(node.children), Load())
    if nm == "STRINGS":
        s = ""
        for c in node.children:
            s = s + _leaf(c)
        return Constant(s)
    if nm == "generator":
        inner = node.children[0]
        if _is(inner, "testlist_comp"):
            return _conv(inner)
        return GeneratorExp(_conv(inner), _conv_comp_generators(node))
    if nm == "no_param":
        return Tuple([], Load())
    if nm == "testlist_comp":
        if _has(node, "list_for"):
            return GeneratorExp(_conv(node.children[0]),
                                _conv_comp_generators(node))
        return Tuple(_conv_list(node.children), Load())
    if nm == "listcomp_arg":
        has_for = False
        for c in node.children:
            if _is(c, "list_for"):
                has_for = True
        if has_for:
            return GeneratorExp(_conv(node.children[0]),
                                _conv_comp_generators(node))
        return _conv(node.children[0])
    if nm == "dictmaker":
        return _conv_dictmaker(node)
    if nm == "setmaker":
        return _conv_setmaker(node)
    if nm == "lambdef":
        raise ValueError("lambda not yet converted")
    raise ValueError("cannot convert expr node: " + nm)


def _unaryop_cls(op):
    if op == "-": return USub()
    if op == "+": return UAdd()
    if op == "~": return Invert()
    raise ValueError("unknown unaryop: " + op)


def _conv_list(children):
    out = []
    for c in children:
        if is_node(c):
            out.append(_conv(c))
    return out


def _fill_arglist(arglist, args, kwargs):
    for a in arglist.children:
        if _is(a, "keyword_arg"):
            kwargs.append(keyword(_leaf(a.children[0]), _conv(a.children[1])))
        elif _is(a, "star_arg"):
            args.append(Starred(_conv(a.children[0]), Load()))
        elif _is(a, "remaining_args"):
            args.append(Starred(_conv(a.children[0]), Load()))
        elif _is(a, "kwargs"):
            kwargs.append(keyword(None, _conv(a.children[0])))
        elif is_node(a):
            args.append(_conv(a))


def _conv_call(node):
    func = _conv(node.children[0])
    args = []
    kwargs = []
    if len(node.children) > 1:
        _fill_arglist(node.children[1], args, kwargs)
    return Call(func, args, kwargs)


def _conv_binary(node):
    op = node.children[0]
    if _in_tuple(op, _CMP_OPS):
        return _conv_compare(node)
    return BinOp(_conv(node.children[1]), _binop_cls(op),
                 _conv(node.children[2]))


def _in_tuple(x, tup):
    for t in tup:
        if t == x:
            return True
    return False


def _rev(xs):
    out = []
    i = len(xs) - 1
    while i >= 0:
        out.append(xs[i])
        i = i - 1
    return out


def _conv_compare(node):
    # flatten the left-nested chain a<b<=c (nested __binary__) into one Compare
    ops = []
    comps = []
    cur = node
    base = None
    while True:
        op = cur.children[0]
        ops.append(op)
        comps.append(cur.children[2])
        left = cur.children[1]
        if _is(left, "__binary__") and _in_tuple(left.children[0], _CMP_OPS):
            cur = left
        else:
            base = left
            break
    ops = _rev(ops)
    comps = _rev(comps)
    op_objs = []
    for o in ops:
        op_objs.append(_cmpop_cls(o))
    comp_objs = []
    for c in comps:
        comp_objs.append(_conv(c))
    return Compare(_conv(base), op_objs, comp_objs)


def _conv_subscript(node):
    val = _conv(node.children[0])
    subs = []
    i = 1
    while i < len(node.children):
        c = node.children[i]
        if is_node(c) and c.name == "subscript":
            inner = c.children[0]
            if _is(inner, "slice"):
                subs.append(_conv_slice(inner))
            else:
                subs.append(_conv(inner))
        i = i + 1
    if len(subs) == 1:
        return Subscript(val, subs[0], Load())
    return Subscript(val, Tuple(subs, Load()), Load())


def _conv_slice(node):
    lower = None
    upper = None
    step = None
    for part in node.children:
        if _is(part, "start") and part.children:
            lower = _conv(part.children[0])
        elif _is(part, "stop") and part.children:
            upper = _conv(part.children[0])
        elif _is(part, "step") and part.children:
            step = _conv(part.children[0])
    return Slice(lower, upper, step)


def _conv_ternary(node):
    # test := or_test "if" or_test "else" test  ->  body if test else orelse
    body = _conv(node.children[0])
    test = _conv(node.children[1])
    orelse = _conv(node.children[2])
    return IfExp(test, body, orelse)


def _has(node, nm):
    for c in node.children:
        if is_node(c) and c.name == nm:
            return True
    return False


def _conv_listmaker(node):
    if len(node.children) == 1 and _is(node.children[0], "listcomp"):
        lc = node.children[0]
        return ListComp(_conv(lc.children[0]), _conv_comp_generators(lc))
    if _has(node, "list_for"):
        return ListComp(_conv(node.children[0]), _conv_comp_generators(node))
    return List(_conv_list(node.children), Load())


def _conv_setmaker(node):
    kids = []
    for c in node.children:
        if is_node(c):
            kids.append(c)
    if len(kids) == 1 and _is(kids[0], "listcomp"):
        lc = kids[0]
        return SetComp(_conv(lc.children[0]), _conv_comp_generators(lc))
    if _has(node, "list_for"):
        return SetComp(_conv(kids[0]), _conv_comp_generators(node))
    out = []
    for c in kids:
        out.append(_conv(c))
    return Set(out)


def _conv_dictmaker(node):
    if len(node.children) == 1 and _is(node.children[0], "dictcomp"):
        dc = node.children[0]
        return DictComp(_conv(dc.children[0]), _conv(dc.children[1]),
                        _conv_comp_generators(dc))
    if _has(node, "list_for"):
        return DictComp(_conv(node.children[0]), _conv(node.children[1]),
                        _conv_comp_generators(node))
    keys = []
    vals = []
    for pair in node.children:
        keys.append(_conv(pair.children[0]))
        vals.append(_conv(pair.children[1]))
    return Dict(keys, vals)


def _conv_comp_generators(comp):
    # comp children: elt, then a run of list_for / list_if (dictcomp: key,val,...)
    gens = []
    cur = None
    for c in comp.children:
        if _is(c, "list_for"):
            target = _store(_conv(c.children[0]))
            itr = _conv(c.children[1])
            cur = comprehension(target, itr, [], 0)
            gens.append(cur)
        elif _is(c, "list_if"):
            cur.ifs.append(_conv(c.children[0]))
    return gens


# ---------------------------------------------------------------------------
# Statement conversion.
# ---------------------------------------------------------------------------

def _store(t):
    if isinstance(t, Name) or isinstance(t, Attribute) \
            or isinstance(t, Subscript) or isinstance(t, Starred):
        t.ctx = Store()
    elif isinstance(t, Tuple) or isinstance(t, List):
        t.ctx = Store()
        for e in t.elts:
            _store(e)
    return t


def _delctx(t):
    if isinstance(t, Name) or isinstance(t, Attribute) \
            or isinstance(t, Subscript):
        t.ctx = Del()
    elif isinstance(t, Tuple) or isinstance(t, List):
        for e in t.elts:
            _delctx(e)
    return t


def _conv_arguments(pnode):
    posonly = []
    args = []
    defaults = []
    kwonlyargs = []
    kw_defaults = []
    vararg = None
    kwarg = None
    seen_star = False
    if pnode is not None:
        for p in pnode.children:
            if p.name == "NAME":
                if seen_star:                       # keyword-only, no default
                    kwonlyargs.append(arg(_leaf(p), None))
                    kw_defaults.append(None)
                else:
                    args.append(arg(_leaf(p), None))
            elif p.name == "fpdef_opt":
                name = _leaf(p.children[0])
                ann = None
                dflt = None
                j = 1
                while j < len(p.children):
                    extra = p.children[j]
                    if extra.name == "annotation":
                        ann = _conv(extra.children[0])
                    else:
                        dflt = _conv(extra)
                    j = j + 1
                if seen_star:                       # keyword-only (dflt may be None)
                    kwonlyargs.append(arg(name, ann))
                    kw_defaults.append(dflt)
                else:
                    args.append(arg(name, ann))
                    if dflt is not None:
                        defaults.append(dflt)
            elif p.name == "remaining_args":
                if len(p.children) > 0:             # *args (named)
                    vararg = arg(_leaf(p.children[0]), None)
                seen_star = True                    # subsequent params are keyword-only
            elif p.name == "kwargs":
                kwarg = arg(_leaf(p.children[0]), None)
    return arguments(posonly, args, vararg, kwonlyargs, kw_defaults, kwarg, defaults)


def _skip_noise(nm):
    return nm == "EMPTY_LINE" or nm == "comment"


def _append_stmt(node, out):
    """Convert one grammar statement node and append it to `out`.

    `simple_stmt` is the grammar's wrapper for a single source *line* of small
    statements -- `a = 1; b = 2` parses to one simple_stmt with two children --
    so one node can produce several body entries. Every place that builds a
    statement list has to expand it; converting it directly fell through
    _conv_stmt's final `Expr(_conv(node))` and raised "cannot convert expr node:
    simple_stmt". rast.py uses this form 12 times and compiler.py 84 times, so
    minast could not convert either of them and self-hosting stopped here.
    """
    if node.name == "simple_stmt":
        for c in node.children:
            if is_node(c) and not _skip_noise(c.name):
                _append_stmt(c, out)
        return
    out.append(_conv_stmt(node))


def _suite(children, start):
    out = []
    i = start
    while i < len(children):
        c = children[i]
        if is_node(c):
            if c.name == "suite":
                for sc in c.children:
                    if is_node(sc) and not _skip_noise(sc.name):
                        _append_stmt(sc, out)
            elif c.name != "elseblock" and not _skip_noise(c.name):
                _append_stmt(c, out)
        i = i + 1
    return out


def _find_elseblock(children):
    for c in children:
        if is_node(c) and c.name == "elseblock":
            return _suite(c.children, 0)
    return []


def _dotted(node):
    # dotted_name -> "a.b.c" ; a bare NAME -> "a"
    if node.name == "NAME":
        return _leaf(node)
    parts = []
    for c in node.children:
        parts.append(_leaf(c))
    return ".".join(parts)


def _conv_stmt(node):
    nm = node.name
    if nm == "single_if":
        return If(_conv(node.children[0]), _suite(node.children, 1), [])
    if nm == "regular_assign":
        n = len(node.children)
        value = _conv(node.children[n - 1])
        targets = []
        i = 0
        while i < n - 1:
            targets.append(_store(_conv(node.children[i])))
            i = i + 1
        return Assign(targets, value)
    if nm == "aug_assign":
        target = _store(_conv(node.children[0]))
        op = _augop_cls(node.children[1].children[0])
        return AugAssign(target, op, _conv(node.children[2]))
    if nm == "ann_assign":
        target = _store(_conv(node.children[0]))
        annotation = _conv(node.children[1])
        value = None
        if len(node.children) > 2 and node.children[2].children:
            value = _conv(node.children[2].children[0])
        return AnnAssign(target, annotation, value, 1)
    if nm == "return_stmt":
        value = None
        if node.children:
            value = _conv(node.children[0])
        return Return(value)
    if nm == "if_stmt":
        return _conv_if(node, 0)
    if nm == "for_stmt":
        target = _store(_conv(node.children[0]))
        itr = _conv(node.children[1])
        body = _suite(node.children, 2)
        orelse = _find_elseblock(node.children)
        return For(target, itr, body, orelse)
    if nm == "while_stmt":
        test = _conv(node.children[0])
        body = _suite(node.children, 1)
        orelse = _find_elseblock(node.children)
        return While(test, body, orelse)
    if nm == "funcdef":
        name = _leaf(node.children[0])
        params = _conv_arguments(node.children[1])
        returns = None
        rnode = node.children[2]
        if rnode.children:
            returns = _conv(rnode.children[0])
        body = _suite(node.children, 3)
        return FunctionDef(name, params, body, [], returns)
    if nm == "classdef":
        name = _leaf(node.children[0])
        bases = []
        pnode = node.children[1]
        if pnode.children:
            first = pnode.children[0]
            if _is(first, "testlist"):
                for b in first.children:
                    if is_node(b):
                        bases.append(_conv(b))
            else:
                for b in pnode.children:
                    if is_node(b):
                        bases.append(_conv(b))
        body = _suite(node.children, 2)
        return ClassDef(name, bases, [], body, [])
    if nm == "import_names":
        names = []
        for c in node.children:
            if not is_node(c):
                continue
            if c.name == "dotted_as_name":
                names.append(alias(_dotted(c.children[0]),
                                   _leaf(c.children[1])))
            else:
                names.append(alias(_dotted(c), None))
        return Import(names)
    if nm == "import_from":
        module = _dotted(node.children[0])
        names = []
        asnames = node.children[1]
        for c in asnames.children:
            if not is_node(c):
                continue
            if c.name == "import_as_name":
                names.append(alias(_leaf(c.children[0]), _leaf(c.children[1])))
            elif c.name == "NAME":
                names.append(alias(_leaf(c), None))
        return ImportFrom(module, names, 0)
    if nm == "pass_stmt":
        return Pass()
    if nm == "break_stmt":
        return Break()
    if nm == "continue_stmt":
        return Continue()
    if nm == "global_stmt":
        names = []
        for c in node.children:
            if is_node(c) and c.name == "NAME":
                names.append(_leaf(c))
        return Global(names)
    if nm == "nonlocal_stmt":
        names = []
        for c in node.children:
            if is_node(c) and c.name == "NAME":
                names.append(_leaf(c))
        return Nonlocal(names)
    if nm == "raise_stmt":
        exc = None
        cause = None
        if node.children:
            exc = _conv(node.children[0])
        if len(node.children) > 1:
            cause = _conv(node.children[1])
        return _raise(exc, cause)
    if nm == "assert_stmt":
        test = _conv(node.children[0])
        msg = None
        i = 1
        while i < len(node.children):
            if is_node(node.children[i]):
                msg = _conv(node.children[i])
            i = i + 1
        return _assert(test, msg)
    if nm == "del_stmt":
        targets = []
        el = node.children[0]
        for c in el.children:
            if is_node(c):
                targets.append(_delctx(_conv(c)))
        return _delete(targets)
    if nm == "decorated":
        return _conv_decorated(node)
    if nm == "try_stmt":
        return _conv_try(node)
    if nm == "with_stmt":
        return _conv_with(node)
    # fall through: a bare expression used as a statement
    return Expr(_conv(node))


def _conv_if(node, i):
    clauses = node.children
    si = clauses[i]
    body = _suite(si.children, 1)
    orelse = []
    if i + 1 < len(clauses):
        nxt = clauses[i + 1]
        if _is(nxt.children[0], "gen_true"):
            orelse = _suite(nxt.children, 1)
        else:
            orelse = [_conv_if(node, i + 1)]
    return If(_conv(si.children[0]), body, orelse)


# Raise/Assert/Delete kept as thin builders (classes defined lazily below to
# keep the common node classes grouped above).
def _raise(exc, cause):
    return Raise(exc, cause)


def _assert(test, msg):
    return Assert(test, msg)


def _delete(targets):
    return Delete(targets)


class Raise(AST):
    def __init__(self, exc, cause):
        self._fields = ("exc", "cause")
        self._typename = "Raise"
        self._init_loc()
        self.exc = exc
        self.cause = cause


class Assert(AST):
    def __init__(self, test, msg):
        self._fields = ("test", "msg")
        self._typename = "Assert"
        self._init_loc()
        self.test = test
        self.msg = msg


class Delete(AST):
    def __init__(self, targets):
        self._fields = ("targets",)
        self._typename = "Delete"
        self._init_loc()
        self.targets = targets


class Try(AST):
    def __init__(self, body, handlers, orelse, finalbody):
        self._fields = ("body", "handlers", "orelse", "finalbody")
        self._typename = "Try"
        self._init_loc()
        self.body = body
        self.handlers = handlers
        self.orelse = orelse
        self.finalbody = finalbody


class ExceptHandler(AST):
    # Parameter names deliberately match CPython's _fields, even where that
    # shadows a builtin inside the constructor (`type`, `iter`, `slice`, `arg`).
    # Callers construct these by keyword -- compiler.py does
    # `ast.ExceptHandler(type=..., name=..., body=...)` -- so a renamed
    # parameter is a TypeError at the call site even though the attribute it
    # sets was already correct.
    def __init__(self, type, name, body):
        self._fields = ("type", "name", "body")
        self._typename = "ExceptHandler"
        self._init_loc()
        self.type = type
        self.name = name
        self.body = body


class With(AST):
    def __init__(self, items, body):
        self._fields = ("items", "body", "type_comment")
        self._typename = "With"
        self._init_loc()
        self.items = items
        self.body = body
        self.type_comment = None


class withitem(AST):
    def __init__(self, context_expr, optional_vars):
        self._fields = ("context_expr", "optional_vars")
        self._typename = "withitem"
        self.context_expr = context_expr
        self.optional_vars = optional_vars


# ---------------------------------------------------------------------------
# Node types the rast grammar does not (yet) produce, but that py2c references
# in isinstance() checks to detect / reject / special-case unsupported syntax
# (lambdas, generators, f-strings, async).  They must exist as classes so those
# checks resolve to False instead of raising AttributeError; the converter never
# instantiates them.
# ---------------------------------------------------------------------------
class Lambda(AST):
    def __init__(self, args, body):
        self._fields = ("args", "body")
        self._typename = "Lambda"
        self._init_loc()
        self.args = args
        self.body = body


class Yield(AST):
    def __init__(self, value):
        self._fields = ("value",)
        self._typename = "Yield"
        self._init_loc()
        self.value = value


class YieldFrom(AST):
    def __init__(self, value):
        self._fields = ("value",)
        self._typename = "YieldFrom"
        self._init_loc()
        self.value = value


class JoinedStr(AST):
    def __init__(self, values):
        self._fields = ("values",)
        self._typename = "JoinedStr"
        self._init_loc()
        self.values = values


class FormattedValue(AST):
    def __init__(self, value, conversion, format_spec):
        self._fields = ("value", "conversion", "format_spec")
        self._typename = "FormattedValue"
        self._init_loc()
        self.value = value
        self.conversion = conversion
        self.format_spec = format_spec


class AsyncFunctionDef(AST):
    def __init__(self, name, args, body, decorator_list, returns):
        self._fields = ("name", "args", "body", "decorator_list", "returns",
                        "type_comment", "type_params")
        self._typename = "AsyncFunctionDef"
        self._init_loc()
        self.name = name
        self.args = args
        self.body = body
        self.decorator_list = decorator_list
        self.returns = returns
        self.type_comment = None
        self.type_params = []


class AsyncFor(AST):
    def __init__(self, target, iter, body, orelse):
        self._fields = ("target", "iter", "body", "orelse", "type_comment")
        self._typename = "AsyncFor"
        self._init_loc()
        self.target = target
        self.iter = iter
        self.body = body
        self.orelse = orelse
        self.type_comment = None


# ---------------------------------------------------------------------------
# try / with conversion.  Both rast forms are leaf-laden: try_stmt delimits its
# sections with an except_clauses node plus bare 'else'/'finally' leaves, and
# with_stmt lists its items before a bare ':' leaf.  A small state machine walks
# the children and routes statements into the right bucket.
# ---------------------------------------------------------------------------

def _flatten_stmt(c):
    if c.name == "suite":
        out = []
        for sc in c.children:
            if is_node(sc) and not _skip_noise(sc.name):
                _append_stmt(sc, out)
        return out
    if _skip_noise(c.name):
        return []
    out = []
    _append_stmt(c, out)
    return out


def _conv_excepthandler(ec):
    etype = None
    ename = None
    exc = ec.children[0]
    if is_node(exc) and exc.name == "exception":
        if len(exc.children) >= 1:
            etype = _conv(exc.children[0])
        if len(exc.children) >= 2:
            ename = _leaf(exc.children[1])
    body = []
    i = 1
    while i < len(ec.children):
        c = ec.children[i]
        if is_node(c):
            for s in _flatten_stmt(c):
                body.append(s)
        i = i + 1
    return ExceptHandler(etype, ename, body)


def _conv_try(node):
    body = []
    handlers = []
    orelse = []
    finalbody = []
    section = "body"
    for c in node.children:
        if is_node(c):
            if c.name == "except_clauses":
                for ec in c.children:
                    if is_node(ec):
                        handlers.append(_conv_excepthandler(ec))
            else:
                stmts = _flatten_stmt(c)
                if section == "body":
                    for s in stmts:
                        body.append(s)
                elif section == "else":
                    for s in stmts:
                        orelse.append(s)
                else:
                    for s in stmts:
                        finalbody.append(s)
        else:
            if c == "else":
                section = "else"
            elif c == "finally":
                section = "finally"
    return Try(body, handlers, orelse, finalbody)


def _conv_with(node):
    items = []
    body = []
    seen_colon = False
    for c in node.children:
        if not is_node(c):
            if c == ":":
                seen_colon = True
            continue
        if not seen_colon:
            if c.name == "with_item":
                ctx_expr = _conv(c.children[0])
                optvars = None
                if len(c.children) >= 3:
                    optvars = _store(_conv(c.children[2]))
                items.append(withitem(ctx_expr, optvars))
            else:
                items.append(withitem(_conv(c), None))
        else:
            for s in _flatten_stmt(c):
                body.append(s)
    return With(items, body)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Decorators.  rast wraps a decorated def/class in `decorated` -> [decorators,
# funcdef|classdef]; each `decorator` is a bare name, a dotted attribute, or a
# call.  We rebuild the decorator expression and attach it to decorator_list.
# ---------------------------------------------------------------------------

def _dotted_expr(node):
    if node.name == "NAME":
        return _conv(node)
    expr = _conv(node.children[0])
    i = 1
    while i < len(node.children):
        expr = Attribute(expr, _leaf(node.children[i]), Load())
        i = i + 1
    return expr


def _conv_decorator(dec):
    head = dec.children[0]
    if is_node(head) and head.name == "dotted_name":
        func = _dotted_expr(head)
    else:
        func = _conv(head)
    if len(dec.children) >= 2 and _is(dec.children[1], "arglist"):
        args = []
        kwargs = []
        _fill_arglist(dec.children[1], args, kwargs)
        return Call(func, args, kwargs)
    return func


def _conv_decorated(node):
    decs = []
    dnode = node.children[0]
    for d in dnode.children:
        if is_node(d) and d.name == "decorator":
            decs.append(_conv_decorator(d))
    inner = node.children[1]
    result = _conv_stmt(inner)
    result.decorator_list = decs
    return result


# ---------------------------------------------------------------------------
# unparse: node tree -> Python source text.  py2c calls ast.unparse mainly on
# type annotations (Name/Constant/Subscript/Attribute/Tuple -- reproduced
# exactly) and, best-effort, on arbitrary nodes for source comments (every
# py2c call site is wrapped in try/except with a fallback).  Expression output
# matches CPython's ast.unparse for the common cases.
# ---------------------------------------------------------------------------

def _op_sym(op):
    t = op._typename
    if t == "Add": return "+"
    if t == "Sub": return "-"
    if t == "Mult": return "*"
    if t == "Div": return "/"
    if t == "FloorDiv": return "//"
    if t == "Mod": return "%"
    if t == "Pow": return "**"
    if t == "LShift": return "<<"
    if t == "RShift": return ">>"
    if t == "BitOr": return "|"
    if t == "BitXor": return "^"
    if t == "BitAnd": return "&"
    if t == "Eq": return "=="
    if t == "NotEq": return "!="
    if t == "Lt": return "<"
    if t == "LtE": return "<="
    if t == "Gt": return ">"
    if t == "GtE": return ">="
    if t == "Is": return "is"
    if t == "IsNot": return "is not"
    if t == "In": return "in"
    if t == "NotIn": return "not in"
    if t == "And": return "and"
    if t == "Or": return "or"
    if t == "USub": return "-"
    if t == "UAdd": return "+"
    if t == "Invert": return "~"
    if t == "Not": return "not"
    return "?"


def _up_join(nodes, sep):
    parts = []
    for n in nodes:
        parts.append(_up(n))
    return sep.join(parts)


def _up_sub(node):
    # subscript index: a Tuple slice renders without parentheses (dict[str, str])
    if node._typename == "Tuple":
        return _up_join(node.elts, ", ")
    return _up(node)


def _up_slice(node):
    lo = ""
    hi = ""
    st = ""
    if node.lower is not None:
        lo = _up(node.lower)
    if node.upper is not None:
        hi = _up(node.upper)
    out = lo + ":" + hi
    if node.step is not None:
        st = _up(node.step)
        out = out + ":" + st
    return out


def _up_call(node):
    parts = []
    for a in node.args:
        parts.append(_up(a))
    for k in node.keywords:
        if k.arg is None:
            parts.append("**" + _up(k.value))
        else:
            parts.append(k.arg + "=" + _up(k.value))
    return _up_p(node.func, 18) + "(" + ", ".join(parts) + ")"


def _up_target(node):
    if node._typename == "Tuple":
        if len(node.elts) == 0:
            return "()"
        if len(node.elts) == 1:
            return _up(node.elts[0]) + ","
        return _up_join(node.elts, ", ")
    return _up(node)


def _up_target_list(nodes):
    out = []
    for n in nodes:
        out.append(_up_target(n))
    return out


def _binop_prec(op):
    t = op._typename
    if t == "BitOr":
        return 9
    if t == "BitXor":
        return 10
    if t == "BitAnd":
        return 11
    if t == "LShift" or t == "RShift":
        return 12
    if t == "Add" or t == "Sub":
        return 13
    if t == "Mult" or t == "Div" or t == "FloorDiv" or t == "Mod" or t == "MatMult":
        return 14
    if t == "Pow":
        return 16
    return 13


def _prec(node):
    # expression precedence, mirroring CPython ast._Precedence (atoms high)
    nm = node._typename
    if nm == "BoolOp":
        if node.op._typename == "Or":
            return 5
        return 6
    if nm == "UnaryOp":
        if node.op._typename == "Not":
            return 7
        return 15
    if nm == "Compare":
        return 8
    if nm == "BinOp":
        return _binop_prec(node.op)
    if nm == "IfExp":
        return 4
    if nm == "Lambda":
        return 4
    return 18                                  # atoms / self-delimiting


def _up_p(node, min_prec):
    s = _up(node)
    if _prec(node) < min_prec:
        return "(" + s + ")"
    return s


def _up_comp(node):
    # generators: "for t in it if c ..."
    out = ""
    for g in node.generators:
        out = out + " for " + _up_target(g.target) + " in " + _up(g.iter)
        for c in g.ifs:
            out = out + " if " + _up(c)
    return out


def _up(node):
    nm = node._typename
    if nm == "Name":
        return node.id
    if nm == "Constant":
        return repr(node.value)
    if nm == "Attribute":
        return _up_p(node.value, 18) + "." + node.attr
    if nm == "Subscript":
        return _up_p(node.value, 18) + "[" + _up_sub(node.slice) + "]"
    if nm == "Starred":
        return "*" + _up(node.value)
    if nm == "Slice":
        return _up_slice(node)
    if nm == "Tuple":
        if len(node.elts) == 1:
            return "(" + _up(node.elts[0]) + ",)"
        return "(" + _up_join(node.elts, ", ") + ")"
    if nm == "List":
        return "[" + _up_join(node.elts, ", ") + "]"
    if nm == "Set":
        if len(node.elts) == 0:
            return "set()"
        return "{" + _up_join(node.elts, ", ") + "}"
    if nm == "Dict":
        parts = []
        i = 0
        while i < len(node.keys):
            k = node.keys[i]
            v = node.values[i]
            if k is None:
                parts.append("**" + _up(v))
            else:
                parts.append(_up(k) + ": " + _up(v))
            i = i + 1
        return "{" + ", ".join(parts) + "}"
    if nm == "Call":
        return _up_call(node)
    if nm == "BinOp":
        p = _binop_prec(node.op)
        if node.op._typename == "Pow":       # right-associative
            lhs = _up_p(node.left, p + 1)
            rhs = _up_p(node.right, p)
        else:                                # left-associative
            lhs = _up_p(node.left, p)
            rhs = _up_p(node.right, p + 1)
        return lhs + " " + _op_sym(node.op) + " " + rhs
    if nm == "BoolOp":
        p = _prec(node)
        parts = []
        i = 0
        while i < len(node.values):
            parts.append(_up_p(node.values[i], p + 1 + i))   # CPython: each operand needs a higher level
            i = i + 1
        return (" " + _op_sym(node.op) + " ").join(parts)
    if nm == "UnaryOp":
        sym = _op_sym(node.op)
        p = _prec(node)
        if node.op._typename == "Not":
            return "not " + _up_p(node.operand, p)
        return sym + _up_p(node.operand, p)
    if nm == "Compare":
        out = _up_p(node.left, 9)
        i = 0
        while i < len(node.ops):
            out = out + " " + _op_sym(node.ops[i]) + " " + _up_p(node.comparators[i], 9)
            i = i + 1
        return out
    if nm == "IfExp":
        return _up_p(node.body, 5) + " if " + _up_p(node.test, 5) + " else " + _up_p(node.orelse, 4)
    if nm == "ListComp":
        return "[" + _up(node.elt) + _up_comp(node) + "]"
    if nm == "SetComp":
        return "{" + _up(node.elt) + _up_comp(node) + "}"
    if nm == "GeneratorExp":
        return "(" + _up(node.elt) + _up_comp(node) + ")"
    if nm == "DictComp":
        return "{" + _up(node.key) + ": " + _up(node.value) + _up_comp(node) + "}"
    # ---- statements (best-effort; py2c falls back on failure) ----
    if nm == "Expr":
        return _up(node.value)
    if nm == "Assign":
        return _up_join(node.targets, " = ") + " = " + _up(node.value)
    if nm == "Return":
        if node.value is None:
            return "return"
        return "return " + _up(node.value)
    if nm == "Pass":
        return "pass"
    if nm == "Break":
        return "break"
    if nm == "Continue":
        return "continue"
    if nm == "Module":
        return "\n".join(_up_list(node.body))
    return nm


def _up_list(nodes):
    out = []
    for n in nodes:
        out.append(_up(n))
    return out


def _up_arg(a):
    s = a.arg
    if a.annotation is not None:
        s = s + ": " + _up(a.annotation)
    return s


def _up_args(a):
    parts = []
    allpos = a.posonlyargs + a.args
    ndef = len(a.defaults)
    npos = len(allpos)
    nposonly = len(a.posonlyargs)
    i = 0
    while i < npos:
        seg = _up_arg(allpos[i])
        di = i - (npos - ndef)
        if di >= 0:
            seg = seg + "=" + _up(a.defaults[di])
        parts.append(seg)
        if i + 1 == nposonly and nposonly > 0:
            parts.append("/")
        i = i + 1
    if a.vararg is not None:
        parts.append("*" + _up_arg(a.vararg))
    elif len(a.kwonlyargs) > 0:
        parts.append("*")
    j = 0
    while j < len(a.kwonlyargs):
        seg = _up_arg(a.kwonlyargs[j])
        d = a.kw_defaults[j]
        if d is not None:
            seg = seg + "=" + _up(d)
        parts.append(seg)
        j = j + 1
    if a.kwarg is not None:
        parts.append("**" + _up_arg(a.kwarg))
    return ", ".join(parts)


def _up_keyword(k):
    if k.arg is None:
        return "**" + _up(k.value)
    return k.arg + "=" + _up(k.value)


def _is_docstring(node):
    if node._typename != "Expr":
        return 0
    v = node.value
    if v._typename != "Constant":
        return 0
    return 1 if isinstance(v.value, str) else 0


def _emit_docstring(node, ind, lines):
    pad = "    " * ind
    val = node.value.value
    esc = ""
    can = 1
    for c in val:
        o = ord(c)
        if c == "\\":
            esc = esc + "\\\\"
        elif c == "\n" or c == "\t":
            esc = esc + c
        elif o >= 32 and o != 127:
            esc = esc + c                 # printable (incl. non-ASCII) -> literal
        else:
            can = 0                       # control char (ord<32 non-\n\t, or DEL) -> fall back
    if can == 0:
        lines.append(pad + repr(val))
        return
    q = "\"\"\""
    if q in esc:
        q = "'''"
        if q in esc:
            lines.append(pad + repr(val))
            return
    if len(esc) > 0 and esc[len(esc) - 1] == q[0]:
        esc = esc[0:len(esc) - 1] + "\\" + esc[len(esc) - 1]
    lines.append(pad + q + esc + q)


def _emit_body(stmts, ind, lines):
    # module/def/class body: a leading string-constant Expr is a docstring
    if len(stmts) > 0 and _is_docstring(stmts[0]):
        _emit_docstring(stmts[0], ind, lines)
        _emit_stmts(stmts[1:], ind, lines)
    else:
        _emit_stmts(stmts, ind, lines)


def _emit_stmts(stmts, ind, lines):
    i = 0
    while i < len(stmts):
        _emit_stmt(stmts[i], ind, lines)
        i = i + 1


def _emit_stmt(node, ind, lines):
    nm = node._typename
    pad = "    " * ind
    if nm == "FunctionDef" or nm == "AsyncFunctionDef":
        if len(lines) > 0:
            lines.append("")
        for deco in node.decorator_list:
            lines.append(pad + "@" + _up(deco))
        pre = "def "
        if nm == "AsyncFunctionDef":
            pre = "async def "
        sig = pad + pre + node.name + "(" + _up_args(node.args) + ")"
        if node.returns is not None:
            sig = sig + " -> " + _up(node.returns)
        lines.append(sig + ":")
        _emit_body(node.body, ind + 1, lines)
        return
    if nm == "ClassDef":
        if len(lines) > 0:
            lines.append("")
        for deco in node.decorator_list:
            lines.append(pad + "@" + _up(deco))
        parts = []
        for b in node.bases:
            parts.append(_up(b))
        for k in node.keywords:
            parts.append(_up_keyword(k))
        head = pad + "class " + node.name
        if len(parts) > 0:
            head = head + "(" + ", ".join(parts) + ")"
        lines.append(head + ":")
        _emit_body(node.body, ind + 1, lines)
        return
    if nm == "If":
        lines.append(pad + "if " + _up(node.test) + ":")
        _emit_stmts(node.body, ind + 1, lines)
        orelse = node.orelse
        while len(orelse) == 1 and orelse[0]._typename == "If":
            e = orelse[0]
            lines.append(pad + "elif " + _up(e.test) + ":")
            _emit_stmts(e.body, ind + 1, lines)
            orelse = e.orelse
        if len(orelse) > 0:
            lines.append(pad + "else:")
            _emit_stmts(orelse, ind + 1, lines)
        return
    if nm == "For" or nm == "AsyncFor":
        pre = "for "
        if nm == "AsyncFor":
            pre = "async for "
        lines.append(pad + pre + _up_target(node.target) + " in " + _up(node.iter) + ":")
        _emit_stmts(node.body, ind + 1, lines)
        if len(node.orelse) > 0:
            lines.append(pad + "else:")
            _emit_stmts(node.orelse, ind + 1, lines)
        return
    if nm == "While":
        lines.append(pad + "while " + _up(node.test) + ":")
        _emit_stmts(node.body, ind + 1, lines)
        if len(node.orelse) > 0:
            lines.append(pad + "else:")
            _emit_stmts(node.orelse, ind + 1, lines)
        return
    if nm == "With" or nm == "AsyncWith":
        pre = "with "
        if nm == "AsyncWith":
            pre = "async with "
        parts = []
        for it in node.items:
            seg = _up(it.context_expr)
            if it.optional_vars is not None:
                seg = seg + " as " + _up(it.optional_vars)
            parts.append(seg)
        lines.append(pad + pre + ", ".join(parts) + ":")
        _emit_stmts(node.body, ind + 1, lines)
        return
    if nm == "Try":
        lines.append(pad + "try:")
        _emit_stmts(node.body, ind + 1, lines)
        for h in node.handlers:
            head = "except"
            if h.type is not None:
                head = head + " " + _up(h.type)
                if h.name is not None:
                    head = head + " as " + h.name
            lines.append(pad + head + ":")
            _emit_stmts(h.body, ind + 1, lines)
        if len(node.orelse) > 0:
            lines.append(pad + "else:")
            _emit_stmts(node.orelse, ind + 1, lines)
        if len(node.finalbody) > 0:
            lines.append(pad + "finally:")
            _emit_stmts(node.finalbody, ind + 1, lines)
        return
    if nm == "Import":
        parts = []
        for a in node.names:
            seg = a.name
            if a.asname is not None:
                seg = seg + " as " + a.asname
            parts.append(seg)
        lines.append(pad + "import " + ", ".join(parts))
        return
    if nm == "ImportFrom":
        mod = node.module
        if mod is None:
            mod = ""
        dots = "." * node.level
        parts = []
        for a in node.names:
            seg = a.name
            if a.asname is not None:
                seg = seg + " as " + a.asname
            parts.append(seg)
        lines.append(pad + "from " + dots + mod + " import " + ", ".join(parts))
        return
    if nm == "AugAssign":
        lines.append(pad + _up_target(node.target) + " " + _op_sym(node.op) + "= " + _up(node.value))
        return
    if nm == "AnnAssign":
        seg = _up_target(node.target) + ": " + _up(node.annotation)
        if node.value is not None:
            seg = seg + " = " + _up(node.value)
        lines.append(pad + seg)
        return
    if nm == "Raise":
        seg = "raise"
        if node.exc is not None:
            seg = seg + " " + _up(node.exc)
            if node.cause is not None:
                seg = seg + " from " + _up(node.cause)
        lines.append(pad + seg)
        return
    if nm == "Assert":
        seg = "assert " + _up(node.test)
        if node.msg is not None:
            seg = seg + ", " + _up(node.msg)
        lines.append(pad + seg)
        return
    if nm == "Global":
        lines.append(pad + "global " + ", ".join(node.names))
        return
    if nm == "Nonlocal":
        lines.append(pad + "nonlocal " + ", ".join(node.names))
        return
    if nm == "Delete":
        lines.append(pad + "del " + ", ".join(_up_target_list(node.targets)))
        return
    if nm == "Assign":
        lines.append(pad + " = ".join(_up_target_list(node.targets)) + " = " + _up(node.value))
        return
    if nm == "Return":
        if node.value is None:
            lines.append(pad + "return")
        else:
            lines.append(pad + "return " + _up(node.value))
        return
    if nm == "Expr":
        lines.append(pad + _up(node.value))
        return
    if nm == "Pass":
        lines.append(pad + "pass")
        return
    if nm == "Break":
        lines.append(pad + "break")
        return
    if nm == "Continue":
        lines.append(pad + "continue")
        return
    lines.append(pad + _up(node))


def unparse(node):
    if node._typename == "Module":
        lines = []
        _emit_body(node.body, 0, lines)
        return "\n".join(lines)
    lines = []
    _emit_stmt(node, 0, lines)
    return "\n".join(lines)


def parse(source, filename="<unknown>", mode="exec", type_comments=0, feature_version=0):
    tree = parse_python(source)
    body = []
    if _is(tree, "And"):
        for c in tree.children:
            if is_node(c) and not _skip_noise(c.name):
                _append_stmt(c, body)
    else:
        _append_stmt(tree, body)
    return Module(body)
