"""SPIR-V binary writer -- the encoder half of the SPIR-V back end.

`shivyc/wasm.py` turns a module structure into bytes with no external tools;
this does the same for SPIR-V. There is no spirv-as, no glslang and no
SPIRV-Tools anywhere in the path. `shivyc/spirv_reader.py` is the inverse
(decode, disassemble, validate).

Only the subset the back end needs is tabulated. Each opcode carries an
*operand signature*, which is what lets the reader decode, disassemble and
validate generically without a per-opcode function:

    I  an id                      L  a literal word
    S  a nul-terminated string    P  a (literal, id) pair   (OpSwitch)
    Q  an (id, id) pair           (OpPhi)
    X  a trailing '*' makes the character before it repeat zero or more times

so "IL*" is an id then any number of literals, and "II*" an id then any number
of ids.

Self-host note: plain loops and instance attributes only, so the restricted
Python translator can lower this, as with wasm.py.
"""

import struct

MAGIC = 0x07230203
VERSION_1_0 = 0x00010000
VERSION_1_3 = 0x00010300     # Vulkan 1.1 baseline: StorageBuffer is core here
VERSION_1_5 = 0x00010500     # Vulkan 1.2
GENERATOR = 0                # no registered generator magic number

# name -> (opcode, has_result_type, has_result_id, operand signature)
OPCODES = {
    "OpUndef":              (1,   True,  True,  ""),
    "OpSource":             (3,   False, False, "LL"),
    "OpName":               (5,   False, False, "IS"),
    "OpMemberName":         (6,   False, False, "ILS"),
    "OpExtInstImport":      (11,  False, True,  "S"),
    "OpExtInst":            (12,  True,  True,  "ILI*"),
    "OpMemoryModel":        (14,  False, False, "LL"),
    "OpEntryPoint":         (15,  False, False, "LISI*"),
    "OpExecutionMode":      (16,  False, False, "ILL*"),
    "OpCapability":         (17,  False, False, "L"),
    "OpTypeVoid":           (19,  False, True,  ""),
    "OpTypeBool":           (20,  False, True,  ""),
    "OpTypeInt":            (21,  False, True,  "LL"),
    "OpTypeFloat":          (22,  False, True,  "L"),
    "OpTypeVector":         (23,  False, True,  "IL"),
    "OpTypeArray":          (28,  False, True,  "II"),
    "OpTypeRuntimeArray":   (29,  False, True,  "I"),
    "OpTypeStruct":         (30,  False, True,  "I*"),
    "OpTypePointer":        (32,  False, True,  "LI"),
    "OpTypeFunction":       (33,  False, True,  "II*"),
    "OpConstantTrue":       (41,  True,  True,  ""),
    "OpConstantFalse":      (42,  True,  True,  ""),
    "OpConstant":           (43,  True,  True,  "L*"),
    "OpConstantComposite":  (44,  True,  True,  "I*"),
    "OpConstantNull":       (46,  True,  True,  ""),
    "OpFunction":           (54,  True,  True,  "LI"),
    "OpFunctionParameter":  (55,  True,  True,  ""),
    "OpFunctionEnd":        (56,  False, False, ""),
    "OpFunctionCall":       (57,  True,  True,  "II*"),
    "OpVariable":           (59,  True,  True,  "LI*"),
    "OpLoad":               (61,  True,  True,  "IL*"),
    "OpStore":              (62,  False, False, "IIL*"),
    "OpAccessChain":        (65,  True,  True,  "II*"),
    "OpInBoundsAccessChain": (66, True,  True,  "II*"),
    "OpArrayLength":        (68,  True,  True,  "IL"),
    "OpDecorate":           (71,  False, False, "ILL*"),
    "OpMemberDecorate":     (72,  False, False, "ILLL*"),
    "OpVectorShuffle":      (79,  True,  True,  "IIL*"),
    "OpCompositeConstruct": (80,  True,  True,  "I*"),
    "OpCompositeExtract":   (81,  True,  True,  "IL*"),
    "OpCompositeInsert":    (82,  True,  True,  "IIL*"),
    "OpCopyObject":         (83,  True,  True,  "I"),
    "OpConvertFToU":        (109, True,  True,  "I"),
    "OpConvertFToS":        (110, True,  True,  "I"),
    "OpConvertSToF":        (111, True,  True,  "I"),
    "OpConvertUToF":        (112, True,  True,  "I"),
    "OpUConvert":           (113, True,  True,  "I"),
    "OpSConvert":           (114, True,  True,  "I"),
    "OpFConvert":           (115, True,  True,  "I"),
    "OpBitcast":            (124, True,  True,  "I"),
    "OpSNegate":            (126, True,  True,  "I"),
    "OpFNegate":            (127, True,  True,  "I"),
    "OpIAdd":               (128, True,  True,  "II"),
    "OpFAdd":               (129, True,  True,  "II"),
    "OpISub":               (130, True,  True,  "II"),
    "OpFSub":               (131, True,  True,  "II"),
    "OpIMul":               (132, True,  True,  "II"),
    "OpFMul":               (133, True,  True,  "II"),
    "OpUDiv":               (134, True,  True,  "II"),
    "OpSDiv":               (135, True,  True,  "II"),
    "OpFDiv":               (136, True,  True,  "II"),
    "OpUMod":               (137, True,  True,  "II"),
    "OpSRem":               (138, True,  True,  "II"),
    "OpSMod":               (139, True,  True,  "II"),
    "OpFRem":               (140, True,  True,  "II"),
    "OpFMod":               (141, True,  True,  "II"),
    "OpLogicalEqual":       (164, True,  True,  "II"),
    "OpLogicalNotEqual":    (165, True,  True,  "II"),
    "OpLogicalOr":          (166, True,  True,  "II"),
    "OpLogicalAnd":         (167, True,  True,  "II"),
    "OpLogicalNot":         (168, True,  True,  "I"),
    "OpSelect":             (169, True,  True,  "III"),
    "OpIEqual":             (170, True,  True,  "II"),
    "OpINotEqual":          (171, True,  True,  "II"),
    "OpUGreaterThan":       (172, True,  True,  "II"),
    "OpSGreaterThan":       (173, True,  True,  "II"),
    "OpUGreaterThanEqual":  (174, True,  True,  "II"),
    "OpSGreaterThanEqual":  (175, True,  True,  "II"),
    "OpULessThan":          (176, True,  True,  "II"),
    "OpSLessThan":          (177, True,  True,  "II"),
    "OpULessThanEqual":     (178, True,  True,  "II"),
    "OpSLessThanEqual":     (179, True,  True,  "II"),
    "OpFOrdEqual":          (180, True,  True,  "II"),
    "OpFUnordEqual":        (181, True,  True,  "II"),
    "OpFOrdNotEqual":       (182, True,  True,  "II"),
    "OpFUnordNotEqual":     (183, True,  True,  "II"),
    "OpFOrdLessThan":       (184, True,  True,  "II"),
    "OpFUnordLessThan":     (185, True,  True,  "II"),
    "OpFOrdGreaterThan":    (186, True,  True,  "II"),
    "OpFUnordGreaterThan":  (187, True,  True,  "II"),
    "OpFOrdLessThanEqual":  (188, True,  True,  "II"),
    "OpFUnordLessThanEqual": (189, True, True,  "II"),
    "OpFOrdGreaterThanEqual": (190, True, True, "II"),
    "OpFUnordGreaterThanEqual": (191, True, True, "II"),
    "OpShiftRightLogical":  (194, True,  True,  "II"),
    "OpShiftRightArithmetic": (195, True, True, "II"),
    "OpShiftLeftLogical":   (196, True,  True,  "II"),
    "OpBitwiseOr":          (197, True,  True,  "II"),
    "OpBitwiseXor":         (198, True,  True,  "II"),
    "OpBitwiseAnd":         (199, True,  True,  "II"),
    "OpNot":                (200, True,  True,  "I"),
    "OpBitCount":           (205, True,  True,  "I"),
    "OpControlBarrier":     (224, False, False, "III"),
    "OpMemoryBarrier":      (225, False, False, "II"),
    "OpAtomicLoad":         (227, True,  True,  "III"),
    "OpAtomicStore":        (228, False, False, "IIII"),
    "OpAtomicExchange":     (229, True,  True,  "IIII"),
    "OpAtomicCompareExchange": (230, True, True, "IIIIII"),
    "OpAtomicIAdd":         (234, True,  True,  "IIII"),
    "OpAtomicISub":         (235, True,  True,  "IIII"),
    "OpAtomicSMin":         (236, True,  True,  "IIII"),
    "OpAtomicUMin":         (237, True,  True,  "IIII"),
    "OpAtomicSMax":         (238, True,  True,  "IIII"),
    "OpAtomicUMax":         (239, True,  True,  "IIII"),
    "OpAtomicAnd":          (240, True,  True,  "IIII"),
    "OpAtomicOr":           (241, True,  True,  "IIII"),
    "OpAtomicXor":          (242, True,  True,  "IIII"),
    "OpPhi":                (245, True,  True,  "Q*"),
    "OpLoopMerge":          (246, False, False, "IIL*"),
    "OpSelectionMerge":     (247, False, False, "IL"),
    "OpLabel":              (248, False, True,  ""),
    "OpBranch":             (249, False, False, "I"),
    "OpBranchConditional":  (250, False, False, "IIIL*"),
    "OpSwitch":             (251, False, False, "IIP*"),
    "OpReturn":             (253, False, False, ""),
    "OpReturnValue":        (254, False, False, "I"),
    "OpUnreachable":        (255, False, False, ""),
}

