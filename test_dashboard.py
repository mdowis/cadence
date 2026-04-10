"""
Lint dashboard.html's inline JavaScript.

Extracts the <script> block and runs it through `node --check` if available.
Skips cleanly if node is not installed.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile


def test_dashboard_js_syntax():
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        print("SKIP: node not available")
        return

    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(html_path) as f:
        content = f.read()

    # Extract all <script>...</script> blocks
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", content, re.DOTALL)
    if not scripts:
        raise AssertionError("No <script> block found in dashboard.html")

    combined = "\n".join(scripts)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False
    ) as tmp:
        tmp.write(combined)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [node, "--check", tmp_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Dashboard JS syntax error:\n{result.stderr}"
            )
    finally:
        os.unlink(tmp_path)

    print(f"Dashboard JS syntax OK ({len(combined)} chars)")


if __name__ == "__main__":
    test_dashboard_js_syntax()
    print("All dashboard tests passed!")
