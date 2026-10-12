"""
Deterministic engineering mathematics.

    the model explains  ->  this module computes  ->  a second, independent
    computation verifies  ->  only then is a number presented as a result

Three entry points:

evaluate(expression)
    A SymPy expression, optionally with units ("12.5 bar * 0.3 m**2 in kN"),
    parsed by SymPy with no builtins: only whitelisted functions and symbols are
    recognised. Returns the exact form, the numeric value, and the units. The
    numeric value is re-evaluated by an independent path (mpmath at high
    precision) and the two must agree.

convert(value, from_unit, to_unit)
    Unit conversion through sympy.physics.units, with a dimension check -
    converting bar to metres is refused, not silently scaled.

formula(name, inputs)
    A library of standard industrial calculations (pump power, head, Reynolds
    number, Darcy-Weisbach pressure drop, heat duty, LMTD, Barlow wall
    thickness / MAWP, corrosion rate and remaining life, NPSH available, flow
    velocity). Every result states the formula, each input with its unit, the
    assumptions, and a verification computed a different way.

Nothing here calls a model. Expressions go through SymPy's parser with no
builtins, after a filter that rejects dunders, imports, attribute access,
brackets and statement separators; result size is bounded before evaluation.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ------------------------------------------------------------------ units --

_UNIT_ALIASES = {
    "bar": "bar", "barg": "bar", "psi": "psi", "pa": "pascal", "kpa": "kilopascal",
    "mpa": "megapascal", "atm": "atmosphere",
    "m": "meter", "mm": "millimeter", "cm": "centimeter", "km": "kilometer",
    "in": "inch", "inch": "inch", "ft": "foot",
    "s": "second", "min": "minute", "h": "hour", "hr": "hour", "day": "day",
    "year": "year", "yr": "year", "y": "year",
    "kg": "kilogram", "g": "gram", "t": "tonne", "tonne": "tonne", "lb": "pound",
    "n": "newton", "kn": "kilonewton",
    "j": "joule", "kj": "kilojoule", "mj": "megajoule",
    "w": "watt", "kw": "kilowatt", "mw": "megawatt", "hp": "hp",
    "k": "kelvin", "l": "liter", "m3": "meter**3", "m2": "meter**2",
    "degc": "kelvin",
}


def _units_module():
    from sympy.physics import units as u
    extra = {}
    try:
        extra["kilopascal"] = 1000 * u.pascal
        extra["megapascal"] = 10 ** 6 * u.pascal
        extra["kilonewton"] = 1000 * u.newton
        extra["kilojoule"] = 1000 * u.joule
        extra["megajoule"] = 10 ** 6 * u.joule
        extra["megawatt"] = 10 ** 6 * u.watt
        extra["kilowatt"] = 1000 * u.watt
        extra["tonne"] = 1000 * u.kilogram
        extra["hp"] = 745.699872 * u.watt
        extra["psi"] = u.psi
    except Exception:
        pass
    return u, extra


def _unit_expr(text: str):
    """Parse a unit string such as 'm3/h', 'kPa', 'mm/year', 'kg/(m*s)'."""
    import sympy
    u, extra = _units_module()
    text = (text or "").strip()
    if not text:
        return sympy.Integer(1)
    token = re.compile(r"[A-Za-z]+[0-9]*")

    def replace(match):
        word = match.group(0)
        power = ""
        base = word
        m = re.match(r"([A-Za-z]+)([23])$", word)
        if m and m.group(1).lower() in ("m", "mm", "cm", "ft", "in"):
            base, power = m.group(1), f"**{m.group(2)}"
        key = base.lower()
        name = _UNIT_ALIASES.get(key, key)
        if "**" in name:
            name, name_power = name.split("**")
            power = power or f"**{name_power}"
        return f"U['{name}']{power}"

    expression = token.sub(replace, text.replace("^", "**").replace("·", "*"))
    namespace = dict(extra)

    class _Lookup(dict):
        def __missing__(self, key):
            if key in namespace:
                return namespace[key]
            value = getattr(u, key, None)
            if value is None:
                raise ValueError(f"unknown unit '{key}'")
            return value

    return _safe_eval_units(expression, _Lookup())


def _safe_eval_units(expression: str, lookup) -> Any:
    """Evaluate a unit expression built only from U['name'], numbers, * / ** ( )."""
    import ast
    tree = ast.parse(expression, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.Mult, ast.Div, ast.Pow, ast.Constant,
               ast.Subscript, ast.Name, ast.Load, ast.UnaryOp, ast.USub, ast.Index
               if hasattr(ast, "Index") else ast.Constant)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError(f"unsupported unit syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id != "U":
            raise ValueError(f"unsupported unit name '{node.id}'")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Subscript):
            key = node.slice.value if isinstance(node.slice, ast.Constant) else node.slice.value.value
            return lookup[key]
        if isinstance(node, ast.UnaryOp):
            return -walk(node.operand)
        left, right = walk(node.left), walk(node.right)
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left ** right
    return walk(tree)


def convert(value: float, from_unit: str, to_unit: str) -> Dict[str, Any]:
    """Convert with a dimension check. Temperatures (degC/degF/K) are affine and
    handled explicitly - multiplying by a factor would be wrong."""
    temp = {"c": "C", "degc": "C", "°c": "C", "celsius": "C", "f": "F", "degf": "F",
            "°f": "F", "fahrenheit": "F", "k": "K", "kelvin": "K"}
    source, target = temp.get(from_unit.strip().lower()), temp.get(to_unit.strip().lower())
    if source and target and (source != "K" or target != "K"):
        kelvin = {"C": value + 273.15, "F": (value - 32) * 5 / 9 + 273.15, "K": value}[source]
        out = {"C": kelvin - 273.15, "F": (kelvin - 273.15) * 9 / 5 + 32, "K": kelvin}[target]
        back = {"C": out + 273.15, "F": (out - 32) * 5 / 9 + 273.15, "K": out}[target]
        return {"value": value, "from": from_unit, "to": to_unit, "result": round(out, 10),
                "verified": math.isclose(back, kelvin, rel_tol=1e-12, abs_tol=1e-9),
                "method": "affine temperature conversion"}
    from sympy.physics.units import convert_to
    import sympy
    a, b = _unit_expr(from_unit), _unit_expr(to_unit)
    converted = convert_to(value * a, b).simplify()
    ratio = sympy.simplify(converted / b)
    if ratio.free_symbols or not ratio.is_number:
        raise ValueError(f"cannot convert {from_unit} to {to_unit}: the dimensions differ")
    result = float(ratio)
    # verify by converting back
    back = float(sympy.simplify(convert_to(result * b, a) / a))
    return {"value": value, "from": from_unit, "to": to_unit, "result": result,
            "verified": math.isclose(back, value, rel_tol=1e-9, abs_tol=1e-12),
            "method": "sympy.physics.units", "dimension_check": "passed"}


# ------------------------------------------------------------- expressions --

_ALLOWED_FUNCTIONS = ("sqrt", "exp", "log", "ln", "sin", "cos", "tan", "asin", "acos",
                      "atan", "sinh", "cosh", "tanh", "Abs", "abs", "floor", "ceiling",
                      "pi", "E", "Rational", "factorial", "atan2", "log10", "Min", "Max")


MAX_LOG10 = 300          # results beyond 1e300 are refused, not computed


def _log10_bound(node) -> float:
    """Upper bound of log10(|value|) for a numeric expression tree, computed
    without evaluating it. Symbols count as 0 (checked later, once bound)."""
    import sympy
    if node.is_Number:
        value = abs(float(node)) if node != 0 else 0.0
        return math.log10(value) if value > 0 else 0.0
    if node.is_Symbol or not node.args:
        return 0.0
    if isinstance(node, sympy.Pow):
        base_bound = _log10_bound(node.args[0])
        exp_bound = _log10_bound(node.args[1])
        if exp_bound > 6:                      # exponent above a million
            if base_bound > 0.0 or node.args[0].is_Symbol:
                raise ValueError("that power is too large to compute")
            return 0.0
        exponent = 10 ** exp_bound
        return max(0.0, base_bound) * exponent
    if isinstance(node, sympy.Mul):
        return sum(max(0.0, _log10_bound(arg)) for arg in node.args)
    if isinstance(node, sympy.factorial):
        n_bound = _log10_bound(node.args[0])
        if n_bound > 3:
            raise ValueError("that factorial is too large to compute")
        n = 10 ** n_bound
        return n * math.log10(max(n, 2))
    return max((max(0.0, _log10_bound(arg)) for arg in node.args), default=0.0) + 1


def _check_magnitude(expr) -> None:
    if _log10_bound(expr) > MAX_LOG10:
        raise ValueError("the result would be too large to represent (above 1e300)")


def evaluate(expression: str, variables: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Exact + numeric evaluation of an arithmetic/symbolic expression.

    Supports '^' for powers, implicit multiplication ('2pi'), and the functions
    in _ALLOWED_FUNCTIONS. Refuses anything else - no attribute access, no
    names that are not declared variables."""
    import sympy
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application,
                                            parse_expr, standard_transformations)
    text = (expression or "").strip()
    if not text:
        raise ValueError("empty expression")
    if len(text) > 500:
        raise ValueError("expression too long")
    if re.search(r"__|\bimport\b|\blambda\b|[;\[\]{}@`$]|\.\s*[A-Za-z_]", text):
        raise ValueError("that expression contains syntax that is not allowed")
    namespace = {name: getattr(sympy, name) for name in _ALLOWED_FUNCTIONS if hasattr(sympy, name)}
    namespace.update({"ln": sympy.log, "abs": sympy.Abs, "log10": lambda x: sympy.log(x, 10),
                      "e": sympy.E, "pi": sympy.pi})
    symbols = {}
    for name, value in (variables or {}).items():
        symbols[name] = sympy.Symbol(name)
    namespace.update(symbols)
    transformations = standard_transformations + (implicit_multiplication_application, convert_xor)
    # SymPy's parser evaluates its transformed code; it gets NO builtins, only
    # the constructors it needs, and the text was pre-filtered above (no
    # dunders, imports, attribute access, brackets or statement separators).
    global_dict = {"__builtins__": {}, "Integer": sympy.Integer, "Float": sympy.Float,
                   "Symbol": sympy.Symbol, "Rational": sympy.Rational, "Mul": sympy.Mul,
                   "Add": sympy.Add, "Pow": sympy.Pow, "Function": sympy.Function}
    # Parse WITHOUT evaluating first and bound the size of the result: SymPy
    # computes integer powers exactly, so '9^9^9' would otherwise try to build
    # a number with 370 million digits and hang the thread.
    unevaluated = parse_expr(text, local_dict=namespace, global_dict=global_dict,
                             transformations=transformations, evaluate=False)
    _check_magnitude(unevaluated)
    parsed = parse_expr(text, local_dict=namespace, global_dict=global_dict,
                        transformations=transformations, evaluate=True)
    unknown = [str(s) for s in parsed.free_symbols if str(s) not in (variables or {})]
    if unknown:
        return {"expression": text, "symbolic": str(sympy.simplify(parsed)),
                "free_symbols": sorted(unknown), "numeric": None,
                "note": "the expression has undeclared symbols - supply their values to evaluate it"}
    substituted = parsed.subs({symbols[k]: v for k, v in (variables or {}).items()})
    exact = sympy.nsimplify(substituted) if substituted.is_Rational else sympy.simplify(substituted)
    magnitude = abs(float(sympy.N(substituted, 30))) if substituted.is_number else None
    if magnitude is not None and magnitude > 1e300:
        raise ValueError("result is too large to represent")
    numeric = float(sympy.N(substituted, 17))
    check = float(sympy.N(substituted, 50))           # independent high-precision path
    verified = math.isclose(numeric, check, rel_tol=1e-12, abs_tol=1e-15)
    return {"expression": text, "exact": str(exact), "numeric": numeric,
            "verified": verified, "method": "sympy (exact) + 50-digit re-evaluation"}


