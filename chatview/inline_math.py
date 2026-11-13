"""Lightweight inline LaTeX math to Unicode conversion for Sublime Text chat view."""

import re

_PROTECTED_PATTERN = re.compile(
    r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^`\n]+`+)'
)
_SINGLE_LINE_BLOCK_MATH_RE = re.compile(
    r'(?<!\\)\$\$((?:\\.|[^$\\\n])+?)\$\$'
)
_INLINE_MATH_RE = re.compile(
    r'(?<![\$\\\d])\$(?!\s)((?:\\.|[^$\\\n])+?)(?<!\s)\$(?![0-9\$])'
)
_PAREN_MATH_RE = re.compile(
    r'(?<!\\)\\\(((?:\\.|[^\\])+?)\\\)'
)
_COMMAND_RE = re.compile(r'\\([a-zA-Z]+)')

COMMAND_MAP = {
    # Relations & Inequalities
    'ge': '≥', 'geq': '≥',
    'le': '≤', 'leq': '≤',
    'ne': '≠', 'neq': '≠',
    'approx': '≈', 'equiv': '≡', 'sim': '∼', 'simeq': '≃', 'cong': '≅',
    'll': '≪', 'gg': '≫',
    'pm': '±', 'mp': '∓',
    'propto': '∝', 'parallel': '∥', 'perp': '⊥',

    # Greek lowercase
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ',
    'epsilon': 'ε', 'varepsilon': 'ε', 'zeta': 'ζ', 'eta': 'η',
    'theta': 'θ', 'vartheta': 'ϑ', 'iota': 'ι', 'kappa': 'κ',
    'lambda': 'λ', 'mu': 'μ', 'nu': 'ν', 'xi': 'ξ',
    'pi': 'π', 'varpi': 'ϖ', 'rho': 'ρ', 'varrho': 'ϱ',
    'sigma': 'σ', 'varsigma': 'ς', 'tau': 'τ', 'upsilon': 'υ',
    'phi': 'φ', 'varphi': 'ϕ', 'chi': 'χ', 'psi': 'ψ', 'omega': 'ω',

    # Greek uppercase
    'Gamma': 'Γ', 'Delta': 'Δ', 'Theta': 'Θ', 'Lambda': 'Λ',
    'Xi': 'Ξ', 'Pi': 'Π', 'Sigma': 'Σ', 'Upsilon': 'Υ',
    'Phi': 'Φ', 'Psi': 'Ψ', 'Omega': 'Ω',

    # Arithmetic, Logic, Sets
    'times': '×', 'div': '÷', 'cdot': '·', 'circ': '°', 'degree': '°', 'bullet': '•',
    'in': '∈', 'notin': '∉', 'ni': '∋',
    'subset': '⊂', 'subseteq': '⊆', 'supset': '⊃', 'supseteq': '⊇',
    'cap': '∩', 'cup': '∪', 'setminus': '∖', 'emptyset': '∅',
    'forall': '∀', 'exists': '∃', 'nexists': '∄',
    'neg': '¬', 'land': '∧', 'lor': '∨',
    'to': '→', 'rightarrow': '→', 'leftarrow': '←', 'gets': '←',
    'Rightarrow': '⇒', 'Leftarrow': '⇐', 'Leftrightarrow': '⇔', 'iff': '⇔',
    'implies': '⇒', 'impliedby': '⇐',
    'uparrow': '↑', 'downarrow': '↓',
    'infty': '∞', 'partial': '∂', 'nabla': '∇', 'hbar': 'ℏ', 'ell': 'ℓ',
    'sum': '∑', 'prod': '∏', 'int': '∫', 'oint': '∮',
    'dots': '…', 'cdots': '⋯', 'ldots': '…',
    'quad': '  ', 'qquad': '    ', 'mid': '∣', 'vert': '∣',
    'Vert': '‖', 'lVert': '‖', 'rVert': '‖',
    'langle': '⟨', 'rangle': '⟩',
    'lfloor': '⌊', 'rfloor': '⌋', 'lceil': '⌈', 'rceil': '⌉',
    'odot': '⊙', 'oplus': '⊕', 'otimes': '⊗',
    'top': '⊤', 'bot': '⊥',
}

BLACKBOARD_BOLD = {
    'R': 'ℝ', 'N': 'ℕ', 'Z': 'ℤ', 'Q': 'ℚ', 'C': 'ℂ', 'P': 'ℙ', 'E': '𝔼'
}

MATHCAL_MAP = {
    'A': '𝒜', 'B': 'ℬ', 'C': '𝒞', 'D': '𝒟', 'E': 'ℰ', 'F': 'ℱ', 'G': '𝒢',
    'H': 'ℋ', 'I': 'ℐ', 'J': '𝒥', 'K': '𝒦', 'L': 'ℒ', 'M': 'ℳ', 'N': '𝒩',
    'O': '𝒪', 'P': '𝒫', 'Q': '𝒬', 'R': 'ℛ', 'S': '𝒮', 'T': '𝒯', 'U': '𝒰',
    'V': '𝒱', 'W': '𝒲', 'X': '𝒳', 'Y': '𝒴', 'Z': '𝒵'
}

