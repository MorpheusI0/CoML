from __future__ import annotations

import warnings
from typing import Any

import ipywidgets as widgets
import markdown
from IPython.core.magic import (
    Magics,
    cell_magic,
    line_cell_magic,
    line_magic,
    magics_class,
    no_var_expand,
)
from IPython.display import HTML, Code, clear_output, display
from langchain_community.chat_models import ChatOllama

from .core import CoMLAgent
from .ipython_utils import (
    get_ipython_history,
    get_last_cell,
    insert_cell_below,
    parse_cell_outputs,
    run_code_in_next_cell,
    update_running_cell_metadata,
)
from .linter import lint
from .prompt_utils import (
    FixContext,
    GenerateContext,
    InteractionIncomplete,
    describe_variable,
    filter_variables,
)

VERIFY_STYLE = """
<style>
summary {
  display: list-style;
}
details :last-child {
  margin-bottom: 1em;
}
.loader {
  width: 1em;
  height: 1em;
  border: 0.1em solid;
  border-bottom-color: transparent;
  border-radius: 50%;
  display: inline-block;
  box-sizing: border-box;
  animation: rotation 1s linear infinite;
  margin-bottom: 0 !important;
}

@keyframes rotation {
  0% {
    transform: rotate(0deg);
  }
  100% {
    transform: rotate(360deg);
  }
}

</style>
"""

VERIFY_STATUS_ICON = {
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
    "ok": "✅",
    None: "❔",
    True: "✅",
    False: "❌",
}


