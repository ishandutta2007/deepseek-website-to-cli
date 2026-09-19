"""
CLI Entry Point
~~~~~~~~~~~~~~~~

Async command-line interface for deepseek-website-to-cli.
Starts a local WebSocket server, waits for the paired browser extension
to connect, then orchestrates the DeepSeek interaction pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import chardet
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from deepseek_website_to_cli import __version__
from deepseek_website_to_cli.browser import DeepSeekBridge, DEFAULT_PORT
from deepseek_website_to_cli.deepseek import DeepSeekAutomation

console = Console()


def _setup_logging(verbose: bool) -> None:
    """Configure logging with Rich handler for pretty terminal output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, console=console)],
    )
    # Suppress noisy library loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _detect_file_encoding(path: Path) -> str:
    """Detect encoding of an existing file using BOM checks and chardet, defaulting to utf-8."""
    if path.is_file() and path.stat().st_size > 0:
        try:
            raw = path.read_bytes()
            # Fast check for Byte Order Marks (BOM)
            if raw.startswith(b"\xff\xfe"):
                return "utf-16-le"
            if raw.startswith(b"\xfe\xff"):
                return "utf-16-be"
            if raw.startswith(b"\xef\xbb\xbf"):
                return "utf-8-sig"

            # Check for UTF-16 null-byte pattern (e.g. Windows PowerShell text)
            if len(raw) >= 2 and (raw[1::2].count(b"\x00") > len(raw) // 4):
                return "utf-16-le"

            detected = chardet.detect(raw)
            encoding = detected.get("encoding")
            if encoding:
                encoding_lower = encoding.lower().replace("_", "-")
                # ASCII files should be treated as UTF-8 so Unicode content can be appended safely
                if encoding_lower in ("ascii", "us-ascii"):
                    return "utf-8"
                return encoding
        except Exception:
            pass
    return "utf-8"


def _read_prompt_file(filepath: str) -> str:
    """Read and validate the prompt file."""
    path = Path(filepath).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    encoding = _detect_file_encoding(path)
    try:
        text = path.read_text(encoding=encoding).strip()
    except (UnicodeDecodeError, LookupError):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise ValueError(f"Prompt file is empty: {path}")

    return text


def _write_output(content: str, output_path: str | None) -> None:
    """Write extracted content to file or display in terminal."""
    if output_path:
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoding = _detect_file_encoding(path)
        try:
            with open(path, "a", encoding=encoding, errors="replace") as f:
                f.write(content)
                f.write("\n")
        except (UnicodeEncodeError, LookupError):
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(content)
                f.write("\n")
        console.print(f"\n[green]+[/green] Output appended to [bold]{path}[/bold]")
    else:
        console.print()
        console.print(
            Panel(
                Syntax(content, "text", theme="monokai", word_wrap=True),
                title="[bold cyan]DeepSeek Response (Last Code Block)[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="deepseek-cli",
        description=(
            "DeepSeek Website-to-CLI -- Send prompts to DeepSeek via browser "
            "extension bridge and extract code block responses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  deepseek-cli prompt.txt                    # Display result in terminal\n"
            "  deepseek-cli prompt.txt -o output.py       # Append result to output.py\n"
            "  deepseek-cli prompt.txt -o out.py -w 300   # Wait up to 5 minutes\n"
            "  deepseek-cli prompt.txt --browser chrome   # Use Chrome instead of Edge\n"
            "  deepseek-cli prompt.txt -v                 # Verbose logging\n"
        ),
    )
    parser.add_argument(
        "prompt_file",
        help="Path to a text file containing the prompt to send to DeepSeek.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        default=None,
        help="Path to output file. Result will be appended. "
        "If not specified, output is printed to terminal.",
    )
    parser.add_argument(
        "-w",
        "--max-wait",
        type=int,
        default=180,
        metavar="SECONDS",
        help="Maximum seconds to wait for DeepSeek response (default: 180).",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"WebSocket bridge port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--full-response",
        action="store_true",
        help="Extract the full response text instead of just the last code block.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging.",
    )
    parser.add_argument(
        "-b",
        "--browser",
        choices=["edge", "chrome"],
        default="edge",
        help="Browser to use (default: edge).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    """Async main pipeline.

    1. Start WebSocket server
    2. Wait for extension to connect
    3. Find/open DeepSeek tab
    4. Send prompt
    5. Wait for response
    6. Extract code block
    7. Output result
    """
    # ── Read prompt ───────────────────────────────────────────────────
    try:
        prompt_text = _read_prompt_file(args.prompt_file)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1

    console.print(
        f"[dim]Prompt loaded ({len(prompt_text)} chars) from:[/dim] {args.prompt_file}"
    )

    # ── Start bridge server ───────────────────────────────────────────
    browser_name = "Chrome" if args.browser == "chrome" else "Edge"
    extensions_url = (
        "chrome://extensions" if args.browser == "chrome" else "edge://extensions"
    )

    bridge = DeepSeekBridge(port=args.port, browser_name=browser_name)

    try:
        await bridge.start()
        console.print(
            f"[green]+[/green] WebSocket bridge running on "
            f"[bold]ws://127.0.0.1:{args.port}[/bold]"
        )

        # ── Wait for extension ────────────────────────────────────────
        with console.status(
            "[bold blue]Waiting for DeepSeek CLI Bridge extension to connect...[/bold blue]\n"
            f"[dim]Make sure the extension is installed and enabled in {browser_name}[/dim]",
            spinner="dots",
        ):
            try:
                await bridge.wait_for_extension(timeout=60)
            except TimeoutError as exc:
                console.print(f"\n[red]Error:[/red] {exc}")
                console.print(
                    "\n[yellow]Setup:[/yellow] Load the extension from the "
                    "[bold]extension/[/bold] folder:\n"
                    f"  1. Open [bold]{extensions_url}[/bold]\n"
                    "  2. Enable [bold]Developer mode[/bold]\n"
                    "  3. Click [bold]Load unpacked[/bold] and select the "
                    "[bold]extension/[/bold] directory\n"
                )
                return 1

        console.print("[green]+[/green] Extension connected")

        # ── Find or open DeepSeek tab ────────────────────────────────────
        deepseek = DeepSeekAutomation(
            bridge=bridge,
            max_wait_seconds=args.max_wait,
        )

        with console.status("[bold blue]Finding or opening DeepSeek tab...[/bold blue]"):
            await deepseek.find_or_open_deepseek_tab()
        console.print("[green]+[/green] DeepSeek tab is active")

        # Wait for page to be ready after tab switch
        await asyncio.sleep(2)

        # ── Send prompt ───────────────────────────────────────────────
        with console.status("[bold blue]Sending prompt to DeepSeek...[/bold blue]"):
            await deepseek.send_prompt(prompt_text)
        console.print("[green]+[/green] Prompt submitted")

        # ── Wait for response ─────────────────────────────────────────
        with console.status(
            f"[bold yellow]Waiting for DeepSeek response "
            f"(up to {args.max_wait}s)...[/bold yellow]",
            spinner="dots",
        ):
            await deepseek.wait_for_response()
        console.print("[green]+[/green] Response received")

        # ── Extract result ────────────────────────────────────────────
        with console.status("[bold blue]Extracting code block...[/bold blue]"):
            if args.full_response:
                result = await deepseek.extract_full_response()
            else:
                result = await deepseek.extract_last_code_block()

        if result is None and not args.full_response:
            console.print(
                "[yellow]![/yellow] No code blocks found. "
                "Trying full response extraction..."
            )
            result = await deepseek.extract_full_response()

        if result is None:
            console.print(
                "[red]x[/red] Could not extract any response from DeepSeek. "
                "The page layout may have changed, or the response was empty."
            )
            return 1

        # ── Output ────────────────────────────────────────────────────
        _write_output(result, args.output)
        console.print("\n[bold green]Done![/bold green]")
        return 0

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        return 130
    except Exception as exc:
        logging.getLogger(__name__).exception("An error occurred:")
        console.print(f"\n[red]Error:[/red] {exc}")
        return 1
    finally:
        await bridge.stop()


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the deepseek-cli command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    # ── Banner ────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            Text.from_markup(
                "[bold cyan]DeepSeek Website-to-CLI[/bold cyan]\n"
                f"[dim]v{__version__} | Extension-bridged RPA automation[/dim]"
            ),
            border_style="bright_blue",
            padding=(1, 4),
        )
    )
    console.print()

    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