ACCENT_MAP = {
    'hat': '\u0302',
    'widehat': '\u0302',
    'bar': '\u0304',
    'tilde': '\u0303',
    'widetilde': '\u0303',
    'vec': '\u20d7',
    'dot': '\u0307',
    'ddot': '\u0308',
}

SUPERSCRIPT_MAP = {
    '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
    '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
    '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾',
    'a': 'ᵃ', 'b': 'ᵇ', 'c': 'ᶜ', 'd': 'ᵈ', 'e': 'ᵉ',
    'f': 'ᶠ', 'g': 'ᵍ', 'h': 'ʰ', 'i': 'ⁱ', 'j': 'ʲ',
    'k': 'ᵏ', 'l': 'ˡ', 'm': 'ᵐ', 'n': 'ⁿ', 'o': 'ᵒ',
    'p': 'ᵖ', 'r': 'ʳ', 's': 'ˢ', 't': 'ᵗ', 'u': 'ᵘ',
    'v': 'ᵛ', 'w': 'ʷ', 'x': 'ˣ', 'y': 'ʸ', 'z': 'ᶻ',
    'A': 'ᴬ', 'B': 'ᴮ', 'D': 'ᴰ', 'E': 'ᴱ', 'H': 'ᴴ',
    'I': 'ᴵ', 'J': 'ᴶ', 'K': 'ᴷ', 'L': 'ᴸ', 'M': 'ᴹ',
    'N': 'ᴺ', 'P': 'ᴾ', 'R': 'ᴿ', 'T': 'ᵀ', 'U': 'ᵁ',
    'W': 'ᵂ',
}

SUBSCRIPT_MAP = {
    '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄',
    '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
    '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎',
    'a': 'ₐ', 'e': 'ₑ', 'h': 'ₕ', 'i': 'ᵢ', 'j': 'ⱼ',
    'k': 'ₖ', 'l': 'ₗ', 'm': 'ₘ', 'n': 'ₙ', 'o': 'ₒ',
    'p': 'ₚ', 'r': 'ᵣ', 's': 'ₛ', 't': 'ₜ', 'u': 'ᵤ',
    'v': 'ᵥ', 'x': 'ₓ'
}

FRACTION_MAP = {
    ('1', '2'): '½', ('1', '3'): '⅓', ('2', '3'): '⅔',
    ('1', '4'): '¼', ('3', '4'): '¾', ('1', '5'): '⅕',
    ('2', '5'): '⅖', ('3', '5'): '⅗', ('4', '5'): '⅘',
    ('1', '6'): '⅙', ('5', '6'): '⅚', ('1', '8'): '⅛',
    ('3', '8'): '⅜', ('5', '8'): '⅝', ('7', '8'): '⅞'
}


def _needs_fraction_parentheses(s):
    s = s.strip()
    if not s:
        return False
    if (s.startswith('(') and s.endswith(')')) or (s.startswith('[') and s.endswith(']')):
        depth = 0
        for i, c in enumerate(s):
            if c in '([':
                depth += 1
            elif c in ')]':
                depth -= 1
                if depth == 0 and i < len(s) - 1:
                    break
        else:
            if depth == 0:
                return False
    if any(op in s for op in ('+', '-', '±', '∓', '=', '<', '>', '≤', '≥', '≠')):
        return True
    if ' ' in s:
        return True
    return False


def _format_frac_part(part):
    part = part.strip()
    if _needs_fraction_parentheses(part):
        return f"({part})"
    return part


