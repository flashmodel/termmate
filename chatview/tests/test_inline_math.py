import sys
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("sublime", MagicMock())

from chatview.inline_math import latex_to_unicode, render_inline_math
from chatview.md_render import MarkdownFormatter


class TestInlineMath(unittest.TestCase):
    """Test suite for inline LaTeX to Unicode conversion and markdown rendering."""

    def test_relational_operators(self):
        self.assertEqual(latex_to_unicode(r"\ge 2"), "≥ 2")
        self.assertEqual(latex_to_unicode(r"x \le 10"), "x ≤ 10")
        self.assertEqual(latex_to_unicode(r"a \ne b"), "a ≠ b")
        self.assertEqual(latex_to_unicode(r"\pi \approx 3.14"), "π ≈ 3.14")
        self.assertEqual(latex_to_unicode(r"\pm 5\%"), "± 5%")
        self.assertEqual(latex_to_unicode(r"a \ll b \gg c"), "a ≪ b ≫ c")

    def test_greek_letters(self):
        self.assertEqual(latex_to_unicode(r"\alpha + \beta = \gamma"), "α + β = γ")
        self.assertEqual(latex_to_unicode(r"\Delta x \to 0"), "Δ x → 0")
        self.assertEqual(latex_to_unicode(r"\lambda, \mu, \sigma, \omega"), "λ, μ, σ, ω")
        self.assertEqual(latex_to_unicode(r"\Omega, \Theta, \Phi"), "Ω, Θ, Φ")

    def test_superscripts_and_subscripts(self):
        self.assertEqual(latex_to_unicode(r"O(n^2)"), "O(n²)")
        self.assertEqual(latex_to_unicode(r"x_1 + x_2 = x_i"), "x₁ + x₂ = xᵢ")
        self.assertEqual(latex_to_unicode(r"2^{10}"), "2¹⁰")
        self.assertEqual(latex_to_unicode(r"a_{max}"), "aₘₐₓ")
        self.assertEqual(latex_to_unicode(r"x_{total}"), "xₜₒₜₐₗ")
        self.assertEqual(latex_to_unicode(r"x^{a + b}"), "x^(a + b)")
        self.assertEqual(latex_to_unicode(r"x_{bad}"), "x_(bad)")

    def test_fractions_and_roots(self):
        self.assertEqual(latex_to_unicode(r"\frac{1}{2}"), "½")
        self.assertEqual(latex_to_unicode(r"\frac{3}{4}"), "¾")
        self.assertEqual(latex_to_unicode(r"\frac{a}{b}"), "a/b")
        self.assertEqual(latex_to_unicode(r"\frac{x + 1}{y - 1}"), "(x + 1)/(y - 1)")
        self.assertEqual(latex_to_unicode(r"\frac{x + y}{2}"), "(x + y)/2")
        self.assertEqual(latex_to_unicode(r"\frac{1}{x + y}"), "1/(x + y)")
        self.assertEqual(latex_to_unicode(r"\sqrt{x^2 + 1}"), "√(x² + 1)")
        self.assertEqual(latex_to_unicode(r"\sqrt[3]{8}"), "³√(8)")

    def test_sets_and_logic(self):
        self.assertEqual(latex_to_unicode(r"x \in \mathbb{R}"), "x ∈ ℝ")
        self.assertEqual(latex_to_unicode(r"\forall x \in S, \exists y"), "∀ x ∈ S, ∃ y")
        self.assertEqual(latex_to_unicode(r"A \cap B \subseteq C"), "A ∩ B ⊆ C")
        self.assertEqual(latex_to_unicode(r"P \implies Q \iff R"), "P ⇒ Q ⇔ R")

    def test_escaped_characters_and_sizing(self):
        self.assertEqual(latex_to_unicode(r"\% \& \$ \_"), "% & $ _")
        self.assertEqual(latex_to_unicode(r"\{a, b\}"), "{a, b}")
        self.assertEqual(latex_to_unicode(r"\|x\|"), "∥x∥")
        self.assertEqual(latex_to_unicode(r"(\frac{1}{2})"), "(½)")
        self.assertEqual(latex_to_unicode(r"\text{count} \le 5"), "count ≤ 5")
        self.assertEqual(latex_to_unicode(r"\log_2(n) + \sin(\theta)"), "log₂(n) + sin(θ)")

    def test_combining_accents(self):
        self.assertEqual(latex_to_unicode(r"\hat{y}"), "ŷ")
        self.assertEqual(latex_to_unicode(r"\hat y"), "ŷ")
        self.assertEqual(latex_to_unicode(r"\widehat{y}"), "ŷ")
        self.assertEqual(latex_to_unicode(r"\bar{x}"), "x̄")
        self.assertEqual(latex_to_unicode(r"\vec{w}"), "w⃗")
        self.assertEqual(latex_to_unicode(r"\dot{x} + \ddot{x}"), "ẋ + ẍ")
        self.assertEqual(latex_to_unicode(r"\tilde{x}"), "x̃")
        self.assertEqual(latex_to_unicode(r"\hat{\theta}_0"), "θ̂₀")
        self.assertEqual(latex_to_unicode(r"\hat{y}_i"), "ŷᵢ")
        self.assertEqual(latex_to_unicode(r"\vec{w}^T"), "w⃗ᵀ")

    def test_script_and_calligraphy(self):
        self.assertEqual(latex_to_unicode(r"\mathcal{L}"), "ℒ")
        self.assertEqual(latex_to_unicode(r"\mathcal L"), "ℒ")
        self.assertEqual(latex_to_unicode(r"\mathcal{D}"), "𝒟")
        self.assertEqual(latex_to_unicode(r"\mathcal{N}(\mu, \sigma^2)"), "𝒩(μ, σ²)")
        self.assertEqual(latex_to_unicode(r"\mathcal{H}"), "ℋ")

    def test_delimiters_and_advanced_operators(self):
        self.assertEqual(latex_to_unicode(r"\langle x, y \rangle"), "⟨ x, y ⟩")
        self.assertEqual(latex_to_unicode(r"\Vert x \Vert"), "‖ x ‖")
        self.assertEqual(
            latex_to_unicode(r"\lfloor x \rfloor \le \lceil x \rceil"),
            "⌊ x ⌋ ≤ ⌈ x ⌉"
        )
        self.assertEqual(latex_to_unicode(r"\hbar \omega"), "ℏ ω")
        self.assertEqual(latex_to_unicode(r"A \odot B \oplus C \otimes D"), "A ⊙ B ⊕ C ⊗ D")

    def test_parentheses_delimiters(self):
        md = (
            r"The loss \( \mathcal{L} \) and prediction "
            r"\( \hat{y} = \mathbf{w}^T \mathbf{x} + b \)"
        )
        res = render_inline_math(md)
        self.assertIn("ℒ", res)
        self.assertIn("ŷ = wᵀ x + b", res)

    def test_code_spans_and_blocks_protected(self):
        md = (
            "Inline `$code$` and ```\necho $VAR\n``` should not change, "
            "but $\\ge 2$ should."
        )
        res = render_inline_math(md)
        self.assertIn("`$code$`", res)
        self.assertIn("echo $VAR", res)
        self.assertIn("≥ 2", res)

    def test_currency_and_escaped_dollars(self):
        md = "Costs $50 and $20, or \\$100 total."
        self.assertEqual(render_inline_math(md), md)

        md_bold = "**$65.76**, experiencing a sharp decline of **$29.77 (-31.16%)**"
        self.assertEqual(render_inline_math(md_bold), md_bold)

    def test_single_line_block_math(self):
        self.assertEqual(render_inline_math(r"$$ E = mc^2 $$"), "E = mc²")

        md = (
            r"$$\text{Foundry Valuation} = 190 \text{ billion USD (base revenue)} "
            r"\times 3.5 = \mathbf{665 \text{ billion ~ 700 billion USD}}$$"
        )
        self.assertEqual(
            render_inline_math(md),
            "Foundry Valuation = 190 billion USD (base revenue) "
            "× 3.5 = 665 billion ~ 700 billion USD"
        )

        dcf = (
            r"$$\text{DCF Value} = \frac{100 \text{ billion}}{9\% - 3\%} "
            r"\approx \mathbf{2,400 \text{ billion ~ 2,600 billion}}$$"
        )
        self.assertEqual(
            render_inline_math(dcf),
            "DCF Value = (100 billion)/(9% - 3%) ≈ 2,400 billion ~ 2,600 billion"
        )

        multi = r"$$x = 1$$ and $$y = 2$$"
        self.assertEqual(render_inline_math(multi), "x = 1 and y = 2")

    def test_multiline_block_math_protected(self):
        md = "$$\n\\int_0^\\infty f(x) dx\n$$\n\nInline: $\\alpha = 1$"
        res = render_inline_math(md)
        self.assertIn("$$\n\\int_0^\\infty f(x) dx\n$$", res)
        self.assertIn("Inline: α = 1", res)

        # Single-line block math preceding multi-line block math
        edge = "$$x = 1$$\n$$\n\\int_0^\\infty f(x) dx\n$$"
        res_edge = render_inline_math(edge)
        self.assertIn("x = 1\n", res_edge)
        self.assertIn("$$\n\\int_0^\\infty f(x) dx\n$$", res_edge)