OPCODE_BY_CODE = {}
for _n in OPCODES:
    OPCODE_BY_CODE[OPCODES[_n][0]] = _n

TERMINATORS = ["OpBranch", "OpBranchConditional", "OpSwitch", "OpReturn",
               "OpReturnValue", "OpUnreachable"]

# --- enumerants (only those the subset uses) ---------------------------------

CAPABILITY = {"Shader": 1, "Float16": 9, "Float64": 10, "Int64": 11,
              "Int16": 22, "Int8": 39}
ADDRESSING = {"Logical": 0, "Physical32": 1, "Physical64": 2}
MEMORY_MODEL = {"Simple": 0, "GLSL450": 1, "OpenCL": 2, "Vulkan": 3}
EXEC_MODEL = {"Vertex": 0, "Fragment": 4, "GLCompute": 5, "Kernel": 6}
EXEC_MODE = {"LocalSize": 17}
STORAGE = {"UniformConstant": 0, "Input": 1, "Uniform": 2, "Output": 3,
           "Workgroup": 4, "CrossWorkgroup": 5, "Private": 6, "Function": 7,
           "PushConstant": 9, "StorageBuffer": 12}
DECORATION = {"Block": 2, "BufferBlock": 3, "ArrayStride": 6, "BuiltIn": 11,
              "NoContraction": 42, "NonWritable": 24, "NonReadable": 25,
              "Binding": 33, "DescriptorSet": 34, "Offset": 35}
