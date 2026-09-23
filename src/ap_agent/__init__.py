"""Accounts Payable Agent package.

No eager submodule imports here (C-5 in the modularisation map): adapters
for heavy, optional OCR engines must only be imported when explicitly
requested, never as a side effect of `import ap_agent`.
"""

__version__ = "0.2.0"