def latex_to_unicode(latex):
    """Convert an inline LaTeX formula string into a readable Unicode equivalent."""
    if not latex:
        return ""
    s = latex.strip()

    # Escaped punctuation
    s = s.replace(r'\%', '%').replace(r'\&', '&').replace(r'\$', '$').replace(r'\_', '_')
    s = s.replace(r'\|', '∥')
    s = s.replace(r'\{', '\uFFF0').replace(r'\}', '\uFFF1').replace(r'\,', ' ').replace(r'\;', ' ')

    # Strip sizing delimiters \left and \right
    s = re.sub(r'\\(left|right)\b', '', s)

    # Formatting macros: \text{...}, \mathbf{...}, \mathrm{...} (resolve nested from inside out)
    for _ in range(5):
        if not re.search(r'\\(text|mathrm|mathbf|mathit|textbf|textit|operatorname)\{', s):
            break
        s = re.sub(
            r'\\(text|mathrm|mathbf|mathit|textbf|textit|operatorname)\{([^{}]+)\}',
            r'\2',
            s
        )
    s = re.sub(r' {2,}', ' ', s)

    # Combining accents: \hat{y}, \bar{x}, \vec{w}, \dot{x}, \ddot{x}, \tilde{x}
    def _apply_accent(name, inner):
        accent = ACCENT_MAP.get(name, '')
        rendered = latex_to_unicode(inner)
        if not rendered or not accent:
            return rendered
        return f"{rendered}{accent}"

    s = re.sub(
        r'\\(hat|widehat|bar|tilde|widetilde|vec|dot|ddot)\{([^{}]+)\}',
        lambda m: _apply_accent(m.group(1), m.group(2)),
        s
    )
    s = re.sub(
        r'\\(hat|widehat|bar|tilde|widetilde|vec|dot|ddot)\s*(\\[a-zA-Z]+|[a-zA-Z0-9])',
        lambda m: _apply_accent(m.group(1), m.group(2)),
        s
    )

    # Script / Calligraphic \mathcal{L}
    s = re.sub(
        r'\\mathcal\{([^{}]+)\}',
        lambda m: ''.join(MATHCAL_MAP.get(c, c) for c in m.group(1)),
        s
    )
    s = re.sub(
        r'\\mathcal\s*([A-Za-z])',
        lambda m: MATHCAL_MAP.get(m.group(1), m.group(1)),
        s
    )

    # Fractions \frac{num}{den} (resolve from innermost out)
    for _ in range(5):
        if r'\frac' not in s:
            break

        def replace_frac(m):
            num = latex_to_unicode(m.group(1))
            den = latex_to_unicode(m.group(2))
            if (num, den) in FRACTION_MAP:
                return FRACTION_MAP[(num, den)]
            return f"{_format_frac_part(num)}/{_format_frac_part(den)}"

        s = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', replace_frac, s)

    # Square roots \sqrt[n]{x} and \sqrt{x}
    s = re.sub(
        r'\\sqrt\[([^{}]+)\]\{([^{}]+)\}',
        lambda m: (
            f"{''.join(SUPERSCRIPT_MAP.get(c, c) for c in m.group(1))}"
            f"√({latex_to_unicode(m.group(2))})"
        ),
        s
    )
    s = re.sub(r'\\sqrt\{([^{}]+)\}', lambda m: f"√({latex_to_unicode(m.group(1))})", s)

    # Blackboard bold \mathbb{R}
    s = re.sub(
        r'\\mathbb\{([A-Za-z])\}',
        lambda m: BLACKBOARD_BOLD.get(m.group(1), m.group(1)),
        s
    )

    # Function names like \log, \sin
    s = re.sub(r'\\(log|ln|exp|sin|cos|tan|min|max|lim|det)(?![a-zA-Z])', r'\1', s)

    # Command symbols with word boundaries
    s = _COMMAND_RE.sub(lambda m: COMMAND_MAP.get(m.group(1), m.group(0)), s)

    # Superscripts
    def replace_sup(m):
        content = m.group(1) or m.group(2)
        if all(c in SUPERSCRIPT_MAP for c in content):
            return ''.join(SUPERSCRIPT_MAP[c] for c in content)
        if len(content) > 1:
            return f"^({content})"
        return f"^{content}"

    s = re.sub(r'\^\{([^{}]+)\}|\^([0-9a-zA-Z+-=()])', replace_sup, s)

    # Subscripts
    def replace_sub(m):
        content = m.group(1) or m.group(2)
        if all(c in SUBSCRIPT_MAP for c in content):
            return ''.join(SUBSCRIPT_MAP[c] for c in content)
        if len(content) > 1:
            return f"_({content})"
        return f"_{content}"

    s = re.sub(r'_\{([^{}]+)\}|_([0-9a-zA-Z+-=()])', replace_sub, s)

    # Remove grouping braces and restore literal escaped braces
    s = re.sub(r'\{([^{}]+)\}', r'\1', s)
    s = s.replace('\uFFF0', '{').replace('\uFFF1', '}')

    return s


def render_inline_math(markdown_text):
    """Scan markdown for inline and single-line block math expressions.

    Replaces them with Unicode equivalents.
    Fenced code blocks, code spans, and multi-line block math are left untouched.
    """
    if not markdown_text or ('$' not in markdown_text and r'\(' not in markdown_text):
        return markdown_text

    parts = _PROTECTED_PATTERN.split(markdown_text)
    for i in range(0, len(parts), 2):
        parts[i] = _SINGLE_LINE_BLOCK_MATH_RE.sub(
            lambda m: latex_to_unicode(m.group(1)),
            parts[i]
        )
        parts[i] = _INLINE_MATH_RE.sub(
            lambda m: latex_to_unicode(m.group(1)),
            parts[i]
        )
        if r'\(' in parts[i]:
            parts[i] = _PAREN_MATH_RE.sub(
                lambda m: latex_to_unicode(m.group(1)),
                parts[i]
            )
    return ''.join(parts)