BUILTIN = {"NumWorkgroups": 24, "WorkgroupSize": 25, "WorkgroupId": 26,
           "LocalInvocationId": 27, "GlobalInvocationId": 28,
           "LocalInvocationIndex": 29}
SCOPE = {"CrossDevice": 0, "Device": 1, "Workgroup": 2, "Subgroup": 3}
# memory semantics bits used with barriers
SEM_ACQUIRE_RELEASE = 0x8
SEM_WORKGROUP_MEMORY = 0x100
SEM_UNIFORM_MEMORY = 0x40

GLSL_STD_450 = {"Round": 1, "RoundEven": 2, "Trunc": 3, "FAbs": 4, "SAbs": 5,
                "FSign": 6, "SSign": 7, "Floor": 8, "Ceil": 9, "Fract": 10,
                "Sin": 13, "Cos": 14, "Tan": 15, "Atan2": 25, "Pow": 26,
                "Exp": 27, "Log": 28, "Exp2": 29, "Log2": 30, "Sqrt": 31,
                "InverseSqrt": 32, "FMin": 37, "UMin": 38, "SMin": 39,
                "FMax": 40, "UMax": 41, "SMax": 42, "FClamp": 43,
                "UClamp": 44, "SClamp": 45, "Fma": 50}

# Section order the spec demands for everything outside function bodies.
SEC_CAPABILITY = 0
SEC_EXTENSION = 1
SEC_EXTIMPORT = 2
SEC_MEMMODEL = 3
SEC_ENTRY = 4
SEC_EXECMODE = 5
SEC_DEBUG = 6
SEC_ANNOT = 7
SEC_GLOBALS = 8
SEC_FUNCS = 9
SECTION_OF = {
    "OpCapability": 0, "OpExtInstImport": 2, "OpMemoryModel": 3,
    "OpEntryPoint": 4, "OpExecutionMode": 5, "OpSource": 6, "OpName": 6,
    "OpMemberName": 6, "OpDecorate": 7, "OpMemberDecorate": 7,
}