def solve(equation: str, unknown: str) -> Dict[str, Any]:
    """Solve 'lhs = rhs' for one unknown. Numeric roots are substituted back
    into the equation to verify them."""
    import sympy
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application,
                                            parse_expr, standard_transformations)
    if "=" not in equation:
        raise ValueError("write the equation as 'left = right'")
    left, right = equation.split("=", 1)
    x = sympy.Symbol(unknown)
    namespace = {name: getattr(sympy, name) for name in _ALLOWED_FUNCTIONS if hasattr(sympy, name)}
    namespace[unknown] = x
    transformations = standard_transformations + (implicit_multiplication_application, convert_xor)
    expr = parse_expr(left, local_dict=namespace, transformations=transformations) - \
        parse_expr(right, local_dict=namespace, transformations=transformations)
    roots = sympy.solve(sympy.Eq(expr, 0), x)
    results = []
    for root in roots:
        value = complex(sympy.N(root))
        residual = abs(complex(sympy.N(expr.subs(x, root))))
        results.append({"exact": str(root), "value": value.real if abs(value.imag) < 1e-12 else str(value),
                        "residual": residual, "verified": residual < 1e-9})
    return {"equation": equation, "unknown": unknown, "solutions": results,
            "method": "sympy.solve, each root substituted back"}