class TestMarkdownFormatterMathIntegration(unittest.TestCase):
    """Test inline math formatting inside MarkdownFormatter, including streaming and tables."""

    def test_streaming_normal_text(self):
        formatter = MarkdownFormatter()
        chunk1 = formatter.format("Complexity is $O(n^")
        self.assertEqual(chunk1, "")
        chunk2 = formatter.format("2)$ and space is $O(1)$.\n")
        self.assertEqual(chunk2, "Complexity is O(n²) and space is O(1).\n")

    def test_streaming_single_line_block_math(self):
        formatter = MarkdownFormatter()
        out = formatter.format("Formula:\n$$E = mc^2$$\nDone.\n", flush=True)
        self.assertIn("E = mc²", out)
        self.assertNotIn("$$", out)

    def test_bordered_table_with_math(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "bordered" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

        formatter = MarkdownFormatter()
        text = (
            "| Algorithm | Time | Space |\n"
            "|---|---|---|\n"
            "| Binary Search | $O(\\log n)$ | $O(1)$ |\n"
            "| Merge Sort | $O(n \\log n)$ | $O(n)$ |\n"
        )
        out = formatter.format(text, flush=True)
        self.assertIn("O(log n)", out)
        self.assertIn("O(n log n)", out)
        self.assertIn("─", out)
        self.assertNotIn("$O(", out)

    def test_markdown_table_with_math(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "markdown" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

        formatter = MarkdownFormatter()
        text = (
            "| Function | Value |\n"
            "|---|---|\n"
            "| Square | $x^2 + y_1$ |\n"
            "| Limit | $\\lim_{n \\to \\infty} \\frac{1}{n} = 0$ |\n"
        )
        out = formatter.format(text, flush=True)
        self.assertIn("x² + y₁", out)
        self.assertIn("lim_(n → ∞) 1/n = 0", out)
        self.assertIn("|", out)
        self.assertNotIn("$x^2", out)

    def test_html_table_with_math(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "html" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

        formatter = MarkdownFormatter()
        text = (
            "| Property | Expression |\n"
            "|---|---|\n"
            "| Bound | $x \\le y$ |\n"
            "| Sum | $\\sum x_i$ |\n"
        )
        out = formatter.format(text, flush=True)
        tables = formatter.take_html_tables()
        self.assertEqual(len(tables), 1)
        self.assertIn("x ≤ y", tables[0]["html"])
        self.assertIn("∑ xᵢ", tables[0]["html"])
        self.assertNotIn("$x \\le y$", tables[0]["html"])
        # The folded source text also shows clean Unicode math
        self.assertIn("x ≤ y", out)

    def test_format_table_direct_call_with_math(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "bordered" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

        formatter = MarkdownFormatter()
        lines = [
            "| Concept | Math |",
            "|---|---|",
            "| Circle | $\\pi r^2$ |",
        ]
        out_lines = formatter.format_table(lines)
        out = "\n".join(out_lines)
        self.assertIn("π r²", out)
        self.assertNotIn("$\\pi", out)


if __name__ == "__main__":
    unittest.main()
