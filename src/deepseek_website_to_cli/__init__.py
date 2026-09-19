"""
deepseek-website-to-cli
~~~~~~~~~~~~~~~~~~~~

RPA-powered CLI tool that converts DeepSeek website (chat.deepseek.com) interactions
into a command-line interface using a paired browser extension.
"""

__version__ = "0.1.0"
__author__ = "Ishan Dutta"

from deepseek_website_to_cli.browser import DeepSeekBridge
from deepseek_website_to_cli.deepseek import DeepSeekAutomation

__all__ = ["DeepSeekBridge", "DeepSeekAutomation", "__version__"]