# ---------------------------------------------------------------- formulas --

G = 9.80665                  # m/s2, standard gravity


@dataclass
class FormulaResult:
    name: str
    formula: str
    inputs: Dict[str, str]
    result: float
    unit: str
    verification: Dict[str, Any]
    assumptions: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["verified"] = bool(self.verification.get("agrees"))
        return data


def _agree(a: float, b: float, rel: float = 1e-6) -> Dict[str, Any]:
    return {"primary": a, "check": b, "agrees": math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)}


def _q(value: str) -> Tuple[float, str]:
    """'50 m3/h' -> (50.0, 'm3/h')."""
    m = re.match(r"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(.*)$", str(value))
    if not m:
        raise ValueError(f"'{value}' is not a number with an optional unit")
    return float(m.group(1)), m.group(2).strip()


def _si(value: str, unit_default: str, si_unit: str) -> float:
    number, unit = _q(value)
    unit = unit or unit_default
    if unit.lower() == si_unit.lower():
        return number
    return convert(number, unit, si_unit)["result"]


def f_pump_power(inputs: Dict[str, str]) -> FormulaResult:
    q = _si(inputs["flow"], "m3/h", "m3/s")
    h = _si(inputs["head"], "m", "m")
    rho = _si(inputs.get("density", "1000 kg/m3"), "kg/m3", "kg/m3")
    eta = float(_q(inputs.get("efficiency", "0.7"))[0])
    eta = eta / 100 if eta > 1 else eta
    hydraulic = rho * G * q * h
    shaft = hydraulic / eta
    check = (rho * q) * (G * h) / eta                  # mass flow x specific energy
    return FormulaResult("pump shaft power", "P = rho * g * Q * H / eta",
                         {"Q": f"{q:.6g} m3/s", "H": f"{h:.6g} m", "rho": f"{rho:.6g} kg/m3",
                          "eta": f"{eta:.4g}"}, shaft / 1000, "kW",
                         _agree(shaft, check), ["g = 9.80665 m/s2"],
                         {"hydraulic_power_kW": hydraulic / 1000})