def encode_string(s):
    """A literal string: UTF-8, nul-terminated, padded to a word, little
    endian within each word."""
    b = s.encode("utf-8") + b"\x00"
    while len(b) % 4:
        b += b"\x00"
    words = []
    i = 0
    while i < len(b):
        words.append(struct.unpack("<I", b[i:i + 4])[0])
        i += 4
    return words


def decode_string(words, start):
    """(string, index of the word after it) from `words` at `start`."""
    out = b""
    i = start
    while i < len(words):
        chunk = struct.pack("<I", words[i])
        i += 1
        z = chunk.find(b"\x00")
        if z >= 0:
            out += chunk[:z]
            return out.decode("utf-8"), i
        out += chunk
    raise ValueError("unterminated string literal")


def inst_words(name, result_type, result_id, operands):
    """One instruction as words: (wordcount << 16 | opcode), the optional
    result type and result id, then the operands. An operand is an int (one
    word), a str (a literal string) or a list of ints (spliced in)."""
    entry = OPCODES[name]
    body = []
    if entry[1]:
        body.append(result_type)
    if entry[2]:
        body.append(result_id)
    for o in operands:
        if isinstance(o, str):
            body.extend(encode_string(o))
        elif isinstance(o, list):
            body.extend(o)
        else:
            body.append(o)
    return [((len(body) + 1) << 16) | entry[0]] + body


class Block:
    """A basic block under construction. Append instructions with op();
    every block must end in exactly one terminator."""

    def __init__(self, module, label):
        self.module = module
        self.label = label
        self.insts = []          # list of word lists
        self.names = []
        self.terminated = False

    def op(self, name, result_type, *operands):
        """Append an instruction. Returns its result id when it has one."""
        entry = OPCODES[name]
        if self.terminated:
            raise ValueError("%%%d: instruction after terminator" % self.label)
        rid = 0
        if entry[2]:
            rid = self.module.new_id()
        self.insts.append(inst_words(name, result_type, rid, list(operands)))
        if name in TERMINATORS:
            self.terminated = True
        return rid

    def store(self, ptr, value):
        self.op("OpStore", 0, ptr, value)

    def load(self, type_id, ptr):
        return self.op("OpLoad", type_id, ptr)

    def branch(self, target):
        self.op("OpBranch", 0, target)

    def cond_branch(self, cond, t, f):
        self.op("OpBranchConditional", 0, cond, t, f)

    def selection_merge(self, merge):
        self.op("OpSelectionMerge", 0, merge, 0)

    def loop_merge(self, merge, cont):
        self.op("OpLoopMerge", 0, merge, cont, 0)

    def ret(self):
        self.op("OpReturn", 0)

    def phi(self, type_id, pairs):
        flat = []
        for pair in pairs:
            flat.append(pair[0])
            flat.append(pair[1])
        return self.op("OpPhi", type_id, *flat)


