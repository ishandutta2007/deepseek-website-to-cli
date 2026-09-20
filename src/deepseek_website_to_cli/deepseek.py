"""
DeepSeek Automation (Extension-based)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

High-level orchestration of DeepSeek interactions via the paired browser extension.
All DOM operations are performed by the extension's content script running
inside the user's real browser session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from deepseek_website_to_cli.browser import DeepSeekBridge

logger = logging.getLogger(__name__)


class DeepSeekAutomation:
    """Orchestrates DeepSeek interactions through the browser extension bridge.

    Sends high-level commands to the extension, which handles the actual
    DOM manipulation inside the user's logged-in browser.
    """

    def __init__(
        self,
        bridge: DeepSeekBridge,
        max_wait_seconds: int = 180,
        poll_interval: float = 1.5,
    ) -> None:
        self.bridge = bridge
        self.max_wait_seconds = max_wait_seconds
        self.poll_interval = poll_interval
        self.tab_id: Optional[int] = None
        self._initial_response_count: int = 0
        self._initial_response_len: int = 0

    async def find_or_open_deepseek_tab(self) -> int:
        """Find an existing DeepSeek tab or open a new one.

        Returns:
            The tab ID of the DeepSeek tab.
        """
        # Search for existing DeepSeek tabs
        result = await self.bridge.send_command("find_deepseek_tab")
        tabs = result.get("tabs", [])

        if tabs:
            tab = tabs[0]
            tab_id = tab["id"]
            self.tab_id = tab_id
            logger.info("Found DeepSeek tab: %s (id=%d)", tab.get("title", ""), tab_id)
            # Activate the tab and bring window to focus
            await self.bridge.send_command("activate_tab", tabId=tab_id)
            return tab_id
        else:
            logger.info("No DeepSeek tab found. Opening a new one...")
            result = await self.bridge.send_command("open_deepseek_tab", timeout=30)
            tab_id = result["tabId"]
            self.tab_id = tab_id
            logger.info("Opened new DeepSeek tab (id=%d)", tab_id)
            return tab_id

    async def send_prompt(
        self,
        prompt_text: str,
        max_retries: int = 5,
        retry_delay: float = 3.0,
    ) -> None:
        """Send a prompt to the DeepSeek input box.

        The extension's content script handles finding the input element,
        setting the value (React-compatible), and submitting with Enter.

        Retries up to ``max_retries`` times on transient failures
        (timeouts, disconnections, extension errors).

        Args:
            prompt_text: The full prompt text to send.
            max_retries: Maximum number of send attempts.
            retry_delay: Seconds to wait between retries.
        """
        logger.info("Sending prompt to DeepSeek (%d chars)...", len(prompt_text))

        # Check initial state before sending
        try:
            initial_status = await self.bridge.send_command(
                "check_response_status",
                tabId=self.tab_id,
                timeout=5,
            )
            self._initial_response_count = initial_status.get("responseCount", 0)
            self._initial_response_len = initial_status.get("latestResponseLength", 0)
        except Exception:
            self._initial_response_count = 0
            self._initial_response_len = 0

        for attempt in range(1, max_retries + 1):
            try:
                await self.bridge.send_command(
                    "send_prompt",
                    prompt=prompt_text,
                    tabId=self.tab_id,
                    timeout=30,
                )
                logger.info(
                    "Prompt submitted successfully (attempt %d/%d).",
                    attempt,
                    max_retries,
                )
                return
            except (ConnectionError, TimeoutError, RuntimeError, OSError) as exc:
                logger.warning(
                    "send_prompt failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    logger.info("Retrying send_prompt in %.1fs...", retry_delay)
                    await asyncio.sleep(retry_delay)

        raise RuntimeError(f"Failed to send prompt after {max_retries} attempts.")

    async def wait_for_response(self) -> None:
        """Wait for DeepSeek to finish generating its response.

        Polls the extension's content script for response status until
        generation is complete or the timeout is reached.

        Includes a "stale generation" safety valve: if the extension
        reports ``generating=True`` but the response content hasn't
        changed for several consecutive polls, we treat generation as
        complete.  This handles false-positive generation indicators
        from persistent DOM elements on chat.deepseek.com.
        """

        logger.info(
            "Waiting for DeepSeek response (up to %ds)...", self.max_wait_seconds
        )

        # Brief delay to allow prompt submission and generation start
        await asyncio.sleep(1.0)

        start_time = asyncio.get_event_loop().time()
        was_generating = False
        last_response_length = 0
        stable_count = 0
        stale_generating_count = 0  # Tracks consecutive polls where generating=True but content unchanged
        has_response = False
        response_length = 0
        initial_count = getattr(self, "_initial_response_count", 0)
        initial_len = getattr(self, "_initial_response_len", 0)

        # How many consecutive stable polls during "generating" before we
        # declare the generation stale and treat it as complete.
        STALE_GENERATING_THRESHOLD = 5

        while (asyncio.get_event_loop().time() - start_time) < self.max_wait_seconds:
            try:
                status = await self.bridge.send_command(
                    "check_response_status",
                    tabId=self.tab_id,
                    timeout=10,
                )
            except Exception as exc:
                logger.debug("Status check failed: %s", exc)
                await asyncio.sleep(self.poll_interval)
                continue

            generating = status.get("generating", False)
            has_response = status.get("hasResponse", False)
            response_count = status.get("responseCount", 0)
            response_length = status.get("latestResponseLength", 0)
            code_block_count = status.get("codeBlockCount", 0)
            elapsed = int(asyncio.get_event_loop().time() - start_time)

            if generating:
                was_generating = True
                stable_count = 0

                # ── Stale generation detection ────────────────────────
                # If the response has meaningful content but its length
                # hasn't changed, the "generating" indicator is likely a
                # false positive from a lingering DOM element.
                if has_response and response_length > 0 and response_length == last_response_length:
                    stale_generating_count += 1
                    logger.debug(
                        "Generating flag still set but content unchanged "
                        "(%d/%d stale polls, len=%d, %ds elapsed).",
                        stale_generating_count,
                        STALE_GENERATING_THRESHOLD,
                        response_length,
                        elapsed,
                    )
                    if stale_generating_count >= STALE_GENERATING_THRESHOLD:
                        logger.info(
                            "Generation indicator appears stale after %ds "
                            "(content stable for %d polls, len=%d, code_blocks=%d). "
                            "Treating as complete.",
                            elapsed,
                            stale_generating_count,
                            response_length,
                            code_block_count,
                        )
                        await asyncio.sleep(1.0)  # Grace period
                        return
                else:
                    stale_generating_count = 0
                    logger.debug(
                        "Still generating... (len=%d, code_blocks=%d, %ds elapsed)",
                        response_length,
                        code_block_count,
                        elapsed,
                    )
            elif was_generating:
                # Was generating but stopped — generation is complete!
                logger.info(
                    "Generation complete after %ds (len=%d, code_blocks=%d).",
                    elapsed,
                    response_length,
                    code_block_count,
                )
                await asyncio.sleep(1.0)  # Grace period for DOM to settle
                return
            elif has_response and response_length > 0:
                # Response appeared without explicit generating indicator
                # Check if a new message appeared or content grew beyond initial
                is_new_or_grown = (
                    response_count > initial_count
                    or response_length > initial_len
                    or initial_count == 0
                )
                if is_new_or_grown:
                    if response_length == last_response_length:
                        stable_count += 1
                        if stable_count >= 2:
                            logger.info(
                                "Response stabilized after %ds (len=%d, code_blocks=%d).",
                                elapsed,
                                response_length,
                                code_block_count,
                            )
                            await asyncio.sleep(0.5)
                            return
                    else:
                        stable_count = 0
                        logger.debug(
                            "Response content changing (len=%d, %ds elapsed)...",
                            response_length,
                            elapsed,
                        )

            last_response_length = response_length
            await asyncio.sleep(self.poll_interval)

        if was_generating or (has_response and response_length > 0):
            logger.info(
                "Proceeding with available response after %ds.",
                self.max_wait_seconds,
            )
            return

        logger.warning(
            "Timed out after %ds while waiting for response.", self.max_wait_seconds
        )

    async def extract_last_code_block(
        self,
        max_retries: int = 5,
        retry_delay: float = 3.0,
        fallback_to_full: bool = True,
    ) -> Optional[str]:
        """Extract the text of the last code block from the DeepSeek response.

        The extension tries multiple strategies:
        1. Click the copy button on the code block
        2. Read innerText directly
        3. Fall back to extracting the full response text if no code block exists

        Retries up to ``max_retries`` times on transient failures (empty
        result, connection errors, or extension errors) with a delay between
        each attempt.

        Args:
            max_retries: Maximum number of extraction attempts.
            retry_delay: Seconds to wait between retries.
            fallback_to_full: If True, fall back to extracting the full response text
                if no code block is found after all retries.

        Returns:
            The code block text, full response text fallback, or None if no
            response could be extracted.
        """
        for attempt in range(1, max_retries + 1):
            try:
                result = await self.bridge.send_command(
                    "extract_last_code_block",
                    tabId=self.tab_id,
                    timeout=15,
                )
                text = result.get("text")
                if text:
                    method = result.get("method", "unknown")
                    logger.info(
                        "Extracted code block (%d chars) via %s (attempt %d/%d).",
                        len(text),
                        method,
                        attempt,
                        max_retries,
                    )
                    return text.strip()
                else:
                    logger.warning(
                        "No code blocks found (attempt %d/%d).",
                        attempt,
                        max_retries,
                    )
            except (RuntimeError, ConnectionError, OSError) as exc:
                logger.warning(
                    "Code block extraction failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )

            if attempt < max_retries:
                logger.info("Retrying code block extraction in %.1fs...", retry_delay)
                await asyncio.sleep(retry_delay)

        if fallback_to_full:
            logger.info(
                "Code block extraction yielded nothing; falling back to full response..."
            )
            return await self.extract_full_response(
                max_retries=max_retries, retry_delay=retry_delay
            )

        logger.warning("Could not extract code block after %d attempts.", max_retries)
        return None

    async def extract_full_response(
        self,
        max_retries: int = 5,
        retry_delay: float = 3.0,
    ) -> Optional[str]:
        """Extract the full text of the latest DeepSeek response.

        Retries up to ``max_retries`` times on transient failures.

        Args:
            max_retries: Maximum number of extraction attempts.
            retry_delay: Seconds to wait between retries.

        Returns:
            The full response text, or None if nothing was found after all
            retries.
        """
        for attempt in range(1, max_retries + 1):
            try:
                result = await self.bridge.send_command(
                    "extract_full_response",
                    tabId=self.tab_id,
                    timeout=15,
                )
                text = result.get("text")
                if text:
                    logger.info(
                        "Extracted full response (%d chars, attempt %d/%d).",
                        len(text),
                        attempt,
                        max_retries,
                    )
                    return text.strip()
                else:
                    logger.warning(
                        "No response content found (attempt %d/%d).",
                        attempt,
                        max_retries,
                    )
            except (RuntimeError, ConnectionError, OSError) as exc:
                logger.warning(
                    "Full response extraction failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )

            if attempt < max_retries:
                logger.info(
                    "Retrying full response extraction in %.1fs...", retry_delay
                )
                await asyncio.sleep(retry_delay)

        logger.warning(
            "Could not extract full response after %d attempts.", max_retries
        )
        return None