def f_head_from_pressure(inputs: Dict[str, str]) -> FormulaResult:
    dp = _si(inputs["pressure"], "bar", "Pa")
    rho = _si(inputs.get("density", "1000 kg/m3"), "kg/m3", "kg/m3")
    head = dp / (rho * G)
    check = (dp / 1e5) * 1e5 / rho / G
    return FormulaResult("head from pressure", "H = dP / (rho * g)",
                         {"dP": f"{dp:.6g} Pa", "rho": f"{rho:.6g} kg/m3"}, head, "m",
                         _agree(head, check), ["g = 9.80665 m/s2"])


def f_reynolds(inputs: Dict[str, str]) -> FormulaResult:
    rho = _si(inputs.get("density", "1000 kg/m3"), "kg/m3", "kg/m3")
    v = _si(inputs["velocity"], "m/s", "m/s")
    d = _si(inputs["diameter"], "m", "m")
    mu = float(_q(inputs.get("viscosity", "0.001"))[0])        # Pa.s
    re_ = rho * v * d / mu
    check = v * d / (mu / rho)                                   # V*D/nu
    regime = "laminar" if re_ < 2300 else ("transitional" if re_ < 4000 else "turbulent")
    return FormulaResult("Reynolds number", "Re = rho * V * D / mu",
                         {"rho": f"{rho:.6g} kg/m3", "V": f"{v:.6g} m/s", "D": f"{d:.6g} m",
                          "mu": f"{mu:.6g} Pa.s"}, re_, "-", _agree(re_, check),
                         ["Newtonian fluid"], {"regime": regime})