class Function:
    """A function under construction."""

    def __init__(self, module, fid, ret_type, fn_type, param_types):
        self.module = module
        self.id = fid
        self.ret_type = ret_type
        self.fn_type = fn_type
        self.params = []
        for t in param_types:
            self.params.append(module.new_id())
        self.param_types = param_types
        self.blocks = []
        self.local_vars = []     # word lists, hoisted to the entry block

    def block(self):
        b = Block(self.module, self.module.new_id())
        self.blocks.append(b)
        return b

    def new_block(self):
        """A block with an id but no place in the function yet. Merge and
        continue blocks are created early (their ids are needed by the
        header) and placed with place() once the construct's body is out,
        so the layout is header, body, continue, merge."""
        return Block(self.module, self.module.new_id())

    def place(self, b):
        self.blocks.append(b)
        return b

    def local_var(self, ptr_type, init=None):
        """A Function-storage variable. The spec wants these first in the
        entry block, so they are collected and emitted there."""
        rid = self.module.new_id()
        ops = [STORAGE["Function"]]
        if init is not None:
            ops.append(init)
        self.local_vars.append(inst_words("OpVariable", ptr_type, rid, ops))
        return rid

    def words(self):
        out = inst_words("OpFunction", self.ret_type, self.id,
                         [0, self.fn_type])
        for i in range(len(self.params)):
            out.extend(inst_words("OpFunctionParameter", self.param_types[i],
                                  self.params[i], []))
        first = True
        for b in self.blocks:
            if not b.terminated:
                raise ValueError("block %%%d has no terminator" % b.label)
            out.extend(inst_words("OpLabel", 0, b.label, []))
            if first:
                for lv in self.local_vars:
                    out.extend(lv)
                first = False
            for i in b.insts:
                out.extend(i)
        out.extend(inst_words("OpFunctionEnd", 0, 0, []))
        return out


