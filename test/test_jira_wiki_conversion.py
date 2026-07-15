#!/usr/bin/env python3
"""Test Jira Wiki markup conversion"""

import re

def markdown_to_jira_wiki(text: str) -> str:
    """Convert Markdown text to Jira Wiki Markup format"""

    # Use placeholder system to protect code content from other conversions
    code_blocks = {}
    inline_codes = {}
    block_counter = 0
    inline_counter = 0

    # STEP 1: Extract and protect code blocks with placeholders
    def replace_code_block(match):
        nonlocal block_counter
        placeholder = f"__CODE_BLOCK_{block_counter}__"
        code_blocks[placeholder] = "{code}" + match.group(1) + "{code}"
        block_counter += 1
        return placeholder

    text = re.sub(r'```([^`]*?)```', replace_code_block, text, flags=re.DOTALL)

    # STEP 2: Extract and protect inline code with placeholders
    def replace_inline_code(match):
        nonlocal inline_counter
        placeholder = f"__INLINE_CODE_{inline_counter}__"
        inline_codes[placeholder] = "{{" + match.group(1) + "}}"
        inline_counter += 1
        return placeholder

    text = re.sub(r'`([^`]+?)`', replace_inline_code, text)

    # STEP 3: Now safely process other formatting (code is protected)
    # Convert headers (# → h1., ## → h2., etc.)
    text = re.sub(r'^### (.+)$', r'h3. \1', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'h2. \1', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'h1. \1', text, flags=re.MULTILINE)

    # Convert bold first (**text** → *text*) and mark with special chars to protect
    text = re.sub(r'\*\*(.+?)\*\*', r'__BOLD_START__\1__BOLD_END__', text)

    # Convert italic (*text* → _text*) - now safe from bold interference
    text = re.sub(r'\*([^*]+?)\*', r'_\1_', text)

    # Restore bold with correct syntax
    text = re.sub(r'__BOLD_START__(.+?)__BOLD_END__', r'*\1*', text)

    # Convert checkboxes (- [x] → * (x) checked, - [ ] → * ( ) unchecked)
    text = re.sub(r'^- \[x\] (.+)$', r'* (x) \1', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^- \[ \] (.+)$', r'* ( ) \1', text, flags=re.MULTILINE)

    # Convert bullet points (- → *)
    text = re.sub(r'^- (.+)$', r'* \1', text, flags=re.MULTILINE)

    # Convert numbered lists (1. → #)
    text = re.sub(r'^\d+\. (.+)$', r'# \1', text, flags=re.MULTILINE)

    # Convert links ([text](url) → [text|url])
    text = re.sub(r'\[([^\]]+?)\]\(([^)]+?)\)', r'[\1|\2]', text)

    # Convert blockquotes (> text → bq. text)
    text = re.sub(r'^> (.+)$', r'bq. \1', text, flags=re.MULTILINE)

    # Convert horizontal rules (--- → ----)
    text = re.sub(r'^---+$', '----', text, flags=re.MULTILINE)

    # STEP 4: Restore protected code content
    for placeholder, code_content in code_blocks.items():
        text = text.replace(placeholder, code_content)

    for placeholder, code_content in inline_codes.items():
        text = text.replace(placeholder, code_content)

    return text


# Test cases
test_cases = [
    ("**bold text**", "*bold text*", "Bold conversion"),
    ("*italic text*", "_italic text_", "Italic conversion"),
    ("**bold** and *italic*", "*bold* and _italic_", "Mixed bold and italic"),
    ("**bold with *nested italic* inside**", "*bold with _nested italic_ inside*", "Nested bold and italic"),
    ("- [x] checked item", "* (x) checked item", "Checked checkbox"),
    ("- [ ] unchecked item", "* ( ) unchecked item", "Unchecked checkbox"),
    ("`inline code`", "{{inline code}}", "Inline code"),
    ("```\ncode block\n```", "{code}\ncode block\n{code}", "Code block"),
]

print("Testing Jira Wiki Markup Conversion\n" + "="*50)
for markdown, expected, description in test_cases:
    result = markdown_to_jira_wiki(markdown)
    status = "✅ PASS" if result == expected else "❌ FAIL"
    print(f"\n{status} - {description}")
    print(f"  Input:    {repr(markdown)}")
    print(f"  Expected: {repr(expected)}")
    print(f"  Got:      {repr(result)}")
    if result != expected:
        print(f"  Diff:     Expected [{expected}] but got [{result}]")