def _swamee_jain(re_: float, rel_rough: float) -> float:
    return 0.25 / (math.log10(rel_rough / 3.7 + 5.74 / re_ ** 0.9)) ** 2


def _colebrook(re_: float, rel_rough: float) -> float:
    from scipy.optimize import brentq
    return brentq(lambda f: 1 / math.sqrt(f) + 2 * math.log10(rel_rough / 3.7 + 2.51 / (re_ * math.sqrt(f))),
                  1e-5, 1.0)


def f_pressure_drop(inputs: Dict[str, str]) -> FormulaResult:
    rho = _si(inputs.get("density", "1000 kg/m3"), "kg/m3", "kg/m3")
    mu = float(_q(inputs.get("viscosity", "0.001"))[0])
    d = _si(inputs["diameter"], "m", "m")
    length = _si(inputs["length"], "m", "m")
    if "velocity" in inputs:
        v = _si(inputs["velocity"], "m/s", "m/s")
    else:
        q = _si(inputs["flow"], "m3/h", "m3/s")
        v = q / (math.pi * d ** 2 / 4)
    roughness = _si(inputs.get("roughness", "0.045 mm"), "mm", "m")
    re_ = rho * v * d / mu
    if re_ < 2300:
        f = 64 / re_
        check_f = 64 / re_
        method = "laminar f = 64/Re"
    else:
        f = _swamee_jain(re_, roughness / d)
        check_f = _colebrook(re_, roughness / d)
        method = "Swamee-Jain, checked against Colebrook-White (SciPy brentq)"
    dp = f * (length / d) * rho * v ** 2 / 2
    dp_check = check_f * (length / d) * rho * v ** 2 / 2
    return FormulaResult("pipe pressure drop (Darcy-Weisbach)",
                         "dP = f * (L/D) * rho * V^2 / 2",
                         {"D": f"{d:.6g} m", "L": f"{length:.6g} m", "V": f"{v:.6g} m/s",
                          "rho": f"{rho:.6g} kg/m3", "mu": f"{mu:.6g} Pa.s",
                          "roughness": f"{roughness:.6g} m"},
                         dp / 1e5, "bar", _agree(dp, dp_check, rel=0.02),
                         ["single-phase, incompressible, fully developed flow", method],
                         {"reynolds": re_, "friction_factor": f, "pressure_drop_Pa": dp})


def f_heat_duty(inputs: Dict[str, str]) -> FormulaResult:
    m = _si(inputs["mass_flow"], "kg/s", "kg/s")
    cp = float(_q(inputs.get("cp", "4.186"))[0])            # kJ/kg.K
    dt = float(_q(inputs["delta_t"])[0])                    # K (= degC difference)
    q = m * cp * dt
    check = (m * 3600) * cp * dt / 3600
    return FormulaResult("heat duty", "Q = m * cp * dT",
                         {"m": f"{m:.6g} kg/s", "cp": f"{cp:.6g} kJ/kg.K", "dT": f"{dt:.6g} K"},
                         q, "kW", _agree(q, check), ["no phase change"])


def f_lmtd(inputs: Dict[str, str]) -> FormulaResult:
    th_in, th_out = float(_q(inputs["hot_in"])[0]), float(_q(inputs["hot_out"])[0])
    tc_in, tc_out = float(_q(inputs["cold_in"])[0]), float(_q(inputs["cold_out"])[0])
    counter = str(inputs.get("arrangement", "counter")).lower().startswith("counter")
    d1, d2 = (th_in - tc_out, th_out - tc_in) if counter else (th_in - tc_in, th_out - tc_out)
    if d1 <= 0 or d2 <= 0:
        raise ValueError("temperature cross - check the inlet/outlet temperatures")
    lmtd = d1 if math.isclose(d1, d2) else (d1 - d2) / math.log(d1 / d2)
    from scipy.integrate import quad
    # independent check: LMTD is the harmonic-log mean; 1/LMTD = avg of 1/dT over a linear dT profile
    check = d1 if math.isclose(d1, d2) else (d2 - d1) / quad(lambda x: 1 / x, d1, d2)[0]
    return FormulaResult("log mean temperature difference",
                         "LMTD = (dT1 - dT2) / ln(dT1/dT2)",
                         {"dT1": f"{d1:.6g} K", "dT2": f"{d2:.6g} K",
                          "arrangement": "counter-current" if counter else "co-current"},
                         lmtd, "K", _agree(lmtd, check), [])