class Module:
    """A SPIR-V module under construction.

        m = Module()
        m.capability("Shader")
        ...
        words = m.words()
        data = m.bytes()
    """

    def __init__(self, version=VERSION_1_3):
        self.version = version
        self.bound = 1
        self.sections = []
        for _i in range(10):
            self.sections.append([])
        self.functions = []
        self.type_cache = {}
        self.const_cache = {}
        self.ext_ids = {}
        self.capabilities = []

    def new_id(self):
        i = self.bound
        self.bound = self.bound + 1
        return i

    def emit(self, section, name, result_type, result_id, *operands):
        self.sections[section].append(
            inst_words(name, result_type, result_id, list(operands)))

    # --- header-ish sections ---------------------------------------------

    def capability(self, name):
        if name in self.capabilities:
            return
        self.capabilities.append(name)
        self.emit(SEC_CAPABILITY, "OpCapability", 0, 0, CAPABILITY[name])

    def ext_inst_import(self, name):
        if name not in self.ext_ids:
            rid = self.new_id()
            self.ext_ids[name] = rid
            self.emit(SEC_EXTIMPORT, "OpExtInstImport", 0, rid, name)
        return self.ext_ids[name]

    def memory_model(self, addressing="Logical", model="GLSL450"):
        self.emit(SEC_MEMMODEL, "OpMemoryModel", 0, 0,
                  ADDRESSING[addressing], MEMORY_MODEL[model])

    def entry_point(self, func, name, model="GLCompute", interface=None):
        ops = [EXEC_MODEL[model], func.id, name]
        if interface:
            ops.extend(interface)
        self.emit(SEC_ENTRY, "OpEntryPoint", 0, 0, *ops)

    def local_size(self, func, x, y=1, z=1):
        self.emit(SEC_EXECMODE, "OpExecutionMode", 0, 0,
                  func.id, EXEC_MODE["LocalSize"], x, y, z)

    def name(self, target, text):
        self.emit(SEC_DEBUG, "OpName", 0, 0, target, text)

    def member_name(self, struct_id, index, text):
        self.emit(SEC_DEBUG, "OpMemberName", 0, 0, struct_id, index, text)

    def decorate(self, target, decoration, *args):
        # Types are cached, so the same type can be decorated by several
        # callers; repeating an identical decoration is invalid SPIR-V.
        key = ("dec", target, decoration, args)
        if key in self.const_cache:
            return
        self.const_cache[key] = True
        self.emit(SEC_ANNOT, "OpDecorate", 0, 0, target,
                  DECORATION[decoration], *args)

    def member_decorate(self, struct_id, index, decoration, *args):
        key = ("mdec", struct_id, index, decoration, args)
        if key in self.const_cache:
            return
        self.const_cache[key] = True
        self.emit(SEC_ANNOT, "OpMemberDecorate", 0, 0, struct_id, index,
                  DECORATION[decoration], *args)

    # --- types and constants (cached: one id per distinct type) ----------

    def _type(self, key, name, *operands):
        if key not in self.type_cache:
            rid = self.new_id()
            self.type_cache[key] = rid
            self.emit(SEC_GLOBALS, name, 0, rid, *operands)
        return self.type_cache[key]

    def t_void(self):
        return self._type(("void",), "OpTypeVoid")

    def t_bool(self):
        return self._type(("bool",), "OpTypeBool")

    def t_int(self, width=32, signed=False):
        if width == 8:
            self.capability("Int8")
        elif width == 16:
            self.capability("Int16")
        elif width == 64:
            self.capability("Int64")
        return self._type(("int", width, signed), "OpTypeInt", width,
                          1 if signed else 0)

    def t_float(self, width=32):
        if width == 64:
            self.capability("Float64")
        elif width == 16:
            self.capability("Float16")
        return self._type(("float", width), "OpTypeFloat", width)

    def t_vector(self, elem, count):
        return self._type(("vec", elem, count), "OpTypeVector", elem, count)

    def t_array(self, elem, length_const):
        return self._type(("array", elem, length_const), "OpTypeArray",
                          elem, length_const)

    def t_runtime_array(self, elem):
        return self._type(("rtarray", elem), "OpTypeRuntimeArray", elem)

    def t_struct(self, members):
        # A struct is nominal in SPIR-V (decorations differ per use), so it
        # is not cached: each call is a distinct type.
        rid = self.new_id()
        self.emit(SEC_GLOBALS, "OpTypeStruct", 0, rid, *members)
        return rid

    def t_pointer(self, storage, pointee):
        return self._type(("ptr", storage, pointee), "OpTypePointer",
                          STORAGE[storage], pointee)

    def t_function(self, ret, params):
        key = ("fn", ret, tuple(params))
        return self._type(key, "OpTypeFunction", ret, *params)

    def const_int(self, type_id, value, width=32):
        key = ("ci", type_id, value)
        if key not in self.const_cache:
            rid = self.new_id()
            self.const_cache[key] = rid
            if width == 64:
                v = value & 0xFFFFFFFFFFFFFFFF
                words = [v & 0xFFFFFFFF, v >> 32]
            else:
                words = [value & 0xFFFFFFFF]
            self.emit(SEC_GLOBALS, "OpConstant", type_id, rid, words)
        return self.const_cache[key]

    def const_float(self, type_id, value, width=32):
        if width == 64:
            raw = struct.pack("<d", value)
            words = list(struct.unpack("<II", raw))
        else:
            words = [struct.unpack("<I", struct.pack("<f", value))[0]]
        key = ("cf", type_id, tuple(words))
        if key not in self.const_cache:
            rid = self.new_id()
            self.const_cache[key] = rid
            self.emit(SEC_GLOBALS, "OpConstant", type_id, rid, words)
        return self.const_cache[key]

    def const_bool(self, type_id, value):
        key = ("cb", type_id, bool(value))
        if key not in self.const_cache:
            rid = self.new_id()
            self.const_cache[key] = rid
            self.emit(SEC_GLOBALS,
                      "OpConstantTrue" if value else "OpConstantFalse",
                      type_id, rid)
        return self.const_cache[key]

    def const_composite(self, type_id, parts):
        rid = self.new_id()
        self.emit(SEC_GLOBALS, "OpConstantComposite", type_id, rid, *parts)
        return rid

    def global_var(self, ptr_type, storage, init=None):
        rid = self.new_id()
        ops = [STORAGE[storage]]
        if init is not None:
            ops.append(init)
        self.emit(SEC_GLOBALS, "OpVariable", ptr_type, rid, *ops)
        return rid

    # --- functions -------------------------------------------------------

    def function(self, ret_type, param_types=None):
        params = param_types
        if params is None:
            params = []
        fn_type = self.t_function(ret_type, params)
        f = Function(self, self.new_id(), ret_type, fn_type, params)
        self.functions.append(f)
        return f

    # --- output ----------------------------------------------------------

    def words(self):
        out = [MAGIC, self.version, GENERATOR, self.bound, 0]
        for s in range(10):
            if s == SEC_FUNCS:
                for f in self.functions:
                    out.extend(f.words())
            else:
                for inst in self.sections[s]:
                    out.extend(inst)
        return out

    def bytes(self):
        return words_to_bytes(self.words())


def words_to_bytes(words):
    return struct.pack("<%dI" % len(words), *words)


def bytes_to_words(data):
    if len(data) % 4:
        raise ValueError("SPIR-V binary length is not a multiple of 4")
    return list(struct.unpack("<%dI" % (len(data) // 4), data))
