"""Allow running the package directly with ``python -m deepseek_website_to_cli``."""

from __future__ import annotations

import sys

from deepseek_website_to_cli.cli import main

sys.exit(main())