def f_wall_thickness(inputs: Dict[str, str]) -> FormulaResult:
    p = _si(inputs["pressure"], "bar", "Pa")
    d = _si(inputs["diameter"], "mm", "m")
    s = _si(inputs["allowable_stress"], "MPa", "Pa")
    e = float(_q(inputs.get("joint_efficiency", "1.0"))[0])
    ca = _si(inputs.get("corrosion_allowance", "0 mm"), "mm", "m")
    t = p * d / (2 * s * e) + ca
    check = (p / 1e6) * (d * 1000) / (2 * (s / 1e6) * e) / 1000 + ca
    return FormulaResult("minimum wall thickness (Barlow)",
                         "t = P * D / (2 * S * E) + CA",
                         {"P": f"{p:.6g} Pa", "D": f"{d:.6g} m", "S": f"{s:.6g} Pa",
                          "E": f"{e:.4g}", "CA": f"{ca:.6g} m"},
                         t * 1000, "mm", _agree(t, check),
                         ["thin-wall Barlow formula - a screening value, not a code calculation "
                          "(ASME B31.3 / VIII apply their own coefficients)"])


def f_mawp(inputs: Dict[str, str]) -> FormulaResult:
    t = _si(inputs["thickness"], "mm", "m")
    ca = _si(inputs.get("corrosion_allowance", "0 mm"), "mm", "m")
    d = _si(inputs["diameter"], "mm", "m")
    s = _si(inputs["allowable_stress"], "MPa", "Pa")
    e = float(_q(inputs.get("joint_efficiency", "1.0"))[0])
    p = 2 * s * e * (t - ca) / d
    check = (2 * (s / 1e6) * e * ((t - ca) * 1000) / (d * 1000)) * 1e6
    return FormulaResult("maximum allowable working pressure (Barlow)",
                         "P = 2 * S * E * (t - CA) / D",
                         {"t": f"{t:.6g} m", "CA": f"{ca:.6g} m", "D": f"{d:.6g} m",
                          "S": f"{s:.6g} Pa", "E": f"{e:.4g}"},
                         p / 1e5, "bar", _agree(p, check),
                         ["thin-wall screening value - confirm against the governing code"])


def f_corrosion(inputs: Dict[str, str]) -> FormulaResult:
    t_prev = _si(inputs["previous_thickness"], "mm", "mm")
    t_now = _si(inputs["current_thickness"], "mm", "mm")
    years = float(_q(inputs["years"])[0])
    t_min = _si(inputs.get("minimum_thickness", "0 mm"), "mm", "mm")
    if years <= 0:
        raise ValueError("the interval between readings must be positive")
    rate = (t_prev - t_now) / years
    remaining = (t_now - t_min) / rate if rate > 0 else math.inf
    check_rate = (t_prev - t_now) * 1000 / (years * 1000)
    return FormulaResult("corrosion rate and remaining life",
                         "CR = (t_prev - t_now) / years ; RL = (t_now - t_min) / CR",
                         {"t_prev": f"{t_prev:.6g} mm", "t_now": f"{t_now:.6g} mm",
                          "interval": f"{years:.6g} years", "t_min": f"{t_min:.6g} mm"},
                         rate, "mm/year", _agree(rate, check_rate),
                         ["uniform, linear metal loss between the two readings (API 510/570 short-term rate)"],
                         {"remaining_life_years": remaining,
                          "half_life_inspection_interval_years": remaining / 2
                          if math.isfinite(remaining) else None})


def f_velocity(inputs: Dict[str, str]) -> FormulaResult:
    q = _si(inputs["flow"], "m3/h", "m3/s")
    d = _si(inputs["diameter"], "mm", "m")
    area = math.pi * d ** 2 / 4
    v = q / area
    check = 4 * q / (math.pi * d * d)
    return FormulaResult("flow velocity", "V = Q / (pi * D^2 / 4)",
                         {"Q": f"{q:.6g} m3/s", "D": f"{d:.6g} m"}, v, "m/s", _agree(v, check), [])