@magics_class
class CoMLMagics(Magics):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        import dotenv
        import os

        dotenv.load_dotenv()
        headers = {
            "Authorization": f"Bearer {os.getenv('OLLAMA_API_KEY')}"
        }
        llm = ChatOllama(
            temperature=0.0,
            headers=headers,
            model=os.getenv("OLLAMA_MODEL"),
            base_url=os.getenv("OLLAMA_API_BASE_URL")
        )
        self.agent = CoMLAgent(llm)

    def _get_variable_context(self) -> dict[str, Any]:
        assert self.shell is not None
        return {
            key: describe_variable(value)
            for key, value in filter_variables(self.shell.user_ns).items()
        }

    def _get_code_context(self) -> list[str]:
        assert self.shell is not None
        return get_ipython_history(self.shell)

    def _post_generation(
        self, code: str, context: GenerateContext | FixContext
    ) -> None:
        def run_button_on_click(b):
            run_code_in_next_cell("%%comlrun\n" + code, {"action": "run", **context})

        def edit_button_on_click(b):
            insert_cell_below(code, context)

        def explain_button_on_click(b):
            run_code_in_next_cell("%%comlexplain\n" + code)

        def verify_button_on_click(b):
            run_code_in_next_cell("%comlverify")

        run_button = widgets.Button(
            description="👍 Run it!", layout=widgets.Layout(width="24.5%")
        )
        edit_button = widgets.Button(
            description="🤔 Let me edit.", layout=widgets.Layout(width="24.5%")
        )
        explain_button = widgets.Button(
            description="🧐 Explain it.", layout=widgets.Layout(width="24.5%")
        )
        verify_button = widgets.Button(
            description="🔍 Check yourself.", layout=widgets.Layout(width="24.5%")
        )
        run_button.on_click(run_button_on_click)
        edit_button.on_click(edit_button_on_click)
        explain_button.on_click(explain_button_on_click)
        verify_button.on_click(verify_button_on_click)

        update_running_cell_metadata({"action": "generate", **context})

        combined = widgets.HBox(
            [run_button, edit_button, explain_button, verify_button]
        )
        display(Code(code, language="python"))
        display(combined)

    def _fix_context_from_cell(self, source: str, **kwargs: Any) -> FixContext:
        return FixContext(
            variables=self._get_variable_context(),
            codes=self._get_code_context(),
            request=None,
            first_attempt=source,
            interactions=[InteractionIncomplete(**kwargs)],
        )

    @no_var_expand
    @line_cell_magic
    def coml(self, line, cell=None):
        request: str = line
        if cell is not None:
            request += "\n" + cell
        generate_context = self.agent.generate_code(
            request.strip(), self._get_variable_context(), self._get_code_context()
        )
        return self._post_generation(generate_context["answer"], generate_context)

    @no_var_expand
    @line_magic
    def comlset(self, line):
        key, value = line.lower().strip().split()
        if key == "debug":
            from . import core

            if value == "on":
                core._debug_mode = True
            elif value == "off":
                core._debug_mode = False
            else:
                raise ValueError("Debug mode must be either on or off.")
        else:
            raise ValueError("Unknown setting.")

    @no_var_expand
    @line_magic
    def comlinspire(self, line):
        if line:
            warnings.warn(r"The argument of %comlinspire is ignored.")
        suggestions = self.agent.suggest(self._get_code_context())

        def run_button_on_click(b):
            run_code_in_next_cell(r"%coml " + b.description)

        buttons = [
            widgets.Button(description=s, layout=widgets.Layout(width="100%"))
            for s in suggestions
        ]
        for button in buttons:
            button.on_click(run_button_on_click)
        display(widgets.VBox(buttons))

    @no_var_expand
    @line_magic
    def comlfix(self, line):
        hint: str | None = line.strip()
        if not hint:
            hint = None

        target_cell = get_last_cell()
        if target_cell is None:
            warnings.warn("No cell to fix!")
            return
        if target_cell["cell_type"] != "code":
            warnings.warn("Only code cells can be fixed.")
            return

        error, output = parse_cell_outputs(target_cell["outputs"])
        if "coml" in target_cell["metadata"]:
            context = target_cell["metadata"]["coml"]
        else:
            # Last cell is created by user.
            print("This cell is not created by coml. Still trying to fix it though.")
            context = FixContext(
                variables=self._get_variable_context(),
                codes=self._get_code_context(),
                request=None,
                first_attempt=target_cell["source"],
                interactions=[
                    InteractionIncomplete(error=error, output=output, hint=hint)
                ],
            )

        fix_context = self.agent.fix_code(error, output, hint, context)
        if fix_context is None:
            return
        assert "code" in fix_context["interactions"][-1]
        return self._post_generation(
            fix_context["interactions"][-1]["code"], fix_context
        )

    @no_var_expand
    @cell_magic
    def comlexplain(self, line, cell):
        if line:
            warnings.warn(r"The argument of %%comlexplain is ignored.")
        explanation = self.agent.explain(cell)
        display(Code(explanation, language="markdown"))

    @no_var_expand
    @line_magic
    def comlverify(self, line):
        target_cell = get_last_cell()
        if target_cell is None:
            raise RuntimeError("No cell to verify!")
        if target_cell["cell_type"] != "code":
            raise RuntimeError("Only code cells can be verified.")
        if "coml" not in target_cell["metadata"]:
            raise RuntimeError("This cell is not created by coml.")

        context = target_cell["metadata"]["coml"]
        if context.get("interactions"):
            code = context["interactions"][-1]["code"]
        else:
            code = context["answer"]

        error = output = None
        generated_vis = False
        if context.get("action") == "run":
            error, output = parse_cell_outputs(target_cell["outputs"])
            generated_vis = output and "<image/svg+xml>" in output

        def display_statuses(statuses):
            clear_output(wait=True)
            html = VERIFY_STYLE + "\n"
            display_names = {
                "lint": "PyLint",
                "rubberduck": "Rubberduck",
            }
            if generated_vis:
                display_names["vis"] = "Visualization check"
            elif error or output:
                display_names["sanity"] = "Output sanity check"

            loading = "<span class='loader'></span>"
            message_template = "<details><summary><b>{}:</b> {}</summary>\n{}</details>"
            for name in display_names:
                detail_message = "Still loading..."
                if name in statuses:
                    detail_message = markdown.markdown(
                        statuses[name]["details"], extensions=["nl2br"]
                    )
                html += message_template.format(
                    display_names[name],
                    (
                        loading
                        if name not in statuses
                        else VERIFY_STATUS_ICON[statuses[name]["result"]]
                    ),
                    detail_message,
                )

            display(HTML(html))

        result = {}
        display_statuses(result)

        lint_result, lint_details = lint("\n".join(self._get_code_context()), code)
        result["lint"] = {
            "result": lint_result,
            "details": lint_details,
        }
        display_statuses(result)

        rubberduck_result, rubberduck_details = self.agent.static_check(code, context)
        result["rubberduck"] = {
            "result": rubberduck_result,
            "details": rubberduck_details,
        }
        display_statuses(result)

        if generated_vis:
            # verify generated visualization
            # Roughly judge the source of the visualization
            if "plt.show()" in code:
                vis_framework = "matplotlib"
                (
                    visualization_check_result,
                    visualization_check_details,
                ) = self.agent.visualization_check(
                    context["request"],
                    "\n".join(context["codes"]),
                    output.replace("<image/svg+xml>", ""),
                    context["variables"],
                    vis_framework,
                )
                details = ""
                for detail in visualization_check_details:
                    details += VERIFY_STATUS_ICON[detail[0]] + " " + detail[1] + "\n"
                result["vis"] = {
                    "result": visualization_check_result,
                    "details": details,
                }
                display_statuses(result)

        elif error or output:
            sanity_result, sanity_details = self.agent.output_sanity_check(
                code, context, error, output
            )
            result["sanity"] = {
                "result": sanity_result,
                "details": sanity_details,
            }
            display_statuses(result)

    @no_var_expand
    @cell_magic
    def comlrun(self, line, cell):
        if line:
            warnings.warn(r"The argument of %comlrun is ignored.")
        assert self.shell is not None
        output = None
        try:
            # show as svg
            if "plt.show()" in cell:
                cell = cell.replace(
                    "plt.show()",
                    "show_svg(plt)",
                )
                import inspect

                from .vis_utils import show_svg

                self.shell.run_cell(inspect.getsource(show_svg))
            output = self.shell.run_cell(cell)
            return output.result
        finally:

            def like_button_on_click(b):
                print("Thanks for your feedback! 🤗")

            def fix_button_on_click(b):
                run_code_in_next_cell(r"%comlfix")

            def fix_with_comment_button_on_click(b):
                insert_cell_below(r"%comlfix <describe the problem here>")

            def verify_button_on_click(b):
                run_code_in_next_cell("%comlverify")

            like_button = widgets.Button(
                description="🤗 Looks good!", layout=widgets.Layout(width="24.5%")
            )
            retry_button = widgets.Button(
                description="🤬 Try again!", layout=widgets.Layout(width="24.5%")
            )
            comment_button = widgets.Button(
                description="🤯 I'll show you what's wrong.",
                layout=widgets.Layout(width="24.5%"),
            )
            verify_button = widgets.Button(
                description="🔍 Check yourself.", layout=widgets.Layout(width="24.5%")
            )
            like_button.on_click(like_button_on_click)
            retry_button.on_click(fix_button_on_click)
            comment_button.on_click(fix_with_comment_button_on_click)
            verify_button.on_click(verify_button_on_click)

            combined = widgets.HBox(
                [like_button, retry_button, comment_button, verify_button]
            )
            display(combined)
