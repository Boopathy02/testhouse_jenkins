import re
import json
from mcp.protocol import MCPAgentBase, MCPResponse


# agents/python_agent.py
def build_method(entry, language="python"):
    label_text = entry.get("label_text", "")
    ocr_type = (entry.get("ocr_type") or "").lower()
    intent = (entry.get("intent") or "").lower()
    unique_name = entry.get("unique_name")

    def func(name):
        return safe(label_text or intent or name)
    
    def safe(s):
        return re.sub(r'\W+', '_', s.lower()).strip('_')

    # -- Text Inputs --
    if ocr_type in ("textbox", "text", "input"):
        func_name = f"enter_{func('textbox')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    elif ocr_type == "textarea":
        func_name = f"enter_{func('textarea')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    elif ocr_type == "password":
        func_name = f"enter_{func('password')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    elif ocr_type == "email":
        func_name = f"enter_{func('email')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    # -- Buttons, Links, Icons --
    elif ocr_type in ("button", "submit", "iconbutton"):
        func_name = f"click_{func('button')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    elif ocr_type in ("link", "anchor"):
        func_name = f"click_{func('link')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    elif ocr_type == "imagebutton":
        func_name = f"click_{func('imagebutton')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    # -- Selectors --
    elif ocr_type in ("select", "dropdown", "combobox"):
        func_name = f"select_{func('select')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').select_option(value)\n"
        )
    elif ocr_type == "multiselect":
        func_name = f"select_{func('multiselect')}_values"
        code = (
            f"def {func_name}(page, values):\n"
            f"    page.smartAI('{unique_name}').select_options(values)\n"
        )
    # -- Checkboxes, Radios, Toggles, Switches --
    elif ocr_type == "checkbox":
        func_name = f"toggle_{func('checkbox')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    elif ocr_type in ("radio", "radiogroup"):
        func_name = f"select_{func('radio')}_option"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').check(value)\n"
        )
    elif ocr_type in ("toggle", "switch"):
        func_name = f"toggle_{func('toggle')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    # -- Date/Time Pickers --
    elif ocr_type in ("date", "datepicker"):
        func_name = f"pick_{func('date')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    elif ocr_type in ("time", "timepicker"):
        func_name = f"pick_{func('time')}"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    # -- File Upload --
    elif ocr_type in ("file", "fileinput", "upload"):
        func_name = f"upload_{func('file')}"
        code = (
            f"def {func_name}(page, file_path):\n"
            f"    page.smartAI('{unique_name}').set_input_files(file_path)\n"
        )
    # -- Table/Grid/Data Grid --
    elif ocr_type in ("table", "datatable", "grid"):
        func_name = f"read_{func('table')}_data"
        code = (
            f"def {func_name}(page):\n"
            f"    return page.smartAI('{unique_name}').get_table_data()\n"
        )
    elif ocr_type in ("tablecell", "cell"):
        func_name = f"get_{func('cell')}_text"
        code = (
            f"def {func_name}(page, row, col):\n"
            f"    return page.smartAI('{unique_name}').get_cell_text(row, col)\n"
        )
    # -- Image --
    elif ocr_type == "image":
        func_name = f"verify_{func('image')}_visible"
        code = (
            f"def {func_name}(page):\n"
            f"    assert page.smartAI('{unique_name}').is_visible()\n"
        )
    # -- Slider/Range --
    elif ocr_type in ("slider", "range"):
        func_name = f"set_{func('slider')}_value"
        code = (
            f"def {func_name}(page, value):\n"
            f"    page.smartAI('{unique_name}').fill(value)\n"
        )
    # -- Progressbar --
    elif ocr_type == "progressbar":
        func_name = f"get_{func('progressbar')}_value"
        code = (
            f"def {func_name}(page):\n"
            f"    return page.smartAI('{unique_name}').get_attribute('value')\n"
        )
    # -- Alert/Dialog/Modal/Toast --
    elif ocr_type in ("alert", "dialog", "modal", "toast"):
        func_name = f"verify_{func('alert')}_visible"
        code = (
            f"def {func_name}(page):\n"
            f"    assert page.smartAI('{unique_name}').is_visible()\n"
        )
    # -- Tab/Accordion/Panel --
    elif ocr_type in ("tab", "tabpanel"):
        func_name = f"open_{func('tab')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    elif ocr_type in ("accordion", "panel"):
        func_name = f"expand_{func('accordion')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    # -- Tree/Treeview --
    elif ocr_type in ("tree", "treeview"):
        func_name = f"expand_{func('tree')}"
        code = (
            f"def {func_name}(page, node_label):\n"
            f"    page.smartAI('{unique_name}').expand_node(node_label)\n"
        )
    # -- Menu --
    elif ocr_type in ("menu", "menubar"):
        func_name = f"open_{func('menu')}"
        code = (
            f"def {func_name}(page):\n"
            f"    page.smartAI('{unique_name}').click()\n"
        )
    # -- Breadcrumb --
    elif ocr_type == "breadcrumb":
        func_name = f"navigate_{func('breadcrumb')}"
        code = (
            f"def {func_name}(page, crumb_label):\n"
            f"    page.smartAI('{unique_name}').click_crumb(crumb_label)\n"
        )
    # -- Badge/Chip/Tag --
    elif ocr_type in ("badge", "chip", "tag"):
        func_name = f"verify_{func('badge')}_visible"
        code = (
            f"def {func_name}(page):\n"
            f"    assert page.smartAI('{unique_name}').is_visible()\n"
        )
    # -- Avatar/Userpic --
    elif ocr_type in ("avatar", "userpic"):
        func_name = f"verify_{func('avatar')}_visible"
        code = (
            f"def {func_name}(page):\n"
            f"    assert page.smartAI('{unique_name}').is_visible()\n"
        )
    # -- Pagination --
    elif ocr_type == "pagination":
        func_name = f"goto_{func('page')}"
        code = (
            f"def {func_name}(page, page_number):\n"
            f"    page.smartAI('{unique_name}').goto_page(page_number)\n"
        )
    # -- Default/Fallback --
    else:
        func_name = f"verify_{func('element')}_visible"
        code = (
            f"def {func_name}(page):\n"
            f"    assert page.smartAI('{unique_name}').is_visible()\n"
        )
    return code


class PlaywrightPythonAgent(MCPAgentBase):
    def generate_method(self, element_spec):
        code = build_method(element_spec, language="python")
        return MCPResponse(True, code)

    def generate_test(self, test_case_spec):
        # Example: just stub out a test using the generated methods
        method_calls = "\n    ".join(test_case_spec.get("steps", []))
        code = f"def test_case(page):\n    {method_calls}\n"
        return MCPResponse(True, code)

    def generate_page_file(self, payload):
        entries = payload["entries"]
        page_name = payload.get("page_name", "page")
        header = (
            "from lib.smart_ai import patch_page_with_smartai\n\n"
            f"# Methods for page: {page_name}\n\n"
        )
        methods = [self.generate_method(e).payload for e in entries]
        filename = f"{page_name}_page_methods.py"  # Python agent returns .py
        code = header + "\n".join(methods)
        return MCPResponse(True, {"filename": filename, "code": code})