def f_npsh_available(inputs: Dict[str, str]) -> FormulaResult:
    p_surface = _si(inputs["suction_pressure"], "bar", "Pa")       # absolute
    p_vapour = _si(inputs["vapour_pressure"], "bar", "Pa")          # absolute
    rho = _si(inputs.get("density", "1000 kg/m3"), "kg/m3", "kg/m3")
    z = _si(inputs.get("static_head", "0 m"), "m", "m")
    hf = _si(inputs.get("friction_loss", "0 m"), "m", "m")
    npsha = (p_surface - p_vapour) / (rho * G) + z - hf
    check = p_surface / (rho * G) - p_vapour / (rho * G) + z - hf
    return FormulaResult("NPSH available", "NPSHa = (Ps - Pv)/(rho g) + z - hf",
                         {"Ps": f"{p_surface:.6g} Pa abs", "Pv": f"{p_vapour:.6g} Pa abs",
                          "rho": f"{rho:.6g} kg/m3", "z": f"{z:.6g} m", "hf": f"{hf:.6g} m"},
                         npsha, "m", _agree(npsha, check), ["pressures are absolute"])


FORMULAS: Dict[str, Tuple[Callable[[Dict[str, str]], FormulaResult], List[str], List[str]]] = {
    "pump_power": (f_pump_power, ["flow", "head"], ["density", "efficiency"]),
    "head_from_pressure": (f_head_from_pressure, ["pressure"], ["density"]),
    "reynolds": (f_reynolds, ["velocity", "diameter"], ["density", "viscosity"]),
    "pressure_drop": (f_pressure_drop, ["diameter", "length"],
                      ["flow", "velocity", "density", "viscosity", "roughness"]),
    "heat_duty": (f_heat_duty, ["mass_flow", "delta_t"], ["cp"]),
    "lmtd": (f_lmtd, ["hot_in", "hot_out", "cold_in", "cold_out"], ["arrangement"]),
    "wall_thickness": (f_wall_thickness, ["pressure", "diameter", "allowable_stress"],
                       ["joint_efficiency", "corrosion_allowance"]),
    "mawp": (f_mawp, ["thickness", "diameter", "allowable_stress"],
             ["joint_efficiency", "corrosion_allowance"]),
    "corrosion": (f_corrosion, ["previous_thickness", "current_thickness", "years"],
                  ["minimum_thickness"]),
    "velocity": (f_velocity, ["flow", "diameter"], []),
    "npsh_available": (f_npsh_available, ["suction_pressure", "vapour_pressure"],
                       ["density", "static_head", "friction_loss"]),
}


def parse_inputs(text: str) -> Dict[str, str]:
    """'flow=50 m3/h, head=30 m' -> {'flow': '50 m3/h', 'head': '30 m'}."""
    result = {}
    for part in re.split(r"[,;]\s*(?=[A-Za-z_][A-Za-z_0-9 ]*\s*[=:])", text or ""):
        if not part.strip():
            continue
        key, _, value = part.partition("=") if "=" in part else part.partition(":")
        result[key.strip().lower().replace(" ", "_")] = value.strip()
    return result


def formula(name: str, inputs: Dict[str, str]) -> Dict[str, Any]:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    if key not in FORMULAS:
        return {"error": f"unknown formula '{name}'", "available": sorted(FORMULAS)}
    function, required, optional = FORMULAS[key]
    missing = [r for r in required if r not in inputs]
    if missing and not (key == "pressure_drop" and "flow" not in inputs and "velocity" not in inputs):
        if missing:
            return {"error": f"missing input(s): {', '.join(missing)}",
                    "required": required, "optional": optional}
    if key == "pressure_drop" and "flow" not in inputs and "velocity" not in inputs:
        return {"error": "give either 'flow' or 'velocity'", "required": required, "optional": optional}
    return function(inputs).as_dict()


def catalogue() -> List[Dict[str, Any]]:
    return [{"formula": name, "required": req, "optional": opt}
            for name, (_, req, opt) in sorted(FORMULAS.items())]
